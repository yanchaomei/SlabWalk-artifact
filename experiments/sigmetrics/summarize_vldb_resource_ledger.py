#!/usr/bin/env python3
"""Validate and summarize the VLDB physical-layout resource ledger."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


CELL_RE = re.compile(
    r"^(?P<dataset>[a-z0-9]+)_(?P<layout>legacy|fixed|variable)_s"
    r"(?P<mns>[135])_measure_r(?P<repeat>[0-9]+)$"
)
RSS_RE = re.compile(r"Maximum resident set size \(kbytes\):\s*([0-9]+)")
ACCOUNTING_RE = re.compile(r"^LAVD_PHYSICAL_ACCOUNTING (\{.*\})$", re.MULTILINE)
STAGED_READ_RE = re.compile(
    r"^\[LAVD\]\[multi\] MN ([0-9]+) staged-read ([0-9]+)B via [0-9]+B MR$",
    re.MULTILINE,
)
MANIFEST_REQUIRED_KEYS = {
    "tag",
    "layout",
    "memory_nodes",
    "hosts",
    "capacity_per_mn",
    "index_region_bytes",
    "binary_sha256",
    "cn_host",
    "started_utc",
    "layout_env",
    "build_threads",
    "build_cpu_base",
    "build_cpu_stride",
}
T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=0)
    parser.add_argument("--expected-layouts", default="legacy,fixed,variable")
    parser.add_argument("--expected-mn-counts", default="1,3,5")
    parser.add_argument("--require-latency", action="store_true")
    return parser.parse_args()


def parse_max_rss_kib(text: str) -> int:
    matches = RSS_RE.findall(text)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one peak RSS, found {len(matches)}")
    return int(matches[0])


def parse_accounting(text: str) -> list[dict[str, object]]:
    rows = [json.loads(match) for match in ACCOUNTING_RE.findall(text)]
    if not rows:
        raise ValueError("missing LAVD_PHYSICAL_ACCOUNTING records")
    return rows


def parse_staged_read_bytes(text: str, expected_mns: int) -> list[int]:
    rows = [(int(mn), int(size)) for mn, size in STAGED_READ_RE.findall(text)]
    if [mn for mn, _ in rows] != list(range(expected_mns)):
        raise ValueError(
            "LAVD staged-read records do not cover all MNs exactly once: "
            f"expected={list(range(expected_mns))} actual={[mn for mn, _ in rows]}"
        )
    if any(size <= 0 for _, size in rows):
        raise ValueError("LAVD staged-read extents must be positive")
    return [size for _, size in rows]


def parse_manifest_text(text: str, source: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"{source}:{lineno}: malformed manifest line")
        key, value = line.split("=", 1)
        if not key or not value or key in manifest:
            raise ValueError(f"{source}:{lineno}: invalid or duplicate manifest field {key!r}")
        manifest[key] = value
    missing = MANIFEST_REQUIRED_KEYS - set(manifest)
    if missing:
        raise ValueError(f"{source}: missing manifest fields {sorted(missing)}")
    return manifest


def stable_fingerprint(mapping: dict[str, object]) -> str:
    encoded = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def manifest_cell_fingerprint(manifest: dict[str, str]) -> str:
    return stable_fingerprint(
        {key: value for key, value in manifest.items() if key not in {"tag", "started_utc"}}
    )


def campaign_protocol_fingerprint(
    manifest: dict[str, str], meta: dict[str, object], data: dict[str, object]
) -> str:
    hnsw = data.get("hnsw_parameters")
    if not isinstance(hnsw, dict):
        raise ValueError("missing hnsw_parameters for campaign protocol")
    protocol = {
        "binary_sha256": manifest["binary_sha256"],
        "cn_host": manifest["cn_host"],
        "build_threads": manifest["build_threads"],
        "build_cpu_base": manifest["build_cpu_base"],
        "build_cpu_stride": manifest["build_cpu_stride"],
        "dataset": meta.get("dataset"),
        "query_suffix": meta.get("query_suffix"),
        "compute_threads": meta.get("compute_threads"),
        "coroutines_per_thread": meta.get("coroutines_per_thread"),
        "threads_pinned": meta.get("threads_pinned"),
        "hyperthreading": meta.get("hyperthreading"),
        "num_vectors": data.get("num_vectors"),
        "num_queries": data.get("num_queries"),
        "hnsw_parameters": hnsw,
    }
    if any(value is None for value in protocol.values()):
        raise ValueError("incomplete campaign protocol metadata")
    return stable_fingerprint(protocol)


def t_ci_half(values: Iterable[float]) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    critical = T95.get(len(vals) - 1, 1.960)
    return critical * statistics.stdev(vals) / math.sqrt(len(vals))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def required_number(mapping: dict[str, object], key: str, source: Path) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{source}: missing or invalid numeric field {key}")
    return float(value)


def required_one_of_numbers(
    mapping: dict[str, object], keys: tuple[str, ...], source: Path
) -> float:
    present = [key for key in keys if key in mapping]
    if len(present) != 1:
        raise ValueError(f"{source}: expected exactly one of {keys}, found {present}")
    return required_number(mapping, present[0], source)


def parse_query_latency(
    queries: dict[str, object], processed: int, required: bool, source: Path
) -> dict[str, object]:
    keys = (
        "local_latency_samples",
        "local_latency_p50_us",
        "local_latency_p95_us",
        "local_latency_p99_us",
    )
    present = [key for key in keys if key in queries]
    if not present and not required:
        return {}
    if len(present) != len(keys):
        raise ValueError(f"{source}: missing required query latency samples or quantiles")
    samples = int(required_number(queries, "local_latency_samples", source))
    p50 = required_number(queries, "local_latency_p50_us", source)
    p95 = required_number(queries, "local_latency_p95_us", source)
    p99 = required_number(queries, "local_latency_p99_us", source)
    if samples != processed or p50 < 0 or not p50 <= p95 <= p99:
        raise ValueError(f"{source}: invalid query latency samples or quantiles")
    return {
        "query_latency_samples": samples,
        "query_latency_p50_us": p50,
        "query_latency_p95_us": p95,
        "query_latency_p99_us": p99,
    }


def read_status(path: Path) -> None:
    if not path.is_file() or path.read_text().strip() != "0":
        raise ValueError(f"{path}: missing or nonzero memory-node status")


def parse_cell(
    cell: Path, match: re.Match[str], require_latency: bool = False
) -> tuple[dict[str, object], list[dict[str, object]]]:
    dataset = match.group("dataset")
    layout = match.group("layout")
    mns = int(match.group("mns"))
    repeat = int(match.group("repeat"))
    cn_json = cell / "cn.json"
    cn_err = cell / "cn.err"
    if not cn_json.is_file() or not cn_err.is_file():
        raise ValueError(f"{cell}: missing CN JSON/stderr pair")
    data = json.loads(cn_json.read_text())
    err_text = cn_err.read_text(errors="replace")
    manifest_path = cell / "manifest.txt"
    if not manifest_path.is_file():
        raise ValueError(f"{cell}: missing manifest.txt")
    manifest = parse_manifest_text(manifest_path.read_text(), manifest_path)
    meta = data.get("meta")
    queries = data.get("queries")
    timings = data.get("timings")
    if not isinstance(meta, dict) or not isinstance(queries, dict) or not isinstance(timings, dict):
        raise ValueError(f"{cn_json}: malformed meta/queries/timings")
    if int(meta.get("memory_nodes", -1)) != mns:
        raise ValueError(f"{cn_json}: memory-node count does not match directory")
    if str(meta.get("dataset", "")).lower() != dataset:
        raise ValueError(f"{cn_json}: dataset does not match directory")
    if manifest["layout"] != layout or int(manifest["memory_nodes"]) != mns:
        raise ValueError(f"{manifest_path}: layout or memory-node count does not match directory")
    if manifest["tag"] != cell.name:
        raise ValueError(f"{manifest_path}: tag does not match directory")

    accounts = sorted(parse_accounting(err_text), key=lambda row: int(row["mn"]))
    if [int(row["mn"]) for row in accounts] != list(range(mns)):
        raise ValueError(f"{cn_err}: physical accounts do not cover all MNs")
    expected_record_layout = {
        "legacy": "legacy_sparse_fixed",
        "fixed": "fixed",
        "variable": "variable",
    }[layout]
    if any(str(row.get("record_layout")) != expected_record_layout for row in accounts):
        raise ValueError(f"{cn_err}: accounting record layout does not match {layout}")
    scoring_codes = {str(row.get("scoring_code")) for row in accounts}
    scoring_bits = {int(row.get("scoring_bits", -1)) for row in accounts}
    if len(scoring_codes) != 1 or len(scoring_bits) != 1:
        raise ValueError(f"{cn_err}: MNs disagree on scoring-code metadata")

    num_queries = int(data.get("num_queries", 0))
    processed = int(queries.get("processed", 0))
    if num_queries <= 0 or processed != num_queries:
        raise ValueError(f"{cn_json}: incomplete query run")
    query_latency = parse_query_latency(
        queries, processed, require_latency, cn_json
    )
    bytes_per_mn = [int(value) for value in queries["local_cn_read_bytes_per_mn"]]
    wrs_per_mn = [int(value) for value in queries["local_cn_read_wrs_per_mn"]]
    submits_per_mn = [int(value) for value in queries["local_cn_read_submits_per_mn"]]
    if len(bytes_per_mn) != mns or len(wrs_per_mn) != mns or len(submits_per_mn) != mns:
        raise ValueError(f"{cn_json}: per-MN traffic vector has wrong length")
    if sum(bytes_per_mn) != int(queries["rdma_reads_in_bytes"]):
        raise ValueError(f"{cn_json}: per-MN read bytes do not close")
    if sum(wrs_per_mn) != int(queries["rdma_wrs"]):
        raise ValueError(f"{cn_json}: per-MN logical WRs do not close")
    if sum(submits_per_mn) != int(queries["rdma_posts"]):
        raise ValueError(f"{cn_json}: per-MN submits do not close")

    mn_rss = []
    per_mn_rows = []
    for mn, account in enumerate(accounts):
        mn_dir = cell / f"mn{mn + 1}"
        read_status(mn_dir / "status")
        mn_err = mn_dir / "mn.err"
        rss = parse_max_rss_kib(mn_err.read_text(errors="replace"))
        mn_rss.append(rss)
        per_mn_rows.append(
            {
                "dataset": dataset,
                "layout": layout,
                "memory_nodes": mns,
                "repeat": repeat,
                "mn": mn,
                "descriptor_version": int(account["descriptor_version"]),
                "policy": str(account["policy"]),
                "record_layout": str(account["record_layout"]),
                "scoring_code": str(account["scoring_code"]),
                "scoring_bits": int(account["scoring_bits"]),
                "local_slots": int(account["local_slots"]),
                "registered_bytes": int(account["registered_bytes"]),
                "materialized_bytes": int(account["materialized_bytes"]),
                "actual_write_bytes": int(account["actual_write_bytes"]),
                "header_bytes": int(account["header_bytes"]),
                "budget_map_bytes": int(account["budget_map_bytes"]),
                "placement_padding_bytes": int(account["placement_padding_bytes"]),
                "offset_table_bytes": int(account["offset_table_bytes"]),
                "record_bytes": int(account["record_bytes"]),
                "query_read_bytes": bytes_per_mn[mn],
                "query_read_wrs": wrs_per_mn[mn],
                "query_read_submits": submits_per_mn[mn],
                "mn_peak_rss_kib": rss,
            }
        )

    registered = sum(int(row["registered_bytes"]) for row in accounts)
    materialized = sum(int(row["materialized_bytes"]) for row in accounts)
    written = sum(int(row["actual_write_bytes"]) for row in accounts)
    staged_read_bytes = parse_staged_read_bytes(err_text, mns)
    authoritative = sum(staged_read_bytes)
    if authoritative <= 0 or materialized <= 0 or registered < materialized:
        raise ValueError(f"{cell}: invalid authoritative/sidecar byte accounting")
    qps = int(required_number(queries, "queries_per_sec", cn_json))
    recall = required_number(queries, "recall", cn_json)
    if qps <= 0 or recall < 0 or recall > 1:
        raise ValueError(f"{cn_json}: invalid QPS or recall")

    row = {
        "dataset": dataset,
        "layout": layout,
        "memory_nodes": mns,
        "repeat": repeat,
        "num_vectors": int(data["num_vectors"]),
        "num_queries": num_queries,
        "threads": int(meta["compute_threads"]),
        "coroutines_per_thread": int(meta["coroutines_per_thread"]),
        "binary_sha256": manifest["binary_sha256"],
        "manifest_cell_fingerprint": manifest_cell_fingerprint(manifest),
        "campaign_protocol_fingerprint": campaign_protocol_fingerprint(manifest, meta, data),
        "descriptor_version": int(accounts[0]["descriptor_version"]),
        "policy": str(accounts[0]["policy"]),
        "record_layout": str(accounts[0]["record_layout"]),
        "scoring_code": next(iter(scoring_codes)),
        "scoring_bits": next(iter(scoring_bits)),
        "recall": recall,
        "qps": qps,
        "query_wall_ms": required_number(timings, "query_max", cn_json),
        "query_read_bytes": int(queries["rdma_reads_in_bytes"]),
        "query_read_bytes_per_query": int(queries["rdma_reads_in_bytes"]) / num_queries,
        "query_read_wrs": int(queries["rdma_wrs"]),
        "query_read_wrs_per_query": int(queries["rdma_wrs"]) / num_queries,
        "query_read_submits": int(queries["rdma_posts"]),
        "query_read_submits_per_query": int(queries["rdma_posts"]) / num_queries,
        "read_bytes_gini": required_number(queries, "local_cn_read_bytes_gini", cn_json),
        "read_wrs_gini": required_number(queries, "local_cn_read_wrs_gini", cn_json),
        "read_submits_gini": required_number(queries, "local_cn_read_submits_gini", cn_json),
        "measured_authoritative_index_bytes": authoritative,
        "registered_sidecar_bytes": registered,
        "materialized_sidecar_bytes": materialized,
        "actual_sidecar_write_bytes": written,
        "sidecar_bytes_per_vector": materialized / int(data["num_vectors"]),
        "sidecar_to_authoritative_ratio": materialized / authoritative,
        "storage_amplification": (authoritative + materialized) / authoritative,
        "registered_utilization": materialized / registered,
        "cn_peak_rss_kib": parse_max_rss_kib(err_text),
        "mn_peak_rss_sum_kib": sum(mn_rss),
        "mn_peak_rss_max_kib": max(mn_rss),
        "lavd_build_ms": required_number(timings, "lavd_build_multi", cn_json),
        "lavd_build_fetch_ms": required_number(timings, "lavd_build_fetch", cn_json),
        "lavd_build_parse_ms": required_number(timings, "lavd_build_parse", cn_json),
        "lavd_build_rank_ms": required_number(timings, "lavd_build_rank", cn_json),
        "lavd_build_encode_ms": required_number(timings, "lavd_build_encode", cn_json),
        "lavd_build_metadata_ms": required_number(timings, "lavd_build_metadata", cn_json),
        "lavd_build_materialize_ms": required_number(timings, "lavd_build_materialize", cn_json),
        "resident_upper_build_ms": required_one_of_numbers(
            timings, ("crane_build_multi", "crane_build"), cn_json
        ),
        "cell_path": str(cell),
    }
    row.update(query_latency)
    return row, per_mn_rows


def collect(
    raw: Path, require_latency: bool = False
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not raw.is_dir():
        raise ValueError(f"raw directory does not exist: {raw}")
    runs = []
    per_mn = []
    for cell in sorted(path for path in raw.iterdir() if path.is_dir()):
        match = CELL_RE.match(cell.name)
        if match is None:
            continue
        row, mn_rows = parse_cell(cell, match, require_latency)
        runs.append(row)
        per_mn.extend(mn_rows)
    if not runs:
        raise ValueError(f"{raw}: no measured resource-ledger cells")
    return runs, per_mn


def validate_matrix(
    runs: list[dict[str, object]], layouts: list[str], mn_counts: list[int], repeats: int
) -> None:
    grouped = defaultdict(list)
    for row in runs:
        grouped[(str(row["layout"]), int(row["memory_nodes"]))].append(row)
    expected = {(layout, count) for layout in layouts for count in mn_counts}
    actual = set(grouped)
    if actual != expected:
        raise ValueError(f"matrix mismatch: missing={sorted(expected - actual)} extra={sorted(actual - expected)}")
    if repeats > 0:
        for key, rows in grouped.items():
            ids = sorted(int(row["repeat"]) for row in rows)
            if len(rows) != repeats or len(set(ids)) != repeats:
                raise ValueError(f"{key}: expected {repeats} unique repeats, found {ids}")
            fingerprints = {str(row["manifest_cell_fingerprint"]) for row in rows}
            if len(fingerprints) != 1:
                raise ValueError(f"{key}: manifest drift across repeats")
    campaign_fingerprints = {str(row["campaign_protocol_fingerprint"]) for row in runs}
    if len(campaign_fingerprints) != 1:
        raise ValueError("campaign protocol drift across resource-ledger cells")


def summarize(runs: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped = defaultdict(list)
    for row in runs:
        grouped[(str(row["dataset"]), str(row["layout"]), int(row["memory_nodes"]))].append(row)
    metrics = [
        "recall",
        "qps",
        "query_read_bytes_per_query",
        "query_read_wrs_per_query",
        "query_read_submits_per_query",
        "read_bytes_gini",
        "materialized_sidecar_bytes",
        "registered_utilization",
        "storage_amplification",
        "cn_peak_rss_kib",
        "mn_peak_rss_sum_kib",
        "mn_peak_rss_max_kib",
        "lavd_build_ms",
        "lavd_build_fetch_ms",
        "lavd_build_encode_ms",
        "lavd_build_materialize_ms",
        "resident_upper_build_ms",
    ]
    if all("query_latency_p50_us" in row for row in runs):
        metrics.extend(
            [
                "query_latency_p50_us",
                "query_latency_p95_us",
                "query_latency_p99_us",
            ]
        )
    output = []
    for (dataset, layout, mns), rows in sorted(grouped.items()):
        summary: dict[str, object] = {
            "dataset": dataset,
            "layout": layout,
            "memory_nodes": mns,
            "n": len(rows),
        }
        for metric in metrics:
            values = [float(row[metric]) for row in rows]
            summary[f"{metric}_mean"] = statistics.mean(values)
            summary[f"{metric}_median"] = statistics.median(values)
            summary[f"{metric}_ci95"] = t_ci_half(values)
        output.append(summary)
    return output


def write_markdown(path: Path, summaries: list[dict[str, object]]) -> None:
    lines = [
        "# VLDB Resource Ledger",
        "",
        "All resource values are measured. Authoritative HNSW bytes are the exact staged-read "
        "extents consumed by the builder; intervals are two-sided 95% Student-t confidence intervals.",
        "",
        "| Layout | MNs | n | Recall | QPS | MiB/query | WR/query | Sidecar GiB | Build s | CN RSS GiB | Max MN RSS GiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            "| {layout} | {memory_nodes} | {n} | {recall:.4f} +/- {recall_ci:.4f} | "
            "{qps:.0f} +/- {qps_ci:.0f} | {mib:.3f} | {wrs:.1f} | {sidecar:.2f} | "
            "{build:.2f} | {cn:.2f} | {mn:.2f} |".format(
                layout=row["layout"],
                memory_nodes=row["memory_nodes"],
                n=row["n"],
                recall=row["recall_mean"],
                recall_ci=row["recall_ci95"],
                qps=row["qps_mean"],
                qps_ci=row["qps_ci95"],
                mib=row["query_read_bytes_per_query_mean"] / (1024**2),
                wrs=row["query_read_wrs_per_query_mean"],
                sidecar=row["materialized_sidecar_bytes_mean"] / (1024**3),
                build=row["lavd_build_ms_mean"] / 1000,
                cn=row["cn_peak_rss_kib_mean"] / (1024**2),
                mn=row["mn_peak_rss_max_kib_mean"] / (1024**2),
            )
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    args = parse_args()
    layouts = [value.strip() for value in args.expected_layouts.split(",") if value.strip()]
    mn_counts = [int(value) for value in args.expected_mn_counts.split(",") if value.strip()]
    runs, per_mn = collect(args.raw, args.require_latency)
    validate_matrix(runs, layouts, mn_counts, args.expected_repeats)
    summaries = summarize(runs)
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "runs.csv", runs)
    write_csv(args.out / "per_mn.csv", per_mn)
    write_csv(args.out / "summary.csv", summaries)
    write_markdown(args.out / "README.md", summaries)
    print(f"validated {len(runs)} measured runs; wrote {args.out}")


if __name__ == "__main__":
    main()
