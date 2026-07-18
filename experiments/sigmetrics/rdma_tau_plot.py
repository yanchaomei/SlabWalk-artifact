#!/usr/bin/env python3
"""Plot the controlled RDMA-read microbenchmark."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

PDF_METADATA = {"CreationDate": datetime(2026, 7, 7, tzinfo=timezone.utc)}

INK = "#23313F"
MUTED = "#6B7280"
GRID = "#D4DAE3"
SLAB = "#0D6EB5"
SLAB_LIGHT = "#DCECF8"
SHINE = "#1D7A74"
SHINE_LIGHT = "#D9EEEC"
DHNSW = "#F47A13"
DHNSW_LIGHT = "#FFE3C2"
RDMA = "#C0504D"
MEMORY = "#5B3F8F"
MEMORY_LIGHT = "#E7DCF3"

CN_FIG = os.environ.get("SLABWALK_FIG_LANG", "").lower() in {"cn", "zh", "chinese"}

CN_LABELS = {
    "avg": "平均",
    "P99": "P99",
    "RDMA read payload (B)": "RDMA 读取 payload (B)",
    "Latency (us)": "延迟 (us)",
    "Queue pairs": "QP 数量",
    "RDMA reads (Mops/s)": "RDMA 读取 (Mops/s)",
    "SlabWalk ceiling band": "SlabWalk 上限带",
    "CQ mod": "CQ 合并",
    "Outstanding reads": "未完成读取数",
    "Mops/s": "Mops/s",
    "Latency / default": "延迟 / 默认",
    "MTU / NUMA": "MTU / NUMA",
}


def label(text: object) -> str:
    s = str(text)
    return CN_LABELS.get(s, s) if CN_FIG else s


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
            "font.size": 8,
            "axes.labelsize": 8,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.fontsize": 7,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "axes.edgecolor": INK,
            "axes.labelcolor": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": INK,
            "ytick.color": INK,
            "hatch.color": INK,
        }
    )


def medians(df: pd.DataFrame) -> pd.DataFrame:
    numeric = [
        "size",
        "mtu",
        "outs",
        "qps",
        "cq_mod",
        "client_numa",
        "server_numa",
        "avg_us",
        "p99_us",
        "p999_us",
        "stdev_us",
        "bw_avg_gbps",
        "msg_rate_mpps",
    ]
    for col in numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    keys = ["sweep", "label", "tool", "size", "mtu", "outs", "qps", "cq_mod", "client_numa", "server_numa"]
    return df.groupby(keys, dropna=False, as_index=False)[numeric].median()


def annotate_panel(ax: plt.Axes, label: str) -> None:
    ax.text(0.02, 0.98, label, transform=ax.transAxes, ha="left", va="top", fontsize=8, color=INK)


def plot(df: pd.DataFrame, out_pdf: Path, out_png: Path) -> None:
    set_style()
    summary = medians(df)
    fig, axes = plt.subplots(
        1,
        4,
        figsize=(7.15, 2.05),
        gridspec_kw={"width_ratios": [1.18, 1.08, 0.72, 0.90]},
    )

    ax = axes[0]
    payload = summary[summary["sweep"] == "payload_latency"].sort_values("size")
    ax.plot(
        payload["size"],
        payload["avg_us"],
        marker="s",
        markersize=4,
        linewidth=1.15,
        color=SLAB,
        label=label("avg"),
    )
    ax.plot(
        payload["size"],
        payload["p99_us"],
        marker="o",
        markersize=3.6,
        linewidth=1.0,
        color=RDMA,
        linestyle="--",
        label=label("P99"),
    )
    ax.axvspan(64, 4096, facecolor=SLAB_LIGHT, edgecolor="none", zorder=0, alpha=0.65)
    ax.set_xscale("log", base=2)
    ax.set_xticks([64, 256, 1024, 4096, 16384])
    ax.set_xticklabels(["64", "256", "1K", "4K", "16K"])
    ax.set_xlabel(label("RDMA read payload (B)"))
    ax.set_ylabel(label("Latency (us)"))
    ax.grid(True, which="both", axis="both", linestyle=":", linewidth=0.5, color=GRID)
    ax.legend(frameon=False, loc="upper left")
    annotate_panel(ax, "(a)")

    ax = axes[1]
    qp = summary[summary["sweep"] == "qp_cq_msg_rate"].sort_values(["cq_mod", "qps"])
    for cq_mod, cur in qp.groupby("cq_mod"):
        ax.plot(
            cur["qps"],
            cur["msg_rate_mpps"],
            marker="s" if cq_mod == 1 else "o",
            markersize=4,
            linewidth=1.15,
            linestyle="-" if cq_mod == 1 else "--",
            color=SLAB if cq_mod == 1 else MEMORY,
            label=f"{label('CQ mod')} {int(cq_mod)}",
        )
    ax.axhspan(3.5, 4.0, facecolor=SLAB_LIGHT, edgecolor="none", zorder=0, alpha=0.65)
    ax.text(2.25, 3.55, label("SlabWalk ceiling band"), fontsize=6.6, color=SLAB, ha="left", va="top")
    ax.set_xlabel(label("Queue pairs"))
    ax.set_ylabel(label("RDMA reads (Mops/s)"))
    ax.set_xticks([1, 2, 4, 8])
    ax.set_ylim(0, max(4.6, qp["msg_rate_mpps"].max() * 1.18))
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    ax.legend(frameon=False, loc="lower right", handlelength=1.4)
    annotate_panel(ax, "(b)")

    ax = axes[2]
    controls = summary[summary["sweep"].isin(["mtu_latency", "numa_latency", "outs_msg_rate"])].copy()
    mtu = controls[controls["sweep"] == "mtu_latency"].sort_values("mtu")
    numa = controls[controls["sweep"] == "numa_latency"].sort_values("client_numa")
    outs = controls[controls["sweep"] == "outs_msg_rate"].sort_values("outs")
    ax.plot(
        outs["outs"],
        outs["msg_rate_mpps"],
        marker="o",
        markersize=4,
        linewidth=1.15,
        color=SLAB,
    )
    ax.set_xscale("log", base=4)
    ax.set_xticks([1, 4, 16])
    ax.set_xticklabels(["1", "4", "16"])
    ax.set_xlabel(label("Outstanding reads"))
    ax.set_ylabel(label("Mops/s"))
    ax.set_ylim(0, max(4.5, outs["msg_rate_mpps"].max() * 1.20))
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(c)")

    ax = axes[3]
    default_lat = float(mtu[mtu["mtu"] == 4096]["avg_us"].iloc[0])
    labels = [str(int(x)) for x in mtu["mtu"]] + [f"N{int(x)}" for x in numa["client_numa"]]
    values = list(mtu["avg_us"] / default_lat) + list(numa["avg_us"] / default_lat)
    x = range(len(labels))
    bars = ax.bar(
        list(x),
        values,
        width=0.58,
        edgecolor=INK,
        linewidth=0.8,
    )
    faces = [DHNSW_LIGHT, SHINE_LIGHT, SLAB, DHNSW_LIGHT, MEMORY]
    hatches = ["////", "", "", "////", ""]
    for bar, face, hatch in zip(bars, faces, hatches):
        bar.set_facecolor(face)
        bar.set_hatch(hatch)
    ax.axhline(1.0, color=INK, linewidth=0.7)
    ax.set_ylabel(label("Latency / default"))
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_xlabel(label("MTU / NUMA"))
    ax.set_ylim(0.88, max(1.18, max(values) * 1.07))
    ax.grid(True, axis="y", linestyle=":", linewidth=0.5, color=GRID)
    annotate_panel(ax, "(d)")

    fig.tight_layout(pad=0.2, w_pad=0.8)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_pdf, bbox_inches="tight", pad_inches=0.01, metadata=PDF_METADATA)
    fig.savefig(out_png, bbox_inches="tight", pad_inches=0.01, dpi=260)


def write_summary(df: pd.DataFrame, out_path: Path) -> None:
    summary = medians(df)
    payload = summary[summary["sweep"] == "payload_latency"].sort_values("size")
    sub_mtu = payload[payload["size"] <= 4096]
    qp = summary[summary["sweep"] == "qp_cq_msg_rate"].sort_values(["qps", "cq_mod"])
    mtu = summary[summary["sweep"] == "mtu_latency"].sort_values("mtu")
    numa = summary[summary["sweep"] == "numa_latency"].sort_values("client_numa")
    outs = summary[summary["sweep"] == "outs_msg_rate"].sort_values("outs")

    lines = [
        "# RDMA tau microbenchmark summary",
        "",
        "Source: `ib_read_lat` / `ib_read_bw` from one initiator CN to one passive MN over a ConnectX-6 DX RoCEv2 link; host aliases and device identifiers remain in the raw CSV.",
        "",
    ]
    if not sub_mtu.empty:
        lo = sub_mtu["avg_us"].min()
        hi = sub_mtu["avg_us"].max()
        lines.append(f"- 64B--4KB payload average latency range: {lo:.2f}--{hi:.2f} us ({hi / lo:.2f}x).")
    if not payload.empty:
        p99_lo = sub_mtu["p99_us"].min()
        p99_hi = sub_mtu["p99_us"].max()
        lines.append(f"- 64B--4KB P99 latency range: {p99_lo:.2f}--{p99_hi:.2f} us ({p99_hi / p99_lo:.2f}x).")
    if not qp.empty:
        one = qp[(qp["qps"] == 1) & (qp["cq_mod"] == 1)]["msg_rate_mpps"].median()
        max_rate = qp["msg_rate_mpps"].max()
        lines.append(f"- Single-QP/CQ1 256B read rate: {one:.2f} Mops/s; best QP/CQ point: {max_rate:.2f} Mops/s.")
    if not mtu.empty:
        spread = mtu["avg_us"].max() / mtu["avg_us"].min()
        lines.append(f"- MTU 1024--4096 average-latency spread: {spread:.2f}x.")
    if not numa.empty:
        spread = numa["avg_us"].max() / numa["avg_us"].min()
        lines.append(f"- Same-node NUMA placement average-latency spread: {spread:.2f}x.")
    if not outs.empty:
        lines.append(
            "- Outstanding-read sweep Mops/s: "
            + ", ".join(f"outs={int(row.outs)}: {row.msg_rate_mpps:.2f}" for row in outs.itertuples())
            + "."
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=root / "results" / "sigmetrics_rdma_tau_microbench" / "rdma_tau_raw.csv")
    parser.add_argument("--out-pdf", type=Path, default=root / "paper_sigmetrics" / "figs" / "q1_rdma_tau_microbench.pdf")
    parser.add_argument("--out-png", type=Path, default=root / "paper_sigmetrics" / "figs" / "q1_rdma_tau_microbench.png")
    parser.add_argument("--summary", type=Path, default=root / "results" / "sigmetrics_rdma_tau_microbench" / "summary.md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = pd.read_csv(args.csv)
    plot(df, args.out_pdf, args.out_png)
    write_summary(df, args.summary)
    print(f"wrote {args.out_pdf}")
    print(f"wrote {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
