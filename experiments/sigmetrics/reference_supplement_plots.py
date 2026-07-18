#!/usr/bin/env python3
"""Generate reference-facing supplemental plots for SIGMETRICS.

The main paper is already at the 20-page body limit, so this script collects
measured evidence that is useful for rebuttal, appendix, and slides but should
not be inserted into the main body unless page budget opens.
"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DHNSW_EF_SWEEP = [
    {"dataset": "SIFT", "ef": 48, "dhnsw_recall": 0.9238, "slabwalk_recall": 0.9323},
    {"dataset": "SIFT", "ef": 64, "dhnsw_recall": 0.9253, "slabwalk_recall": 0.9323},
    {"dataset": "SIFT", "ef": 96, "dhnsw_recall": 0.9274, "slabwalk_recall": 0.9323},
    {"dataset": "SIFT", "ef": 128, "dhnsw_recall": 0.9280, "slabwalk_recall": 0.9323},
    {"dataset": "SIFT", "ef": 200, "dhnsw_recall": 0.9283, "slabwalk_recall": 0.9323},
    {"dataset": "GIST", "ef": 48, "dhnsw_recall": 0.7725, "slabwalk_recall": 0.9257},
    {"dataset": "GIST", "ef": 64, "dhnsw_recall": 0.7754, "slabwalk_recall": 0.9257},
    {"dataset": "GIST", "ef": 96, "dhnsw_recall": 0.7814, "slabwalk_recall": 0.9257},
    {"dataset": "GIST", "ef": 128, "dhnsw_recall": 0.7934, "slabwalk_recall": 0.9257},
    {"dataset": "GIST", "ef": 200, "dhnsw_recall": 0.7934, "slabwalk_recall": 0.9257},
]


SHINE_SKEW = [
    {"dataset": "DEEP", "shine_uniform": 3014, "shine_zipf": 4457, "slab_uniform": 14467, "slab_zipf": 14528},
    {"dataset": "BIGANN", "shine_uniform": 2970, "shine_zipf": 3932, "slab_uniform": 14002, "slab_zipf": 15357},
    {"dataset": "SPACEV", "shine_uniform": 641, "shine_zipf": 801, "slab_uniform": 5477, "slab_zipf": 5642},
    {"dataset": "TURING", "shine_uniform": 249, "shine_zipf": 270, "slab_uniform": 2678, "slab_zipf": 2795},
    {"dataset": "TTI", "shine_uniform": 1045, "shine_zipf": 1442, "slab_uniform": 4034, "slab_zipf": 4590},
]


COVERAGE_ROWS = [
    {
        "axis": "RDMA operation-count wall",
        "main_evidence": "Fig. cntime, Fig. costmodel, Fig. omega-collapse, Fig. ceiling, Fig. trace-latency",
        "supplement": "tail CDF already in results/sigmetrics_trace_tail_cdf",
        "decision": "Covered in main body",
    },
    {
        "axis": "Memory/payload placement",
        "main_evidence": "Fig. budget-pareto, Table dhnsw footprint rows",
        "supplement": "No separate table",
        "decision": "Covered; extra table would duplicate Q2/Q4",
    },
    {
        "axis": "SHINE skew/cache behavior",
        "main_evidence": "Zipf(1.0) prose sentence in Q1",
        "supplement": "shine_skew_cache.{csv,pdf,png}",
        "decision": "Supplemented; keep out of main body unless page budget opens",
    },
    {
        "axis": "d-HNSW recall plateau",
        "main_evidence": "Table dhnsw caption and Q4 prose",
        "supplement": "dhnsw_recall_plateau.{csv,pdf,png}",
        "decision": "Supplemented; useful if reviewer asks for the ef sweep shape",
    },
    {
        "axis": "Dynamic insert/rebuild workloads",
        "main_evidence": "Implementation/discussion scope boundary",
        "supplement": "None",
        "decision": "Do not run unless the paper claims dynamic serving",
    },
    {
        "axis": "Multi-dataset 10M generality",
        "main_evidence": "DEEP 1M/3M/10M ceiling validation",
        "supplement": "None",
        "decision": "Do not run unless adding a broad 10M-generalization claim",
    },
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def style_axes(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color="0.75")
    ax.tick_params(labelsize=8)


def plot_dhnsw(df: pd.DataFrame, out_dir: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(6.5, 2.4), sharex=True)
    ylims = {"SIFT": (0.920, 0.935), "GIST": (0.75, 0.94)}
    for ax, dataset in zip(axes, ["SIFT", "GIST"]):
        sub = df[df["dataset"] == dataset]
        ax.plot(
            sub["ef"],
            sub["dhnsw_recall"],
            color="black",
            marker="o",
            linewidth=1.4,
            markersize=3.5,
            label="d-HNSW",
        )
        ax.plot(
            sub["ef"],
            sub["slabwalk_recall"],
            color="0.35",
            linestyle="--",
            linewidth=1.4,
            label="SlabWalk point",
        )
        ax.set_title(dataset, fontsize=9)
        ax.set_xlabel("d-HNSW sub-search ef", fontsize=8)
        ax.set_ylim(*ylims[dataset])
        style_axes(ax)
    axes[0].set_ylabel("recall@10", fontsize=8)
    axes[1].legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout(pad=0.4, w_pad=1.0)
    fig.savefig(out_dir / "dhnsw_recall_plateau.pdf", bbox_inches="tight")
    fig.savefig(out_dir / "dhnsw_recall_plateau.png", bbox_inches="tight", dpi=240)
    plt.close(fig)


def plot_shine_skew(df: pd.DataFrame, out_dir: Path) -> None:
    datasets = df["dataset"].tolist()
    x = np.arange(len(datasets))
    width = 0.34
    shine_uplift = df["shine_zipf"] / df["shine_uniform"]
    slab_uplift = df["slab_zipf"] / df["slab_uniform"]
    zipf_ratio = df["slab_zipf"] / df["shine_zipf"]

    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.5))
    ax = axes[0]
    ax.bar(x - width / 2, shine_uplift, width, color="0.80", edgecolor="black", hatch="///", label="SHINE-Cached")
    ax.bar(x + width / 2, slab_uplift, width, color="0.30", edgecolor="black", label="SlabWalk")
    ax.axhline(1.0, color="black", linewidth=0.7)
    ax.set_ylabel("Zipf / uniform QPS", fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=20, ha="right")
    ax.set_ylim(0, 1.65)
    ax.legend(frameon=False, fontsize=8, loc="upper right")
    style_axes(ax)

    ax = axes[1]
    bars = ax.bar(x, zipf_ratio, color="0.55", edgecolor="black")
    for bar, value in zip(bars, zipf_ratio):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 0.15,
            f"{value:.1f}x",
            ha="center",
            va="bottom",
            fontsize=7,
        )
    ax.set_ylabel("SlabWalk / SHINE-Cached (Zipf)", fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=20, ha="right")
    ax.set_ylim(0, max(zipf_ratio) * 1.25)
    style_axes(ax)

    fig.tight_layout(pad=0.45, w_pad=1.0)
    fig.savefig(out_dir / "shine_skew_cache.pdf", bbox_inches="tight")
    fig.savefig(out_dir / "shine_skew_cache.png", bbox_inches="tight", dpi=240)
    plt.close(fig)


def write_coverage(path: Path) -> None:
    lines = [
        "# SIGMETRICS Reference-Facing Experiment Coverage",
        "",
        "This generated note records which reference-paper evaluation axes are already",
        "covered by the main 20-page body and which ones are kept as supplemental",
        "artifacts rather than inserted as extra floats.",
        "",
        "| Axis | Main-body evidence | Supplemental artifact | Decision |",
        "|---|---|---|---|",
    ]
    for row in COVERAGE_ROWS:
        lines.append(
            f"| {row['axis']} | {row['main_evidence']} | {row['supplement']} | {row['decision']} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_readme(out_dir: Path) -> None:
    text = """# SIGMETRICS Reference Supplemental Figures

Generated by:

```bash
python3 experiments/sigmetrics/reference_supplement_plots.py
```

Artifacts:

- `dhnsw_recall_plateau.csv`: d-HNSW ef sweep measured on the SKV cluster,
  copied from `docs/dhnsw_measured.md` (2026-06-23).  The figure overlays the
  matched SlabWalk operating points used by Table `dhnsw`.
- `dhnsw_recall_plateau.pdf` / `.png`: recall plateau figure for rebuttal,
  appendix, or slides.
- `shine_skew_cache.csv`: Zipf(1.0) 40-thread cache-best-case measurements
  preserved from the earlier paper table `paper/atc_graphbeyond.tex`.
- `shine_skew_cache.pdf` / `.png`: skew/cache figure showing that skew helps
  SHINE-Cached, but operation collapse remains ahead under the same skew.
- `coverage_matrix.md`: systematic decision log for which reference-paper axes
  belong in the main body versus supplemental evidence.

Main-body decision:

The current SIGMETRICS PDF already ends the main text on page 20.  These
figures are intentionally not inserted into `paper_sigmetrics/sigmetrics_slabwalk.tex`
unless page budget opens.  They supplement existing prose/table claims without
adding new system claims or changing any experiment conclusion.
"""
    (out_dir / "README.md").write_text(text, encoding="utf-8")


def main() -> None:
    out_dir = repo_root() / "results" / "sigmetrics_reference_supplement"
    out_dir.mkdir(parents=True, exist_ok=True)

    dhnsw_csv = out_dir / "dhnsw_recall_plateau.csv"
    shine_csv = out_dir / "shine_skew_cache.csv"
    write_csv(DHNSW_EF_SWEEP, dhnsw_csv)
    write_csv(SHINE_SKEW, shine_csv)

    dhnsw = pd.read_csv(dhnsw_csv)
    shine = pd.read_csv(shine_csv)
    plot_dhnsw(dhnsw, out_dir)
    plot_shine_skew(shine, out_dir)
    write_coverage(out_dir / "coverage_matrix.md")
    write_readme(out_dir)

    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
