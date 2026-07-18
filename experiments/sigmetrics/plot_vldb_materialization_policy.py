#!/usr/bin/env python3
"""Plot semantically validated matched-byte materialization policies."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    from .publication_metadata import pdf_metadata
    from . import summarize_vldb_materialization_policy as policy_summary
except ImportError:
    from publication_metadata import pdf_metadata
    import summarize_vldb_materialization_policy as policy_summary


INK = "#263442"
GRID = "#C8CED6"
POLICY_STYLE = {
    "benefit": ("Benefit/byte", "#0072B2", "o"),
    "indeg": ("Indegree", "#D55E00", "s"),
    "hop": ("Hop reach", "#009E73", "^"),
}
DATASET_ORDER = ("DEEP1M", "SIFT1M", "GIST1M")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty materialization summary: {path}")
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
            "font.size": 7.0,
            "axes.labelsize": 7.2,
            "xtick.labelsize": 6.4,
            "ytick.labelsize": 6.4,
            "legend.fontsize": 6.5,
            "axes.linewidth": 0.7,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def prepare_axis(ax: plt.Axes) -> None:
    ax.grid(True, axis="y", linestyle=":", color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _dataset_order(observed: set[str]) -> list[str]:
    ordered = [dataset for dataset in DATASET_ORDER if dataset in observed]
    ordered.extend(sorted(observed - set(ordered)))
    return ordered


def _format_gib(value: float) -> str:
    if value >= 1.0:
        return f"{value:g}"
    return f"{value:.2g}"


def generate(
    bundle: Path,
    output: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
    allow_smoke: bool = False,
    allow_incomplete: bool = False,
) -> dict[str, object]:
    report = policy_summary.validate_bundle(
        bundle,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    if report["campaign_kind"] != "formal" and not allow_smoke:
        raise ValueError("refusing a non-formal materialization campaign for plotting")

    rows = read_csv(bundle / "summary.csv")
    datasets = _dataset_order({row["dataset"] for row in rows})
    policies = {row["policy"] for row in rows}
    budgets_by_dataset = {
        dataset: {
            int(row["requested_bytes"])
            for row in rows
            if row["dataset"] == dataset
        }
        for dataset in datasets
    }
    if not allow_incomplete:
        if set(datasets) != set(DATASET_ORDER):
            raise ValueError("paper plot requires DEEP1M, SIFT1M, and GIST1M")
        if policies != set(POLICY_STYLE):
            raise ValueError("paper plot requires benefit, indeg, and hop policies")
        if any(len(values) < 2 for values in budgets_by_dataset.values()):
            raise ValueError("paper plot requires at least two byte caps per dataset")
    if not datasets or not policies.issubset(POLICY_STYLE):
        raise ValueError("materialization summary contains unsupported dimensions")

    cells: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        cells[(row["dataset"], row["policy"])].append(row)
    for values in cells.values():
        values.sort(key=lambda row: int(row["requested_bytes"]))

    set_style()
    width = 7.08 if len(datasets) >= 3 else max(2.75, 2.45 * len(datasets))
    fig, axes = plt.subplots(
        2,
        len(datasets),
        figsize=(width, 3.05),
        squeeze=False,
        sharex="col",
    )
    legend_handles = []
    legend_labels = []
    for column, dataset in enumerate(datasets):
        top = axes[0, column]
        bottom = axes[1, column]
        prepare_axis(top)
        prepare_axis(bottom)
        for policy in POLICY_STYLE:
            values = cells.get((dataset, policy), [])
            if not values:
                continue
            label, color, marker = POLICY_STYLE[policy]
            x = np.array(
                [int(row["requested_bytes"]) / (1024.0**3) for row in values]
            )
            qps = np.array([float(row["qps_mean"]) for row in values])
            qps_ci = np.array([float(row["qps_ci95"]) for row in values])
            posts = np.array(
                [float(row["posts_per_query_mean"]) for row in values]
            )
            posts_ci = np.array(
                [float(row["posts_per_query_ci95"]) for row in values]
            )
            line = top.errorbar(
                x,
                qps,
                yerr=qps_ci,
                color=color,
                marker=marker,
                linewidth=1.25,
                markersize=3.5,
                capsize=1.6,
                elinewidth=0.7,
                label=label,
            )
            bottom.errorbar(
                x,
                posts,
                yerr=posts_ci,
                color=color,
                marker=marker,
                linewidth=1.25,
                markersize=3.5,
                capsize=1.6,
                elinewidth=0.7,
            )
            if column == 0:
                legend_handles.append(line.lines[0])
                legend_labels.append(label)

        ticks = sorted(value / (1024.0**3) for value in budgets_by_dataset[dataset])
        bottom.set_xticks(ticks)
        bottom.set_xticklabels([_format_gib(value) for value in ticks])
        top.set_ylim(bottom=0)
        bottom.set_ylim(bottom=0)
        bottom.set_xlabel("Sidecar cap (GiB)")
        if column == 0:
            top.set_ylabel("Throughput (QPS)")
            bottom.set_ylabel("RDMA posts/query")
        bottom.text(
            0.5,
            -0.36,
            f"({chr(ord('a') + column)}) {dataset}",
            transform=bottom.transAxes,
            ha="center",
            va="top",
            fontsize=7.3,
            color=INK,
        )

    fig.legend(
        legend_handles,
        legend_labels,
        frameon=False,
        ncol=3,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.005),
        columnspacing=1.25,
        handlelength=2.0,
    )
    fig.subplots_adjust(
        left=0.078,
        right=0.993,
        top=0.88,
        bottom=0.19,
        wspace=0.28,
        hspace=0.18,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output,
        bbox_inches="tight",
        pad_inches=0.025,
        metadata=pdf_metadata(),
    )
    plt.close(fig)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    parser.add_argument("--allow-smoke", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = generate(
        args.bundle,
        args.out,
        expected_sha=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
        allow_smoke=args.allow_smoke,
        allow_incomplete=args.allow_incomplete,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
