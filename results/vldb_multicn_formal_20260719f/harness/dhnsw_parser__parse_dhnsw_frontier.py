#!/usr/bin/env python3
"""Parse d-HNSW recall-QPS frontier logs into a checked CSV.

The released d-HNSW benchmark prints a latency-derived throughput number that
is not sustained QPS.  For the paper frontier we recompute QPS from the per-EF
query counts in the client log and the configured benchmark duration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path


def parse_ef_list(text: str) -> list[int]:
    return [int(x) for x in text.replace(",", " ").split() if x.strip()]


def parse_details(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)
        if len(nums) < 7:
            continue
        latency, recall, network, compute, meta, deserialize, raw_qps = (float(x) for x in nums[:7])
        rows.append(
            {
                "latency_us": latency,
                "recall": recall,
                "network_us": network,
                "compute_us": compute,
                "meta_us": meta,
                "deserialize_us": deserialize,
                "raw_qps_buggy": raw_qps,
            }
        )
    return rows


THREAD_BENCH_RE = re.compile(
    r"Thread\s+(?P<thread>\d+)\s+EF\s+(?P<ef>\d+)\s+benchmark:\s*"
    r"Queries executed:\s*(?P<queries>\d+)\s*"
    r"Avg total latency:\s*(?P<latency>[-+0-9.eE]+)\s+us\s*"
    r"Avg meta search latency:\s*(?P<meta>[-+0-9.eE]+)\s+us\s*"
    r"Avg compute time:\s*(?P<compute>[-+0-9.eE]+)\s+us\s*"
    r"Avg network latency:\s*(?P<network>[-+0-9.eE]+)\s+us\s*"
    r"Avg deserialize time:\s*(?P<deserialize>[-+0-9.eE]+)\s+us\s*"
    r"Recall:\s*(?P<recall>[-+0-9.eE]+)\s*"
    r"Throughput:\s*(?P<qps>[-+0-9.eE]+)\s+QPS",
    re.MULTILINE,
)
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
REPORTER_INLINE_RE = re.compile(
    r"\[reporter\.hh:\d+\]\s*\[Batch\s+\d+\][^\r\n]*"
)
BROKEN_PROTOCOL_CSI_RE = re.compile(
    r"\x1b\[(?=(?:FRONTIER_(?:QUERY_POOL|THREAD_RESULT)|QUERY_GT_SHAPE))"
)
BROKEN_METRIC_CSI_RE = re.compile(
    r"(?P<prefix>(?:Avg (?:total|meta search|compute|network|deserialize) "
    r"(?:latency|time)|Recall|Throughput):\s*)\x1b\[(?=[-+0-9.])"
)


def normalize_client_log(text: str) -> str:
    """Remove asynchronous reporter records spliced into benchmark lines."""
    # With many writers on stdout, a color-reset escape can lose its numeric
    # payload and land immediately before a protocol sentinel as ``ESC[``.
    # Preserve the sentinel before applying the generic ANSI matcher; the
    # latter would otherwise consume its first character as a CSI terminator.
    text = BROKEN_PROTOCOL_CSI_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    text = BROKEN_METRIC_CSI_RE.sub(r"\g<prefix>", text)
    return REPORTER_INLINE_RE.sub("", text)


def parse_client_details(
    text: str, ef: int, expected_threads: int
) -> dict[str, float]:
    rows: list[dict[str, float | int]] = []
    for match in THREAD_BENCH_RE.finditer(normalize_client_log(text)):
        row: dict[str, float | int] = {
            "thread": int(match.group("thread")),
            "ef": int(match.group("ef")),
            "queries": int(match.group("queries")),
            "latency_us": float(match.group("latency")),
            "meta_us": float(match.group("meta")),
            "compute_us": float(match.group("compute")),
            "network_us": float(match.group("network")),
            "deserialize_us": float(match.group("deserialize")),
            "recall": float(match.group("recall")),
            "raw_qps_buggy": float(match.group("qps")),
        }
        rows.append(row)
    thread_ids = sorted(int(row["thread"]) for row in rows)
    if thread_ids != list(range(expected_threads)):
        raise ValueError(
            f"client-detail thread coverage mismatch: expected="
            f"{list(range(expected_threads))} actual={thread_ids}"
        )
    if any(int(row["ef"]) != ef or int(row["queries"]) <= 0 for row in rows):
        raise ValueError("client-detail ef or query count mismatch")
    total_queries = sum(int(row["queries"]) for row in rows)
    metrics = (
        "latency_us", "recall", "network_us", "compute_us", "meta_us",
        "deserialize_us",
    )
    result: dict[str, float] = {}
    for metric in metrics:
        value = sum(
            float(row[metric]) * int(row["queries"]) for row in rows
        ) / total_queries
        if not math.isfinite(value) or value < 0 or (metric == "recall" and value > 1):
            raise ValueError(f"invalid client-detail metric {metric}")
        result[metric] = value
    result["raw_qps_buggy"] = sum(float(row["raw_qps_buggy"]) for row in rows)
    return result


def parse_query_counts(path: Path, ef_count: int, threads: int) -> list[int]:
    if not path.exists():
        return []
    counts = [int(match.group(1)) for match in re.finditer(r"Queries executed:\s+(\d+)", path.read_text(errors="replace"))]
    expected = ef_count * threads
    if len(counts) < expected:
        return []
    counts = counts[:expected]
    return [sum(counts[i : i + threads]) for i in range(0, expected, threads)]


POOL_RE = re.compile(
    r"^FRONTIER_QUERY_POOL total_queries=(\d+) threads=(\d+) top_k=(\d+) fixed=([01])$",
    re.MULTILINE,
)
SHAPE_RE = re.compile(
    r"^QUERY_GT_SHAPE query_rows=(\d+) ground_truth_rows=(\d+) "
    r"query_rows_per_ground_truth=(\d+)$",
    re.MULTILINE,
)
THREAD_RESULT_RE = re.compile(
    r"^(?:(?P<interleaved_thread_prefix>Thread )|"
    r"(?P<interleaved_metric_value_prefix>[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?))?"
    r"FRONTIER_THREAD_RESULT ef=(?P<ef>\d+) thread=(?P<thread>\d+) "
    r"queries=(?P<queries>\d+) elapsed_s=(?P<elapsed>[-+0-9.eE]+) "
    r"recall=(?P<recall>[-+0-9.eE]+)$",
    re.MULTILINE,
)


def parse_fixed_pool_result(text: str, ef: int, expected_threads: int) -> dict[str, object]:
    text = normalize_client_log(text)
    shapes = SHAPE_RE.findall(text)
    if len(shapes) != 1:
        raise ValueError(f"expected one QUERY_GT_SHAPE record, found {len(shapes)}")
    query_rows, ground_truth_rows, query_rows_per_ground_truth = (
        int(value) for value in shapes[0]
    )
    if (
        query_rows <= 0
        or ground_truth_rows <= 0
        or query_rows_per_ground_truth not in (1, 2)
        or query_rows != ground_truth_rows * query_rows_per_ground_truth
    ):
        raise ValueError("invalid query/ground-truth shape record")
    pools = POOL_RE.findall(text)
    if len(pools) != 1:
        raise ValueError(f"expected one FRONTIER_QUERY_POOL record, found {len(pools)}")
    expected_queries, threads, top_k, fixed = (int(value) for value in pools[0])
    if threads != expected_threads or fixed != 1 or expected_queries <= 0 or top_k <= 0:
        raise ValueError("invalid fixed query-pool protocol record")
    results = []
    prefix_interleavings = 0
    for match in THREAD_RESULT_RE.finditer(text):
        ef_raw = match.group("ef")
        thread_raw = match.group("thread")
        queries_raw = match.group("queries")
        elapsed_raw = match.group("elapsed")
        recall_raw = match.group("recall")
        prefix_interleavings += int(
            match.group("interleaved_thread_prefix") is not None
            or match.group("interleaved_metric_value_prefix") is not None
        )
        if int(ef_raw) != ef:
            raise ValueError(f"unexpected ef={ef_raw} in fixed-pool result for ef={ef}")
        result = {
            "thread": int(thread_raw),
            "queries": int(queries_raw),
            "elapsed_s": float(elapsed_raw),
            "recall": float(recall_raw),
        }
        if (
            result["queries"] <= 0
            or not math.isfinite(result["elapsed_s"])
            or result["elapsed_s"] <= 0
            or not math.isfinite(result["recall"])
            or not 0 <= result["recall"] <= 1
        ):
            raise ValueError("invalid fixed-pool thread result")
        results.append(result)
    thread_ids = sorted(int(row["thread"]) for row in results)
    if thread_ids != list(range(expected_threads)):
        raise ValueError(
            f"fixed-pool thread coverage mismatch: expected={list(range(expected_threads))} "
            f"actual={thread_ids}"
        )
    processed = sum(int(row["queries"]) for row in results)
    if processed != expected_queries:
        raise ValueError(
            f"fixed-pool query count mismatch: processed={processed} expected={expected_queries}"
        )
    wall = max(float(row["elapsed_s"]) for row in results)
    recall = sum(
        float(row["recall"]) * int(row["queries"]) for row in results
    ) / processed
    return {
        "processed_queries": processed,
        "expected_queries": expected_queries,
        "failed_queries": 0,
        "wall_seconds": wall,
        "qps": processed / wall,
        "recall": recall,
        "top_k": top_k,
        "query_rows": query_rows,
        "ground_truth_rows": ground_truth_rows,
        "query_rows_per_ground_truth": query_rows_per_ground_truth,
        "machine_record_prefix_interleavings": prefix_interleavings,
    }


def protocol_fingerprint(protocol: dict[str, object]) -> str:
    payload = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def parse_rss_gb(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    match = re.search(r"VmHWM:\s+(\d+)\s+kB", text)
    if not match:
        match = re.search(r"VmRSS:\s+(\d+)\s+kB", text)
    if not match:
        return ""
    return f"{int(match.group(1)) / (1024.0 * 1024.0):.3f}"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "dataset",
        "ef",
        "campaign_id",
        "protocol_fingerprint",
        "binary_sha256",
        "threads",
        "duration_s",
        "measurement_mode",
        "processed_queries",
        "expected_queries",
        "failed_queries",
        "wall_seconds",
        "top_k",
        "metric",
        "query_rows",
        "ground_truth_rows",
        "query_rows_per_ground_truth",
        "qps_recomputed",
        "recall",
        "latency_us",
        "network_us",
        "compute_us",
        "meta_us",
        "deserialize_us",
        "raw_qps_buggy",
        "server_rss_before_gb",
        "server_rss_after_gb",
        "status",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--datasets", nargs="+", required=True)
    parser.add_argument("--ef-list", default="48 64 96 128 200")
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--threads", type=int, default=10)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    ef_values = parse_ef_list(args.ef_list)
    rows: list[dict[str, object]] = []
    for dataset in args.datasets:
        rss_before = parse_rss_gb(args.result_dir / f"{dataset}_server_rss_before.txt")
        rss_after = parse_rss_gb(args.result_dir / f"{dataset}_server_rss_after.txt")
        for ef in ef_values:
            metric = "ip" if "text" in dataset.lower() or "tti" in dataset.lower() else "l2"
            details = parse_details(args.result_dir / f"{dataset}_ef{ef}_benchmark_details.txt")
            client_log = args.result_dir / f"{dataset}_ef{ef}_client.log"
            client_text = client_log.read_text(errors="replace") if client_log.exists() else ""
            detail_error = ""
            if len(details) != 1:
                try:
                    details = [parse_client_details(client_text, ef, args.threads)]
                except ValueError as exc:
                    details = []
                    detail_error = str(exc)
            try:
                fixed = parse_fixed_pool_result(client_text, ef, args.threads)
            except (OSError, ValueError) as exc:
                fixed = None
                error = str(exc)
            if len(details) != 1 or fixed is None:
                rows.append(
                    {
                        "dataset": dataset,
                        "ef": ef,
                        "campaign_id": args.campaign_id,
                        "protocol_fingerprint": "",
                        "binary_sha256": args.binary_sha256,
                        "threads": args.threads,
                        "duration_s": f"{args.duration:.0f}",
                        "measurement_mode": "fixed_query_pool",
                        "processed_queries": "",
                        "expected_queries": "",
                        "failed_queries": "",
                        "wall_seconds": "",
                        "top_k": 10,
                        "metric": metric,
                        "query_rows": "",
                        "ground_truth_rows": "",
                        "query_rows_per_ground_truth": "",
                        "qps_recomputed": "",
                        "recall": "",
                        "latency_us": "",
                        "network_us": "",
                        "compute_us": "",
                        "meta_us": "",
                        "deserialize_us": "",
                        "raw_qps_buggy": "",
                        "server_rss_before_gb": rss_before,
                        "server_rss_after_gb": rss_after,
                        "status": (
                            f"incomplete details={len(details)}"
                            f"({detail_error or 'missing'}) "
                            f"fixed={error if fixed is None else 'ok'}"
                        ),
                    }
                )
                continue
            detail = details[0]
            fingerprint = protocol_fingerprint(
                {
                    "binary_sha256": args.binary_sha256,
                    "dataset": dataset,
                    "ef": ef,
                    "threads": args.threads,
                    "top_k": fixed["top_k"],
                    "metric": metric,
                    "measurement_mode": "fixed_query_pool",
                    "expected_queries": fixed["expected_queries"],
                    "query_rows": fixed["query_rows"],
                    "ground_truth_rows": fixed["ground_truth_rows"],
                    "query_rows_per_ground_truth": fixed["query_rows_per_ground_truth"],
                }
            )
            rows.append(
                {
                    "dataset": dataset,
                    "ef": ef,
                    "campaign_id": args.campaign_id,
                    "protocol_fingerprint": fingerprint,
                    "binary_sha256": args.binary_sha256,
                    "threads": args.threads,
                    "duration_s": f"{args.duration:.0f}",
                    "measurement_mode": "fixed_query_pool",
                    "processed_queries": fixed["processed_queries"],
                    "expected_queries": fixed["expected_queries"],
                    "failed_queries": fixed["failed_queries"],
                    "wall_seconds": f"{float(fixed['wall_seconds']):.6f}",
                    "top_k": fixed["top_k"],
                    "metric": metric,
                    "query_rows": fixed["query_rows"],
                    "ground_truth_rows": fixed["ground_truth_rows"],
                    "query_rows_per_ground_truth": fixed["query_rows_per_ground_truth"],
                    "qps_recomputed": f"{float(fixed['qps']):.3f}",
                    "recall": f"{detail['recall']:.6f}",
                    "latency_us": f"{detail['latency_us']:.3f}",
                    "network_us": f"{detail['network_us']:.3f}",
                    "compute_us": f"{detail['compute_us']:.3f}",
                    "meta_us": f"{detail['meta_us']:.3f}",
                    "deserialize_us": f"{detail['deserialize_us']:.3f}",
                    "raw_qps_buggy": f"{detail['raw_qps_buggy']:.6g}",
                    "server_rss_before_gb": rss_before,
                    "server_rss_after_gb": rss_after,
                    "status": "ok",
                }
            )
    write_csv(args.out, rows)
    print(f"wrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
