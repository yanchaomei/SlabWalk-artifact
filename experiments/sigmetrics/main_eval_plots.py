#!/usr/bin/env python3
"""Generate main-body SIGMETRICS evaluation figures from checked CSV data."""

from __future__ import annotations

import textwrap
import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle

try:
    from .validate_frontier_matrix import validate as validate_frontier_matrix
except ImportError:
    from validate_frontier_matrix import validate as validate_frontier_matrix

PDF_METADATA = {"CreationDate": datetime(2026, 7, 7, tzinfo=timezone.utc)}

INK = "#23313F"
MUTED = "#6B7280"
GRID = "#D4DAE3"
PANEL_BG = "#F8FAFC"
BASELINE = "#6B7280"
BASELINE_LIGHT = "#ECEFF3"
SLAB = "#0D6EB5"
SLAB_LIGHT = "#DCECF8"
SLAB_MID = "#79B7E3"
UPPER_GRAPH = "#2E7D46"
UPPER_GRAPH_LIGHT = "#DDEEDC"
SHINE = "#1D7A74"
SHINE_LIGHT = "#D9EEEC"
DHNSW = "#F47A13"
DHNSW_LIGHT = "#FFE3C2"
RDMA = "#C0504D"
RDMA_LIGHT = "#F6D8D2"
MEMORY = "#5B3F8F"
MEMORY_LIGHT = "#E7DCF3"
MN1 = "#1D7A74"
MN2 = "#C98A1B"
MN3 = "#C0504D"

CN_FIG = os.environ.get("SLABWALK_FIG_LANG", "").lower() in {"cn", "zh", "chinese"}

CN_LABELS = {
    "distance kernel (useful U)": "距离计算（有用 U）",
    "CQ-poll spinlock": "CQ 轮询自旋锁",
    "alloc/shared_ptr churn": "分配/shared_ptr 抖动",
    "visited-set dedup": "visited 去重",
    "libibverbs plumbing": "libibverbs 管线",
    "buffer recycle": "buffer 回收",
    "search/cache/heap": "搜索/cache/堆",
    "other (<0.4% each)": "其他（单项 <0.4%）",
    "remote object grows": "远程对象变大",
    "Node/vector unit": "节点/向量\n单元",
    "One expansion": "一次\nexpansion",
    "Routed partition": "路由分区",
    "keeps global graph": "保留全局图",
    "op path": "操作路径",
    "graph + amortization": "图语义 + 摊销",
    "bounded M/B": "有界\nM/B",
    "amortizes pointer chasing": "摊销指针追逐",
    "router/deser/state": "路由/反序列化\n/状态",
    "SHINE node/vector": "SHINE 节点/向量",
    "SlabWalk expansion": "SlabWalk expansion",
    "d-HNSW partition": "d-HNSW 分区",
    "Baseline": "基线",
    "SHINE-Cached": "SHINE-Cache",
    "Slabs": "Slab 块",
    "Slabs+upper graph": "Slab 块+上层图",
    "+upper graph": "+上层图",
    "P99 latency": "P99 延迟",
    "P99 posts": "P99 posts",
    "P99 reduction (x)": "P99 降低 (x)",
    "Workers": "worker 数",
    "QPS (K, log)": "QPS (K, log)",
    "remote upper D": "远端上层 D",
    "level-0 path": "第 0 层路径",
    "1 CN": "1 CN",
    "3 CN": "3 CN",
    "Mean": "平均",
    "measured": "实测",
    "1M op-rate": "1M 操作速率",
    "SIFT-1M, in-degree": "SIFT-1M，入度",
    "GIST-200K, in-degree": "GIST-200K，入度",
    "GIST-200K, hop": "GIST-200K，hop",
    "GIST\nsq8\nfull": "GIST\nsq8\n完整",
    "GIST\nsq8\nbudg.": "GIST\nsq8\n预算",
    "GIST\nRaBitQ\n2-bit": "GIST\nRaBitQ\n2-bit",
    "DEEP\nfixed": "DEEP\n固定",
    "DEEP\nprefix": "DEEP\nprefix",
    "Base": "基线",
    "Cached": "Cache",
    "Aqr": "Aqr",
    "SW sq8": "SW sq8",
    "SW+R": "SW+R",
    "1 MN": "1 MN",
    "3 MN": "3 MN",
    "Aqr@3MN": "Aqr@3MN",
    "d-HNSW": "d-HNSW",
    "SlabWalk": "SlabWalk",
    "SW SIFT": "SW SIFT",
    "SW GIST": "SW GIST",
    "touched blocks / insert": "触达块 / insert",
    "avg degree 19.1": "平均度 19.1",
    "Insert batch K": "插入批大小 K",
    "Blocks / insert": "块 / insert",
    "diff read": "增量读",
    "full read": "全量读",
    "Diff read / full index": "增量读 / 全索引",
    "all rows PASS; R@10=0.97662": "全部 PASS；R@10=0.97662",
    "QPS speedup": "QPS 加速比",
    "RDMA-post reduction": "RDMA post 降低",
    "RDMA-post reduction (x)": "RDMA post 降低 (x)",
    "Latency reduction (x)": "延迟降低 (x)",
    "GIST QPS (K)": "GIST QPS (K)",
    "QPS (10T, log)": "QPS（10T, log）",
    "QPS (matched 10T, log)": "QPS (10T, log)",
    "QPS / 1-worker QPS": "QPS / 单 worker QPS",
    "recall@10": "recall@10",
    "SIFT-1M": "SIFT-1M",
    "GIST-1M": "GIST-1M",
    "SHINE-derived": "SHINE 衍生基线",
    "Object-native": "对象原生基线",
    "Ideal linear": "理想线性",
    "d-HNSW partition": "d-HNSW 分区",
    "SlabWalk expansion": "SlabWalk expansion",
    "other L2": "其他 L2",
    "QPS (K)": "QPS (K)",
    "MN region (GB)": "MN 区域 (GB)",
    "GIST QPS (16T)": "GIST QPS (16T)",
    "Sidecar GB (log)": "Sidecar GB (log)",
    "GIST\nRaBitQ\n2b": "GIST\nRaBitQ\n2-bit",
    "d-HNSW sub-search": "d-HNSW 子搜索",
    "Deserialize (ms, log)": "反序列化 (ms, log)",
    "SlabWalk: 0": "SlabWalk：0",
    "Search state (GB, log)": "搜索侧状态 (GB, log)",
    "MN bytes (GB)": "MN 字节 (GB)",
    "Diff read / full": "增量读 / 全量读",
}


def label(text: object) -> str:
    s = str(text)
    return CN_LABELS.get(s, s) if CN_FIG else s


def wrap_label(text: object, width: int) -> str:
    s = label(text)
    return s if CN_FIG else textwrap.fill(s, width=width)


def times_label(value: float, digits: int = 1) -> str:
    """Format multiplicative annotations consistently in publication plots."""
    return rf"${value:.{digits}f}\times$"


def figure_out_dir(root: Path) -> Path:
    return root / "paper_sigmetrics" / ("figs_cn" if CN_FIG else "figs")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def set_style() -> None:
    font_family = "sans-serif" if CN_FIG else "serif"
    font_serif = ["Times New Roman", "Times", "DejaVu Serif"]
    font_sans = ["PingFang SC", "Songti SC", "Hiragino Sans GB", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams.update(
        {
            "font.family": font_family,
            "font.serif": font_serif,
            "font.sans-serif": font_sans,
            "font.size": 9,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "axes.edgecolor": INK,
            "axes.labelcolor": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": INK,
            "ytick.color": INK,
            "hatch.color": INK,
            "axes.unicode_minus": False,
        }
    )


def annotate_panel(ax: plt.Axes, label: str) -> None:
    ax.text(
        0.01,
        0.99,
        label,
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=9,
        color=INK,
    )


def annotate_panel_outside(ax: plt.Axes, label: str) -> None:
    """Place evaluation panel letters above data and away from plotted marks."""
    ax.text(
        0.0,
        1.025,
        label,
        transform=ax.transAxes,
        ha="left",
        va="bottom",
        fontsize=9,
        color=INK,
        clip_on=False,
    )


def paper_legend(ax: plt.Axes, **kwargs: object) -> None:
    legend = ax.legend(frameon=True, edgecolor="none", facecolor="white", framealpha=1.0, **kwargs)
    legend.get_frame().set_linewidth(0)


def save_paper_figure(fig: plt.Figure, pdf_path: Path, png_path: Path) -> None:
    # Render Agg first.  Some Matplotlib backends mutate hatch transforms while
    # writing PDF; a subsequent high-DPI PNG can then contain oversized black
    # hatch tiles even though the vector output is correct.
    fig.savefig(png_path, bbox_inches="tight", pad_inches=0.01, dpi=260)
    fig.savefig(pdf_path, bbox_inches="tight", pad_inches=0.01, metadata=PDF_METADATA)


def generate_q0_cntime(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q0_cntime.csv"
    cache_path = root / "results" / "sigmetrics_measurement_rigor" / "shine_cache_ci.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    cache = pd.read_csv(cache_path).set_index("condition")
    cache = cache.loc[["SHINE-cache-off", "SHINE-cache-50pct"]]

    fig = plt.figure(figsize=(7.15, 2.12))
    outer = fig.add_gridspec(1, 2, width_ratios=[1.62, 1.0], wspace=0.30)
    profile_grid = outer[0].subgridspec(2, 1, height_ratios=[0.86, 1.0], hspace=0.38)
    ax = fig.add_subplot(profile_grid[0])
    legend_ax = fig.add_subplot(profile_grid[1])
    left = 0.0
    shades = [SLAB_LIGHT, RDMA, DHNSW, MEMORY, BASELINE, SHINE, UPPER_GRAPH, BASELINE_LIGHT]
    hatches = ["////", "", "", "", "", "", "", ""]
    handles = []

    for (_, row), shade, hatch in zip(df.iterrows(), shades, hatches):
        bar = ax.barh(
            [0],
            [row["percent"]],
            left=left,
            height=0.36,
            color=shade,
            edgecolor=INK,
            linewidth=0.55,
            hatch=hatch,
            label=row["segment"],
        )
        handles.append(bar[0])
        left += float(row["percent"])

    ax.set_xlim(0, 100)
    ax.set_ylim(-0.42, 0.62)
    ax.set_yticks([])
    ax.set_xlabel("饱和 CN 线程时间占比 (%)" if CN_FIG else "% of the saturated CN thread's time")
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.tick_params(axis="x", length=2.5)
    ax.spines["left"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["top"].set_visible(False)
    ax.grid(False)
    ax.text(
        0.50,
        0.92,
        r"$17.9\%$ 有用工作 $\mid$ $82.1\%$ 逐 RDMA 操作税 $\tau$" if CN_FIG else r"$17.9\%$ useful $\mid$ $82.1\%$ per-RDMA-op tax $\tau$",
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=8,
        fontstyle="italic",
        color=INK,
    )
    annotate_panel_outside(ax, "(a)")
    legend_ax.axis("off")
    legend_ax.legend(
        handles=handles,
        labels=[label(s) for s in df["segment"].tolist()],
        loc="center",
        ncol=2,
        frameon=False,
        fontsize=6.9,
        handlelength=1.4,
        columnspacing=0.9,
        labelspacing=0.4,
    )

    cache_ax = fig.add_subplot(outer[1])
    xpos = np.arange(2)
    width = 0.32
    base_qps = float(cache.iloc[0]["mean_qps"])
    base_posts = float(cache.iloc[0]["posts_per_query"])
    qps_norm = cache["mean_qps"].to_numpy(dtype=float) / base_qps
    qps_ci = cache["ci95_half_width"].to_numpy(dtype=float) / base_qps
    posts_norm = cache["posts_per_query"].to_numpy(dtype=float) / base_posts
    cache_ax.bar(
        xpos - width / 2,
        posts_norm,
        width,
        label="RDMA posts/query",
        color=SHINE_LIGHT,
        edgecolor=INK,
        linewidth=0.75,
        hatch="////",
    )
    cache_ax.bar(
        xpos + width / 2,
        qps_norm,
        width,
        yerr=qps_ci,
        capsize=2.0,
        label="QPS",
        color=SLAB,
        edgecolor=INK,
        linewidth=0.75,
        error_kw={"linewidth": 0.7, "capthick": 0.7},
    )
    cache_ax.axhline(1.0, color=MUTED, linestyle=":", linewidth=0.75)
    cache_ax.text(1 - width / 2, posts_norm[1] + 0.06, "-83.2%", ha="center", va="bottom", fontsize=7.2, color=SHINE)
    cache_ax.text(1 + width / 2, qps_norm[1] + qps_ci[1] + 0.04, "-25.1%", ha="center", va="bottom", fontsize=7.2, color=SLAB)
    cache_ax.set_ylabel("相对 cache-off 归一化" if CN_FIG else "Normalized to cache off")
    cache_ax.set_xticks(xpos)
    cache_ax.set_xticklabels(["关闭 cache", "50% cache"] if CN_FIG else ["cache off", "50% cache"])
    cache_ax.set_ylim(0, 1.20)
    cache_ax.set_yticks([0, 0.25, 0.50, 0.75, 1.00])
    cache_ax.set_axisbelow(True)
    cache_ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(cache_ax, loc="upper right", fontsize=6.8, handlelength=1.2, labelspacing=0.3)
    annotate_panel_outside(cache_ax, "(b)")

    fig.subplots_adjust(left=0.035, right=0.995, top=0.88, bottom=0.09)
    save_paper_figure(
        fig,
        out_dir / "q0_cntime.pdf",
        out_dir / "q0_cntime.png",
    )


def generate_q0_competitor_endpoints(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q0_competitor_endpoints.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    spectrum = df[df["panel"] == "spectrum"].sort_values("value").reset_index(drop=True)
    shine = df[df["panel"] == "shine"].reset_index(drop=True)
    dhnsw = df[df["panel"] == "dhnsw"].reset_index(drop=True)

    fig = plt.figure(figsize=(7.15, 2.55))
    grid = fig.add_gridspec(
        2,
        2,
        height_ratios=[0.90, 1.35],
        width_ratios=[1.25, 1.0],
        hspace=0.42,
        wspace=0.34,
    )

    ax = fig.add_subplot(grid[0, :])
    ax.axis("off")
    ax.set_xlim(-0.55, 2.55)
    ax.set_ylim(0, 1)
    ax.annotate(
        "",
        xy=(2.42, 0.10),
        xytext=(-0.42, 0.10),
        arrowprops={"arrowstyle": "->", "linewidth": 0.8, "color": MUTED},
    )
    ax.text(
        1.0,
        -0.02,
        label("remote object grows"),
        ha="center",
        va="top",
        fontsize=7,
        color=MUTED,
        clip_on=False,
    )
    box_faces = [SHINE_LIGHT, SLAB, DHNSW_LIGHT]
    text_colors = [INK, "white", INK]
    for idx, (_, data) in enumerate(spectrum.iterrows()):
        ax.add_patch(
            Rectangle(
                (idx - 0.45, 0.21),
                0.90,
                0.70,
                facecolor=box_faces[idx],
                edgecolor=INK,
                linewidth=0.8,
            )
        )
        ax.text(
            idx,
            0.78,
            label(data["label"]),
            ha="center",
            va="center",
            fontsize=7.7,
            fontweight="bold",
            color=text_colors[idx],
        )
        ax.text(
            idx,
            0.60,
            wrap_label(data["series"], width=18),
            ha="center",
            va="center",
            fontsize=6.6,
            color=text_colors[idx],
        )
        ax.text(
            idx,
            0.43,
            wrap_label(data["benefit"], width=20),
            ha="center",
            va="center",
            fontsize=6.2,
            color=text_colors[idx],
        )
        ax.text(
            idx,
            0.28,
            wrap_label(data["residual"], width=22),
            ha="center",
            va="center",
            fontsize=6.1,
            color=text_colors[idx],
        )
    ax.text(0.01, 0.98, "(a)", transform=ax.transAxes, ha="left", va="top", fontsize=9)

    ax = fig.add_subplot(grid[1, 0])
    x = np.arange(len(shine))
    colors = [SHINE_LIGHT if "SHINE" in s else SLAB for s in shine["series"]]
    hatches = ["////" if "SHINE" in s else "" for s in shine["series"]]
    bars = ax.bar(
        x,
        shine["value"],
        0.55,
        color=colors,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, hatch, value in zip(bars, hatches, shine["value"]):
        bar.set_hatch(hatch)
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.18,
            f"{value:.1f}",
            ha="center",
            va="bottom",
            fontsize=7,
        )
    ax.set_ylabel("QPS（K）" if CN_FIG else "QPS (K)")
    ax.set_xticks(x)
    ax.set_xticklabels([label(s) for s in shine["label"]], rotation=18, ha="right")
    ax.set_ylim(0, 7.5)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    ax.text(0.01, 0.99, "(b)", transform=ax.transAxes, ha="left", va="top", fontsize=9)

    ax = fig.add_subplot(grid[1, 1])
    datasets = ["SIFT", "GIST"]
    x = np.arange(len(datasets))
    width = 0.30
    part = [
        float(dhnsw[(dhnsw["label"] == d) & (dhnsw["series"] == "d-HNSW partition")]["value"].iloc[0])
        for d in datasets
    ]
    slab = [
        float(dhnsw[(dhnsw["label"] == d) & (dhnsw["series"] == "SlabWalk expansion")]["value"].iloc[0])
        for d in datasets
    ]
    ax.bar(
        x - width / 2,
        part,
        width,
        label=label("d-HNSW partition"),
        facecolor=DHNSW_LIGHT,
        hatch="////",
        edgecolor=INK,
        linewidth=0.8,
    )
    bars = ax.bar(
        x + width / 2,
        slab,
        width,
        label=label("SlabWalk expansion"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, value in zip(bars, slab):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.16,
            times_label(float(value)),
            ha="center",
            va="bottom",
            fontsize=7,
        )
    for xpos, value in zip(x - width / 2, part):
        ax.text(xpos, value + 0.14, r"$1\times$", ha="center", va="bottom", fontsize=7)
    ax.set_ylabel("匹配 QPS / d-HNSW" if CN_FIG else "Matched QPS / d-HNSW")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 5.8)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper left", handlelength=1.2)
    ax.text(0.98, 0.99, "(c)", transform=ax.transAxes, ha="right", va="top", fontsize=9)

    fig.subplots_adjust(left=0.065, right=0.995, top=0.98, bottom=0.12)
    save_paper_figure(
        fig,
        out_dir / "q0_competitor_endpoints.pdf",
        out_dir / "q0_competitor_endpoints.png",
    )


def generate_q1_omega(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q1_omega_collapse.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    datasets = df["dataset"].tolist()
    x = np.arange(len(datasets))

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(7.15, 2.25),
        gridspec_kw={"width_ratios": [1.0, 1.08]},
    )

    ax = axes[0]
    ax.set_axisbelow(True)
    width = 0.34
    base = ax.bar(
        x - width / 2,
        df["base_qps"],
        width,
        label=label("Baseline"),
        facecolor=BASELINE_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    slab = ax.bar(
        x + width / 2,
        df["slab_qps"],
        width,
        label=label("SlabWalk"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, (_, row) in zip(slab, df.iterrows()):
        speedup = row["slab_qps"] / row["base_qps"]
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 55,
            times_label(float(speedup)),
            ha="center",
            va="bottom",
            fontsize=7,
        )
    ax.set_ylabel("QPS")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=20, ha="right")
    ax.set_ylim(0, 2350)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", ncol=1)
    annotate_panel(ax, "(a)")

    ax = axes[1]
    post_width = 0.25
    ax.bar(
        x - post_width,
        df["posts_base"],
        post_width,
        label=label("Baseline"),
        facecolor=BASELINE_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    ax.set_axisbelow(True)
    ax.bar(
        x,
        df["posts_slab"],
        post_width,
        label=label("Slabs"),
        facecolor=SLAB_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
    )
    ax.bar(
        x + post_width,
        df["posts_rung"],
        post_width,
        label=label("+upper graph"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    ax.set_yscale("log")
    ax.set_ylabel("每查询 RDMA posts" if CN_FIG else "RDMA posts/query")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=20, ha="right")
    ax.set_ylim(70, 25000)
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", ncol=1)
    annotate_panel(ax, "(b)")

    fig.tight_layout(pad=0.25, w_pad=0.9)
    save_paper_figure(
        fig,
        out_dir / "q1_omega_collapse.pdf",
        out_dir / "q1_omega_collapse.png",
    )


def generate_q1_tail_trace(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q1_tail_trace.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = pd.read_csv(data_path)
    datasets = ["SIFT", "DEEP", "GIST"]
    x = np.arange(len(datasets))

    latency_metrics = [
        ("P50", "latency_p50_ms"),
        ("P99", "latency_p99_ms"),
        ("P99.9", "latency_p99_9_ms"),
    ]
    post_metrics = [
        ("Mean", "posts_mean"),
        ("P99", "posts_p99"),
        ("P99.9", "posts_p99_9"),
    ]

    def reductions(metrics: list[tuple[str, str]]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for metric_label, col in metrics:
            values: list[float] = []
            for dataset in datasets:
                base = raw[(raw["dataset"] == dataset) & (raw["system"] == "Baseline")][col].iloc[0]
                slab = raw[(raw["dataset"] == dataset) & (raw["system"] == "SlabWalk")][col].iloc[0]
                values.append(float(base) / float(slab))
            out[metric_label] = values
        return out

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(7.15, 1.58),
        gridspec_kw={"width_ratios": [1.0, 1.0]},
    )

    styles = [
        {"facecolor": SLAB_LIGHT, "hatch": "////", "label": label("P50")},
        {"facecolor": SLAB_MID, "hatch": "", "label": label("P99")},
        {"facecolor": SLAB, "hatch": "", "label": label("P99.9")},
    ]
    width = 0.23

    ax = axes[0]
    ax.set_axisbelow(True)
    lat = reductions(latency_metrics)
    for i, (metric_label, values) in enumerate(lat.items()):
        style = styles[i]
        ax.bar(
            x + (i - 1) * width,
            values,
            width,
            label=label(metric_label),
            facecolor=style["facecolor"],
            hatch=style["hatch"],
            edgecolor=INK,
            linewidth=0.8,
        )
    ax.set_ylabel("延迟降低 (x)" if CN_FIG else "Latency reduction (x)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 17.5)
    ax.set_yticks([0, 5, 10, 15])
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", ncol=1, handlelength=1.2)
    annotate_panel(ax, "(a)")

    ax = axes[1]
    ax.set_axisbelow(True)
    posts = reductions(post_metrics)
    post_styles = [
        {"facecolor": UPPER_GRAPH_LIGHT, "hatch": "////", "label": label("Mean")},
        {"facecolor": SLAB_MID, "hatch": "", "label": label("P99")},
        {"facecolor": SLAB, "hatch": "", "label": label("P99.9")},
    ]
    for i, (metric_label, values) in enumerate(posts.items()):
        style = post_styles[i]
        ax.bar(
            x + (i - 1) * width,
            values,
            width,
            label=label(metric_label),
            facecolor=style["facecolor"],
            hatch=style["hatch"],
            edgecolor=INK,
            linewidth=0.8,
        )
    ax.set_ylabel("RDMA-post 降低 (x)" if CN_FIG else "RDMA-post reduction (x)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 18.5)
    ax.set_yticks([0, 5, 10, 15])
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", ncol=1, handlelength=1.2)
    annotate_panel(ax, "(b)")

    fig.tight_layout(pad=0.25, w_pad=0.75)
    save_paper_figure(
        fig,
        out_dir / "q1_tail_trace.pdf",
        out_dir / "q1_tail_trace.png",
    )


def generate_q1_ceiling(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q1_ceiling.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    fig, ax = plt.subplots(1, 1, figsize=(4.35, 1.98))

    ax.axhspan(3.5, 4.0, facecolor=SLAB_LIGHT, edgecolor="none", zorder=0, alpha=0.65)
    deep = df[df["series"] == "deep-scale"].sort_values("omega")
    other_l2 = df[df["series"] == "other-l2"].sort_values("omega")
    ip = df[df["series"] == "ip-boundary"].sort_values("omega")

    ax.plot(
        deep["omega"],
        deep["theta_mops"],
        marker="s",
        markersize=5,
        linewidth=1.25,
        color=SLAB,
        markeredgecolor=INK,
        label=label("DEEP 1M/3M/10M"),
    )
    ax.scatter(
        other_l2["omega"],
        other_l2["theta_mops"],
        s=34,
        marker="o",
        color=SHINE,
        edgecolor=INK,
        linewidth=0.6,
        label="其他 L2，1M" if CN_FIG else "other L2, 1M",
        zorder=3,
    )
    ax.scatter(
        ip["omega"],
        ip["theta_mops"],
        s=42,
        marker="^",
        facecolor=DHNSW_LIGHT,
        edgecolor=DHNSW,
        linewidth=0.9,
        label="TTI（IP）" if CN_FIG else "TTI (IP)",
        zorder=3,
    )

    ax.text(315, 4.17, "3.5--4.0 M 区间" if CN_FIG else "3.5--4.0 M band", fontsize=8, color=SLAB, ha="left", va="center")
    ax.set_xscale("log", base=10)
    ax.set_xlim(95, 1750)
    ax.set_ylim(0, 4.6)
    ax.set_xticks([100, 200, 500, 1000])
    ax.set_xticklabels(["100", "200", "500", "1000"])
    ax.set_xlabel(r"每查询操作数 $\Omega$（log）" if CN_FIG else r"operations per query $\Omega$ (log)")
    ax.set_ylabel(r"上限 $\Theta$（M ops/s）" if CN_FIG else r"ceiling $\Theta$ (M ops/s)")
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="lower right", handlelength=1.5)
    fig.tight_layout(pad=0.25)
    save_paper_figure(
        fig,
        out_dir / "q1_ceiling.pdf",
        out_dir / "q1_ceiling.png",
    )


def generate_q1_deep_scale(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q1_deep_scale.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    labels = df["scale"].tolist()
    x = np.arange(len(df))

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(4.35, 1.70),
        gridspec_kw={"width_ratios": [1.08, 1.0]},
    )

    ax = axes[0]
    ax.set_axisbelow(True)
    width = 0.34
    measured = ax.bar(
        x - width / 2,
        df["measured_qps"] / 1000.0,
        width,
        label=label("measured"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    predicted = ax.bar(
        x + width / 2,
        df["predicted_qps"] / 1000.0,
        width,
        label=label("1M op-rate"),
        facecolor=SLAB_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    for bar, value in zip(measured, df["measured_qps"] / 1000.0):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() - 1.5,
            f"{value:.1f}K",
            ha="center",
            va="top",
            fontsize=6.5,
            color="white",
        )
    for bar, value in zip(predicted, df["predicted_qps"] / 1000.0):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1.1,
            f"{value:.1f}K",
            ha="center",
            va="bottom",
            fontsize=6.5,
        )
    ax.set_ylabel("QPS（K）" if CN_FIG else "QPS (K)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 35)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", fontsize=6.7, handlelength=1.2)
    annotate_panel(ax, "(a)")

    ax = axes[1]
    ax.axhspan(3.5, 4.0, facecolor=SLAB_LIGHT, edgecolor="none", zorder=0, alpha=0.65)
    ax.plot(
        x,
        df["theta_mops"],
        marker="s",
        markersize=4.5,
        linewidth=1.2,
        color=SLAB,
        markeredgecolor=INK,
        zorder=3,
    )
    for xpos, value in zip(x, df["theta_mops"]):
        ax.text(xpos, value + 0.11, f"{value:.2f}", ha="center", va="bottom", fontsize=6.8)
    ax.text(1.0, 4.08, "3.5--4.0M 区间" if CN_FIG else "3.5--4.0M band", ha="center", va="bottom", fontsize=6.8, color=SLAB)
    ax.set_ylabel(r"$\Theta$（M ops/s）" if CN_FIG else r"$\Theta$ (M ops/s)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_xlim(-0.22, len(df) - 0.78)
    ax.set_ylim(3.25, 4.35)
    ax.set_yticks([3.5, 4.0])
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(b)")

    fig.tight_layout(pad=0.25, w_pad=0.75)
    save_paper_figure(
        fig,
        out_dir / "q1_deep_scale.pdf",
        out_dir / "q1_deep_scale.png",
    )


def generate_q2_budget_pareto(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q2_budget_pareto.csv"
    knee_path = root / "results" / "sigmetrics_main_figures" / "q2_budget_knee.csv"
    guard_path = root / "results" / "sigmetrics_main_figures" / "q2_memory_guards.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    knee = pd.read_csv(knee_path)
    guards = pd.read_csv(guard_path)
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(7.15, 2.12),
        gridspec_kw={"width_ratios": [1.18, 1.0, 1.08]},
    )
    styles = [
        ("SIFT-1M", "in-degree", "s", "-", SLAB, label("SIFT-1M, in-degree")),
        ("GIST-200K", "in-degree", "o", "-", MEMORY, label("GIST-200K, in-degree")),
        ("GIST-200K", "hop", "^", "--", DHNSW, label("GIST-200K, hop")),
    ]

    def q2_panel_label(ax: plt.Axes, label: str) -> None:
        ax.text(
            0.50,
            1.02,
            label,
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=9,
            clip_on=False,
        )

    ax = axes[0]
    ax.set_axisbelow(True)
    for dataset, policy, marker, linestyle, color, legend_label in styles:
        cur = df[(df["dataset"] == dataset) & (df["policy"] == policy)].sort_values("region_gb")
        ax.plot(
            cur["region_gb"],
            cur["qps"],
            marker=marker,
            markersize=4.5,
            linewidth=1.25,
            linestyle=linestyle,
            color=color,
            markerfacecolor=DHNSW_LIGHT if marker == "^" else color,
            markeredgecolor=INK,
            markeredgewidth=0.7,
            label=legend_label,
        )

    gist_indeg = df[(df["dataset"] == "GIST-200K") & (df["policy"] == "in-degree")]
    f025 = gist_indeg[gist_indeg["f"] == 0.25].iloc[0]
    f1 = gist_indeg[gist_indeg["f"] == 1.00].iloc[0]
    ax.annotate(
        r"$f{=}0.25$",
        xy=(f025["region_gb"], f025["qps"]),
        xytext=(5, 8),
        textcoords="offset points",
        fontsize=8,
        ha="left",
        va="bottom",
    )
    ax.annotate(
        r"$f{=}1$",
        xy=(f1["region_gb"], f1["qps"]),
        xytext=(-4, -12),
        textcoords="offset points",
        fontsize=8,
        ha="right",
        va="top",
    )

    ax.set_xlabel("MN 共置区域 (GB)" if CN_FIG else "MN co-located region (GB)")
    ax.set_ylabel("QPS")
    ax.set_xlim(0, 6.8)
    ax.set_ylim(0, 2350)
    ax.set_xticks([0, 1.5, 3.0, 4.5, 6.0])
    ax.grid(True, axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper left", fontsize=6.5, handlelength=1.3)
    q2_panel_label(ax, "(a)")

    ax = axes[1]
    ax.set_axisbelow(True)
    knee = knee.sort_values("region_gb")
    ax.plot(
        knee["region_gb"],
        knee["qps"],
        marker="s",
        markersize=4.4,
        linewidth=1.25,
        color=SLAB,
        markeredgecolor=INK,
    )
    for _, row in knee.iterrows():
        if row["f"] in (0.05, 0.25, 1.0):
            if row["f"] == 0.25:
                offset = (8, -14)
                va = "top"
            elif row["f"] == 1.0:
                offset = (4, -13)
                va = "top"
            else:
                offset = (4, 6)
                va = "bottom"
            ax.annotate(
                rf"$f={row['f']:.2g}$",
                xy=(row["region_gb"], row["qps"]),
                xytext=offset,
                textcoords="offset points",
                fontsize=6.8,
                ha="left",
                va=va,
            )
    ax.set_xlabel("MN 区域 (GB)" if CN_FIG else "MN region (GB)")
    ax.set_ylabel("QPS（16T）" if CN_FIG else "QPS (16T)")
    ax.set_xlim(0, 6.8)
    ax.set_ylim(2250, 3650)
    ax.set_xticks([0, 1.5, 3.0, 4.5, 6.0])
    ax.set_yticks([2500, 3000, 3500])
    ax.grid(True, axis="both", linestyle=":", linewidth=0.5, color=GRID)
    q2_panel_label(ax, "(b)")

    ax = axes[2]
    ax.set_axisbelow(True)
    labels = [label(s) for s in ["GIST\nsq8\nfull", "GIST\nsq8\nbudg.", "GIST\nRaBitQ\n2-bit", "DEEP\nfixed", "DEEP\nprefix"]]
    x = np.arange(len(guards))
    colors = [BASELINE_LIGHT, SLAB_LIGHT, SLAB, BASELINE_LIGHT, UPPER_GRAPH]
    hatches = ["////", "", "", "////", ""]
    bars = ax.bar(
        x,
        guards["region_gb"],
        0.62,
        color=colors,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, hatch, value in zip(bars, hatches, guards["region_gb"]):
        bar.set_hatch(hatch)
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value * 1.08,
            f"{value:.2g}",
            ha="center",
            va="bottom",
            fontsize=6.5,
        )
    ax.axhline(12.0, color=RDMA, linestyle="--", linewidth=0.8)
    ax.text(4.42, 12.7, "12GB MN", ha="right", va="bottom", fontsize=6.7, color=RDMA)
    ax.set_yscale("log")
    ax.set_ylabel("Sidecar 区域 (GB, log)" if CN_FIG else "Sidecar region (GB, log)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylim(1.7, 42)
    ax.set_yticks([2, 4, 8, 12, 32])
    ax.set_yticklabels(["2", "4", "8", "12", "32"])
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    q2_panel_label(ax, "(c)")

    fig.tight_layout(pad=0.25, w_pad=0.65)
    save_paper_figure(
        fig,
        out_dir / "q2_budget_pareto.pdf",
        out_dir / "q2_budget_pareto.png",
    )


def generate_q3_shine_scaleout(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q3_shine_scaleout.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    x = np.arange(len(df))
    labels = [label(s) for s in df["label"].tolist()]

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(7.15, 1.85),
        gridspec_kw={"width_ratios": [1.55, 1.0]},
    )

    ax = axes[0]
    ax.set_axisbelow(True)
    width = 0.34
    ax.bar(
        x - width / 2,
        df["qps_1mn"] / 1000.0,
        width,
        label=label("1 MN"),
        facecolor=SHINE_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    bars_3mn = ax.bar(
        x + width / 2,
        df["qps_3mn"] / 1000.0,
        width,
        label=label("3 MN"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    aqr_3mn = float(df.loc[df["label"] == "Aqr", "qps_3mn"].iloc[0]) / 1000.0
    ax.axhline(aqr_3mn, color=DHNSW, linestyle="--", linewidth=0.8, label=label("Aqr@3MN"))
    for bar, ratio in zip(bars_3mn, df["vs_aqr_3mn"]):
        if ratio >= 1.25:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.16,
                times_label(float(ratio), 2),
                ha="center",
                va="bottom",
                fontsize=7,
            )
    ax.set_ylabel("汇总 QPS（K）" if CN_FIG else "Aggregate QPS (K)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 7.45)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(
        ax,
        loc="upper left",
        bbox_to_anchor=(0.08, 1.0),
        ncol=3,
        columnspacing=0.8,
        handlelength=1.4,
    )
    annotate_panel(ax, "(a)")

    ax = axes[1]
    ax.set_axisbelow(True)
    bar_colors = [SHINE_LIGHT, SHINE_LIGHT, SHINE_LIGHT, SLAB_LIGHT, SLAB]
    hatches = ["////", "////", "////", "", ""]
    bars = ax.bar(
        x,
        df["scale"],
        0.56,
        color=bar_colors,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, hatch, scale in zip(bars, hatches, df["scale"]):
        bar.set_hatch(hatch)
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.04,
            times_label(float(scale), 2),
            ha="center",
            va="bottom",
            fontsize=7,
        )
    ax.axhline(2.0, color=RDMA, linestyle="--", linewidth=0.8)
    ax.set_ylabel("3 MN / 1 MN QPS")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=18, ha="right")
    ax.set_ylim(0, 2.25)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    ax.text(
        -0.15,
        1.02,
        "(b)",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=9,
        clip_on=False,
    )

    fig.tight_layout(pad=0.25, w_pad=0.85)
    save_paper_figure(
        fig,
        out_dir / "q3_shine_scaleout.pdf",
        out_dir / "q3_shine_scaleout.png",
    )


def generate_q4_dhnsw_endpoint(root: Path) -> None:
    endpoint_path = root / "results" / "sigmetrics_main_figures" / "q4_dhnsw_endpoint.csv"
    plateau_path = root / "results" / "sigmetrics_main_figures" / "q4_dhnsw_plateau.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    endpoint = pd.read_csv(endpoint_path)
    plateau = pd.read_csv(plateau_path)
    datasets = ["SIFT", "GIST"]
    systems = ["d-HNSW", "SlabWalk"]
    x = np.arange(len(datasets))
    width = 0.32

    fig = plt.figure(figsize=(7.15, 3.18))
    grid = fig.add_gridspec(2, 6, height_ratios=[1.0, 1.0], hspace=0.62, wspace=0.95)
    axes = [
        fig.add_subplot(grid[0, 0:2]),
        fig.add_subplot(grid[0, 2:4]),
        fig.add_subplot(grid[0, 4:6]),
        fig.add_subplot(grid[1, 0:3]),
        fig.add_subplot(grid[1, 3:6]),
    ]

    def values(metric: str, system: str) -> list[float]:
        return [
            float(endpoint[(endpoint["metric"] == metric) & (endpoint["dataset"] == dataset) & (endpoint["system"] == system)]["value"].iloc[0])
            for dataset in datasets
        ]

    def labels(metric: str, system: str) -> list[str]:
        return [
            str(endpoint[(endpoint["metric"] == metric) & (endpoint["dataset"] == dataset) & (endpoint["system"] == system)]["label"].iloc[0])
            for dataset in datasets
        ]

    # (a) Router-coverage plateau.  The dashed SlabWalk lines are the matched
    # global-graph operating points used by the endpoint comparison.
    ax = axes[0]
    markers = {"SIFT": "s", "GIST": "o"}
    colors = {"SIFT": SLAB, "GIST": MEMORY}
    for dataset in datasets:
        cur = plateau[plateau["dataset"] == dataset].sort_values("ef")
        ax.plot(
            cur["ef"],
            cur["dhnsw_recall"],
            marker=markers[dataset],
            markersize=3.8,
            linewidth=1.1,
            color=colors[dataset],
            label=label(f"d-HNSW {dataset}"),
        )
        ax.axhline(
            float(cur["slabwalk_recall"].iloc[0]),
            color=colors[dataset],
            linestyle="--",
            linewidth=0.9,
            label=label(f"SW {dataset}"),
        )
    ax.set_xlabel(r"d-HNSW 子搜索 $\mathit{ef}$" if CN_FIG else r"d-HNSW sub-search $\mathit{ef}$")
    ax.set_ylabel("recall@10")
    ax.set_xlim(42, 206)
    ax.set_ylim(0.72, 0.95)
    ax.set_xticks([48, 96, 128, 200])
    ax.grid(True, axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="lower right", fontsize=6.4, handlelength=1.4)
    annotate_panel(ax, "(a)")

    # (b) Same-cluster, matched-10-thread throughput.
    ax = axes[1]
    ax.set_axisbelow(True)
    dh_vals = values("qps", "d-HNSW")
    sw_vals = values("qps", "SlabWalk")
    bars_dh = ax.bar(
        x - width / 2,
        dh_vals,
        width,
        label=label("d-HNSW"),
        facecolor=DHNSW_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    bars_sw = ax.bar(
        x + width / 2,
        sw_vals,
        width,
        label=label("SlabWalk"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.8,
    )
    for bar, lab in zip(list(bars_dh) + list(bars_sw), labels("qps", "d-HNSW") + labels("qps", "SlabWalk")):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() * 1.14,
            lab,
            ha="center",
            va="bottom",
            fontsize=6.7,
        )
    ax.set_yscale("log")
    ax.set_ylabel("QPS（10T, log）" if CN_FIG else "QPS (10T, log)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(300, 70000)
    ax.set_yticks([500, 2000, 10000, 50000])
    ax.set_yticklabels(["0.5K", "2K", "10K", "50K"])
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", fontsize=6.8, handlelength=1.2)
    annotate_panel(ax, "(b)")

    # (c) Partition materialization cost.  SlabWalk scores returned RDMA bytes
    # in place, so the corresponding deserialization bars are true zeros.
    ax = axes[2]
    ax.set_axisbelow(True)
    deser = values("deserialize_ms", "d-HNSW")
    ax.bar(
        x - width / 2,
        deser,
        width,
        label=label("d-HNSW"),
        facecolor=DHNSW_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
    )
    for xpos, lab in zip(x - width / 2, labels("deserialize_ms", "d-HNSW")):
        value = deser[int(round(xpos + width / 2))]
        ax.text(xpos, value * 1.18, f"{lab} ms", ha="center", va="bottom", fontsize=6.7)
    for xpos in x + width / 2:
        ax.text(xpos, 0.055, "0", ha="center", va="bottom", fontsize=7, color="white", bbox={"facecolor": SLAB, "edgecolor": "none", "pad": 1.0})
    ax.set_yscale("log")
    ax.set_ylabel("反序列化 (ms, log)" if CN_FIG else "Deserialize (ms, log)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_xlim(-0.5, len(datasets) - 0.5)
    ax.set_ylim(0.045, 45)
    ax.set_yticks([0.1, 1, 10])
    ax.set_yticklabels(["0.1", "1", "10"])
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(c)")

    # (d) Search-side memory footprint.
    ax = axes[3]
    ax.set_axisbelow(True)
    for offset, system, face, hatch in [
        (-width / 2, "d-HNSW", DHNSW_LIGHT, "////"),
        (width / 2, "SlabWalk", SLAB, ""),
    ]:
        bars = ax.bar(
            x + offset,
            values("search_state_gb", system),
            width,
            label=label(system),
            facecolor=face,
            edgecolor=INK,
            linewidth=0.8,
            hatch=hatch,
        )
        for bar, lab in zip(bars, labels("search_state_gb", system)):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() * 1.22,
                lab,
                ha="center",
                va="bottom",
                fontsize=6.7,
            )
    ax.set_yscale("log")
    ax.set_ylabel("搜索侧状态 (GB, log)" if CN_FIG else "Search-side state (GB, log)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0.03, 40)
    ax.set_yticks([0.1, 1, 10])
    ax.set_yticklabels(["0.1", "1", "10"])
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(d)")

    # (e) Bytes that stay in the memory-node pool.
    ax = axes[4]
    ax.set_axisbelow(True)
    for offset, system, face, hatch in [
        (-width / 2, "d-HNSW", DHNSW_LIGHT, "////"),
        (width / 2, "SlabWalk", SLAB, ""),
    ]:
        bars = ax.bar(
            x + offset,
            values("mn_bytes_gb", system),
            width,
            label=label(system),
            facecolor=face,
            edgecolor=INK,
            linewidth=0.8,
            hatch=hatch,
        )
        for bar, lab in zip(bars, labels("mn_bytes_gb", system)):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.45,
                lab,
                ha="center",
                va="bottom",
                fontsize=6.7,
            )
    ax.set_ylabel("MN payload/sidecar (GB)")
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 18.5)
    ax.set_yticks([0, 5, 10, 15])
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(e)")

    fig.subplots_adjust(left=0.075, right=0.995, top=0.985, bottom=0.105)
    save_paper_figure(
        fig,
        out_dir / "q4_dhnsw_endpoint.pdf",
        out_dir / "q4_dhnsw_endpoint.png",
    )


def generate_q5_rebuild_maintenance(root: Path) -> None:
    data_path = root / "results" / "sigmetrics_main_figures" / "q5_rebuild_maintenance.csv"
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(data_path)
    df = df.sort_values("batch_inserts")
    x_all = df["batch_inserts"].to_numpy(dtype=float)
    x_labels = ["1K", "10K", "50K", "100K"]

    fig = plt.figure(figsize=(7.15, 2.15))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.04, 1.0], wspace=0.34)
    ax1 = fig.add_subplot(grid[0])
    ax2 = fig.add_subplot(grid[1])

    ax1.set_axisbelow(True)
    ax1.plot(
        x_all,
        df["write_amp_blocks_per_insert"],
        marker="s",
        markersize=4.2,
        linewidth=1.25,
        color=SLAB,
        label=label("touched blocks / insert"),
    )
    ax1.axhline(32, color=MUTED, linewidth=0.8, linestyle="--", label=r"$M_{\max0}=32$")
    ax1.axhline(19.14, color=DHNSW, linewidth=0.8, linestyle=":", label=label("avg degree 19.1"))
    for xval, yval in zip(x_all, df["write_amp_blocks_per_insert"]):
        ax1.text(xval, yval + 0.8, f"{yval:.1f}", ha="center", va="bottom", fontsize=7)
    ax1.set_xscale("log")
    ax1.set_xticks(x_all)
    ax1.set_xticklabels(x_labels)
    ax1.set_xlabel(label("Insert batch K"))
    ax1.set_ylabel(label("Blocks / insert"))
    ax1.set_ylim(0, 35)
    ax1.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax1, loc="upper right", fontsize=6.7, handlelength=1.6)
    annotate_panel(ax1, "(a)")

    diff = df.dropna(subset=["diff_read_frac"]).copy()
    x_diff = diff["batch_inserts"].to_numpy(dtype=float)
    read_pct = diff["diff_read_frac"].to_numpy(dtype=float) * 100.0
    ax2.set_axisbelow(True)
    ax2.bar(
        x_diff,
        read_pct,
        width=x_diff * 0.28,
        facecolor=SLAB_LIGHT,
        edgecolor=INK,
        linewidth=0.8,
        hatch="////",
        label=label("diff read"),
    )
    ax2.axhline(100, color=MUTED, linewidth=0.8, linestyle="--", label=label("full read"))
    for xval, yval in zip(x_diff, read_pct):
        ax2.text(xval, yval + 3.2, f"{yval:.1f}%", ha="center", va="bottom", fontsize=7)
    ax2.set_xscale("log")
    ax2.set_xticks(x_diff)
    ax2.set_xticklabels(["1K", "10K", "100K"])
    ax2.set_xlabel(label("Insert batch K"))
    ax2.set_ylabel(label("Diff read / full index"))
    ax2.set_ylim(0, 112)
    ax2.set_yticks([0, 25, 50, 75, 100])
    ax2.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax2.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax2, loc="upper right", fontsize=6.7, handlelength=1.6)
    annotate_panel(ax2, "(b)")

    fig.subplots_adjust(left=0.07, right=0.995, top=0.98, bottom=0.24)
    save_paper_figure(
        fig,
        out_dir / "q5_rebuild_maintenance.pdf",
        out_dir / "q5_rebuild_maintenance.png",
    )


def generate_eval_overall_compact(root: Path) -> None:
    """Mechanism summary complementary to the full recall-QPS frontier figure."""
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    spectrum = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q0_competitor_endpoints.csv")
    omega = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q1_omega_collapse.csv")
    shine = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q3_shine_scaleout.csv")

    fig = plt.figure(figsize=(7.15, 3.48))
    grid = fig.add_gridspec(2, 2, hspace=0.62, wspace=0.34)
    axes = [fig.add_subplot(grid[i, j]) for i in range(2) for j in range(2)]

    # (a) Remote-object spectrum.
    ax = axes[0]
    ax.axis("off")
    ax.set_xlim(-0.55, 2.55)
    ax.set_ylim(0, 1)
    spec = spectrum[spectrum["panel"] == "spectrum"].sort_values("value").reset_index(drop=True)
    box_faces = [SHINE_LIGHT, SLAB, DHNSW_LIGHT]
    text_colors = [INK, "white", INK]
    for idx, (_, data) in enumerate(spec.iterrows()):
        ax.add_patch(
            Rectangle(
                (idx - 0.42, 0.27),
                0.84,
                0.57,
                facecolor=box_faces[idx],
                edgecolor=INK,
                linewidth=0.75,
            )
        )
        ax.text(idx, 0.72, label(data["label"]), ha="center", va="center", fontsize=8.0, fontweight="bold", color=text_colors[idx])
        ax.text(idx, 0.53, wrap_label(data["series"], 15), ha="center", va="center", fontsize=6.4, color=text_colors[idx])
        ax.text(idx, 0.36, wrap_label(data["residual"], 13), ha="center", va="center", fontsize=6.1, color=text_colors[idx])
    ax.annotate("", xy=(2.35, 0.14), xytext=(-0.35, 0.14), arrowprops={"arrowstyle": "->", "linewidth": 0.75, "color": MUTED})
    ax.text(1.0, 0.03, label("remote object grows"), ha="center", va="top", fontsize=6.8, color=MUTED)
    annotate_panel_outside(ax, "(a)")

    # (b) Equal-recall throughput speedup from resizing the remote object.
    ax = axes[1]
    omega_l2 = omega[omega["dataset"].isin(["DEEP", "BIGANN", "SPACEV", "TURING"])].reset_index(drop=True)
    datasets = omega_l2["dataset"].tolist()
    x = np.arange(len(datasets))
    speedup = omega_l2["slab_qps"] / omega_l2["base_qps"]
    bars = ax.bar(x, speedup, 0.58, color=SLAB, edgecolor=INK, linewidth=0.75)
    for bar, val in zip(bars, speedup):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.08, times_label(float(val)), ha="center", va="bottom", fontsize=6.8)
    ax.set_ylabel(label("QPS speedup"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=14, ha="right")
    ax.set_ylim(0, 3.15)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(b)")

    # (c) Operation collapse from the same-binary layout gate.
    ax = axes[2]
    reduction = omega_l2["posts_base"] / omega_l2["posts_slab"]
    bars = ax.bar(x, reduction, 0.58, color=SLAB_LIGHT, edgecolor=INK, linewidth=0.75, hatch="////")
    for bar, val in zip(bars, reduction):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.35, times_label(float(val)), ha="center", va="bottom", fontsize=6.8)
    ax.set_ylabel(label("RDMA-post reduction"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets, rotation=14, ha="right")
    ax.set_ylim(0, 13.0)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(c)")

    # (d) Graph-preserving endpoint after scale-out.
    ax = axes[3]
    labels_short = [label(s) for s in shine["label"].tolist()]
    x_sh = np.arange(len(shine))
    bar_colors = [SHINE_LIGHT if f == "SHINE" else SLAB for f in shine["family"]]
    bar_hatches = ["////" if f == "SHINE" else "" for f in shine["family"]]
    bars = ax.bar(x_sh, shine["qps_3mn"] / 1000.0, 0.58, color=bar_colors, edgecolor=INK, linewidth=0.75)
    for bar, hatch, ratio in zip(bars, bar_hatches, shine["vs_aqr_3mn"]):
        bar.set_hatch(hatch)
        if ratio >= 1.25:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.18, times_label(float(ratio)), ha="center", va="bottom", fontsize=6.7)
    ax.axhline(float(shine.loc[shine["label"] == "Aqr", "qps_3mn"].iloc[0]) / 1000.0, color=DHNSW, linestyle="--", linewidth=0.75)
    ax.set_ylabel(label("GIST QPS (K)"))
    ax.set_xticks(x_sh)
    ax.set_xticklabels(labels_short, rotation=14, ha="right")
    ax.set_ylim(0, 7.4)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(d)")

    fig.subplots_adjust(left=0.065, right=0.995, top=0.95, bottom=0.12)
    save_paper_figure(
        fig,
        out_dir / "eval_overall_compact.pdf",
        out_dir / "eval_overall_compact.png",
    )


def generate_eval_frontier_curves(root: Path) -> None:
    """Per-dataset recall-QPS frontiers for SHINE, d-HNSW, and SlabWalk."""
    data_path = root / "results" / "sigmetrics_main_figures" / "q0_frontier_curves.csv"
    if not data_path.exists():
        return
    validate_frontier_matrix(data_path, min_points=5)

    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(data_path)
    if df.empty:
        return
    df["recall"] = pd.to_numeric(df["recall"], errors="coerce")
    df["qps"] = pd.to_numeric(df["qps"], errors="coerce")
    df["ef"] = pd.to_numeric(df["ef"], errors="coerce")
    df = df.dropna(subset=["dataset", "method", "recall", "qps"])
    order = [
        "SIFT1M",
        "GIST1M",
        "DEEP1M",
        "BIGANN1M",
        "SPACEV1M",
        "TURING1M",
        "TTI1M",
        "DEEP10M",
        "TTI10M",
        "SIFT10M",
    ]
    required_methods = {"SHINE", "d-HNSW", "SlabWalk"}
    datasets = [
        d
        for d in order
        if required_methods.issubset(set(df.loc[df["dataset"] == d, "method"]))
    ]
    if not datasets:
        return

    ncols = min(4, len(datasets))
    nrows = int(np.ceil(len(datasets) / ncols))
    fig = plt.figure(figsize=(7.15, 1.78 * nrows + 0.44))
    grid = fig.add_gridspec(nrows, ncols, hspace=0.72, wspace=0.50)
    axes: list[plt.Axes] = []
    for idx in range(nrows * ncols):
        ax = fig.add_subplot(grid[idx // ncols, idx % ncols])
        axes.append(ax)

    styles = {
        "SHINE": {
            "color": SHINE,
            "marker": "s",
            "face": SHINE_LIGHT,
            "label": label("SHINE-derived"),
        },
        "d-HNSW": {
            "color": DHNSW,
            "marker": "^",
            "face": DHNSW_LIGHT,
            "label": label("d-HNSW partition"),
        },
        "SlabWalk": {
            "color": SLAB,
            "marker": "o",
            "face": SLAB,
            "label": label("SlabWalk expansion"),
        },
    }
    panel_letters = "abcdefghijklmnopqrstuvwxyz"
    handles = []
    labels_seen = []

    for idx, dataset in enumerate(datasets):
        ax = axes[idx]
        cur = df[df["dataset"] == dataset].copy()
        display_dataset = f"{dataset[:-2]}-1M" if dataset.endswith("1M") else dataset
        ax.set_axisbelow(True)
        for method in ["SHINE", "d-HNSW", "SlabWalk"]:
            rows = cur[cur["method"] == method].sort_values("ef")
            if rows.empty:
                continue
            st = styles[method]
            (line,) = ax.plot(
                rows["recall"],
                rows["qps"],
                color=st["color"],
                marker=st["marker"],
                markersize=3.8,
                markerfacecolor=st["face"],
                markeredgecolor=INK,
                markeredgewidth=0.55,
                linewidth=1.1,
                label=st["label"],
                zorder=3 if method == "SlabWalk" else 2,
            )
            if st["label"] not in labels_seen:
                handles.append(line)
                labels_seen.append(st["label"])
        if len(cur) > 0:
            x_min, x_max = cur["recall"].min(), cur["recall"].max()
            y_min, y_max = cur["qps"].min(), cur["qps"].max()
            x_pad = max((x_max - x_min) * 0.08, 0.004)
            ax.set_xlim(max(0.0, x_min - x_pad), min(1.0, x_max + x_pad))
            ax.set_ylim(max(1.0, y_min * 0.55), y_max * 1.85)
        ax.set_yscale("log")
        ax.set_xlabel(label("recall@10"))
        if idx % ncols == 0:
            ax.set_ylabel(label("QPS (10T, log)"))
        ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
        ax.set_title(
            f"({panel_letters[idx]}) {display_dataset}",
            loc="left",
            fontsize=7.4,
            color=INK,
            fontweight="bold",
            pad=2.5,
        )

    for ax in axes[len(datasets):]:
        ax.axis("off")
    if handles:
        fig.legend(
            handles,
            labels_seen,
            loc="upper center",
            ncol=min(3, len(handles)),
            frameon=False,
            bbox_to_anchor=(0.52, 1.008),
            handlelength=1.6,
            columnspacing=1.3,
        )

    fig.subplots_adjust(left=0.07, right=0.995, top=0.88 if nrows > 1 else 0.86, bottom=0.12)
    save_paper_figure(
        fig,
        out_dir / "eval_frontier_curves.pdf",
        out_dir / "eval_frontier_curves.png",
    )


def generate_eval_scaling_ablation_compact(root: Path) -> None:
    """Compact figure for tail behavior, scaling model, and memory guards."""
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    trace = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q1_tail_trace.csv")
    worker = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q1_worker_scaling.csv")
    ceiling = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q1_ceiling.csv")
    deep = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q1_deep_scale.csv")
    knee = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q2_budget_knee.csv").sort_values("region_gb")
    guards = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q2_memory_guards.csv")

    fig = plt.figure(figsize=(7.15, 3.58))
    grid = fig.add_gridspec(2, 3, height_ratios=[1.0, 1.0], hspace=0.70, wspace=0.48)
    axes = [fig.add_subplot(grid[i, j]) for i in range(2) for j in range(3)]

    datasets = ["SIFT", "DEEP", "GIST"]
    x = np.arange(len(datasets))
    width = 0.26

    def reduction(col: str) -> list[float]:
        vals = []
        for d in datasets:
            base = float(trace[(trace["dataset"] == d) & (trace["system"] == "Baseline")][col].iloc[0])
            slab = float(trace[(trace["dataset"] == d) & (trace["system"] == "SlabWalk")][col].iloc[0])
            vals.append(base / slab)
        return vals

    # (a) P99 latency and operation reductions share the same scale.
    ax = axes[0]
    tail_width = 0.34
    ax.bar(
        x - tail_width / 2,
        reduction("latency_p99_ms"),
        tail_width,
        label=label("P99 latency"),
        facecolor=SLAB,
        edgecolor=INK,
        linewidth=0.75,
    )
    ax.bar(
        x + tail_width / 2,
        reduction("posts_p99"),
        tail_width,
        label=label("P99 posts"),
        facecolor=UPPER_GRAPH_LIGHT,
        edgecolor=INK,
        linewidth=0.75,
        hatch="////",
    )
    ax.set_ylabel(label("P99 reduction (x)"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 18.0)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", fontsize=6.2, handlelength=1.1)
    annotate_panel_outside(ax, "(a)")

    # (b) Cross-system worker scaling.  Absolute recall-QPS is reported in the
    # frontier figure; normalization isolates how each fixed operating point
    # scales after one worker.
    ax = axes[1]
    deep_worker = worker[worker["dataset"] == "DEEP"]
    dhnsw_runs_path = (
        root
        / "results"
        / "sigmetrics_main_figures"
        / "q1_worker_scaling_dhnsw_runs.csv"
    )
    worker_series: list[tuple[str, str, pd.DataFrame, str, str, str]] = []
    for system, display, color, marker, linestyle in [
        ("Baseline", "SHINE-derived", SHINE, "s", "-"),
        ("Slabs+upper graph", "SlabWalk", SLAB, "o", "-"),
    ]:
        rows = deep_worker[deep_worker["system"] == system].sort_values("threads").copy()
        rows["qps_norm"] = rows["qps"] / float(rows.iloc[0]["qps"])
        rows["qps_norm_ci"] = 0.0
        worker_series.append((system, display, rows, color, marker, linestyle))

    if dhnsw_runs_path.exists():
        dhnsw_runs = pd.read_csv(dhnsw_runs_path)
        dhnsw_runs = dhnsw_runs[dhnsw_runs["status"] == "ok"].copy()
        dhnsw_runs["qps"] = pd.to_numeric(dhnsw_runs["qps"], errors="coerce")
        dhnsw_runs = dhnsw_runs.dropna(subset=["qps"])
        grouped = dhnsw_runs.groupby("threads")["qps"]
        dhnsw_rows = grouped.agg(["median", "std", "count"]).reset_index()
        dhnsw_rows["qps"] = dhnsw_rows["median"]
        one_worker = float(dhnsw_rows.loc[dhnsw_rows["threads"] == 1, "qps"].iloc[0])
        dhnsw_rows["qps_norm"] = dhnsw_rows["qps"] / one_worker
        # Student-t 95% interval for five independent runs.
        dhnsw_rows["qps_norm_ci"] = (
            2.776
            * dhnsw_rows["std"].fillna(0.0)
            / np.sqrt(dhnsw_rows["count"])
            / one_worker
        )
        worker_series.insert(1, ("d-HNSW", "d-HNSW", dhnsw_rows, DHNSW, "^", "--"))

    ideal_x = np.asarray([1.0, 80.0])
    ax.plot(
        ideal_x,
        ideal_x,
        color=MUTED,
        linewidth=0.75,
        linestyle=":",
        label=label("Ideal linear"),
        zorder=1,
    )
    for _, display, rows, color, marker, linestyle in worker_series:
        ax.plot(
            rows["threads"],
            rows["qps_norm"],
            color=color,
            marker=marker,
            markersize=3.5,
            markeredgecolor=INK,
            markeredgewidth=0.45,
            linewidth=1.05,
            linestyle=linestyle,
            label=label(display),
            zorder=3,
        )
        ci = rows["qps_norm_ci"].to_numpy(dtype=float)
        if np.any(ci > 0):
            ax.errorbar(
                rows["threads"],
                rows["qps_norm"],
                yerr=ci,
                fmt="none",
                ecolor=color,
                elinewidth=0.7,
                capsize=1.8,
                zorder=2,
            )
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xlim(0.85, 105)
    ax.set_ylim(0.78, 100)
    ax.set_xticks([1, 8, 16, 40, 80])
    ax.set_xticklabels(["1", "8", "16", "40", "80"])
    ax.set_yticks([1, 2, 4, 8, 16, 32, 64])
    ax.set_yticklabels(["1", "2", "4", "8", "16", "32", "64"])
    ax.set_xlabel(label("Workers"))
    ax.set_ylabel(label("QPS / 1-worker QPS"))
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper left", fontsize=5.7, handlelength=1.2, ncol=2)
    annotate_panel_outside(ax, "(b)")

    # (c) Worker-scaling ceiling.
    ax = axes[2]
    ax.axhspan(3.5, 4.0, facecolor=SLAB_LIGHT, edgecolor="none", alpha=0.65)
    deep_points = ceiling[ceiling["series"] == "deep-scale"].sort_values("omega")
    l2_points = ceiling[ceiling["series"] == "other-l2"].sort_values("omega")
    ip_points = ceiling[ceiling["series"] == "ip-boundary"]
    ax.plot(deep_points["omega"], deep_points["theta_mops"], marker="s", markersize=4.2, linewidth=1.1, color=SLAB, markeredgecolor=INK, label="DEEP")
    ax.scatter(l2_points["omega"], l2_points["theta_mops"], s=28, marker="o", color=SHINE, edgecolor=INK, linewidth=0.55, label=label("other L2"), zorder=3)
    ax.scatter(ip_points["omega"], ip_points["theta_mops"], s=36, marker="^", facecolor=DHNSW_LIGHT, edgecolor=DHNSW, linewidth=0.8, label="TTI", zorder=3)
    ax.set_xscale("log")
    ax.set_xlim(95, 1750)
    ax.set_ylim(0, 4.6)
    ax.set_xticks([100, 200, 500, 1000])
    ax.set_xticklabels(["100", "200", "500", "1000"])
    ax.set_xlabel(r"$\Omega$ (log)")
    ax.set_ylabel(r"$\Theta$ (M ops/s)")
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="lower right", fontsize=6.2, handlelength=1.1)
    annotate_panel_outside(ax, "(c)")

    # (d) DEEP 1M/3M/10M model validation.
    ax = axes[3]
    labels_deep = deep["scale"].tolist()
    xd = np.arange(len(deep))
    bar_w = 0.34
    ax.bar(xd - bar_w / 2, deep["measured_qps"] / 1000.0, bar_w, label=label("measured"), color=SLAB, edgecolor=INK, linewidth=0.75)
    ax.bar(xd + bar_w / 2, deep["predicted_qps"] / 1000.0, bar_w, label=label("1M op-rate"), color=SLAB_LIGHT, edgecolor=INK, linewidth=0.75, hatch="////")
    ax.set_ylabel(label("QPS (K)"))
    ax.set_xticks(xd)
    ax.set_xticklabels(labels_deep)
    ax.set_ylim(0, 35)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", fontsize=6.4, handlelength=1.1)
    annotate_panel_outside(ax, "(d)")

    # (e) GIST budget knee.
    ax = axes[4]
    ax.plot(knee["region_gb"], knee["qps"], marker="s", markersize=4.1, linewidth=1.15, color=SLAB, markeredgecolor=INK)
    for _, row in knee.iterrows():
        if row["f"] in (0.05, 0.25, 1.0):
            ax.annotate(rf"$f={row['f']:.2g}$", xy=(row["region_gb"], row["qps"]), xytext=(4, 5), textcoords="offset points", fontsize=6.4)
    ax.set_xlabel(label("MN region (GB)"))
    ax.set_ylabel(label("GIST QPS (16T)"))
    ax.set_xlim(0, 6.8)
    ax.set_ylim(2250, 3650)
    ax.set_axisbelow(True)
    ax.grid(True, axis="both", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(e)")

    # (f) Memory/code guards.
    ax = axes[5]
    guard_labels = [label(s) for s in ["GIST\nsq8\nfull", "GIST\nsq8\nbudg.", "GIST\nRaBitQ\n2b", "DEEP\nfixed", "DEEP\nprefix"]]
    xg = np.arange(len(guards))
    guard_colors = [BASELINE_LIGHT, SLAB_LIGHT, SLAB, BASELINE_LIGHT, UPPER_GRAPH]
    guard_hatches = ["////", "", "", "////", ""]
    bars = ax.bar(xg, guards["region_gb"], 0.62, color=guard_colors, edgecolor=INK, linewidth=0.75)
    for bar, hatch, value in zip(bars, guard_hatches, guards["region_gb"]):
        bar.set_hatch(hatch)
        ax.text(bar.get_x() + bar.get_width() / 2, value * 1.08, f"{value:.2g}", ha="center", va="bottom", fontsize=6.1)
    ax.axhline(12.0, color=RDMA, linestyle="--", linewidth=0.75)
    ax.set_yscale("log")
    ax.set_ylabel(label("Sidecar GB (log)"))
    ax.set_xticks(xg)
    ax.set_xticklabels(guard_labels)
    ax.set_ylim(1.7, 42)
    ax.set_yticks([2, 4, 8, 12, 32])
    ax.set_yticklabels(["2", "4", "8", "12", "32"])
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(f)")

    fig.subplots_adjust(left=0.06, right=0.995, top=0.96, bottom=0.12)
    save_paper_figure(
        fig,
        out_dir / "eval_scaling_ablation_compact.pdf",
        out_dir / "eval_scaling_ablation_compact.png",
    )


def generate_eval_mechanism_controls(root: Path) -> None:
    """Direct controls for resident upper navigation and MN striping."""
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    upper = pd.read_csv(
        root / "results" / "sigmetrics_main_figures" / "q2_resident_upper_graph.csv"
    )
    stripe = pd.read_csv(
        root / "results" / "sigmetrics_main_figures" / "q3_shine_scaleout.csv"
    )
    sweep = upper[upper["panel"] == "ef_sweep"].copy()
    scale = upper[upper["panel"] == "cn_scale"].copy()
    modes = ["Slabs", "+upper graph"]
    efs = [50, 100, 200]

    fig = plt.figure(figsize=(7.15, 3.38))
    grid = fig.add_gridspec(2, 2, hspace=0.70, wspace=0.34)
    axes = [fig.add_subplot(grid[i, j]) for i in range(2) for j in range(2)]
    width = 0.34

    # (a) Exact resident-upper-graph toggle across the serving-width range.
    ax = axes[0]
    x = np.arange(len(efs))
    bars_by_mode: dict[str, object] = {}
    for idx, mode in enumerate(modes):
        rows = sweep[sweep["mode"] == mode].set_index("ef").loc[efs]
        bars = ax.bar(
            x + (idx - 0.5) * width,
            rows["qps"] / 1000.0,
            width,
            yerr=rows["qps_sd"] / 1000.0,
            capsize=2.0,
            label=label(mode),
            color=SLAB_LIGHT if mode == "Slabs" else UPPER_GRAPH,
            edgecolor=INK,
            linewidth=0.75,
            hatch="////" if mode == "Slabs" else "",
            error_kw={"linewidth": 0.7, "capthick": 0.7},
        )
        bars_by_mode[mode] = bars
    off = sweep[sweep["mode"] == "Slabs"].set_index("ef")
    on = sweep[sweep["mode"] == "+upper graph"].set_index("ef")
    for pos, ef in enumerate(efs):
        gain = 100.0 * (float(on.loc[ef, "qps"]) / float(off.loc[ef, "qps"]) - 1.0)
        ax.text(
            pos + width / 2,
            float(on.loc[ef, "qps"]) / 1000.0 + 0.17,
            f"+{gain:.1f}%",
            ha="center",
            va="bottom",
            fontsize=6.4,
            color=UPPER_GRAPH,
        )
    ax.set_ylabel(label("QPS (K)"))
    ax.set_xlabel("搜索宽度 ef" if CN_FIG else "Search width ef")
    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in efs])
    ax.set_ylim(0, 5.55)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="upper right", fontsize=6.2, handlelength=1.1)
    annotate_panel_outside(ax, "(a)")

    # (b) The off path is exactly the local path plus a fixed remote descent D.
    ax = axes[1]
    off_rows = off.loc[efs]
    on_rows = on.loc[efs]
    off_x = x - width / 2
    on_x = x + width / 2
    ax.bar(
        off_x,
        on_rows["posts_per_query"],
        width,
        color=SLAB_LIGHT,
        edgecolor=INK,
        linewidth=0.75,
        hatch="////",
        label=label("level-0 path"),
    )
    ax.bar(
        off_x,
        off_rows["upper_posts_per_query"],
        width,
        bottom=on_rows["posts_per_query"],
        color=UPPER_GRAPH_LIGHT,
        edgecolor=INK,
        linewidth=0.75,
        label=label("remote upper D"),
    )
    ax.bar(
        on_x,
        on_rows["posts_per_query"],
        width,
        color=UPPER_GRAPH,
        edgecolor=INK,
        linewidth=0.75,
        label=label("+upper graph"),
    )
    ax.text(
        0.99,
        1.025,
        r"$D=122.3$ 次/查询" if CN_FIG else r"fixed $D=122.3$ posts/query",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=6.6,
        color=UPPER_GRAPH,
        clip_on=False,
    )
    ax.set_ylabel("RDMA posts/查询" if CN_FIG else "RDMA posts/query")
    ax.set_xlabel("搜索宽度 ef" if CN_FIG else "Search width ef")
    ax.set_xticks(x)
    ax.set_xticklabels([str(v) for v in efs])
    ax.set_ylim(0, 470)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(
        ax,
        loc="upper left",
        bbox_to_anchor=(0.09, 1.0),
        fontsize=6.3,
        handlelength=1.0,
        ncol=1,
    )
    annotate_panel_outside(ax, "(b)")

    # (c) Per-CN resident state preserves multi-CN scaling.
    ax = axes[2]
    cn_counts = [1, 3]
    xc = np.arange(len(cn_counts))
    for idx, mode in enumerate(modes):
        rows = scale[scale["mode"] == mode].set_index("cn_count").loc[cn_counts]
        ax.bar(
            xc + (idx - 0.5) * width,
            rows["qps"] / 1000.0,
            width,
            yerr=rows["qps_sd"] / 1000.0,
            capsize=2.0,
            label=label(mode),
            color=SLAB_LIGHT if mode == "Slabs" else UPPER_GRAPH,
            edgecolor=INK,
            linewidth=0.75,
            hatch="////" if mode == "Slabs" else "",
            error_kw={"linewidth": 0.7, "capthick": 0.7},
        )
    ax.text(
        0.03,
        0.67,
        "常驻状态：32 MB/CN" if CN_FIG else "resident state: 32 MB/CN",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=6.5,
        color=UPPER_GRAPH,
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.4, "alpha": 0.88},
    )
    ax.text(1.0, 7.65, r"$2.98\times$", ha="center", va="bottom", fontsize=6.6)
    ax.set_ylabel("汇总 QPS（K）" if CN_FIG else "Aggregate QPS (K)")
    ax.set_xticks(xc)
    ax.set_xticklabels([label("1 CN"), label("3 CN")])
    ax.set_ylim(0, 8.35)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(
        ax,
        loc="upper left",
        bbox_to_anchor=(0.09, 1.0),
        fontsize=6.2,
        handlelength=1.1,
    )
    annotate_panel_outside(ax, "(c)")

    # (d) The same GIST layout is served from one or three passive MNs.
    ax = axes[3]
    stripe = stripe[stripe["label"].isin(["Aqr", "SW sq8", "SW+R"])].copy()
    order = ["Aqr", "SW sq8", "SW+R"]
    stripe["order"] = stripe["label"].map({name: idx for idx, name in enumerate(order)})
    stripe = stripe.sort_values("order")
    xs = np.arange(len(stripe))
    ax.bar(
        xs - width / 2,
        stripe["qps_1mn"] / 1000.0,
        width,
        label=label("1 MN"),
        color=SHINE_LIGHT,
        edgecolor=INK,
        linewidth=0.75,
        hatch="////",
    )
    bars = ax.bar(
        xs + width / 2,
        stripe["qps_3mn"] / 1000.0,
        width,
        label=label("3 MN"),
        color=SLAB,
        edgecolor=INK,
        linewidth=0.75,
    )
    for bar, ratio in zip(bars, stripe["scale"]):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.16,
            times_label(float(ratio), 2),
            ha="center",
            va="bottom",
            fontsize=6.5,
        )
    ax.set_ylabel("汇总 QPS（K）" if CN_FIG else "Aggregate QPS (K)")
    ax.set_xlabel("GIST，2 CN，ef=300" if CN_FIG else "GIST, 2 CN, ef=300")
    ax.set_xticks(xs)
    ax.set_xticklabels([label(v) for v in stripe["label"]])
    ax.set_ylim(0, 7.35)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(
        ax,
        loc="upper left",
        bbox_to_anchor=(0.09, 1.0),
        fontsize=6.2,
        handlelength=1.1,
    )
    annotate_panel_outside(ax, "(d)")

    fig.subplots_adjust(left=0.065, right=0.995, top=0.94, bottom=0.13)
    save_paper_figure(
        fig,
        out_dir / "eval_mechanism_controls.pdf",
        out_dir / "eval_mechanism_controls.png",
    )


def generate_eval_boundary_compact(root: Path) -> None:
    """Compact endpoint/boundary figure: partition costs and refresh boundary."""
    out_dir = figure_out_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    endpoint = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q4_dhnsw_endpoint.csv")
    plateau = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q4_dhnsw_plateau.csv")
    rebuild = pd.read_csv(root / "results" / "sigmetrics_main_figures" / "q5_rebuild_maintenance.csv").sort_values("batch_inserts")

    fig = plt.figure(figsize=(7.15, 3.60))
    grid = fig.add_gridspec(2, 3, height_ratios=[1.0, 1.0], hspace=0.72, wspace=0.50)
    axes = [fig.add_subplot(grid[i, j]) for i in range(2) for j in range(3)]
    datasets = ["SIFT", "GIST"]
    x = np.arange(len(datasets))
    width = 0.32

    def values(metric: str, system: str) -> list[float]:
        return [
            float(endpoint[(endpoint["metric"] == metric) & (endpoint["dataset"] == dataset) & (endpoint["system"] == system)]["value"].iloc[0])
            for dataset in datasets
        ]

    def labels(metric: str, system: str) -> list[str]:
        return [
            str(endpoint[(endpoint["metric"] == metric) & (endpoint["dataset"] == dataset) & (endpoint["system"] == system)]["label"].iloc[0])
            for dataset in datasets
        ]

    # (a) d-HNSW router-coverage plateau.
    ax = axes[0]
    for dataset, marker, color in [("SIFT", "s", SLAB), ("GIST", "o", MEMORY)]:
        cur = plateau[plateau["dataset"] == dataset].sort_values("ef")
        ax.plot(cur["ef"], cur["dhnsw_recall"], marker=marker, markersize=3.8, linewidth=1.05, color=color, label=label(f"d-HNSW {dataset}"))
        ax.axhline(float(cur["slabwalk_recall"].iloc[0]), color=color, linestyle="--", linewidth=0.85, label=label(f"SW {dataset}"))
    ax.set_xlabel(label("d-HNSW sub-search") + r" $\mathit{ef}$")
    ax.set_ylabel("recall@10")
    ax.set_xlim(42, 206)
    ax.set_ylim(0.72, 0.95)
    ax.set_xticks([48, 96, 128, 200])
    ax.set_axisbelow(True)
    ax.grid(True, axis="both", linestyle=":", linewidth=0.5, color=GRID)
    paper_legend(ax, loc="center right", fontsize=6.2, handlelength=1.0, labelspacing=0.25)
    annotate_panel_outside(ax, "(a)")

    # (b) Partition deserialization.
    ax = axes[1]
    deser = values("deserialize_ms", "d-HNSW")
    bars = ax.bar(x, deser, 0.55, color=DHNSW_LIGHT, edgecolor=INK, linewidth=0.75, hatch="////")
    for bar, lab in zip(bars, labels("deserialize_ms", "d-HNSW")):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.18, f"{lab} ms", ha="center", va="bottom", fontsize=6.4)
    ax.text(
        0.50,
        0.064,
        "SW：0" if CN_FIG else "SW: 0",
        ha="center",
        va="bottom",
        fontsize=6.2,
        color=SLAB,
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.25, "alpha": 0.9},
    )
    ax.set_yscale("log")
    ax.set_ylabel(label("Deserialize (ms, log)"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0.045, 45)
    ax.set_yticks([0.1, 1, 10])
    ax.set_yticklabels(["0.1", "1", "10"])
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(b)")

    # (c) Search-side state.
    ax = axes[2]
    for offset, system, face, hatch in [(-width / 2, "d-HNSW", DHNSW_LIGHT, "////"), (width / 2, "SlabWalk", SLAB, "")]:
        bars = ax.bar(x + offset, values("search_state_gb", system), width, label=label(system), facecolor=face, edgecolor=INK, linewidth=0.75, hatch=hatch)
        for bar, lab in zip(bars, labels("search_state_gb", system)):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.22, lab, ha="center", va="bottom", fontsize=6.1)
    ax.set_yscale("log")
    ax.set_ylabel(label("Search state (GB, log)"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0.03, 40)
    ax.set_yticks([0.1, 1, 10])
    ax.set_yticklabels(["0.1", "1", "10"])
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="y", linestyle=":", linewidth=0.5, color=GRID)
    system_handles, system_labels = ax.get_legend_handles_labels()
    annotate_panel_outside(ax, "(c)")

    # (d) MN payload/sidecar.
    ax = axes[3]
    for offset, system, face, hatch in [(-width / 2, "d-HNSW", DHNSW_LIGHT, "////"), (width / 2, "SlabWalk", SLAB, "")]:
        bars = ax.bar(x + offset, values("mn_bytes_gb", system), width, label=label(system), facecolor=face, edgecolor=INK, linewidth=0.75, hatch=hatch)
        for bar, lab in zip(bars, labels("mn_bytes_gb", system)):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.45, lab, ha="center", va="bottom", fontsize=6.1)
    ax.set_ylabel(label("MN bytes (GB)"))
    ax.set_xticks(x)
    ax.set_xticklabels(datasets)
    ax.set_ylim(0, 18.5)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(d)")

    # (e) Maintenance write amplification.
    ax = axes[4]
    xb = rebuild["batch_inserts"].to_numpy(dtype=float)
    ax.plot(xb, rebuild["write_amp_blocks_per_insert"], marker="s", markersize=4.0, linewidth=1.1, color=SLAB, markeredgecolor=INK)
    ax.axhline(32, color=MUTED, linewidth=0.75, linestyle="--")
    ax.axhline(19.14, color=DHNSW, linewidth=0.75, linestyle=":")
    for xv, yv in zip(xb, rebuild["write_amp_blocks_per_insert"]):
        ax.text(xv, yv + 0.9, f"{yv:.1f}", ha="center", va="bottom", fontsize=6.3)
    ax.set_xscale("log")
    ax.set_xticks([1000, 10000, 100000])
    ax.set_xticklabels(["1K", "10K", "100K"])
    ax.set_ylabel(label("Blocks / insert"))
    ax.set_xlabel(label("Insert batch K"))
    ax.set_ylim(0, 35)
    ax.set_axisbelow(True)
    ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(e)")

    # (f) Maintenance differential read.
    ax = axes[5]
    diff = rebuild.dropna(subset=["diff_read_frac"]).copy()
    xd = diff["batch_inserts"].to_numpy(dtype=float)
    pct = diff["diff_read_frac"].to_numpy(dtype=float) * 100.0
    bars = ax.bar(xd, pct, width=xd * 0.28, color=SLAB_LIGHT, edgecolor=INK, linewidth=0.75, hatch="////")
    ax.axhline(100, color=MUTED, linewidth=0.75, linestyle="--")
    for bar, val in zip(bars, pct):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 3.2, f"{val:.1f}%", ha="center", va="bottom", fontsize=6.3)
    ax.set_xscale("log")
    ax.set_xticks(xd)
    ax.set_xticklabels(["1K", "10K", "100K"])
    ax.set_ylabel(label("Diff read / full"))
    ax.set_xlabel(label("Insert batch K"))
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel_outside(ax, "(f)")

    fig.legend(
        system_handles,
        system_labels,
        loc="upper right",
        ncol=2,
        frameon=False,
        bbox_to_anchor=(0.995, 1.01),
        fontsize=6.3,
        handlelength=1.0,
        columnspacing=1.0,
    )
    fig.subplots_adjust(left=0.06, right=0.995, top=0.90, bottom=0.12)
    save_paper_figure(
        fig,
        out_dir / "eval_boundary_compact.pdf",
        out_dir / "eval_boundary_compact.png",
    )


def main() -> None:
    root = repo_root()
    set_style()
    generate_q0_cntime(root)
    generate_q0_competitor_endpoints(root)
    generate_q1_omega(root)
    generate_q1_tail_trace(root)
    generate_q1_ceiling(root)
    generate_q1_deep_scale(root)
    generate_q2_budget_pareto(root)
    generate_q3_shine_scaleout(root)
    generate_q4_dhnsw_endpoint(root)
    generate_q5_rebuild_maintenance(root)
    generate_eval_frontier_curves(root)
    generate_eval_scaling_ablation_compact(root)
    generate_eval_mechanism_controls(root)
    generate_eval_boundary_compact(root)
    print("wrote paper_sigmetrics/figs/q0_cntime.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q0_competitor_endpoints.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q1_omega_collapse.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q1_tail_trace.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q1_ceiling.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q1_deep_scale.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q2_budget_pareto.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q3_shine_scaleout.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q4_dhnsw_endpoint.{pdf,png}")
    print("wrote paper_sigmetrics/figs/q5_rebuild_maintenance.{pdf,png}")
    if (root / "results" / "sigmetrics_main_figures" / "q0_frontier_curves.csv").exists():
        print("wrote paper_sigmetrics/figs/eval_frontier_curves.{pdf,png}")
    print("wrote paper_sigmetrics/figs/eval_scaling_ablation_compact.{pdf,png}")
    print("wrote paper_sigmetrics/figs/eval_mechanism_controls.{pdf,png}")
    print("wrote paper_sigmetrics/figs/eval_boundary_compact.{pdf,png}")


if __name__ == "__main__":
    main()
