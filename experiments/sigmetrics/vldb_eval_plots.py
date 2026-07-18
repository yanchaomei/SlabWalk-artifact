#!/usr/bin/env python3
"""Generate the PVLDB-only evaluation composites from checked CSV sources."""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "paper_vldb" / "figs"
BUILD_SUMMARY = ROOT / "results" / "vldb_build_cost" / "summary.csv"
BUILD_STAGES = ROOT / "results" / "vldb_build_cost" / "stage_breakdown.csv"
BUDGET = ROOT / "results" / "sigmetrics_main_figures" / "q2_budget_pareto.csv"
GUARDS = ROOT / "results" / "sigmetrics_main_figures" / "q2_memory_guards.csv"
RESIDENT = ROOT / "results" / "sigmetrics_main_figures" / "q2_resident_upper_graph.csv"
STRIPING = ROOT / "results" / "sigmetrics_main_figures" / "q3_shine_scaleout.csv"
DHNSW_PLATEAU = ROOT / "results" / "sigmetrics_main_figures" / "q4_dhnsw_plateau.csv"
REFRESH = ROOT / "results" / "sigmetrics_main_figures" / "q5_rebuild_maintenance.csv"
TTI = ROOT / "results" / "sigmetrics_reviewer_gap_controls" / "tti_boundary.csv"
TAIL = ROOT / "results" / "sigmetrics_main_figures" / "q1_tail_trace.csv"
WORKERS = ROOT / "results" / "sigmetrics_main_figures" / "q1_worker_scaling.csv"
DHNSW_WORKERS = ROOT / "results" / "sigmetrics_main_figures" / "q1_worker_scaling_dhnsw_runs.csv"
CEILING = ROOT / "results" / "sigmetrics_main_figures" / "q1_ceiling.csv"
DEEP_SCALE = ROOT / "results" / "sigmetrics_main_figures" / "q1_deep_scale.csv"
RDMA_TAU = ROOT / "results" / "sigmetrics_rdma_tau_microbench" / "rdma_tau_raw.csv"
RESOURCE_SUMMARY = ROOT / "results" / "vldb_resource_ledger" / "summary.csv"
RESOURCE_RUNS = ROOT / "results" / "vldb_resource_ledger" / "runs.csv"

INK = "#263442"
MUTED = "#667085"
GRID = "#C8CED6"
WHITE = "#FFFFFF"
BLUE = "#0D6EB5"
TEAL = "#147A73"
GREEN = "#2F7D46"
ORANGE = "#CF6B00"
PURPLE = "#5B3F8F"
RED = "#C24F4B"
LIGHT_BLUE = "#DCECF8"
LIGHT_GREEN = "#DDEEDC"
LIGHT_ORANGE = "#FFE3C2"
LIGHT_PURPLE = "#E7DCF3"
LIGHT_GREY = "#E9EDF2"


def rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def f(row: dict[str, str], key: str) -> float:
    value = row[key]
    if value == "":
        return float("nan")
    return float(value)


def style() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["Linux Libertine O", "Libertinus Serif", "Times New Roman", "DejaVu Serif"],
            "font.size": 7.2,
            "axes.titlesize": 8.1,
            "axes.labelsize": 7.4,
            "xtick.labelsize": 6.8,
            "ytick.labelsize": 6.8,
            "legend.fontsize": 6.6,
            "axes.edgecolor": INK,
            "axes.linewidth": 0.75,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def panel(ax: plt.Axes, label: str, title: str) -> None:
    ax.set_title(f"{label} {title}", loc="left", fontweight="bold", pad=3.0)
    ax.grid(True, axis="y", linestyle=":", color=GRID, linewidth=0.55)
    ax.set_axisbelow(True)


def save(fig: plt.Figure, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.pdf", bbox_inches="tight", pad_inches=0.025)
    plt.close(fig)


def build_index_cost() -> None:
    budget = rows(BUDGET)
    resident = rows(RESIDENT)
    resource = rows(RESOURCE_SUMMARY)
    layout_style = {
        "legacy": (RED, "o", "legacy sparse"),
        "fixed": (BLUE, "s", "packed fixed"),
        "variable": (GREEN, "^", "packed variable"),
    }

    fig, axes = plt.subplots(2, 3, figsize=(7.08, 4.18))
    ax = axes[0, 0]
    panel(ax, "(a)", "Budgeted materialization")
    for dataset, color, marker in (("SIFT-1M", BLUE, "o"), ("GIST-200K", ORANGE, "s")):
        selected = [r for r in budget if r["dataset"] == dataset and r["policy"] == "in-degree"]
        selected.sort(key=lambda r: f(r, "f"))
        x = [f(r, "region_gb") for r in selected]
        y = [f(r, "qps") for r in selected]
        ax.plot(x, y, color=color, marker=marker, linewidth=1.5, markersize=4.0, label=dataset)
        for index in (0, len(selected) - 1):
            ax.annotate(
                f"f={f(selected[index], 'f'):.2g}",
                (x[index], y[index]),
                textcoords="offset points",
                xytext=(3, 4 if index == 0 else -10),
                fontsize=6.0,
                color=color,
            )
    ax.set_xlabel("Derived structure (GB)")
    ax.set_ylabel("QPS")
    ax.legend(frameon=False, loc="lower right")

    ax = axes[0, 1]
    panel(ax, "(b)", "Measured storage amplification")
    for layout in ("legacy", "fixed", "variable"):
        selected = sorted(
            (row for row in resource if row["layout"] == layout),
            key=lambda row: f(row, "memory_nodes"),
        )
        color, marker, label = layout_style[layout]
        ax.plot(
            [f(row, "memory_nodes") for row in selected],
            [f(row, "storage_amplification_mean") for row in selected],
            color=color,
            marker=marker,
            linewidth=1.45,
            markersize=4.0,
            label=label,
        )
    ax.axhline(1.0, color=MUTED, linestyle=":", linewidth=0.8)
    ax.set_xticks([1, 3, 5])
    ax.set_xlabel("Passive memory nodes")
    ax.set_ylabel("Total MN bytes / HNSW")
    ax.legend(frameon=False, loc="upper left", handlelength=1.5)

    ax = axes[0, 2]
    panel(ax, "(c)", "Query throughput")
    for layout in ("legacy", "fixed", "variable"):
        selected = sorted(
            (row for row in resource if row["layout"] == layout),
            key=lambda row: f(row, "memory_nodes"),
        )
        color, marker, label = layout_style[layout]
        ax.errorbar(
            [f(row, "memory_nodes") for row in selected],
            [f(row, "qps_mean") for row in selected],
            yerr=[f(row, "qps_ci95") for row in selected],
            color=color,
            marker=marker,
            linewidth=1.35,
            markersize=4.0,
            capsize=2.0,
            label=label,
        )
    ax.set_xticks([1, 3, 5])
    ax.set_xlabel("Passive memory nodes")
    ax.set_ylabel("QPS")
    ax.legend(frameon=False, loc="lower right", handlelength=1.5)

    ax = axes[1, 0]
    panel(ax, "(d)", "Registered vs. materialized")
    selected = [next(row for row in resource if row["layout"] == layout and row["memory_nodes"] == "5")
                for layout in ("legacy", "fixed", "variable")]
    xpos = np.arange(3)
    materialized = np.array([f(row, "materialized_sidecar_bytes_mean") / 2**30 for row in selected])
    registered = np.array([
        materialized[index] / f(row, "registered_utilization_mean")
        for index, row in enumerate(selected)
    ])
    ax.bar(xpos - 0.18, registered, 0.36, color=LIGHT_GREY, edgecolor=INK,
           linewidth=0.6, label="registered")
    ax.bar(xpos + 0.18, materialized, 0.36,
           color=[layout_style[layout][0] for layout in ("legacy", "fixed", "variable")],
           edgecolor=INK, linewidth=0.6, label="materialized")
    ax.set_xticks(xpos)
    ax.set_xticklabels(["legacy", "fixed", "variable"])
    ax.set_ylabel("Aggregate GiB, S=5")
    ax.legend(frameon=False, loc="upper right", ncol=2, columnspacing=0.8, handlelength=1.2)

    ax = axes[1, 1]
    panel(ax, "(e)", "Resident navigation")
    off = sorted((r for r in resident if r["panel"] == "ef_sweep" and r["mode"] == "Slabs"), key=lambda r: f(r, "ef"))
    on = sorted((r for r in resident if r["panel"] == "ef_sweep" and r["mode"] == "+upper graph"), key=lambda r: f(r, "ef"))
    ef = [f(r, "ef") for r in off]
    ax.plot(ef, [f(r, "qps") for r in off], color=MUTED, marker="o", linewidth=1.4, label="remote descent")
    ax.plot(ef, [f(r, "qps") for r in on], color=GREEN, marker="s", linewidth=1.5, label="resident upper graph")
    ax.fill_between(ef, [f(r, "qps") for r in off], [f(r, "qps") for r in on], color=LIGHT_GREEN, alpha=0.7)
    ax.set_xlabel("ef search")
    ax.set_ylabel("QPS")
    ax.text(0.98, 0.96, "32.0 MB/CN\n0 upper posts/q", transform=ax.transAxes, ha="right", va="top", color=GREEN, fontsize=6.4)
    ax.legend(frameon=False, loc="lower left")

    ax = axes[1, 2]
    panel(ax, "(f)", "Block-cyclic balance")
    selected = sorted(
        (row for row in resource if row["layout"] == "variable"),
        key=lambda row: f(row, "memory_nodes"),
    )
    mn = np.array([f(row, "memory_nodes") for row in selected])
    rss = np.array([f(row, "mn_peak_rss_max_kib_mean") / 2**20 for row in selected])
    gini = np.array([f(row, "read_bytes_gini_mean") * 100 for row in selected])
    ax.plot(mn, rss, color=TEAL, marker="o", markersize=4.5, linewidth=1.5, label="max MN RSS")
    ax.set_xticks(mn)
    ax.set_xlabel("Passive memory nodes")
    ax.set_ylabel("Max MN RSS (GiB)", color=TEAL)
    ax.tick_params(axis="y", labelcolor=TEAL)
    ax2 = ax.twinx()
    ax2.bar(mn, gini, width=0.48, color=LIGHT_PURPLE, edgecolor=PURPLE,
            linewidth=0.7, alpha=0.85)
    ax2.set_zorder(1)
    ax.set_zorder(2)
    ax.patch.set_visible(False)
    ax2.set_ylabel("Read-byte Gini (%)", color=PURPLE)
    ax2.tick_params(axis="y", labelcolor=PURPLE)
    ax.text(0.50, 0.94, "owner(u) = u mod S", transform=ax.transAxes,
            color=MUTED, fontsize=6.2, va="top", ha="center")

    fig.subplots_adjust(left=0.07, right=0.955, top=0.94, bottom=0.12,
                        hspace=0.43, wspace=0.39)
    save(fig, "eval_index_cost")


def build_access_scaling() -> None:
    tau = rows(RDMA_TAU)
    tail = rows(TAIL)
    workers = rows(WORKERS)
    dh_workers = rows(DHNSW_WORKERS)
    ceiling = rows(CEILING)
    deep_scale = rows(DEEP_SCALE)

    fig, axes = plt.subplots(2, 3, figsize=(7.08, 4.28))

    ax = axes[0, 0]
    panel(ax, "(a)", "One READ: payload sweep")
    payload_groups: dict[int, list[dict[str, str]]] = {}
    for row in tau:
        if row["sweep"] == "payload_latency":
            payload_groups.setdefault(int(row["size"]), []).append(row)
    payload_x = sorted(payload_groups)
    payload_avg = [
        statistics.median(f(row, "avg_us") for row in payload_groups[size])
        for size in payload_x
    ]
    payload_p99 = [
        statistics.median(f(row, "p99_us") for row in payload_groups[size])
        for size in payload_x
    ]
    ax.plot(payload_x, payload_avg, color=BLUE, marker="s", linewidth=1.35,
            markersize=3.5, label="mean")
    ax.plot(payload_x, payload_p99, color=RED, marker="o", linestyle="--",
            linewidth=1.15, markersize=3.2, label="P99")
    ax.axvspan(64, 4096, color=LIGHT_BLUE, alpha=0.62, zorder=0)
    ax.set_xscale("log", base=2)
    ax.set_xticks([64, 256, 1024, 4096, 16384])
    ax.set_xticklabels(["64", "256", "1K", "4K", "16K"])
    ax.set_xlabel("READ payload (bytes)")
    ax.set_ylabel("Latency (us)")
    ax.legend(frameon=False, loc="upper left", ncol=2, columnspacing=0.7,
              handlelength=1.2)

    ax = axes[0, 1]
    panel(ax, "(b)", "P99 latency and access reduction")
    datasets = ["SIFT", "DEEP", "GIST"]
    x = np.arange(len(datasets))
    width = 0.35
    base = [next(f(r, "latency_p99_ms") for r in tail if r["dataset"] == name and r["system"] == "Baseline") for name in datasets]
    slab = [next(f(r, "latency_p99_ms") for r in tail if r["dataset"] == name and r["system"] == "SlabWalk") for name in datasets]
    base_posts = [next(f(r, "posts_p99") for r in tail if r["dataset"] == name and r["system"] == "Baseline") for name in datasets]
    slab_posts = [next(f(r, "posts_p99") for r in tail if r["dataset"] == name and r["system"] == "SlabWalk") for name in datasets]
    ax.bar(x - width / 2, base, width, color=LIGHT_GREY, edgecolor=MUTED, linewidth=0.7, label="node/vector")
    ax.bar(x + width / 2, slab, width, color=LIGHT_BLUE, edgecolor=BLUE, linewidth=0.7, label="SlabWalk")
    ax.set_yscale("log")
    ax.set_ylim(0.3, 100)
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylabel("P99 latency (ms, log)")
    for xpos, bp, sp, y in zip(x, base_posts, slab_posts, base):
        ax.text(xpos, y * 1.20, f"posts -{(1-sp/bp)*100:.0f}%", ha="center", fontsize=6.0, color=BLUE)
    ax.legend(frameon=False, loc="upper left")

    ax = axes[0, 2]
    panel(ax, "(c)", "DEEP worker scaling")
    series = [
        ("Baseline", "node/vector", MUTED, "o"),
        ("Slabs+upper graph", "SlabWalk", BLUE, "s"),
    ]
    for system, label, color, marker in series:
        selected = sorted((r for r in workers if r["dataset"] == "DEEP" and r["system"] == system), key=lambda r: f(r, "threads"))
        one = f(selected[0], "qps")
        ax.plot([f(r, "threads") for r in selected], [f(r, "qps") / one for r in selected], color=color, marker=marker, linewidth=1.5, markersize=4.0, label=label)
    grouped: dict[int, list[float]] = {}
    for row in dh_workers:
        grouped.setdefault(int(row["threads"]), []).append(f(row, "qps"))
    dh_x = sorted(grouped)
    dh_mean = np.array([np.mean(grouped[t]) for t in dh_x])
    dh_sd = np.array([np.std(grouped[t], ddof=1) for t in dh_x])
    dh_norm = dh_mean / dh_mean[0]
    dh_ci = 2.776 * dh_sd / np.sqrt(5) / dh_mean[0]
    ax.errorbar(dh_x, dh_norm, yerr=dh_ci, color=ORANGE, marker="^", linewidth=1.4, markersize=4.2, capsize=2.0, label="d-HNSW")
    ax.set_xlabel("Workers")
    ax.set_ylabel("QPS / one-worker QPS")
    ax.set_xticks([1, 8, 16, 40, 80])
    ax.legend(frameon=False, loc="upper left")
    ax.text(0.98, 0.02, "own fixed operating point", transform=ax.transAxes, ha="right", va="bottom", fontsize=5.9, color=MUTED)

    ax = axes[1, 0]
    panel(ax, "(d)", "QP/CQ message rate")
    qp_groups: dict[tuple[int, int], list[dict[str, str]]] = {}
    for row in tau:
        if row["sweep"] == "qp_cq_msg_rate":
            qp_groups.setdefault(
                (int(row["qps"]), int(row["cq_mod"])), []
            ).append(row)
    for cq_mod, color, marker, linestyle in (
        (1, BLUE, "s", "-"),
        (16, PURPLE, "o", "--"),
    ):
        qp_x = sorted(qps for qps, cq in qp_groups if cq == cq_mod)
        qp_y = [
            statistics.median(
                f(row, "msg_rate_mpps") for row in qp_groups[(qps, cq_mod)]
            )
            for qps in qp_x
        ]
        ax.plot(qp_x, qp_y, color=color, marker=marker, linestyle=linestyle,
                linewidth=1.35, markersize=3.5, label=f"CQ mod {cq_mod}")
    ax.axhspan(3.5, 4.0, color=LIGHT_GREEN, alpha=0.65, zorder=0)
    ax.set_xticks([1, 2, 4, 8])
    ax.set_xlabel("Queue pairs")
    ax.set_ylabel("256B READs (Mops/s)")
    ax.legend(frameon=False, loc="lower right", handlelength=1.3)

    ax = axes[1, 1]
    panel(ax, "(e)", "Operation-rate ceiling")
    l2 = [r for r in ceiling if r["metric"] == "L2"]
    colors = {"DEEP": GREEN, "BIGANN": BLUE, "SPACEV": PURPLE, "TURING": ORANGE}
    markers = {"1M": "o", "3M": "s", "10M": "^"}
    for row in l2:
        ax.scatter(f(row, "omega"), f(row, "theta_mops"), color=colors[row["dataset"]], marker=markers.get(row["scale"], "o"), s=30, edgecolor=WHITE, linewidth=0.5, zorder=3)
        ax.annotate(f"{row['dataset']} {row['scale']}", (f(row, "omega"), f(row, "theta_mops")), textcoords="offset points", xytext=(4, 3), fontsize=5.8, color=colors[row["dataset"]])
    ax.axhspan(3.5, 4.0, color=LIGHT_GREEN, alpha=0.75, zorder=0)
    ax.set_xlabel("Remote accesses / query (Omega)")
    ax.set_ylabel("Omega x QPS (Mops/s)")
    ax.set_ylim(3.35, 4.12)
    ax.set_xscale("log")

    ax = axes[1, 2]
    panel(ax, "(f)", "DEEP scale transfer")
    selected = sorted(deep_scale, key=lambda r: f(r, "n_million"))
    n = [f(r, "n_million") for r in selected]
    measured = [f(r, "measured_qps") for r in selected]
    predicted = [f(r, "predicted_qps") for r in selected]
    ax.plot(n, measured, color=BLUE, marker="o", linewidth=1.6, label="measured")
    ax.plot(n, predicted, color=MUTED, marker="s", linestyle="--", linewidth=1.3, label="rate transfer")
    for row in selected:
        ax.annotate(f"Omega={int(f(row, 'omega'))}", (f(row, "n_million"), f(row, "measured_qps")), textcoords="offset points", xytext=(3, 4), fontsize=5.8, color=BLUE)
    ax.set_xlabel("Vectors (million)")
    ax.set_ylabel("QPS")
    ax.set_xticks(n)
    ax.set_ylim(11000, 32500)
    ax.legend(frameon=False, loc="upper right")

    fig.subplots_adjust(left=0.065, right=0.99, top=0.94, bottom=0.13,
                        hspace=0.43, wspace=0.40)
    save(fig, "eval_access_scaling")


def build_lifecycle_boundaries() -> None:
    summaries = rows(BUILD_SUMMARY)
    stages = rows(BUILD_STAGES)
    resource_summary = next(
        row for row in rows(RESOURCE_SUMMARY)
        if row["layout"] == "fixed" and row["memory_nodes"] == "1"
    )
    resource_runs = [
        row for row in rows(RESOURCE_RUNS)
        if row["layout"] == "fixed" and row["memory_nodes"] == "1"
    ]
    gist_summary = next(row for row in summaries if row["dataset"] == "GIST1M")
    gist_summary.update(
        {
            "build_mean_s": str(f(resource_summary, "lavd_build_ms_mean") / 1000.0),
            "build_ci95_half_s": str(f(resource_summary, "lavd_build_ms_ci95") / 1000.0),
            "build_peak_rss_mean_gib": str(f(resource_summary, "cn_peak_rss_kib_mean") / 2**20),
            "region_gb": str(f(resource_summary, "materialized_sidecar_bytes_mean") / 1e9),
        }
    )
    stage_columns = {
        "fetch": "lavd_build_fetch_ms",
        "parse": "lavd_build_parse_ms",
        "rank": "lavd_build_rank_ms",
        "encode": "lavd_build_encode_ms",
        "metadata": "lavd_build_metadata_ms",
        "materialize": "lavd_build_materialize_ms",
    }
    stages = [row for row in stages if row["dataset"] != "GIST1M"]
    for stage, column in stage_columns.items():
        shares = [100.0 * f(row, column) / f(row, "lavd_build_ms") for row in resource_runs]
        stages.append(
            {
                "dataset": "GIST1M",
                "stage": stage,
                "median_share_pct": str(statistics.median(shares)),
            }
        )
    refresh = rows(REFRESH)
    plateau = rows(DHNSW_PLATEAU)
    tti = [r for r in rows(TTI) if int(r["threads"]) == 1 and "upper graph" not in r["config"]]

    fig, axes = plt.subplots(2, 3, figsize=(7.08, 4.55))
    datasets = [r["dataset"] for r in summaries]
    x = np.arange(len(datasets))

    ax = axes[0, 0]
    panel(ax, "(a)", "Slab construction time")
    mean = [f(r, "build_mean_s") for r in summaries]
    ci = [f(r, "build_ci95_half_s") for r in summaries]
    bars = ax.bar(x, mean, yerr=ci, capsize=2.2, color=[BLUE, GREEN, ORANGE], edgecolor=INK, linewidth=0.65)
    ax.set_yscale("log")
    ax.set_ylim(max(1.0, min(mean) * 0.55), max(mean) * 1.6)
    ax.set_xticks(x)
    ax.set_xticklabels([name.replace("1M", "") for name in datasets])
    ax.set_ylabel("Seconds (mean; 95% CI, log)")
    for bar, value in zip(bars, mean):
        ax.text(bar.get_x() + bar.get_width() / 2, value * 1.12, f"{value:.1f}", ha="center", fontsize=6.2)

    ax = axes[0, 1]
    panel(ax, "(b)", "Build-stage composition")
    stage_groups = [
        ("scan/register", ["fetch", "parse", "rank"], BLUE),
        ("encode", ["encode"], GREEN),
        ("metadata", ["metadata"], MUTED),
        ("materialize", ["materialize"], PURPLE),
    ]
    left = np.zeros(len(datasets))
    for label, members, color in stage_groups:
        values = []
        for dataset in datasets:
            values.append(
                sum(
                    f(next(r for r in stages if r["dataset"] == dataset and r["stage"] == stage), "median_share_pct")
                    for stage in members
                )
            )
        ax.barh(x, values, left=left, color=color, edgecolor=WHITE, linewidth=0.4, label=label)
        left += np.array(values)
    ax.set_yticks(x)
    ax.set_yticklabels([name.replace("1M", "") for name in datasets])
    ax.set_xlabel("Share of Slab build time (%)")
    ax.set_xlim(0, 100)
    ax.legend(frameon=False, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.21), columnspacing=0.8, handlelength=1.0)

    ax = axes[0, 2]
    panel(ax, "(c)", "Builder DRAM and final bytes")
    width = 0.36
    rss = [f(r, "build_peak_rss_mean_gib") for r in summaries]
    region = [f(r, "region_gb") / 1.073741824 for r in summaries]
    ax.bar(x - width / 2, rss, width, color=LIGHT_BLUE, edgecolor=BLUE, linewidth=0.7, label="builder RSS")
    ax.bar(x + width / 2, region, width, color=LIGHT_PURPLE, edgecolor=PURPLE, linewidth=0.7, label="Slab bytes")
    ax.set_xticks(x)
    ax.set_xticklabels([name.replace("1M", "") for name in datasets])
    ax.set_ylabel("GiB")
    ax.legend(frameon=False, loc="upper left")

    ax = axes[1, 0]
    panel(ax, "(d)", "Offline replay control")
    inserts = np.array([f(r, "batch_inserts") for r in refresh])
    write_amp = np.array([f(r, "write_amp_blocks_per_insert") for r in refresh])
    ax.plot(inserts / 1000.0, write_amp, color=GREEN, marker="o", linewidth=1.5, label="selected/node")
    ax.set_xlabel("Replayed suffix (K)")
    ax.set_ylabel("Selected Slabs / node", color=GREEN)
    ax.tick_params(axis="y", labelcolor=GREEN)
    ax2 = ax.twinx()
    frac = np.array([f(r, "diff_read_frac") for r in refresh]) * 100.0
    mask = np.isfinite(frac)
    ax2.plot(inserts[mask] / 1000.0, frac[mask], color=ORANGE, marker="s", linewidth=1.35, label="read fraction")
    ax2.set_ylabel("Repeat-control read (%)", color=ORANGE)
    ax2.tick_params(axis="y", labelcolor=ORANGE)
    ax.text(0.04, 0.08, "full-region mismatches: 0", transform=ax.transAxes, color=MUTED, fontsize=6.2)

    ax = axes[1, 1]
    panel(ax, "(e)", "Routed-partition coverage")
    for dataset, color, marker in (("SIFT", BLUE, "o"), ("GIST", ORANGE, "s")):
        selected = sorted((r for r in plateau if r["dataset"] == dataset), key=lambda r: f(r, "ef"))
        ef = [f(r, "ef") for r in selected]
        dh = [f(r, "dhnsw_recall") for r in selected]
        sw = f(selected[0], "slabwalk_recall")
        ax.plot(ef, dh, color=color, marker=marker, linewidth=1.4, label=f"d-HNSW {dataset}")
        ax.axhline(sw, color=color, linestyle="--", linewidth=1.0, alpha=0.75)
    ax.set_xlabel("d-HNSW sub-search ef")
    ax.set_ylabel("Recall@10")
    ax.set_ylim(0.70, 0.96)
    ax.legend(frameon=False, loc="lower right")
    ax.text(0.04, 0.93, "dashed: SlabWalk endpoint", transform=ax.transAxes, fontsize=5.9, color=MUTED, va="top")

    ax = axes[1, 2]
    panel(ax, "(f)", "TTI compact-code boundary")
    labels = {
        "fp32 baseline": "fp32",
        "sq8 Slabs": "sq8",
        "RaBitQ-2 Slabs": "RQ-2",
        "RaBitQ-4 Slabs": "RQ-4",
    }
    colors = {"fp32": INK, "sq8": BLUE, "RQ-2": GREEN, "RQ-4": PURPLE}
    for row in tti:
        label = labels.get(row["config"])
        if not label:
            continue
        qps = f(row, "qps")
        ax.scatter(f(row, "mb_per_query"), f(row, "recall"), s=26 + qps * 0.035, color=colors[label], edgecolor=WHITE, linewidth=0.6, zorder=3)
        offset = (-18, 3) if label == "fp32" else (4, 3)
        ax.annotate(label, (f(row, "mb_per_query"), f(row, "recall")), textcoords="offset points", xytext=offset, fontsize=6.1, color=colors[label])
    ax.set_xlabel("Transferred MB/query")
    ax.set_ylabel("Recall@10")
    ax.set_ylim(0.79, 0.98)
    ax.text(0.04, 0.08, "marker area encodes QPS", transform=ax.transAxes, fontsize=5.9, color=MUTED)

    fig.subplots_adjust(left=0.07, right=0.95, top=0.94, bottom=0.13, hspace=0.62, wspace=0.45)
    save(fig, "eval_lifecycle_boundaries")


def main() -> None:
    style()
    build_access_scaling()
    build_index_cost()
    build_lifecycle_boundaries()
    print("wrote eval_access_scaling, eval_index_cost, and eval_lifecycle_boundaries")


if __name__ == "__main__":
    main()
