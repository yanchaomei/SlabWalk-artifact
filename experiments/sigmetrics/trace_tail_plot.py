#!/usr/bin/env python3
"""Generate supplemental tail CDF plots from SIGMETRICS query traces."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DEFAULT_TRACES = [
    (
        "SIFT",
        "Baseline",
        Path("results/sigmetrics_trace_20260706_quick_212028/sift1m_baseline_1t_ef48.trace.csv"),
    ),
    (
        "SIFT",
        "SlabWalk",
        Path("results/sigmetrics_trace_20260706_quick_212028/sift1m_slabwalk_1t_ef48.trace.csv"),
    ),
    (
        "DEEP",
        "Baseline",
        Path("results/sigmetrics_trace_20260706_deep1m_215346/deep1m_baseline_1t_ef80.trace.csv"),
    ),
    (
        "DEEP",
        "SlabWalk",
        Path("results/sigmetrics_trace_20260706_deep1m_215346/deep1m_slabwalk_1t_ef80.trace.csv"),
    ),
    (
        "GIST",
        "Baseline",
        Path("results/sigmetrics_trace_20260706_gist1m_cap36g_node3copy/gist1m_baseline_1t_ef400.trace.csv"),
    ),
    (
        "GIST",
        "SlabWalk",
        Path("results/sigmetrics_trace_20260706_gist1m_cap36g_node3copy/gist1m_slabwalk_1t_ef400.trace.csv"),
    ),
]

PERCENTILES = (50, 90, 95, 99, 99.9)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_trace(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    needed = {"latency_ns", "query_rdma_posts"}
    missing = needed - set(frame.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {sorted(missing)}")
    return frame


def cdf(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    xs = np.sort(values.astype(float))
    ys = np.arange(1, len(xs) + 1, dtype=float) / len(xs)
    return xs, ys


def write_summary(rows: list[dict[str, object]], path: Path) -> None:
    fieldnames = [
        "dataset",
        "system",
        "queries",
        "latency_p50_ms",
        "latency_p99_ms",
        "latency_p99_9_ms",
        "posts_mean",
        "posts_p99",
        "posts_p99_9",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("results/sigmetrics_trace_tail_cdf"),
        help="Directory for tail_cdf.{pdf,png} and tail_summary.csv.",
    )
    args = parser.parse_args()

    root = repo_root()
    out_dir = args.out_dir if args.out_dir.is_absolute() else root / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    loaded: dict[tuple[str, str], pd.DataFrame] = {}
    summary_rows: list[dict[str, object]] = []
    for dataset, system, rel_path in DEFAULT_TRACES:
        path = root / rel_path
        frame = load_trace(path)
        loaded[(dataset, system)] = frame

        lat_ms = frame["latency_ns"].to_numpy(dtype=float) / 1_000_000.0
        posts = frame["query_rdma_posts"].to_numpy(dtype=float)
        pct_lat = np.percentile(lat_ms, PERCENTILES)
        pct_posts = np.percentile(posts, PERCENTILES)
        summary_rows.append(
            {
                "dataset": dataset,
                "system": system,
                "queries": len(frame),
                "latency_p50_ms": f"{pct_lat[0]:.3f}",
                "latency_p99_ms": f"{pct_lat[3]:.3f}",
                "latency_p99_9_ms": f"{pct_lat[4]:.3f}",
                "posts_mean": f"{posts.mean():.1f}",
                "posts_p99": f"{pct_posts[3]:.1f}",
                "posts_p99_9": f"{pct_posts[4]:.1f}",
            }
        )

    write_summary(summary_rows, out_dir / "tail_summary.csv")

    datasets = ["SIFT", "DEEP", "GIST"]
    systems = [("Baseline", "-", "black"), ("SlabWalk", "--", "0.35")]
    fig, axes = plt.subplots(2, 3, figsize=(10.2, 4.8), sharey="row")

    for col, dataset in enumerate(datasets):
        for system, linestyle, color in systems:
            frame = loaded[(dataset, system)]
            posts = frame["query_rdma_posts"].to_numpy(dtype=float)
            latency_ms = frame["latency_ns"].to_numpy(dtype=float) / 1_000_000.0
            x_posts, y_posts = cdf(posts)
            x_lat, y_lat = cdf(latency_ms)
            axes[0, col].plot(x_posts, y_posts, linestyle=linestyle, color=color, linewidth=1.6, label=system)
            axes[1, col].plot(x_lat, y_lat, linestyle=linestyle, color=color, linewidth=1.6, label=system)

        for row in range(2):
            ax = axes[row, col]
            ax.set_xscale("log")
            ax.set_ylim(0.45, 1.005)
            ax.grid(True, which="both", linestyle=":", linewidth=0.5, color="0.75")
            ax.tick_params(labelsize=8)
            ax.text(
                0.03,
                0.08,
                dataset,
                transform=ax.transAxes,
                ha="left",
                va="bottom",
                fontsize=9,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.75, "pad": 1.5},
            )
        axes[0, col].set_xlabel("RDMA posts/query", fontsize=9)
        axes[1, col].set_xlabel("latency (ms)", fontsize=9)

    axes[0, 0].set_ylabel("CDF", fontsize=9)
    axes[1, 0].set_ylabel("CDF", fontsize=9)
    axes[0, 2].legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout(pad=0.5, w_pad=0.9, h_pad=0.8)

    fig.savefig(out_dir / "tail_cdf.pdf", bbox_inches="tight")
    fig.savefig(out_dir / "tail_cdf.png", bbox_inches="tight", dpi=240)
    print(f"wrote {out_dir / 'tail_cdf.pdf'}")
    print(f"wrote {out_dir / 'tail_cdf.png'}")
    print(f"wrote {out_dir / 'tail_summary.csv'}")


if __name__ == "__main__":
    main()
