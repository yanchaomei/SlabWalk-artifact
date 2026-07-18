#!/usr/bin/env python3
"""Generate reviewer-facing boundary-control summaries."""

from __future__ import annotations

import csv
import json
from pathlib import Path


TTI_INPUTS = [
    ("fp32 baseline", "mx_tti1m_base_ef_uniform_T1_ef300_r0.json", "authoritative high-recall point"),
    ("sq8 Slabs", "mx_tti1m_lavd_ef_uniform_T1_ef300_r0.json", "operation collapse, recall boundary"),
    ("sq8 Slabs+upper graph", "mx_tti1m_crane_ef_uniform_T1_ef300_r0.json", "upper navigation removed"),
    ("RaBitQ-2 Slabs", "mx_tti1m_rabitq2_uniform_T1_ef300_r0.json", "fewer bytes, coarse IP code"),
    ("RaBitQ-4 Slabs", "mx_tti1m_rabitq4_uniform_T1_ef300_r0.json", "more bits, still below fp32 recall"),
    ("fp32 baseline 16T", "mx_tti1m_baseline_uniform_T16_ef300_r0.json", "throughput point"),
    ("sq8 Slabs 16T", "mx_tti1m_lavd_uniform_T16_ef300_r0.json", "throughput point"),
    ("sq8 Slabs+upper graph 16T", "mx_tti1m_lavd_crane_uniform_T16_ef300_r0.json", "throughput point"),
]

DHNSW_GIST = {
    "dataset": "GIST-1M",
    "current_qps": 350.7,
    "current_latency_ms": 40.9177,
    "current_deserialize_ms": 13.2797,
    "slabwalk_qps": 2294.0,
    "dhnsw_recall": 0.766766,
    "slabwalk_recall": 0.9257,
    "dhnsw_search_state_gb": 16.5,
    "slabwalk_search_state_gb": 0.078,
}

MATERIALIZATION_INPUTS = [
    (
        "SIFT-1M",
        "sq8",
        "results/sigmetrics_trace_20260706_quick_212028/sift1m_slabwalk_1t_ef48.json",
        128,
        "main SIFT mechanism-depth trace",
    ),
    (
        "DEEP-1M",
        "sq8",
        "results/sigmetrics_trace_20260706_deep1m_215346/deep1m_slabwalk_1t_ef80.json",
        96,
        "main DEEP trace",
    ),
    (
        "TTI-1M",
        "sq8",
        "results/sigmetrics_tti_boundary/raw/mx_tti1m_lavd_ef_uniform_T1_ef300_r0.json",
        200,
        "inner-product boundary run",
    ),
    (
        "GIST-1M",
        "RaBitQ-2",
        "results/sigmetrics_trace_20260706_gist1m_cap36g_node3copy/gist1m_slabwalk_1t_ef400.json",
        960,
        "high-dimensional code run",
    ),
]


def code_payload_bytes(dim: int, code: str) -> int:
    if code == "sq8":
        return dim
    if code == "RaBitQ-2":
        return 8 + (dim * 2 + 7) // 8
    raise ValueError(f"unknown code {code}")


def block_stride(dim: int, code: str, m_max0: int = 32) -> int:
    return 8 + m_max0 * (16 + code_payload_bytes(dim, code))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def read_tti_rows(root: Path) -> list[dict[str, object]]:
    raw_dir = root / "results" / "sigmetrics_tti_boundary" / "raw"
    rows: list[dict[str, object]] = []
    for config, filename, note in TTI_INPUTS:
        path = raw_dir / filename
        with path.open() as f:
            data = json.load(f)
        queries = data["queries"]
        meta = data["meta"]
        processed = float(queries["processed"])
        rows.append(
            {
                "config": config,
                "threads": meta["compute_threads"],
                "ef": data["hnsw_parameters"]["ef_search"],
                "qps": queries["queries_per_sec"],
                "recall": queries["recall"],
                "posts_per_query": queries["rdma_posts"] / processed,
                "mb_per_query": queries["rdma_reads_in_bytes"] / processed / 1_000_000.0,
                "note": note,
                "source": filename,
            }
        )
    return rows


def dhnsw_sensitivity_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fixed_ms = DHNSW_GIST["current_latency_ms"] - DHNSW_GIST["current_deserialize_ms"]
    for assumed_deser_ms in (DHNSW_GIST["current_deserialize_ms"], 5.0, 0.0):
        adjusted_latency_ms = fixed_ms + assumed_deser_ms
        adjusted_qps = DHNSW_GIST["current_qps"] * DHNSW_GIST["current_latency_ms"] / adjusted_latency_ms
        rows.append(
            {
                "dataset": DHNSW_GIST["dataset"],
                "assumed_deserialize_ms": assumed_deser_ms,
                "latency_model_ms": adjusted_latency_ms,
                "upper_bound_qps": adjusted_qps,
                "slabwalk_qps": DHNSW_GIST["slabwalk_qps"],
                "slabwalk_qps_over_bound": DHNSW_GIST["slabwalk_qps"] / adjusted_qps,
                "dhnsw_recall": DHNSW_GIST["dhnsw_recall"],
                "slabwalk_recall": DHNSW_GIST["slabwalk_recall"],
                "dhnsw_search_state_gb": DHNSW_GIST["dhnsw_search_state_gb"],
                "slabwalk_search_state_gb": DHNSW_GIST["slabwalk_search_state_gb"],
            }
        )
    return rows


def materialization_rows(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for dataset, code, rel_path, dim, note in MATERIALIZATION_INPUTS:
        path = root / rel_path
        with path.open() as f:
            data = json.load(f)
        n_vectors = int(data["num_vectors"])
        stride = block_stride(dim, code)
        measured_sidecar_bytes = n_vectors * stride
        build_ms = float(data["timings"]["lavd_build"])
        rows.append(
            {
                "kind": "measured_sidecar_build",
                "item": f"{dataset} {code}",
                "n_vectors": n_vectors,
                "dim": dim,
                "code": code,
                "measured_build_s": build_ms / 1000.0,
                "bytes_at_measured_n": measured_sidecar_bytes,
                "gb_at_measured_n": measured_sidecar_bytes / 1e9,
                "gb_rule_at_100m": stride * 100_000_000 / 1e9,
                "note": note,
                "source": rel_path,
            }
        )

    n100 = 100_000_000
    # SlotBitmap stores one 16-byte epoch-tagged word per 64 slots and is
    # replicated per query coroutine.  The paper's 100M serving rule assumes
    # 12 workers x 8 coroutines on one CN.
    visited_bitmap_bytes = ((n100 + 63) // 64) * 16 * 12 * 8
    startup_controls = [
        ("varblock prefix table", 8 * (n100 + 1), "u64 offset per slot plus sentinel"),
        ("degree-budget compact map", 4 * n100, "u32 slot-to-compact index"),
        (
            "visited bitmaps (12 workers x 8 coroutines)",
            visited_bitmap_bytes,
            "16-byte epoch-tagged word per 64 slots, replicated per coroutine",
        ),
        (
            "tiered slot-code table f=0.05",
            1_240_000_000,
            "RaBitQ packed qvec table from implementation comment; rptr table is separate",
        ),
        ("slot-to-rptr table", 8 * n100, "u64 RemotePtr per slot for slot-only layout"),
    ]
    for item, bytes_value, note in startup_controls:
        rows.append(
            {
                "kind": "100m_startup_metadata_rule",
                "item": item,
                "n_vectors": n100,
                "dim": "",
                "code": "",
                "measured_build_s": "",
                "bytes_at_measured_n": bytes_value,
                "gb_at_measured_n": bytes_value / 1e9,
                "gb_rule_at_100m": bytes_value / 1e9,
                "note": note,
                "source": "graphbeyond/src/lavd/layout.hh and build.hh",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path}")
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: object, digits: int = 2) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_readme(
    path: Path,
    tti: list[dict[str, object]],
    dhnsw: list[dict[str, object]],
    materialization: list[dict[str, object]],
) -> None:
    base = next(row for row in tti if row["config"] == "fp32 baseline")
    sq8 = next(row for row in tti if row["config"] == "sq8 Slabs")
    rq2 = next(row for row in tti if row["config"] == "RaBitQ-2 Slabs")
    rq4 = next(row for row in tti if row["config"] == "RaBitQ-4 Slabs")

    text = f"""# Reviewer-Gap Boundary Controls

Generated by:

```bash
python3 experiments/sigmetrics/reviewer_gap_controls.py
```

## TTI inner-product code boundary

Source raw JSONs are in `results/sigmetrics_tti_boundary/raw/`, copied from
the SKV TTI-1M campaign (`/home/kvgroup/chaomei/hnsw-data/tti1m`).  These
runs are not used to add a new TTI win; they audit the high-recall boundary.

| config | threads | ef | QPS | R@10 | posts/q | MB/q | note |
|---|---:|---:|---:|---:|---:|---:|---|
"""
    for row in tti:
        text += (
            f"| {row['config']} | {row['threads']} | {row['ef']} | "
            f"{fmt(row['qps'], 0)} | {fmt(row['recall'], 5)} | "
            f"{fmt(row['posts_per_query'], 1)} | {fmt(row['mb_per_query'], 3)} | "
            f"{row['note']} |\n"
        )

    text += f"""
The measured high-recall TTI point is still the fp32 baseline
({fmt(base['recall'], 3)} R@10).  Sq8 Slabs collapse posts
({fmt(base['posts_per_query'], 0)} -> {fmt(sq8['posts_per_query'], 0)})
but drop recall to {fmt(sq8['recall'], 3)}.  RaBitQ-2/4 reduce bytes
({fmt(rq2['mb_per_query'], 2)}/{fmt(rq4['mb_per_query'], 2)} MB/q) but remain
below fp32 recall ({fmt(rq2['recall'], 3)}/{fmt(rq4['recall'], 3)}).  This
supports the paper's boundary claim: after operation count is removed, compact
code quality is the limiting resource on this inner-product workload.

The no-compression high-recall endpoint is the object-native fp32 baseline:
260 QPS at one thread, 0.961 R@10, 3870 posts/query, and 2.98 MB/query.
The current SlabWalk implementation exposes 4/8-bit scalar codes and RaBitQ
fanout codes, not an fp32 Slab code path; the paper therefore treats fp32 TTI
as a cost boundary rather than reporting an unimplemented Slab-fp32 result.

## d-HNSW deserialization sensitivity

This is a latency-proportional upper-bound calculation, not a new d-HNSW run.
Starting from the reproduced GIST-1M top-10 path (351 QPS, 40.9 ms/query, about
13.3 ms deserialize), it asks what the QPS upper bound would be if only the
deserialize component changed and all other path costs and batching stayed
fixed.

| assumed deserialize | modeled latency | d-HNSW QPS upper bound | SlabWalk QPS | SlabWalk / bound | d-HNSW R@10 | SlabWalk R@10 |
|---:|---:|---:|---:|---:|---:|---:|
"""
    for row in dhnsw:
        text += (
            f"| {fmt(row['assumed_deserialize_ms'], 0)} ms | "
            f"{fmt(row['latency_model_ms'], 1)} ms | "
            f"{fmt(row['upper_bound_qps'], 0)} | "
            f"{fmt(row['slabwalk_qps'], 0)} | "
            f"{fmt(row['slabwalk_qps_over_bound'], 2)}x | "
            f"{fmt(row['dhnsw_recall'], 3)} | {fmt(row['slabwalk_recall'], 3)} |\n"
        )

    text += """
The fair wording is therefore "against the release-based d-HNSW
partition-fetch path under the corrected top-10 protocol."  A better decoder
would reduce the GIST QPS gap, but even the ideal zero-deserialization bound
does not erase it; the router recall plateau and search-side state remain
separate costs.

## Materialization and startup accounting

This table is not a new 100M serving run.  It closes the accounting for the
derived sidecar path: measured 1M materialization times come from existing
SlabWalk runs, while the 100M rows are layout rules from the implementation.

| kind | item | measured build | bytes at measured N | 100M rule | note |
|---|---|---:|---:|---:|---|
"""
    for row in materialization:
        build_s = row["measured_build_s"]
        build_cell = "" if build_s == "" else f"{float(build_s):.1f} s"
        text += (
            f"| {row['kind']} | {row['item']} | {build_cell} | "
            f"{float(row['gb_at_measured_n']):.2f} GB | "
            f"{float(row['gb_rule_at_100m']):.2f} GB | {row['note']} |\n"
        )
    path.write_text(text.rstrip() + "\n")


def main() -> None:
    root = repo_root()
    out_dir = root / "results" / "sigmetrics_reviewer_gap_controls"
    out_dir.mkdir(parents=True, exist_ok=True)

    tti = read_tti_rows(root)
    dhnsw = dhnsw_sensitivity_rows()
    materialization = materialization_rows(root)
    write_csv(out_dir / "tti_boundary.csv", tti)
    write_csv(out_dir / "dhnsw_deser_sensitivity.csv", dhnsw)
    write_csv(out_dir / "materialization_accounting.csv", materialization)
    write_readme(out_dir / "README.md", tti, dhnsw, materialization)
    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
