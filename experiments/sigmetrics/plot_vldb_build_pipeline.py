#!/usr/bin/env python3
"""Plot semantically validated staged-builder scaling evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    from .publication_metadata import pdf_metadata
    from . import summarize_vldb_build_pipeline as build_summary
except ImportError:
    from publication_metadata import pdf_metadata
    import summarize_vldb_build_pipeline as build_summary


INK = "#263442"
MUTED = "#667085"
GRID = "#C8CED6"
BLUE = "#0072B2"
GREEN = "#009E73"
ORANGE = "#D55E00"
PURPLE = "#7B61A8"
LIGHT_BLUE = "#D7E8F4"
LIGHT_GREEN = "#CDEDE3"
LIGHT_ORANGE = "#F6D7C8"
LIGHT_PURPLE = "#E5DCF0"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty builder summary: {path}")
    return rows


def number(row: dict[str, str], field: str) -> float:
    return float(row[field])


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
            "font.size": 7.2,
            "axes.labelsize": 7.3,
            "xtick.labelsize": 6.7,
            "ytick.labelsize": 6.7,
            "legend.fontsize": 6.2,
            "axes.linewidth": 0.7,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def prepare_axis(ax: plt.Axes) -> None:
    ax.grid(True, axis="y", linestyle=":", color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def panel_label(ax: plt.Axes, label: str) -> None:
    ax.text(
        0.5,
        -0.28,
        label,
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=7.4,
    )


def generate(
    bundle: Path,
    output: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
    allow_smoke: bool = False,
) -> dict[str, object]:
    report = build_summary.validate_bundle(
        bundle,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    if report["campaign_kind"] != "formal" and not allow_smoke:
        raise ValueError("refusing a non-formal builder campaign for plotting")
    rows = sorted(
        read_csv(bundle / "summary.csv"), key=lambda row: int(row["build_workers"])
    )
    workers = np.array([int(row["build_workers"]) for row in rows])
    if workers[0] != 1 or len(set(workers)) != len(workers):
        raise ValueError("builder plot requires unique workers and a one-worker baseline")

    total = np.array([number(row, "build_total_ms_mean") for row in rows])
    total_ci = np.array([number(row, "build_total_ms_ci95") for row in rows])
    rank = np.array([number(row, "build_rank_ms_mean") for row in rows])
    materialize = np.array(
        [number(row, "build_materialize_ms_mean") for row in rows]
    )
    assemble = np.array(
        [number(row, "build_record_assemble_ms_mean") for row in rows]
    )
    publish = np.array(
        [number(row, "build_record_publish_ms_mean") for row in rows]
    )
    residual = total - rank - assemble - publish
    if np.any(residual < -1e-6):
        raise ValueError("builder stage accounting exceeds total build time")
    residual = np.maximum(residual, 0.0)

    set_style()
    fig, axes = plt.subplots(1, 2, figsize=(7.08, 2.25))

    ax = axes[0]
    prepare_axis(ax)
    ax.plot(
        workers,
        total[0] / total,
        color=BLUE,
        marker="o",
        linewidth=1.45,
        markersize=3.8,
        label="Total build",
    )
    ax.plot(
        workers,
        rank[0] / rank,
        color=GREEN,
        marker="s",
        linewidth=1.35,
        markersize=3.6,
        label="Ranking",
    )
    ax.plot(
        workers,
        materialize[0] / materialize,
        color=PURPLE,
        marker="^",
        linewidth=1.35,
        markersize=3.8,
        label="Materialization",
    )
    ax.plot(
        workers,
        workers,
        color=MUTED,
        linestyle="--",
        linewidth=0.85,
        label="Ideal",
    )
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xticks(workers)
    ax.set_xticklabels([str(value) for value in workers])
    ymax = max(float(workers[-1]), float(np.max(rank[0] / rank)))
    yticks = [value for value in (1, 2, 4, 8, 16, 32, 64) if value <= ymax * 1.05]
    ax.set_yticks(yticks)
    ax.set_yticklabels([str(value) for value in yticks])
    ax.set_xlabel("Builder workers")
    ax.set_ylabel("Speedup over one worker")
    ax.legend(frameon=False, ncol=2, loc="upper left", columnspacing=0.9)
    panel_label(ax, "(a) Parallel scaling")

    ax = axes[1]
    prepare_axis(ax)
    xpos = np.arange(len(rows))
    components = (
        (rank / 1000.0, "Ranking", LIGHT_GREEN, GREEN),
        (assemble / 1000.0, "Record assembly", LIGHT_BLUE, BLUE),
        (publish / 1000.0, "RDMA publish", LIGHT_ORANGE, ORANGE),
        (residual / 1000.0, "Other stages", LIGHT_PURPLE, PURPLE),
    )
    bottom = np.zeros(len(rows))
    for values, label, color, edge in components:
        ax.bar(
            xpos,
            values,
            bottom=bottom,
            width=0.66,
            color=color,
            edgecolor=edge,
            linewidth=0.65,
            label=label,
        )
        bottom += values
    ax.errorbar(
        xpos,
        total / 1000.0,
        yerr=total_ci / 1000.0,
        fmt="none",
        ecolor=INK,
        elinewidth=0.75,
        capsize=1.8,
        zorder=4,
    )
    ax.set_xticks(xpos)
    ax.set_xticklabels([str(value) for value in workers])
    ax.set_xlabel("Builder workers")
    ax.set_ylabel("Total build time (s)")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, ncol=2, loc="upper right", columnspacing=0.9)
    for index in {0, len(rows) - 1}:
        ax.text(
            xpos[index],
            total[index] / 1000.0 + max(total / 1000.0) * 0.025,
            f"{total[index] / 1000.0:.2f}",
            ha="center",
            va="bottom",
            fontsize=6.1,
            color=INK,
        )
    panel_label(ax, "(b) Build-stage closure")

    fig.subplots_adjust(
        left=0.075,
        right=0.99,
        top=0.95,
        bottom=0.24,
        wspace=0.28,
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
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = generate(
        args.bundle,
        args.out,
        expected_sha=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
        allow_smoke=args.allow_smoke,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
