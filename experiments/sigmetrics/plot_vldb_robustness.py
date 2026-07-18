#!/usr/bin/env python3
"""Plot validated RDMA model controls and full-query robustness controls."""

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

from publication_metadata import pdf_metadata

try:
    from . import summarize_vldb_robustness as robustness_summary
    from . import validate_vldb_final_evidence as evidence
except ImportError:
    import summarize_vldb_robustness as robustness_summary
    import validate_vldb_final_evidence as evidence


INK = "#263442"
GRID = "#C8CED6"
GREEN = "#009E73"
BLUE = "#0072B2"
ORANGE = "#D55E00"
PURPLE = "#7B61A8"
WORKER_STYLES = {
    "SHINE": ("SHINE-derived", BLUE, "s", "white"),
    "d-HNSW": ("d-HNSW", ORANGE, "^", "white"),
    "SlabWalk": ("SlabWalk", GREEN, "o", GREEN),
}


def load_validated(
    runs: Path,
    worker_runs: Path,
    rdma_runs: Path,
    topology_runs: Path,
    colocation_runs: Path,
    gate: Path,
) -> tuple[
    list[dict[str, str]],
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
    expected_runs_sha = str(gate_obj.get("robustness", {}).get("runs_sha256", ""))
    expected_worker_sha = str(
        gate_obj.get("worker_scaling", {}).get("runs_sha256", "")
    )
    expected_rdma_sha = str(
        gate_obj.get("model_controls", {}).get("runs_sha256", "")
    )
    expected_topology_sha = str(
        gate_obj.get("topology_control", {}).get("runs_sha256", "")
    )
    expected_colocation_sha = str(
        gate_obj.get("colocation_control", {}).get("runs_sha256", "")
    )
    if not runs.is_file():
        raise ValueError(f"missing robustness runs: {runs}")
    actual_runs_sha = hashlib.sha256(runs.read_bytes()).hexdigest()
    if actual_runs_sha != expected_runs_sha:
        raise ValueError(
            f"robustness runs SHA {actual_runs_sha} does not match gate "
            f"{expected_runs_sha!r}"
        )
    evidence.validate_robustness(runs.parent, expected_sha)
    if not worker_runs.is_file():
        raise ValueError(f"missing worker-scaling runs: {worker_runs}")
    actual_worker_sha = hashlib.sha256(worker_runs.read_bytes()).hexdigest()
    if actual_worker_sha != expected_worker_sha:
        raise ValueError(
            f"worker-scaling runs SHA {actual_worker_sha} does not match gate "
            f"{expected_worker_sha!r}"
        )
    evidence.validate_worker_scaling(worker_runs.parent, expected_sha)
    if not rdma_runs.is_file():
        raise ValueError(f"missing model-control runs: {rdma_runs}")
    actual_rdma_sha = hashlib.sha256(rdma_runs.read_bytes()).hexdigest()
    if actual_rdma_sha != expected_rdma_sha:
        raise ValueError(
            f"model-control runs SHA {actual_rdma_sha} does not match gate "
            f"{expected_rdma_sha!r}"
        )
    evidence.validate_model_controls(rdma_runs.parent)
    if not topology_runs.is_file():
        raise ValueError(f"missing topology-control runs: {topology_runs}")
    actual_topology_sha = hashlib.sha256(topology_runs.read_bytes()).hexdigest()
    if actual_topology_sha != expected_topology_sha:
        raise ValueError(
            f"topology-control runs SHA {actual_topology_sha} does not match gate "
            f"{expected_topology_sha!r}"
        )
    evidence.validate_topology_control(topology_runs.parent)
    if not colocation_runs.is_file():
        raise ValueError(f"missing co-location runs: {colocation_runs}")
    actual_colocation_sha = hashlib.sha256(colocation_runs.read_bytes()).hexdigest()
    if actual_colocation_sha != expected_colocation_sha:
        raise ValueError(
            f"co-location runs SHA {actual_colocation_sha} does not match gate "
            f"{expected_colocation_sha!r}"
        )
    evidence.validate_colocation_control(
        colocation_runs.parent.parent, expected_sha
    )
    with runs.open(newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("run_kind") == "measure"]
    with worker_runs.open(newline="") as handle:
        worker_rows = list(csv.DictReader(handle))
    with rdma_runs.open(newline="") as handle:
        model_rows = list(csv.DictReader(handle))
    with topology_runs.open(newline="") as handle:
        topology_rows = list(csv.DictReader(handle))
    with colocation_runs.open(newline="") as handle:
        colocation_rows = list(csv.DictReader(handle))
    return rows, worker_rows, model_rows, topology_rows, colocation_rows


def metric_stats(
    rows: list[dict[str, str]], factor: str, values: list[str], metric: str
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for value in values:
        samples = [
            float(row[metric])
            for row in rows
            if row["factor"] == factor and row["value"] == value and row.get(metric, "") != ""
        ]
        if len(samples) != 5 or any(not math.isfinite(sample) for sample in samples):
            raise ValueError(f"{factor}={value}: expected five finite {metric} samples")
        means.append(statistics.mean(samples))
        intervals.append(robustness_summary.t_ci_half(samples))
    return means, intervals


def worker_metric_stats(
    rows: list[dict[str, str]], method: str, workers: list[int], metric: str
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for worker_count in workers:
        samples = [
            float(row[metric])
            for row in rows
            if row["method"] == method and int(row["workers"]) == worker_count
        ]
        if len(samples) != 5 or any(not math.isfinite(sample) for sample in samples):
            raise ValueError(
                f"{method}/workers={worker_count}: expected five finite {metric} samples"
            )
        means.append(statistics.mean(samples))
        intervals.append(robustness_summary.t_ci_half(samples))
    return means, intervals


def rdma_metric_stats(
    rows: list[dict[str, str]],
    sweep: str,
    x_key: str,
    values: list[int],
    metric: str,
    **fixed: int,
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for value in values:
        samples = [
            float(row[metric])
            for row in rows
            if row["sweep"] == sweep
            and int(row[x_key]) == value
            and all(int(row[key]) == wanted for key, wanted in fixed.items())
        ]
        if len(samples) != 5 or any(not math.isfinite(sample) for sample in samples):
            raise ValueError(
                f"{sweep}/{x_key}={value}: expected five finite {metric} samples"
            )
        means.append(statistics.mean(samples))
        intervals.append(robustness_summary.t_ci_half(samples))
    return means, intervals


def topology_metric_stats(
    rows: list[dict[str, str]], topologies: list[str], metric: str
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for topology in topologies:
        samples = [
            float(row[metric]) for row in rows if row["topology"] == topology
        ]
        if len(samples) != 5 or any(not math.isfinite(sample) for sample in samples):
            raise ValueError(
                f"topology={topology}: expected five finite {metric} samples"
            )
        means.append(statistics.mean(samples))
        intervals.append(robustness_summary.t_ci_half(samples))
    return means, intervals


def colocation_metric_stats(
    rows: list[dict[str, str]], degrees: list[str], metric: str
) -> tuple[list[float], list[float]]:
    means = []
    intervals = []
    for degree in degrees:
        samples = [float(row[metric]) for row in rows if row["degree"] == degree]
        if len(samples) != 5 or any(not math.isfinite(sample) for sample in samples):
            raise ValueError(
                f"co-location degree={degree}: expected five finite {metric} samples"
            )
        means.append(statistics.mean(samples))
        intervals.append(robustness_summary.t_ci_half(samples))
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
        "legend.fontsize": 6.4,
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


def line_with_ci(
    ax: plt.Axes,
    x: list[float] | np.ndarray,
    mean: list[float],
    ci: list[float],
    color: str,
    marker: str,
    face: str | None = None,
) -> plt.Artist:
    return ax.errorbar(
        x,
        mean,
        yerr=ci,
        color=color,
        marker=marker,
        markerfacecolor=color if face is None else face,
        markeredgecolor=INK,
        markeredgewidth=0.45,
        markersize=4.0,
        linewidth=1.25,
        elinewidth=0.65,
        capsize=1.8,
    )


def generate(
    runs: Path,
    worker_runs: Path,
    rdma_runs: Path,
    topology_runs: Path,
    colocation_runs: Path,
    gate: Path,
    out: Path,
) -> None:
    rows, worker_rows, model_rows, topology_rows, colocation_rows = load_validated(
        runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate
    )
    set_style()
    fig, axes = plt.subplots(2, 3, figsize=(7.08, 3.32))

    payload_sizes = [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384]
    payload_avg, payload_avg_ci = rdma_metric_stats(
        model_rows, "payload_latency", "size", payload_sizes, "avg_us"
    )
    payload_p99, payload_p99_ci = rdma_metric_stats(
        model_rows, "payload_latency", "size", payload_sizes, "p99_us"
    )
    ax = axes[0, 0]
    prepare_axis(ax)
    average = line_with_ci(
        ax, payload_sizes, payload_avg, payload_avg_ci, BLUE, "o"
    )
    tail = line_with_ci(
        ax, payload_sizes, payload_p99, payload_p99_ci, ORANGE, "s"
    )
    ax.axvspan(64, 4096, color="#D8EAF5", alpha=0.45, linewidth=0)
    ax.set_xscale("log", base=2)
    ax.set_xticks([64, 256, 1024, 4096, 16384])
    ax.set_xticklabels(["64", "256", "1K", "4K", "16K"])
    ax.set_xlabel("READ payload (B)")
    ax.set_ylabel("Latency (us)")
    ax.legend([average, tail], ["Mean", "P99"], frameon=False, loc="upper left")
    panel_label(ax, "(a) One-READ cost")

    qp_values = [1, 2, 4, 8]
    ax = axes[0, 1]
    prepare_axis(ax)
    qp_handles = []
    for cq_mod, color, marker in ((1, GREEN, "o"), (16, PURPLE, "s")):
        rates, rate_ci = rdma_metric_stats(
            model_rows,
            "qp_cq_msg_rate",
            "qps",
            qp_values,
            "msg_rate_mpps",
            cq_mod=cq_mod,
        )
        qp_handles.append(
            line_with_ci(ax, qp_values, rates, rate_ci, color, marker)
        )
    ax.set_xticks(qp_values)
    ax.set_xlabel("Queue pairs")
    ax.set_ylabel("RDMA READs (Mops/s)")
    ax.set_ylim(bottom=0)
    ax.legend(qp_handles, ["CQ mod 1", "CQ mod 16"], frameon=False, loc="lower right")
    panel_label(ax, "(b) Queue organization")

    worker_x = list(evidence.WORKER_SCALING_WORKERS)
    ax = axes[0, 2]
    prepare_axis(ax)
    worker_handles = []
    worker_labels = []
    for method in evidence.WORKER_SCALING_METHODS:
        label, color, marker, face = WORKER_STYLES[method]
        qps, qps_ci = worker_metric_stats(worker_rows, method, worker_x, "qps")
        recalls, _ = worker_metric_stats(worker_rows, method, worker_x, "recall")
        worker_handles.append(
            line_with_ci(
                ax,
                worker_x,
                [value / 1000 for value in qps],
                [value / 1000 for value in qps_ci],
                color,
                marker,
                face,
            )
        )
        worker_labels.append(f"{label} (R={statistics.mean(recalls):.3f})")
    ax.set_xticks(worker_x)
    ax.set_xlabel("Workers")
    ax.set_ylabel("Throughput (kQPS)")
    ax.set_ylim(bottom=0)
    ax.legend(
        worker_handles,
        worker_labels,
        frameon=False,
        loc="upper left",
        handlelength=1.5,
        fontsize=5.7,
    )
    panel_label(ax, "(c) Workers at ef=200")

    coroutine_values = ["1", "2", "4", "8", "16"]
    coroutine_x = [int(value) for value in coroutine_values]
    coro_qps, coro_qps_ci = metric_stats(rows, "coroutines", coroutine_values, "qps")
    coro_qps = [value / 1000 for value in coro_qps]
    coro_qps_ci = [value / 1000 for value in coro_qps_ci]
    coro_p99, coro_p99_ci = metric_stats(rows, "coroutines", coroutine_values, "p99_us")
    ax = axes[1, 0]
    prepare_axis(ax)
    throughput = line_with_ci(ax, coroutine_x, coro_qps, coro_qps_ci, BLUE, "^")
    ax.set_xticks(coroutine_x)
    ax.set_xlabel("Coroutines/worker")
    ax.set_ylabel("kQPS", color=BLUE)
    ax.tick_params(axis="y", labelcolor=BLUE)
    ax2 = ax.twinx()
    tail = ax2.errorbar(
        coroutine_x,
        coro_p99,
        yerr=coro_p99_ci,
        color=ORANGE,
        marker="s",
        markersize=3.8,
        linewidth=1.2,
        capsize=1.7,
        label="P99",
    )
    ax2.set_ylabel("P99 (us)", color=ORANGE)
    ax2.tick_params(axis="y", labelcolor=ORANGE)
    ax2.set_yscale("log")
    ax.legend([throughput, tail], ["QPS", "P99"], frameon=False, loc="upper left")
    panel_label(ax, "(d) Coroutine depth")

    degree_values = ["1", "4", "8", "16", "24", "full"]
    degree_x = [1, 4, 8, 16, 24, 32]
    degree_qps, degree_qps_ci = colocation_metric_stats(
        colocation_rows, degree_values, "qps"
    )
    degree_qps = [value / 1000 for value in degree_qps]
    degree_qps_ci = [value / 1000 for value in degree_qps_ci]
    ax = axes[1, 1]
    prepare_axis(ax)
    throughput = line_with_ci(ax, degree_x, degree_qps, degree_qps_ci, GREEN, "o")
    ax.set_xticks(degree_x)
    ax.set_xticklabels(["1", "4", "8", "16", "24", "Full"])
    ax.set_xlabel("Inline codes / expansion")
    ax.set_ylabel("kQPS")
    posts, posts_ci = colocation_metric_stats(
        colocation_rows, degree_values, "posts_per_query"
    )
    byte_values, bytes_ci = colocation_metric_stats(
        colocation_rows, degree_values, "bytes_per_query"
    )
    full_posts = posts[-1]
    full_bytes = byte_values[-1]
    normalized_posts = [value / full_posts for value in posts]
    normalized_posts_ci = [value / full_posts for value in posts_ci]
    normalized_bytes = [value / full_bytes for value in byte_values]
    normalized_bytes_ci = [value / full_bytes for value in bytes_ci]
    ax2 = ax.twinx()
    post_handle = ax2.errorbar(
        [value - 0.35 for value in degree_x],
        normalized_posts,
        yerr=normalized_posts_ci,
        color=BLUE,
        marker="s",
        markerfacecolor="white",
        markersize=3.8,
        linewidth=1.2,
        capsize=1.7,
    )
    byte_handle = ax2.errorbar(
        [value + 0.35 for value in degree_x],
        normalized_bytes,
        yerr=normalized_bytes_ci,
        color=PURPLE,
        marker="^",
        markerfacecolor="white",
        markersize=3.8,
        linewidth=1.2,
        capsize=1.7,
    )
    ax2.set_ylabel("Access / full", color=BLUE)
    ax2.tick_params(axis="y", labelcolor=BLUE)
    ax.legend(
        [throughput, post_handle, byte_handle],
        ["QPS", "Posts", "Bytes"],
        frameon=False,
        loc="upper center",
        ncol=3,
        bbox_to_anchor=(0.5, 1.02),
        handlelength=1.2,
    )
    panel_label(ax, "(e) Expansion completeness")

    topologies = ["loopback", "remote"]
    topology_labels = ["Loopback", "Separated"]
    topology_x = np.arange(2)
    total_latency, total_latency_ci = topology_metric_stats(
        topology_rows, topologies, "latency_us"
    )
    network_latency, network_latency_ci = topology_metric_stats(
        topology_rows, topologies, "network_us"
    )
    topology_qps, topology_qps_ci = topology_metric_stats(
        topology_rows, topologies, "qps"
    )
    ax = axes[1, 2]
    prepare_axis(ax)
    width = 0.31
    total_bars = ax.bar(
        topology_x - width / 2,
        [value / 1000 for value in total_latency],
        yerr=[value / 1000 for value in total_latency_ci],
        width=width,
        color="#D7E8F4",
        edgecolor=BLUE,
        linewidth=0.75,
        capsize=2.0,
        label="Total latency",
    )
    network_bars = ax.bar(
        topology_x + width / 2,
        [value / 1000 for value in network_latency],
        yerr=[value / 1000 for value in network_latency_ci],
        width=width,
        color="#F6D7C8",
        edgecolor=ORANGE,
        linewidth=0.75,
        capsize=2.0,
        label="Network phase",
    )
    ax.set_xticks(topology_x)
    ax.set_xticklabels(topology_labels)
    ax.set_ylabel("Latency (ms)")
    ax.set_ylim(bottom=0)
    ax2 = ax.twinx()
    qps_handle = ax2.errorbar(
        topology_x,
        [value / 1000 for value in topology_qps],
        yerr=[value / 1000 for value in topology_qps_ci],
        color=GREEN,
        marker="o",
        markeredgecolor=INK,
        markeredgewidth=0.45,
        linewidth=1.25,
        markersize=4.0,
        capsize=1.8,
        label="QPS",
    )
    ax2.set_ylabel("Throughput (kQPS)", color=GREEN)
    ax2.tick_params(axis="y", labelcolor=GREEN)
    ax2.set_ylim(bottom=0)
    ax.legend(
        [total_bars, network_bars, qps_handle],
        ["Total", "Network", "QPS"],
        frameon=False,
        loc="upper center",
        ncol=3,
        fontsize=5.8,
        handlelength=1.2,
    )
    panel_label(ax, "(f) d-HNSW topology")

    fig.subplots_adjust(left=0.075, right=0.96, top=0.98, bottom=0.15, hspace=0.50, wspace=0.66)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out,
        bbox_inches="tight",
        pad_inches=0.015,
        metadata=pdf_metadata(),
    )
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--worker-runs", type=Path, required=True)
    parser.add_argument("--rdma-runs", type=Path, required=True)
    parser.add_argument("--topology-runs", type=Path, required=True)
    parser.add_argument("--colocation-runs", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate(
        args.runs,
        args.worker_runs,
        args.rdma_runs,
        args.topology_runs,
        args.colocation_runs,
        args.gate,
        args.out,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
