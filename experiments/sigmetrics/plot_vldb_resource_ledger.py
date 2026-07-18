#!/usr/bin/env python3
"""Plot the validated GIST1M physical-layout resource ledger."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

try:
    from . import summarize_vldb_resource_ledger as resource_summary
    from . import validate_vldb_final_evidence as evidence
except ImportError:
    import summarize_vldb_resource_ledger as resource_summary
    import validate_vldb_final_evidence as evidence


LAYOUTS = ("legacy", "fixed", "variable")
MN_COUNTS = (1, 3, 5)
INK = "#263442"
GRID = "#C8CED6"
BLUE = "#0072B2"
GREEN = "#009E73"
ORANGE = "#D55E00"
PURPLE = "#7B61A8"
RED = "#C44E52"
LAYOUT_STYLE = {
    "legacy": {"label": "legacy sparse", "color": RED, "marker": "s", "face": "#F4DADB"},
    "fixed": {"label": "packed fixed", "color": BLUE, "marker": "^", "face": "#D8ECF6"},
    "variable": {"label": "packed variable", "color": GREEN, "marker": "o", "face": GREEN},
}


def load_validated(runs: Path, gate: Path) -> list[dict[str, str]]:
    if not gate.is_file():
        raise ValueError(f"missing evidence gate: {gate}")
    gate_obj = json.loads(gate.read_text())
    if gate_obj.get("ready_for_plotting") is not True:
        raise ValueError(f"evidence gate is not plot-ready: {gate}")
    expected_sha = str(gate_obj.get("expected_slabwalk_sha256", ""))
    expected_runs_sha = str(gate_obj.get("resource_ledger", {}).get("runs_sha256", ""))
    if not runs.is_file():
        raise ValueError(f"missing resource-ledger runs: {runs}")
    actual_runs_sha = hashlib.sha256(runs.read_bytes()).hexdigest()
    if actual_runs_sha != expected_runs_sha:
        raise ValueError(
            f"resource-ledger runs SHA {actual_runs_sha} does not match gate "
            f"{expected_runs_sha!r}"
        )
    evidence.validate_resource_ledger(runs.parent, expected_sha)
    with runs.open(newline="") as handle:
        return list(csv.DictReader(handle))


def validate_bound_summary(
    path: Path, gate: Path, key: str, label: str
) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing {label}: {path}")
    gate_obj = json.loads(gate.read_text())
    expected = str(gate_obj.get("mechanism_controls", {}).get(key, ""))
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"{label} SHA {actual} does not match gate {expected!r}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty {label}: {path}")
    return rows


def samples(
    rows: list[dict[str, str]], layout: str, mns: int, metric: str
) -> list[float]:
    values = [
        float(row[metric])
        for row in rows
        if row["layout"] == layout and int(row["memory_nodes"]) == mns
    ]
    if len(values) != 5 or any(not math.isfinite(value) for value in values):
        raise ValueError(f"{layout}/S={mns}: expected five finite {metric} samples")
    return values


def series(
    rows: list[dict[str, str]], layout: str, metric: str
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for mns in MN_COUNTS:
        values = samples(rows, layout, mns, metric)
        means.append(statistics.mean(values))
        intervals.append(resource_summary.t_ci_half(values))
    return means, intervals


def s5_stats(
    rows: list[dict[str, str]], metric: str, scale: float = 1.0
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for layout in LAYOUTS:
        values = [value / scale for value in samples(rows, layout, 5, metric)]
        means.append(statistics.mean(values))
        intervals.append(resource_summary.t_ci_half(values))
    return means, intervals


def set_style() -> None:
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Libertinus Serif", "Linux Libertine O", "Times New Roman", "DejaVu Serif"],
        "font.size": 7.2,
        "axes.labelsize": 7.3,
        "axes.labelpad": 2.0,
        "xtick.labelsize": 6.7,
        "ytick.labelsize": 6.7,
        "legend.fontsize": 6.3,
        "axes.linewidth": 0.7,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })


def prepare_axis(ax: plt.Axes) -> None:
    ax.grid(True, axis="y", linestyle=":", color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)


def panel_label(ax: plt.Axes, label: str) -> None:
    xlabel = ax.get_xlabel()
    text = f"{xlabel}\n{label}" if xlabel else label
    ax.set_xlabel(text, labelpad=1.0, linespacing=0.90)


def layout_lines(
    ax: plt.Axes, rows: list[dict[str, str]], metric: str, scale: float = 1.0
) -> list[plt.Artist]:
    handles = []
    for layout in LAYOUTS:
        mean, ci = series(rows, layout, metric)
        style = LAYOUT_STYLE[layout]
        artist = ax.errorbar(
            MN_COUNTS,
            [value / scale for value in mean],
            yerr=[value / scale for value in ci],
            color=style["color"],
            marker=style["marker"],
            markerfacecolor=style["face"],
            markeredgecolor=INK,
            markeredgewidth=0.45,
            markersize=4.0,
            linewidth=1.2,
            elinewidth=0.65,
            capsize=1.8,
            label=style["label"],
        )
        handles.append(artist)
    ax.set_xticks(MN_COUNTS)
    ax.set_xlabel("Passive memory nodes")
    return handles


def generate(
    runs: Path,
    budget_summary: Path,
    resident_summary: Path,
    gate: Path,
    out: Path,
) -> None:
    rows = load_validated(runs, gate)
    budget_rows = validate_bound_summary(
        budget_summary, gate, "budget_summary_sha256", "budget summary"
    )
    resident_rows = validate_bound_summary(
        resident_summary, gate, "resident_summary_sha256", "resident summary"
    )
    set_style()
    fig, axes = plt.subplots(2, 3, figsize=(7.08, 3.23))

    budget_rows.sort(key=lambda row: float(row["materialized_fraction"]))
    fractions = np.array(
        [100.0 * float(row["materialized_fraction"]) for row in budget_rows]
    )
    budget_x = np.arange(len(budget_rows))
    budget_labels = [f"{value:g}" for value in fractions]
    ax = axes[0, 0]
    prepare_axis(ax)
    materialized = np.array(
        [float(row["materialized_bytes_mean"]) / 2**30 for row in budget_rows]
    )
    materialized_ci = np.array(
        [float(row["materialized_bytes_ci95"]) / 2**30 for row in budget_rows]
    )
    ax.errorbar(
        budget_x,
        materialized,
        yerr=materialized_ci,
        color=BLUE,
        marker="o",
        markerfacecolor="#D8ECF6",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=4.0,
        linewidth=1.25,
        capsize=1.7,
    )
    ax.fill_between(budget_x, 0, materialized, color="#D8ECF6", alpha=0.45)
    ax.set_xticks(budget_x)
    ax.set_xticklabels(budget_labels)
    ax.set_xlabel("Materialized nodes (%)")
    ax.set_ylabel("Live Slab bytes (GiB)")
    panel_label(ax, "(a) Budgeted materialization")

    ax = axes[0, 1]
    prepare_axis(ax)
    qps = np.array([float(row["qps_mean"]) for row in budget_rows])
    qps_ci = np.array([float(row["qps_ci95"]) for row in budget_rows])
    recall = np.array([float(row["recall_mean"]) for row in budget_rows])
    recall_ci = np.array([float(row["recall_ci95"]) for row in budget_rows])
    qps_artist = ax.errorbar(
        budget_x,
        qps,
        yerr=qps_ci,
        color=ORANGE,
        marker="o",
        markersize=4.0,
        linewidth=1.25,
        capsize=1.7,
        label="QPS",
    )
    ax.set_xticks(budget_x)
    ax.set_xticklabels(budget_labels)
    ax.set_xlabel("Materialized nodes (%)")
    ax.set_ylabel("Queries/s", color=ORANGE)
    ax.tick_params(axis="y", labelcolor=ORANGE)
    ax2 = ax.twinx()
    recall_artist = ax2.errorbar(
        budget_x,
        recall,
        yerr=recall_ci,
        color=GREEN,
        marker="s",
        markerfacecolor="#DDEFE9",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=3.8,
        linewidth=1.15,
        capsize=1.7,
        label="Recall@10",
    )
    recall_pad = max(0.002, 0.18 * max(0.001, float(recall.max() - recall.min())))
    ax2.set_ylim(float(recall.min() - recall_pad), float(recall.max() + recall_pad))
    ax2.set_ylabel("Recall@10", color=GREEN)
    ax2.tick_params(axis="y", labelcolor=GREEN)
    ax.legend(
        [qps_artist, recall_artist],
        ["QPS", "Recall@10"],
        frameon=False,
        loc="lower right",
        handlelength=1.2,
    )
    panel_label(ax, "(b) Budget performance")

    ax = axes[0, 2]
    prepare_axis(ax)
    ef_values = sorted({int(row["ef"]) for row in resident_rows})
    remote_rows = {
        int(row["ef"]): row for row in resident_rows if row["mode"] == "remote"
    }
    local_rows = {
        int(row["ef"]): row for row in resident_rows if row["mode"] == "resident"
    }
    if set(remote_rows) != set(ef_values) or set(local_rows) != set(ef_values):
        raise ValueError("resident summary must contain remote and resident rows per ef")
    remote_posts = [
        float(remote_rows[ef]["posts_upnav_per_query_mean"]) for ef in ef_values
    ]
    post_bars = ax.bar(
        ef_values,
        remote_posts,
        width=24,
        color="#D8ECF6",
        edgecolor=BLUE,
        linewidth=0.65,
        label="remote upper posts/q",
    )
    ax.set_xticks(ef_values)
    ax.set_xlabel("Search width (ef)")
    ax.set_ylabel("Upper posts/q", color=BLUE)
    ax.tick_params(axis="y", labelcolor=BLUE)
    ax2 = ax.twinx()
    remote_qps = ax2.errorbar(
        ef_values,
        [float(remote_rows[ef]["qps_mean"]) for ef in ef_values],
        yerr=[float(remote_rows[ef]["qps_ci95"]) for ef in ef_values],
        color=ORANGE,
        marker="^",
        markerfacecolor="#F6D6C3",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=4.0,
        linewidth=1.2,
        capsize=1.7,
        label="remote QPS",
    )
    resident_qps = ax2.errorbar(
        ef_values,
        [float(local_rows[ef]["qps_mean"]) for ef in ef_values],
        yerr=[float(local_rows[ef]["qps_ci95"]) for ef in ef_values],
        color=GREEN,
        marker="o",
        markerfacecolor="#DDEFE9",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=4.0,
        linewidth=1.2,
        capsize=1.7,
        label="resident QPS",
    )
    ax2.set_ylabel("Queries/s", color=GREEN)
    ax2.tick_params(axis="y", labelcolor=GREEN)
    ax.legend(
        [post_bars, remote_qps, resident_qps],
        ["remote posts", "remote QPS", "resident QPS"],
        frameon=False,
        loc="center right",
        handlelength=1.1,
    )
    panel_label(ax, "(c) Resident upper graph")

    ax = axes[1, 0]
    prepare_axis(ax)
    layout_handles = layout_lines(ax, rows, "storage_amplification")
    ax.axhline(1.0, color="#7A8491", linestyle=":", linewidth=0.7)
    ax.set_ylabel("Total MN bytes / HNSW")
    ax.legend(
        layout_handles,
        [LAYOUT_STYLE[layout]["label"] for layout in LAYOUTS],
        frameon=False,
        loc="upper right",
        handlelength=1.2,
    )
    panel_label(ax, "(d) Storage amplification")

    x = np.arange(len(LAYOUTS))
    ax = axes[1, 1]
    prepare_axis(ax)
    registered, registered_ci = s5_stats(rows, "registered_sidecar_bytes", 2**30)
    materialized, materialized_ci = s5_stats(rows, "materialized_sidecar_bytes", 2**30)
    width = 0.36
    ax.bar(
        x - width / 2,
        registered,
        width,
        yerr=registered_ci,
        color="#E4E8ED",
        edgecolor=INK,
        linewidth=0.6,
        capsize=1.7,
        label="registered",
    )
    ax.bar(
        x + width / 2,
        materialized,
        width,
        yerr=materialized_ci,
        color="#93CFC3",
        edgecolor=GREEN,
        linewidth=0.6,
        capsize=1.7,
        label="materialized",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(["legacy", "fixed", "variable"])
    ax.set_ylabel("Aggregate GiB, S=5")
    ax.legend(frameon=False, loc="upper right", handlelength=1.1)
    panel_label(ax, "(e) Reserved and live bytes")

    ax = axes[1, 2]
    prepare_axis(ax)
    variable_qps, variable_qps_ci = series(rows, "variable", "qps")
    variable_rss, variable_rss_ci = series(rows, "variable", "mn_peak_rss_max_kib")
    qps_artist = ax.errorbar(
        MN_COUNTS,
        variable_qps,
        yerr=variable_qps_ci,
        color=ORANGE,
        marker="o",
        markersize=4.0,
        linewidth=1.25,
        capsize=1.7,
        label="QPS",
    )
    ax.set_xticks(MN_COUNTS)
    ax.set_xlabel("Passive memory nodes")
    ax.set_ylabel("Queries/s", color=ORANGE)
    ax.tick_params(axis="y", labelcolor=ORANGE)
    ax2 = ax.twinx()
    rss_artist = ax2.errorbar(
        MN_COUNTS,
        [value / 2**20 for value in variable_rss],
        yerr=[value / 2**20 for value in variable_rss_ci],
        color=GREEN,
        marker="s",
        markerfacecolor="#DDEFE9",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=4.0,
        linewidth=1.2,
        capsize=1.7,
        label="peak MN RSS",
    )
    ax2.set_ylabel("Peak MN RSS (GiB)", color=GREEN)
    ax2.tick_params(axis="y", labelcolor=GREEN)
    ax.legend(
        [qps_artist, rss_artist],
        ["QPS", "peak MN RSS"],
        frameon=False,
        loc="center right",
        handlelength=1.2,
    )
    panel_label(ax, "(f) Variable-layout striping")

    fig.subplots_adjust(left=0.075, right=0.96, top=0.96, bottom=0.15, hspace=0.50, wspace=0.55)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.015)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--budget-summary", type=Path, required=True)
    parser.add_argument("--resident-summary", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate(
        args.runs,
        args.budget_summary,
        args.resident_summary,
        args.gate,
        args.out,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
