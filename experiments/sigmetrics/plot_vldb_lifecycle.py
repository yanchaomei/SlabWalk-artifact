#!/usr/bin/env python3
"""Plot gated Slab construction, offline refresh, and boundary controls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    from . import validate_vldb_final_evidence as evidence
except ImportError:
    import validate_vldb_final_evidence as evidence


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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def require_gate_sha(path: Path, expected: str, label: str) -> None:
    actual = file_sha256(path)
    if actual != expected:
        raise ValueError(f"{label} SHA {actual} does not match gate {expected!r}")


def load_validated(
    build_cost: Path, lifecycle: Path, gate: Path
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
]:
    if not gate.is_file():
        raise ValueError(f"missing evidence gate: {gate}")
    gate_obj = json.loads(gate.read_text())
    if gate_obj.get("ready_for_plotting") is not True:
        raise ValueError(f"evidence gate is not plot-ready: {gate}")
    expected_sha = str(gate_obj.get("expected_slabwalk_sha256", ""))
    build_gate = gate_obj.get("build_cost", {})
    lifecycle_gate = gate_obj.get("lifecycle_controls", {})
    require_gate_sha(
        build_cost / "runs.csv",
        str(build_gate.get("runs_sha256", "")),
        "build-cost runs",
    )
    require_gate_sha(
        build_cost / "summary.csv",
        str(build_gate.get("summary_sha256", "")),
        "build-cost summary",
    )
    require_gate_sha(
        build_cost / "stage_breakdown.csv",
        str(build_gate.get("stage_breakdown_sha256", "")),
        "build-cost stage breakdown",
    )
    require_gate_sha(
        lifecycle / "refresh.csv",
        str(lifecycle_gate.get("refresh_sha256", "")),
        "lifecycle refresh",
    )
    require_gate_sha(
        lifecycle / "tti.csv",
        str(lifecycle_gate.get("tti_sha256", "")),
        "lifecycle TTI",
    )
    evidence.validate_build_cost(build_cost, expected_sha)
    evidence.validate_lifecycle_controls(lifecycle)
    return (
        read_csv(build_cost / "summary.csv"),
        read_csv(build_cost / "stage_breakdown.csv"),
        read_csv(lifecycle / "refresh.csv"),
        read_csv(lifecycle / "tti.csv"),
    )


def value(row: dict[str, str], field: str) -> float:
    return float(row[field])


def set_style() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Libertinus Serif", "Linux Libertine O", "Times New Roman", "DejaVu Serif"],
        "font.size": 7.2,
        "axes.labelsize": 7.3,
        "xtick.labelsize": 6.7,
        "ytick.labelsize": 6.7,
        "legend.fontsize": 6.2,
        "axes.linewidth": 0.7,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })


def prepare_axis(ax: plt.Axes) -> None:
    ax.grid(True, axis="y", linestyle=":", color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def panel_label(ax: plt.Axes, label: str) -> None:
    ax.text(0.5, -0.31, label, transform=ax.transAxes, ha="center", va="top", fontsize=7.4)


def generate(build_cost: Path, lifecycle: Path, gate: Path, out: Path) -> None:
    summaries, stages, refresh, tti = load_validated(build_cost, lifecycle, gate)
    set_style()
    fig, axes = plt.subplots(2, 3, figsize=(7.08, 3.90))
    dataset_order = ["SIFT1M", "DEEP1M", "GIST1M"]
    summary_by_dataset = {row["dataset"]: row for row in summaries}
    x = np.arange(3)
    labels = ["SIFT", "DEEP", "GIST"]

    ax = axes[0, 0]
    prepare_axis(ax)
    means = [value(summary_by_dataset[name], "build_mean_s") for name in dataset_order]
    intervals = [value(summary_by_dataset[name], "build_ci95_half_s") for name in dataset_order]
    bars = ax.bar(
        x,
        means,
        yerr=intervals,
        color=[LIGHT_BLUE, LIGHT_GREEN, LIGHT_ORANGE],
        edgecolor=[BLUE, GREEN, ORANGE],
        linewidth=0.75,
        capsize=2.0,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Build time (s)")
    ax.set_ylim(bottom=0)
    for bar, mean in zip(bars, means):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(means) * 0.035,
            f"{mean:.1f}",
            ha="center",
            va="bottom",
            fontsize=6.1,
        )
    panel_label(ax, "(a) Derived build time")

    ax = axes[0, 1]
    prepare_axis(ax)
    stage_by_key = {(row["dataset"], row["stage"]): row for row in stages}
    stage_groups = (
        ("Fetch/parse", ("fetch", "parse", "rank"), BLUE),
        ("Encode", ("encode",), GREEN),
        ("Metadata", ("metadata",), MUTED),
        ("Materialize", ("materialize",), PURPLE),
    )
    left = np.zeros(3)
    for group_label, members, color in stage_groups:
        shares = np.array([
            sum(
                value(stage_by_key[(dataset, member)], "median_share_pct")
                for member in members
            )
            for dataset in dataset_order
        ])
        ax.barh(x, shares, left=left, color=color, edgecolor="white", linewidth=0.4, label=group_label)
        left += shares
    ax.set_yticks(x)
    ax.set_yticklabels(labels)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Build-time share (%)")
    ax.legend(frameon=False, loc="lower center", ncol=2, bbox_to_anchor=(0.5, 1.0), columnspacing=0.8, handlelength=1.1)
    panel_label(ax, "(b) Stage composition")

    ax = axes[0, 2]
    prepare_axis(ax)
    width = 0.34
    rss = [value(summary_by_dataset[name], "build_peak_rss_mean_gib") for name in dataset_order]
    slab = [value(summary_by_dataset[name], "region_gb") / 1.073741824 for name in dataset_order]
    ax.bar(x - width / 2, rss, width, color=LIGHT_BLUE, edgecolor=BLUE, linewidth=0.7, label="Builder RSS")
    ax.bar(x + width / 2, slab, width, color=LIGHT_PURPLE, edgecolor=PURPLE, linewidth=0.7, label="Slab bytes")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("GiB")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, loc="upper left", handlelength=1.2)
    panel_label(ax, "(c) DRAM and output")

    refresh = sorted(refresh, key=lambda row: value(row, "batch_inserts"))
    batches = np.array([value(row, "batch_inserts") for row in refresh])
    ax = axes[1, 0]
    prepare_axis(ax)
    amps = [value(row, "write_amp_blocks_per_insert") for row in refresh]
    ax.plot(batches, amps, color=GREEN, marker="o", linewidth=1.45, markersize=4.0)
    ax.axhline(32, color=MUTED, linestyle="--", linewidth=0.9)
    ax.set_xscale("log")
    ax.set_xticks([1000, 10000, 100000])
    ax.set_xticklabels(["1K", "10K", "100K"])
    ax.set_xlabel("Replayed suffix")
    ax.set_ylabel("Selected Slabs / node")
    ax.set_ylim(0, 35)
    ax.text(0.97, 0.93, r"$M_{max0}=32$", transform=ax.transAxes, ha="right", va="top", color=MUTED, fontsize=6.0)
    ax.text(0.04, 0.08, "full-region mismatches: 0", transform=ax.transAxes, color=MUTED, fontsize=6.0)
    panel_label(ax, "(d) Rewrite amplification")

    ax = axes[1, 1]
    prepare_axis(ax)
    diff_rows = [row for row in refresh if row["diff_read_frac"] != ""]
    diff_x = np.arange(len(diff_rows))
    diff_pct = [100 * value(row, "diff_read_frac") for row in diff_rows]
    bars = ax.bar(
        diff_x,
        diff_pct,
        width=0.55,
        color=LIGHT_ORANGE,
        edgecolor=ORANGE,
        linewidth=0.75,
    )
    ax.axhline(100, color=MUTED, linestyle="--", linewidth=0.9)
    ax.set_xticks(diff_x)
    ax.set_xticklabels([f"{int(value(row, 'batch_inserts') / 1000)}K" for row in diff_rows])
    ax.set_xlabel("Replayed suffix")
    ax.set_ylabel("Authoritative bytes reread (%)")
    ax.set_ylim(0, 108)
    for bar, pct in zip(bars, diff_pct):
        ax.text(bar.get_x() + bar.get_width() / 2, pct + 2.5, f"{pct:.1f}", ha="center", fontsize=6.0)
    ax.text(0.04, 0.93, "full reread", transform=ax.transAxes, ha="left", va="top", color=MUTED, fontsize=6.0)
    panel_label(ax, "(e) Differential reread")

    ax = axes[1, 2]
    prepare_axis(ax)
    tti_labels = {
        "fp32 baseline": ("fp32", INK),
        "sq8 Slabs": ("sq8", BLUE),
        "RaBitQ-2 Slabs": ("RQ-2", GREEN),
        "RaBitQ-4 Slabs": ("RQ-4", PURPLE),
    }
    for row in tti:
        if row["config"] not in tti_labels or int(row["threads"]) != 1:
            continue
        short, color = tti_labels[row["config"]]
        qps = value(row, "qps")
        payload = value(row, "mb_per_query")
        recall = value(row, "recall")
        ax.scatter(payload, recall, s=24 + qps * 0.04, color=color, edgecolor="white", linewidth=0.55, zorder=3)
        offset = (-18, 4) if short == "fp32" else (4, 4)
        ax.annotate(short, (payload, recall), textcoords="offset points", xytext=offset, fontsize=6.1, color=color)
    plotted_payloads = [
        value(row, "mb_per_query")
        for row in tti
        if row["config"] in tti_labels and int(row["threads"]) == 1
    ]
    payload_span = max(plotted_payloads) - min(plotted_payloads)
    payload_margin = max(0.08, payload_span * 0.10)
    ax.set_xlim(min(plotted_payloads) - payload_margin, max(plotted_payloads) + payload_margin)
    ax.set_xlabel("Transferred MB/query")
    ax.set_ylabel("Recall@10")
    ax.set_ylim(0.79, 0.98)
    ax.text(0.04, 0.08, "marker area encodes QPS", transform=ax.transAxes, color=MUTED, fontsize=5.9)
    panel_label(ax, "(f) TTI code boundary")

    fig.subplots_adjust(left=0.075, right=0.985, top=0.96, bottom=0.16, hspace=0.66, wspace=0.48)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.025)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-cost", type=Path, required=True)
    parser.add_argument("--lifecycle-controls", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate(args.build_cost, args.lifecycle_controls, args.gate, args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
