#!/usr/bin/env python3
"""Plot the validated three-system 10M recall-QPS frontier."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


DATASETS = ("DEEP10M", "SIFT10M", "TTI10M")
METHODS = ("SHINE", "d-HNSW", "SlabWalk")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STYLES = {
    "SHINE": {
        "label": "SHINE-derived",
        "color": "#0072B2",
        "marker": "s",
        "face": "#D8ECF6",
    },
    "d-HNSW": {
        "label": "d-HNSW",
        "color": "#D55E00",
        "marker": "^",
        "face": "#F8D8C2",
    },
    "SlabWalk": {
        "label": "SlabWalk",
        "color": "#009E73",
        "marker": "o",
        "face": "#009E73",
    },
}


def number(row: dict[str, str], key: str, source: Path) -> float:
    raw = row.get(key, "").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{source}: missing or invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def integer(row: dict[str, str], key: str, source: Path) -> int:
    value = number(row, key, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral {key}")
    return int(value)


def optional_positive_metric(
    row: dict[str, str], name: str, source: Path
) -> float | None:
    raw_n = row.get(f"{name}_n", "").strip()
    raw_mean = row.get(f"{name}_mean", "").strip()
    if not raw_n and not raw_mean:
        return None
    try:
        count = int(raw_n or "0")
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {name}_n") from exc
    if count == 0 and not raw_mean:
        return None
    if count != 5:
        raise ValueError(f"{source}: {name} must have five repeats")
    value = number(row, f"{name}_mean", source)
    interval = number(row, f"{name}_ci95", source)
    if value <= 0 or interval < 0:
        raise ValueError(f"{source}: invalid {name} mean or confidence interval")
    return value


def load_validated(summary: Path, gate: Path) -> list[dict[str, object]]:
    if not gate.is_file():
        raise ValueError(f"missing evidence gate: {gate}")
    gate_obj = json.loads(gate.read_text())
    if gate_obj.get("ready_for_plotting") is not True:
        raise ValueError(f"evidence gate is not plot-ready: {gate}")
    expected_sha = str(gate_obj.get("expected_slabwalk_sha256", ""))
    if SHA256_RE.fullmatch(expected_sha) is None:
        raise ValueError(f"evidence gate has invalid SlabWalk binary SHA: {expected_sha!r}")
    if not summary.is_file():
        raise ValueError(f"missing frontier summary: {summary}")
    expected_summary_sha = str(
        gate_obj.get("frontier", {}).get("summary_sha256", "")
    )
    actual_summary_sha = hashlib.sha256(summary.read_bytes()).hexdigest()
    if SHA256_RE.fullmatch(expected_summary_sha) is None or actual_summary_sha != expected_summary_sha:
        raise ValueError(
            f"frontier summary SHA {actual_summary_sha} does not match gate "
            f"{expected_summary_sha!r}"
        )
    with summary.open(newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    if not source_rows:
        raise ValueError(f"empty frontier summary: {summary}")

    output: list[dict[str, object]] = []
    seen = set()
    counts = {(dataset, method): 0 for dataset in DATASETS for method in METHODS}
    query_counts: dict[str, set[int]] = {dataset: set() for dataset in DATASETS}
    metrics: dict[str, set[str]] = {dataset: set() for dataset in DATASETS}
    for row in source_rows:
        dataset = row.get("dataset", "").strip()
        method = row.get("method", "").strip()
        if dataset not in DATASETS or method not in METHODS:
            raise ValueError(f"unexpected frontier summary row: {dataset}/{method}")
        ef = number(row, "ef", summary)
        key = (dataset, method, ef)
        if key in seen:
            raise ValueError(f"duplicate frontier summary point: {key}")
        seen.add(key)
        if integer(row, "n", summary) != 5:
            raise ValueError(f"{key}: expected five measured repeats")
        if integer(row, "threads", summary) != 10 or integer(row, "top_k", summary) != 10:
            raise ValueError(f"{key}: worker/top-k protocol mismatch")
        if method != "d-HNSW" and integer(row, "query_contexts", summary) != 10:
            raise ValueError(f"{key}: query-context protocol mismatch")
        campaign = row.get("campaign_ids", "").strip()
        binary = row.get("binary_sha256s", "").strip()
        if not campaign or ";" in campaign:
            raise ValueError(f"{key}: campaign drift")
        if SHA256_RE.fullmatch(binary) is None:
            raise ValueError(f"{key}: invalid binary SHA")
        if method != "d-HNSW" and binary != expected_sha:
            raise ValueError(f"{key}: binary SHA {binary} does not match final {expected_sha}")
        recall = number(row, "recall_mean", summary)
        qps = number(row, "qps_mean", summary)
        recall_ci = number(row, "recall_ci95", summary)
        qps_ci = number(row, "qps_ci95", summary)
        if not 0 <= recall <= 1 or qps <= 0 or recall_ci < 0 or qps_ci < 0:
            raise ValueError(f"{key}: invalid frontier mean or confidence interval")
        expected_queries = integer(row, "expected_queries", summary)
        if expected_queries <= 0:
            raise ValueError(f"{key}: empty query pool")
        metric = row.get("metric", "").strip()
        if not metric:
            raise ValueError(f"{key}: missing metric")
        query_counts[dataset].add(expected_queries)
        metrics[dataset].add(metric)
        posts_per_query = optional_positive_metric(
            row, "posts_per_query", summary
        )
        if method != "d-HNSW" and posts_per_query is None:
            raise ValueError(f"{key}: missing posts/query evidence")
        counts[(dataset, method)] += 1
        output.append({
            "dataset": dataset,
            "method": method,
            "ef": ef,
            "recall": recall,
            "recall_ci95": recall_ci,
            "qps": qps,
            "qps_ci95": qps_ci,
            "posts_per_query": posts_per_query,
        })

    missing = [key for key, count in counts.items() if count < 5]
    if missing:
        raise ValueError(f"incomplete 10M frontier matrix: {missing}")
    for dataset in DATASETS:
        if len(query_counts[dataset]) != 1 or len(metrics[dataset]) != 1:
            raise ValueError(f"query-pool or metric drift for {dataset}")
    return output


def select_high_recall_post_pairs(
    rows: list[dict[str, object]],
    *,
    recall_tolerance: float = 0.002,
    recall_floor: float = 0.90,
) -> list[dict[str, object]]:
    if not math.isfinite(recall_tolerance) or recall_tolerance < 0:
        raise ValueError("recall tolerance must be finite and non-negative")
    if not math.isfinite(recall_floor) or not 0 <= recall_floor <= 1:
        raise ValueError("recall floor must be in [0, 1]")
    selected = []
    for dataset in DATASETS:
        current = [row for row in rows if row["dataset"] == dataset]
        shine = {
            float(row["ef"]): row for row in current if row["method"] == "SHINE"
        }
        slab = {
            float(row["ef"]): row
            for row in current
            if row["method"] == "SlabWalk"
        }
        if set(shine) != set(slab):
            raise ValueError(f"{dataset}: SHINE/SlabWalk ef grids differ")
        eligible = []
        for ef in sorted(shine):
            baseline = shine[ef]
            design = slab[ef]
            baseline_recall = float(baseline["recall"])
            design_recall = float(design["recall"])
            baseline_posts = baseline.get("posts_per_query")
            design_posts = design.get("posts_per_query")
            if (
                abs(design_recall - baseline_recall) > recall_tolerance
                or min(baseline_recall, design_recall) + 1e-12 < recall_floor
                or baseline_posts is None
                or design_posts is None
                or float(design_posts) <= 0
            ):
                continue
            reduction = float(baseline_posts) / float(design_posts)
            if reduction <= 1.0 + 1e-12:
                continue
            eligible.append({
                "dataset": dataset,
                "ef": ef,
                "recall_floor": min(baseline_recall, design_recall),
                "post_reduction": reduction,
            })
        if eligible:
            selected.append(
                max(eligible, key=lambda pair: (float(pair["recall_floor"]), float(pair["ef"])))
            )
    return selected


def set_style() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Libertinus Serif", "Linux Libertine O", "Times New Roman", "DejaVu Serif"],
        "font.size": 7.4,
        "axes.labelsize": 7.5,
        "xtick.labelsize": 6.8,
        "ytick.labelsize": 6.8,
        "legend.fontsize": 7.2,
        "axes.linewidth": 0.7,
        "lines.linewidth": 1.25,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })


def generate(summary: Path, gate: Path, out: Path) -> None:
    data = load_validated(summary, gate)
    set_style()
    fig, axes = plt.subplots(1, 3, figsize=(7.08, 2.12))
    legend_handles = []
    for panel_index, (ax, dataset) in enumerate(zip(axes, DATASETS)):
        current = [row for row in data if row["dataset"] == dataset]
        for method in METHODS:
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
                markeredgewidth=0.5,
                markersize=4.0,
                capsize=1.8,
                elinewidth=0.65,
                label=style["label"],
                zorder=3 if method == "SlabWalk" else 2,
            )
            if panel_index == 0:
                legend_handles.append(artist)
        recalls = [float(row["recall"]) for row in current]
        qps = [float(row["qps"]) for row in current]
        x_span = max(recalls) - min(recalls)
        x_pad = max(0.008, x_span * 0.07)
        ax.set_xlim(max(0, min(recalls) - x_pad), min(1, max(recalls) + x_pad))
        ax.set_ylim(max(1, min(qps) * 0.65), max(qps) * 1.45)
        ax.set_yscale("log")
        ax.set_xlabel("Recall@10")
        if panel_index == 0:
            ax.set_ylabel("Queries/s (10 workers, log)")
        ax.grid(True, which="both", linestyle=":", color="#C8CED6", linewidth=0.5)
        ax.set_axisbelow(True)
        ax.text(
            0.5,
            -0.36,
            f"({chr(ord('a') + panel_index)}) {dataset}",
            transform=ax.transAxes,
            ha="center",
            va="top",
            fontsize=7.7,
        )
    fig.legend(
        legend_handles,
        [STYLES[method]["label"] for method in METHODS],
        loc="upper center",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0.515, 1.015),
        columnspacing=1.5,
        handlelength=1.8,
    )
    fig.subplots_adjust(left=0.075, right=0.995, top=0.80, bottom=0.31, wspace=0.34)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.025)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate(args.summary, args.gate, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
