#!/usr/bin/env python3
"""Plot the validated seven-dataset, three-system 1M recall-QPS frontier."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from assemble_vldb_frontier_1m import DATASETS, METHODS
from plot_vldb_frontier_10m import STYLES
from publication_metadata import pdf_metadata


PLOT_METHODS = ("SHINE", "d-HNSW", "SlabWalk")


def number(row: dict[str, str], key: str, source: Path) -> float:
    try:
        value = float(row.get(key, ""))
    except ValueError as exc:
        raise ValueError(f"{source}: missing or invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def load_validated(summary: Path, gate: Path) -> list[dict[str, object]]:
    if not gate.is_file():
        raise ValueError(f"missing 1M evidence gate: {gate}")
    gate_obj = json.loads(gate.read_text())
    if gate_obj.get("kind") != "vldb_frontier_1m_gate" or gate_obj.get(
        "ready_for_plotting"
    ) is not True:
        raise ValueError(f"1M evidence gate is not plot-ready: {gate}")
    if tuple(gate_obj.get("datasets", ())) != DATASETS or tuple(
        gate_obj.get("methods", ())
    ) != METHODS:
        raise ValueError("1M evidence gate matrix does not match the plot contract")
    if not summary.is_file():
        raise ValueError(f"missing 1M frontier summary: {summary}")
    actual_sha = hashlib.sha256(summary.read_bytes()).hexdigest()
    if actual_sha != str(gate_obj.get("summary_sha256", "")):
        raise ValueError("1M frontier summary SHA does not match the evidence gate")
    with summary.open(newline="") as handle:
        source_rows = list(csv.DictReader(handle))

    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, float]] = set()
    counts = {(dataset, method): 0 for dataset in DATASETS for method in METHODS}
    for row in source_rows:
        dataset = row.get("dataset", "").strip()
        method = row.get("method", "").strip()
        if dataset not in DATASETS or method not in METHODS:
            raise ValueError(f"unexpected 1M summary row: {dataset}/{method}")
        ef = number(row, "ef", summary)
        key = (dataset, method, ef)
        if key in seen:
            raise ValueError(f"duplicate 1M frontier point: {key}")
        seen.add(key)
        if number(row, "n", summary) != 5:
            raise ValueError(f"{key}: expected five measured repeats")
        recall = number(row, "recall_mean", summary)
        recall_ci = number(row, "recall_ci95", summary)
        qps = number(row, "qps_mean", summary)
        qps_ci = number(row, "qps_ci95", summary)
        if not 0 <= recall <= 1 or recall_ci < 0 or qps <= 0 or qps_ci < 0:
            raise ValueError(f"{key}: invalid frontier value")
        rows.append(
            {
                "dataset": dataset,
                "method": method,
                "ef": ef,
                "recall": recall,
                "recall_ci95": recall_ci,
                "qps": qps,
                "qps_ci95": qps_ci,
            }
        )
        counts[(dataset, method)] += 1
    missing = [cell for cell, count in counts.items() if count < 5]
    if missing:
        raise ValueError(f"incomplete seven-dataset 1M frontier matrix: {missing}")
    return rows


def set_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": [
                "Libertinus Serif",
                "Linux Libertine O",
                "Times New Roman",
                "DejaVu Serif",
            ],
            "font.size": 6.8,
            "axes.labelsize": 7.0,
            "xtick.labelsize": 6.1,
            "ytick.labelsize": 6.1,
            "legend.fontsize": 7.1,
            "axes.linewidth": 0.65,
            "lines.linewidth": 1.15,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def generate(summary: Path, gate: Path, out: Path) -> None:
    data = load_validated(summary, gate)
    set_style()
    fig = plt.figure(figsize=(7.08, 3.18))
    grid = fig.add_gridspec(2, 8)
    positions = (
        (0, slice(0, 2)),
        (0, slice(2, 4)),
        (0, slice(4, 6)),
        (0, slice(6, 8)),
        (1, slice(1, 3)),
        (1, slice(3, 5)),
        (1, slice(5, 7)),
    )
    axes = [fig.add_subplot(grid[row, columns]) for row, columns in positions]
    legend_handles = []
    for panel_index, (ax, dataset) in enumerate(zip(axes, DATASETS)):
        current = [row for row in data if row["dataset"] == dataset]
        for method in PLOT_METHODS:
            points = sorted(
                (row for row in current if row["method"] == method),
                key=lambda row: float(row["ef"]),
            )
            style = STYLES[method]
            artist = ax.errorbar(
                [float(row["recall"]) for row in points],
                [float(row["qps"]) for row in points],
                xerr=[float(row["recall_ci95"]) for row in points],
                yerr=[float(row["qps_ci95"]) for row in points],
                color=style["color"],
                marker=style["marker"],
                markerfacecolor=style["face"],
                markeredgecolor="#263442",
                markeredgewidth=0.42,
                markersize=3.5,
                capsize=1.35,
                elinewidth=0.55,
                label=style["label"],
                zorder=3 if method == "SlabWalk" else 2,
            )
            if panel_index == 0:
                legend_handles.append(artist)
        recalls = [float(row["recall"]) for row in current]
        qps = [float(row["qps"]) for row in current]
        x_span = max(recalls) - min(recalls)
        ax.set_xlim(
            max(0, min(recalls) - max(0.01, x_span * 0.07)),
            min(1, max(recalls) + max(0.01, x_span * 0.07)),
        )
        ax.set_ylim(max(1, min(qps) * 0.62), max(qps) * 1.5)
        ax.set_yscale("log")
        ax.set_xlabel("Recall@10", labelpad=1.2)
        if panel_index in (0, 4):
            ax.set_ylabel("Queries/s\n(10 workers, log)", labelpad=1.8)
        ax.grid(True, which="both", linestyle=":", color="#C8CED6", linewidth=0.45)
        ax.set_axisbelow(True)
        ax.text(
            0.5,
            -0.35,
            f"({chr(ord('a') + panel_index)}) {dataset}",
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=7.0,
        )
    fig.legend(
        legend_handles,
        [STYLES[method]["label"] for method in PLOT_METHODS],
        loc="upper center",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0.5, 1.015),
        columnspacing=1.6,
        handlelength=1.8,
    )
    fig.subplots_adjust(left=0.072, right=0.995, top=0.86, bottom=0.17, wspace=0.85, hspace=0.72)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.025, metadata=pdf_metadata())
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    generate(args.summary, args.gate, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
