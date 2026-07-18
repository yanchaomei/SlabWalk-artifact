#!/usr/bin/env python3
"""Summarize GB_QUERY_TRACE CSV files for SIGMETRICS evaluation checks."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean


PERCENTILES = (50, 90, 95, 99, 99.9)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    rank = (p / 100.0) * (len(values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - rank) + values[hi] * (rank - lo)


def load_numeric_rows(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            numeric: dict[str, float] = {}
            for key, value in row.items():
                if value is None or value == "":
                    continue
                try:
                    numeric[key] = float(value)
                except ValueError:
                    continue
            if numeric:
                rows.append(numeric)
    return rows


def summarize(path: Path) -> dict[str, object]:
    rows = load_numeric_rows(path)
    lat_us = [r["latency_ns"] / 1000.0 for r in rows if "latency_ns" in r]
    posts = [r["query_rdma_posts"] for r in rows if "query_rdma_posts" in r]
    wrs = [
        r.get("query_rdma_wrs", r["query_rdma_posts"])
        for r in rows
        if "query_rdma_posts" in r
    ]
    single_posts = [r["query_single_posts"] for r in rows if "query_single_posts" in r]
    cwc_posts = [r["query_cwc_post_share"] for r in rows if "query_cwc_post_share" in r]
    phase_cols = [
        "query_phase_upnav",
        "query_phase_l0",
        "query_phase_rerank",
    ]

    out: dict[str, object] = {
        "file": str(path),
        "queries": len(rows),
        "latency_us": {
            "mean": mean(lat_us) if lat_us else math.nan,
            **{f"p{str(p).replace('.', '_')}": percentile(lat_us, p) for p in PERCENTILES},
        },
        "posts": {
            "mean": mean(posts) if posts else math.nan,
            **{f"p{str(p).replace('.', '_')}": percentile(posts, p) for p in PERCENTILES},
        },
        "wrs": {
            "mean": mean(wrs) if wrs else math.nan,
            **{f"p{str(p).replace('.', '_')}": percentile(wrs, p) for p in PERCENTILES},
        },
        "single_posts_mean": mean(single_posts) if single_posts else math.nan,
        "cwc_post_share_mean": mean(cwc_posts) if cwc_posts else math.nan,
        "phase_posts_mean": {
            col: mean([r[col] for r in rows if col in r])
            for col in phase_cols
            if any(col in r for r in rows)
        },
    }
    return out


def fmt(value: object, digits: int = 2) -> str:
    if isinstance(value, (int, float)):
        if math.isnan(value):
            return "NA"
        return f"{value:.{digits}f}"
    return str(value)


def write_markdown(summaries: list[dict[str, object]], path: Path) -> None:
    lines = [
        "# Query Trace Summary",
        "",
        "| trace | queries | latency p50/us | latency p99/us | submits mean | WRs mean | WRs p99 | upnav/l0/rerank WR mean |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for summary in summaries:
        lat = summary["latency_us"]
        posts = summary["posts"]
        wrs = summary["wrs"]
        phases = summary.get("phase_posts_mean", {})
        phase_text = " / ".join(
            fmt(phases.get(k, math.nan), 1)
            for k in ("query_phase_upnav", "query_phase_l0", "query_phase_rerank")
        )
        lines.append(
            "| {trace} | {queries} | {p50} | {p99} | {pm} | {wm} | {wp99} | {phases} |".format(
                trace=Path(str(summary["file"])).name,
                queries=summary["queries"],
                p50=fmt(lat["p50"]),
                p99=fmt(lat["p99"]),
                pm=fmt(posts["mean"], 1),
                wm=fmt(wrs["mean"], 1),
                wp99=fmt(wrs["p99"], 1),
                phases=phase_text,
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    summaries = [summarize(path) for path in args.traces]
    if args.json_out:
        args.json_out.write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
    if args.md_out:
        write_markdown(summaries, args.md_out)
    if not args.json_out and not args.md_out:
        print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
