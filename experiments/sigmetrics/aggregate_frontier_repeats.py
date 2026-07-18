#!/usr/bin/env python3
"""Aggregate repeated three-system frontier campaigns without overwriting runs."""

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


DATASET_LABELS = {
    "sift1M": "SIFT1M",
    "sift1m": "SIFT1M",
    "SIFT1M": "SIFT1M",
    "gist1M": "GIST1M",
    "gist1m": "GIST1M",
    "GIST1M": "GIST1M",
    "deep1M": "DEEP1M",
    "deep1m": "DEEP1M",
    "DEEP1M": "DEEP1M",
    "bigann1M": "BIGANN1M",
    "bigann1m": "BIGANN1M",
    "BIGANN1M": "BIGANN1M",
    "spacev1M": "SPACEV1M",
    "spacev1m": "SPACEV1M",
    "SPACEV1M": "SPACEV1M",
    "turing1M": "TURING1M",
    "turing1m": "TURING1M",
    "TURING1M": "TURING1M",
    "text1M": "TTI1M",
    "text1m": "TTI1M",
    "TEXT1M": "TTI1M",
    "tti1M": "TTI1M",
    "tti1m": "TTI1M",
    "TTI1M": "TTI1M",
    "deep10M": "DEEP10M",
    "deep10m": "DEEP10M",
    "DEEP10M": "DEEP10M",
    "text10M": "TTI10M",
    "text10m": "TTI10M",
    "TEXT10M": "TTI10M",
    "tti10M": "TTI10M",
    "TTI10M": "TTI10M",
    "sift10M": "SIFT10M",
    "sift10m": "SIFT10M",
    "SIFT10M": "SIFT10M",
}
METHODS = ("SHINE", "SlabWalk", "d-HNSW")
OPTIONAL_METRICS = (
    "p50_us",
    "p95_us",
    "p99_us",
    "mean_latency_us",
    "posts_per_query",
    "bytes_per_query",
    "network_us",
    "compute_us",
    "meta_us",
    "deserialize_us",
)
QUERY_POOL_LINK_FIELDS = (
    "query_pool_manifest",
    "query_pool_manifest_sha256",
    "query_path",
    "groundtruth_path",
    "query_canonical_sha256",
    "groundtruth_canonical_sha256",
    "query_file_sha256",
    "groundtruth_file_sha256",
)
RUN_RE = re.compile(r"(?:^|[/_])(r[0-9]+|warmup)(?:[/_.]|$)")
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
       6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
       11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
       16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
       21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
       26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def infer_run_id(path: str) -> str:
    match = RUN_RE.search(path)
    if match is None:
        raise ValueError(f"cannot infer run id from {path}")
    return match.group(1)


def t_ci_half(values: Iterable[float]) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    return T95.get(len(vals) - 1, 1.960) * statistics.stdev(vals) / math.sqrt(len(vals))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing input CSV: {path}")
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def canonical_dataset(raw: str) -> str:
    return DATASET_LABELS.get(raw, raw)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_query_pool_evidence(directory: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not directory.is_dir():
        raise ValueError(f"missing query-pool evidence directory: {directory}")
    links: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid query-pool manifest: {path}") from exc
        if record.get("kind") != "query_pool_fingerprint":
            continue
        dataset = canonical_dataset(str(record.get("dataset", "")))
        method = str(record.get("method", ""))
        key = (dataset, method)
        if not dataset or not method:
            raise ValueError(f"{path}: missing dataset or method")
        if key in links:
            raise ValueError(f"duplicate query-pool manifest for {dataset}/{method}")
        query = record.get("query")
        groundtruth = record.get("groundtruth")
        if not isinstance(query, dict) or not isinstance(groundtruth, dict):
            raise ValueError(f"{path}: missing query or ground-truth fingerprint")

        def manifest_value(obj: dict[str, object], field: str) -> str:
            value = str(obj.get(field, "")).strip()
            if not value:
                raise ValueError(f"{path}: missing {field}")
            return value

        links[key] = {
            "metric": str(record.get("metric", "")).strip(),
            "query_pool_manifest": path.name,
            "query_pool_manifest_sha256": file_sha256(path),
            "query_path": manifest_value(query, "path"),
            "groundtruth_path": manifest_value(groundtruth, "path"),
            "query_canonical_sha256": manifest_value(query, "canonical_sha256"),
            "groundtruth_canonical_sha256": manifest_value(
                groundtruth, "canonical_ids_sha256"
            ),
            "query_file_sha256": manifest_value(query, "file_sha256"),
            "groundtruth_file_sha256": manifest_value(groundtruth, "file_sha256"),
        }
    if not links:
        raise ValueError(f"no query-pool fingerprints in {directory}")
    return links


def attach_query_pool_evidence(
    rows: list[dict[str, object]], directory: Path
) -> list[dict[str, object]]:
    links = load_query_pool_evidence(directory)
    attached: list[dict[str, object]] = []
    for row in rows:
        dataset = canonical_dataset(str(row.get("dataset", "")))
        method = str(row.get("method", ""))
        link = links.get((dataset, method))
        if link is None:
            raise ValueError(f"missing query-pool manifest for {dataset}/{method}")
        row_metric = str(row.get("metric", "")).strip()
        if row_metric and row_metric != link["metric"]:
            raise ValueError(
                f"query-pool metric mismatch for {dataset}/{method}: "
                f"frontier={row_metric} manifest={link['metric']}"
            )
        linked_row = dict(row)
        linked_row["dataset"] = dataset
        for field in QUERY_POOL_LINK_FIELDS:
            linked_row[field] = link[field]
        attached.append(linked_row)
    return attached


def relativize_contained_sources(
    rows: list[dict[str, object]], directory: Path
) -> list[dict[str, object]]:
    """Use portable paths for retained source CSVs inside the output bundle."""
    root = directory.resolve()
    normalized: list[dict[str, object]] = []
    for row in rows:
        linked = dict(row)
        source = Path(str(linked.get("source", ""))).resolve()
        try:
            relative = source.relative_to(root)
        except ValueError:
            pass
        else:
            linked["source"] = relative.as_posix()
        normalized.append(linked)
    return normalized


def required_field(row: dict[str, str], key: str, source: Path) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"{source}: missing required field {key}")
    return value


def finite_number(row: dict[str, str], key: str, source: Path) -> float:
    value = float(required_field(row, key, source))
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite field {key}")
    return value


def collect_sw(path: Path) -> list[dict[str, object]]:
    output = []
    source_rows = read_csv(path)
    source_sha256 = file_sha256(path)
    fallback_run_id = (
        infer_run_id(str(path))
        if source_rows and "run_id" not in source_rows[0]
        else ""
    )
    for row in source_rows:
        if row.get("status") != "ok" or row.get("run_kind", "measure") != "measure":
            continue
        if row.get("trace", "0") not in ("", "0"):
            continue
        run_id = row.get("run_id") or fallback_run_id
        if run_id == "warmup" or not row.get("recall") or not row.get("qps"):
            continue
        failed = int(row.get("failed_queries") or 0)
        if failed != 0:
            raise ValueError(f"{path}: incomplete query pool for {row.get('dataset')}/{row.get('ef')}")
        output.append(
            {
                "dataset": canonical_dataset(row["dataset"]),
                "method": row["method"],
                "ef": float(row["ef"]),
                "run_id": run_id,
                "recall": finite_number(row, "recall", path),
                "qps": finite_number(row, "qps", path),
                "threads": int(required_field(row, "threads", path)),
                "query_contexts": int(required_field(row, "query_contexts", path)),
                "top_k": int(required_field(row, "top_k", path)),
                "metric": required_field(row, "metric", path),
                "measurement_mode": required_field(row, "measurement_mode", path),
                "protocol_fingerprint": required_field(row, "protocol_fingerprint", path),
                "campaign_id": required_field(row, "campaign_id", path),
                "binary_sha256": required_field(row, "binary_sha256", path),
                "variant": required_field(row, "variant", path),
                "lavd_bits": int(required_field(row, "lavd", path)),
                "index_region_bytes": int(
                    required_field(row, "index_region_bytes", path)
                ),
                "lavd_region_bytes": int(
                    required_field(row, "lavd_region_bytes", path)
                ),
                "layout_env": required_field(row, "env", path),
                "processed_queries": int(required_field(row, "processed", path)),
                "expected_queries": int(required_field(row, "expected_queries", path)),
                "failed_queries": failed,
                "p50_us": finite_number(row, "p50_us", path),
                "p95_us": finite_number(row, "p95_us", path),
                "p99_us": finite_number(row, "p99_us", path),
                "mean_latency_us": None,
                "posts_per_query": finite_number(row, "posts_per_q", path),
                "bytes_per_query": finite_number(row, "bytes_per_q", path),
                "network_us": None,
                "compute_us": None,
                "meta_us": None,
                "deserialize_us": None,
                "source": str(path),
                "source_sha256": source_sha256,
            }
        )
    return output


def collect_dhnsw(path: Path) -> list[dict[str, object]]:
    output = []
    source_sha256 = file_sha256(path)
    run_id = infer_run_id(str(path))
    if run_id == "warmup":
        return output
    for row in read_csv(path):
        if row.get("status", "ok") != "ok":
            continue
        qps = row.get("qps_recomputed") or row.get("qps")
        if not row.get("recall") or not qps:
            continue
        output.append(
            {
                "dataset": canonical_dataset(row["dataset"]),
                "method": "d-HNSW",
                "ef": float(row["ef"]),
                "run_id": run_id,
                "recall": finite_number(row, "recall", path),
                "qps": float(qps),
                "threads": int(required_field(row, "threads", path)),
                "query_contexts": "",
                "top_k": int(required_field(row, "top_k", path)),
                "metric": required_field(row, "metric", path),
                "measurement_mode": required_field(row, "measurement_mode", path),
                "protocol_fingerprint": required_field(row, "protocol_fingerprint", path),
                "campaign_id": required_field(row, "campaign_id", path),
                "binary_sha256": required_field(row, "binary_sha256", path),
                "variant": "fixed_routing_partition",
                "lavd_bits": "",
                "index_region_bytes": "",
                "lavd_region_bytes": "",
                "layout_env": "native_dhnsw",
                "processed_queries": int(required_field(row, "processed_queries", path)),
                "expected_queries": int(required_field(row, "expected_queries", path)),
                "failed_queries": int(required_field(row, "failed_queries", path)),
                "p50_us": None,
                "p95_us": None,
                "p99_us": None,
                "mean_latency_us": finite_number(row, "latency_us", path),
                "posts_per_query": None,
                "bytes_per_query": None,
                "network_us": finite_number(row, "network_us", path),
                "compute_us": finite_number(row, "compute_us", path),
                "meta_us": finite_number(row, "meta_us", path),
                "deserialize_us": finite_number(row, "deserialize_us", path),
                "source": str(path),
                "source_sha256": source_sha256,
            }
        )
    return output


def summarize(rows: list[dict[str, object]], expected_repeats: int) -> list[dict[str, object]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(str(row["dataset"]), str(row["method"]), float(row["ef"]))].append(row)
    summaries = []
    for (dataset, method, ef), points in sorted(grouped.items()):
        run_ids = [str(point["run_id"]) for point in points]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError(f"duplicate repeats for {dataset}/{method}/ef={ef}: {run_ids}")
        if expected_repeats > 0 and len(points) != expected_repeats:
            raise ValueError(
                f"{dataset}/{method}/ef={ef}: expected {expected_repeats} repeats, found {run_ids}"
            )
        recalls = [float(point["recall"]) for point in points]
        qps = [float(point["qps"]) for point in points]
        if any(not math.isfinite(value) for value in recalls + qps):
            raise ValueError(f"non-finite frontier value for {dataset}/{method}/ef={ef}")
        if any(value < 0 or value > 1 for value in recalls) or any(value <= 0 for value in qps):
            raise ValueError(f"out-of-range frontier value for {dataset}/{method}/ef={ef}")
        campaign_ids = sorted(
            {str(point.get("campaign_id", "")) for point in points if point.get("campaign_id", "")}
        )
        binary_sha256s = sorted(
            {str(point.get("binary_sha256", "")) for point in points if point.get("binary_sha256", "")}
        )

        def uniform_value(key: str) -> object:
            values = {point.get(key, "") for point in points if point.get(key, "") != ""}
            if not values:
                return ""
            if len(values) == 1:
                return next(iter(values))
            return ";".join(sorted(str(value) for value in values))

        summary = {
                "dataset": dataset,
                "method": method,
                "ef": ef,
                "n": len(points),
                "campaign_ids": ";".join(campaign_ids),
                "binary_sha256s": ";".join(binary_sha256s),
                "threads": uniform_value("threads"),
                "query_contexts": uniform_value("query_contexts"),
                "top_k": uniform_value("top_k"),
                "metric": uniform_value("metric"),
                "expected_queries": uniform_value("expected_queries"),
                "recall_mean": statistics.mean(recalls),
                "recall_median": statistics.median(recalls),
                "recall_ci95": t_ci_half(recalls),
                "qps_mean": statistics.mean(qps),
                "qps_median": statistics.median(qps),
                "qps_ci95": t_ci_half(qps),
                "run_ids": ";".join(sorted(run_ids)),
            }
        for metric in OPTIONAL_METRICS:
            values = [
                float(point[metric])
                for point in points
                if point.get(metric) not in (None, "")
            ]
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError(f"invalid {metric} for {dataset}/{method}/ef={ef}")
            summary[f"{metric}_n"] = len(values)
            summary[f"{metric}_mean"] = statistics.mean(values) if values else ""
            summary[f"{metric}_median"] = statistics.median(values) if values else ""
            summary[f"{metric}_ci95"] = t_ci_half(values) if values else ""
        summaries.append(summary)
    return summaries


def validate_measurement_metrics(rows: list[dict[str, object]]) -> None:
    def value(row: dict[str, object], key: str, label: str) -> float:
        raw = row.get(key)
        if raw in (None, ""):
            raise ValueError(f"{label}: missing {key}")
        result = float(raw)
        if not math.isfinite(result) or result < 0:
            raise ValueError(f"{label}: invalid {key}")
        return result

    for row in rows:
        dataset = str(row.get("dataset", ""))
        method = str(row.get("method", ""))
        ef = row.get("ef", "")
        label = f"{dataset}/{method}/ef={ef}"
        if method in ("SHINE", "SlabWalk"):
            p50 = value(row, "p50_us", label)
            p95 = value(row, "p95_us", label)
            p99 = value(row, "p99_us", label)
            if not p50 <= p95 <= p99:
                raise ValueError(f"{label}: non-monotonic p50_us/p95_us/p99_us")
            value(row, "posts_per_query", label)
            value(row, "bytes_per_query", label)
        elif method == "d-HNSW":
            if value(row, "mean_latency_us", label) <= 0:
                raise ValueError(f"{label}: mean_latency_us must be positive")
            for key in ("network_us", "compute_us", "meta_us", "deserialize_us"):
                value(row, key, label)
        else:
            raise ValueError(f"{label}: unsupported method")


def validate_protocol(
    rows: list[dict[str, object]],
    expected_threads: int,
    expected_top_k: int,
    expected_query_contexts: int,
) -> None:
    grouped = defaultdict(list)
    campaigns = defaultdict(set)
    binaries = defaultdict(set)
    query_counts = defaultdict(set)
    metrics = defaultdict(set)
    for row in rows:
        dataset = str(row["dataset"])
        method = str(row["method"])
        ef = float(row["ef"])
        threads = int(row.get("threads", -1))
        top_k = int(row.get("top_k", -1))
        if threads != expected_threads:
            raise ValueError(f"{dataset}/{method}/ef={ef}: threads={threads}, expected {expected_threads}")
        if method in ("SHINE", "SlabWalk"):
            query_contexts = int(row.get("query_contexts", -1))
            if query_contexts != expected_query_contexts:
                raise ValueError(
                    f"{dataset}/{method}/ef={ef}: query_contexts={query_contexts}, "
                    f"expected {expected_query_contexts}"
                )
        if top_k != expected_top_k:
            raise ValueError(f"{dataset}/{method}/ef={ef}: top_k={top_k}, expected {expected_top_k}")
        if str(row.get("measurement_mode", "")) != "fixed_query_pool":
            raise ValueError(f"{dataset}/{method}/ef={ef}: measurement mode is not fixed_query_pool")
        processed = int(row.get("processed_queries", -1))
        expected = int(row.get("expected_queries", -1))
        failed = int(row.get("failed_queries", -1))
        if expected <= 0 or processed != expected or failed != 0:
            raise ValueError(
                f"{dataset}/{method}/ef={ef}: incomplete query pool "
                f"processed={processed} expected={expected} failed={failed}"
            )
        binary_sha256 = str(row.get("binary_sha256", ""))
        if not binary_sha256:
            raise ValueError(f"{dataset}/{method}/ef={ef}: missing binary SHA")
        campaign_id = str(row.get("campaign_id", ""))
        if not campaign_id:
            raise ValueError(f"{dataset}/{method}/ef={ef}: missing campaign ID")
        fingerprint = str(row.get("protocol_fingerprint", ""))
        if not fingerprint:
            raise ValueError(f"{dataset}/{method}/ef={ef}: missing protocol fingerprint")
        grouped[(dataset, method, ef)].append(fingerprint)
        campaigns[(dataset, method)].add(campaign_id)
        binaries[(dataset, method)].add(binary_sha256)
        query_counts[dataset].add(expected)
        metrics[dataset].add(str(row.get("metric", "")))
    for key, fingerprints in grouped.items():
        if len(set(fingerprints)) != 1:
            raise ValueError(f"protocol drift across repeats for {key}")
    for key, values in campaigns.items():
        if len(values) != 1:
            raise ValueError(f"campaign drift across repeats for {key}: {sorted(values)}")
    for key, values in binaries.items():
        if len(values) != 1:
            raise ValueError(f"binary drift across repeats for {key}: {sorted(values)}")
    for dataset in {key[0] for key in campaigns}:
        shine = campaigns.get((dataset, "SHINE"), set())
        slabwalk = campaigns.get((dataset, "SlabWalk"), set())
        if shine and slabwalk and shine != slabwalk:
            raise ValueError(
                f"SHINE/SlabWalk campaign mismatch for {dataset}: "
                f"SHINE={sorted(shine)} SlabWalk={sorted(slabwalk)}"
            )
    for dataset, counts in query_counts.items():
        if len(counts) != 1:
            raise ValueError(f"query-pool drift across systems for {dataset}: {sorted(counts)}")
    for dataset, values in metrics.items():
        if len(values) != 1 or "" in values:
            raise ValueError(f"metric drift across systems for {dataset}: {sorted(values)}")


def validate_matrix(summaries: list[dict[str, object]], datasets: list[str], min_points: int) -> None:
    counts = defaultdict(int)
    for row in summaries:
        counts[(str(row["dataset"]), str(row["method"]))] += 1
    missing = []
    for dataset in datasets:
        for method in METHODS:
            if counts[(dataset, method)] < min_points:
                missing.append((dataset, method, counts[(dataset, method)]))
    if missing:
        raise ValueError(f"incomplete repeated frontier matrix: {missing}")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sw", type=Path, action="append", default=[])
    parser.add_argument("--dhnsw", type=Path, action="append", default=[])
    parser.add_argument("--expected-repeats", type=int, default=5)
    parser.add_argument("--expected-datasets", default="DEEP10M,TTI10M,SIFT10M")
    parser.add_argument("--min-points", type=int, default=5)
    parser.add_argument("--expected-threads", type=int, default=10)
    parser.add_argument("--expected-query-contexts", type=int, default=10)
    parser.add_argument("--expected-top-k", type=int, default=10)
    parser.add_argument(
        "--query-pools",
        type=Path,
        help="attach content and physical-file fingerprints from query-pool manifests",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = []
    for path in args.sw:
        rows.extend(collect_sw(path))
    for path in args.dhnsw:
        rows.extend(collect_dhnsw(path))
    if not rows:
        raise ValueError("no measured frontier repeats")
    if args.query_pools is not None:
        rows = attach_query_pool_evidence(rows, args.query_pools)
    rows = relativize_contained_sources(rows, args.out_dir)
    validate_protocol(
        rows,
        args.expected_threads,
        args.expected_top_k,
        args.expected_query_contexts,
    )
    validate_measurement_metrics(rows)
    summaries = summarize(rows, args.expected_repeats)
    datasets = [canonical_dataset(value.strip()) for value in args.expected_datasets.split(",") if value.strip()]
    validate_matrix(summaries, datasets, args.min_points)
    write_csv(args.out_dir / "frontier_repeated_raw.csv", rows)
    write_csv(args.out_dir / "frontier_summary.csv", summaries)
    print(f"validated {len(rows)} raw repeat points; wrote {args.out_dir}")


if __name__ == "__main__":
    main()
