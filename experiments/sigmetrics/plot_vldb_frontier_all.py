#!/usr/bin/env python3
"""Plot the validated 1M breadth and 10M scale frontiers as one 2x5 figure."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import plot_vldb_frontier_1m as frontier_1m
import plot_vldb_frontier_10m as frontier_10m
from publication_metadata import pdf_metadata


DATASETS = (*frontier_1m.DATASETS, *frontier_10m.DATASETS)
METHODS = ("SHINE", "d-HNSW", "SlabWalk")


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
            "font.size": 6.5,
            "axes.labelsize": 6.6,
            "xtick.labelsize": 5.7,
            "ytick.labelsize": 5.7,
            "legend.fontsize": 7.0,
            "axes.linewidth": 0.62,
            "lines.linewidth": 1.05,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def generate(
    summary_1m: Path,
    gate_1m: Path,
    summary_10m: Path,
    gate_10m: Path,
    out: Path,
) -> None:
    rows_1m = frontier_1m.load_validated(summary_1m, gate_1m)
    rows_10m = frontier_10m.load_validated(summary_10m, gate_10m)
    rows = [*rows_1m, *rows_10m]
    post_pairs = {
        str(pair["dataset"]): pair
        for pair in frontier_10m.select_high_recall_post_pairs(rows_10m)
    }

    set_style()
    fig, axes = plt.subplots(2, 5, figsize=(7.08, 3.08))
    legend_handles = []
    for panel_index, (ax, dataset) in enumerate(zip(axes.flat, DATASETS)):
        current = [row for row in rows if row["dataset"] == dataset]
        for method in METHODS:
            points = sorted(
                (row for row in current if row["method"] == method),
                key=lambda row: float(row["ef"]),
            )
            style = frontier_10m.STYLES[method]
            artist = ax.errorbar(
                [float(row["recall"]) for row in points],
                [float(row["qps"]) for row in points],
                xerr=[float(row["recall_ci95"]) for row in points],
                yerr=[float(row["qps_ci95"]) for row in points],
                color=style["color"],
                marker=style["marker"],
                markerfacecolor=style["face"],
                markeredgecolor="#263442",
                markeredgewidth=0.38,
                markersize=3.15,
                capsize=1.15,
                elinewidth=0.48,
                label=style["label"],
                zorder=3 if method == "SlabWalk" else 2,
            )
            if panel_index == 0:
                legend_handles.append(artist)
        recalls = [float(row["recall"]) for row in current]
        qps = [float(row["qps"]) for row in current]
        x_span = max(recalls) - min(recalls)
        x_pad = max(0.008, x_span * 0.065)
        ax.set_xlim(max(0, min(recalls) - x_pad), min(1, max(recalls) + x_pad))
        ax.set_ylim(max(1, min(qps) * 0.60), max(qps) * 1.55)
        ax.set_yscale("log")
        ax.set_xlabel(
            f"Recall@10\n({chr(ord('a') + panel_index)}) {dataset}",
            labelpad=0.2,
            linespacing=0.88,
        )
        if panel_index in (0, 5):
            ax.set_ylabel("QPS (10T, log)", labelpad=0.8)
        ax.grid(True, which="both", linestyle=":", color="#C8CED6", linewidth=0.4)
        ax.set_axisbelow(True)
        if dataset in post_pairs:
            pair = post_pairs[dataset]
            ax.text(
                0.04,
                0.94,
                f'{float(pair["post_reduction"]):.1f}$\\times$ fewer posts',
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=5.5,
                color="#0D426A",
                bbox={
                    "boxstyle": "round,pad=0.16",
                    "facecolor": "white",
                    "edgecolor": "#8FB9D6",
                    "linewidth": 0.45,
                    "alpha": 0.90,
                },
                zorder=5,
            )
    fig.legend(
        legend_handles,
        [frontier_10m.STYLES[method]["label"] for method in METHODS],
        loc="upper center",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0.5, 1.005),
        columnspacing=1.3,
        handlelength=1.55,
    )
    fig.subplots_adjust(
        left=0.064,
        right=0.995,
        top=0.84,
        bottom=0.17,
        wspace=0.48,
        hspace=0.58,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.012, metadata=pdf_metadata())
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-1m", type=Path, required=True)
    parser.add_argument("--gate-1m", type=Path, required=True)
    parser.add_argument("--summary-10m", type=Path, required=True)
    parser.add_argument("--gate-10m", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    generate(
        args.summary_1m,
        args.gate_1m,
        args.summary_10m,
        args.gate_10m,
        args.out,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
