#!/usr/bin/env python3
"""Create one source-bound row for the VLDB multi-CN campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import parse_dhnsw_frontier
except ImportError:
    import parse_dhnsw_frontier


FIELDS = (
    "campaign_id",
    "protocol_fingerprint",
    "dataset",
    "system",
    "cn_count",
    "repeat",
    "binary_sha256",
    "query_canonical_sha256",
    "groundtruth_canonical_sha256",
    "processed_queries",
    "expected_queries",
    "failed_queries",
    "qps",
    "recall",
    "p50_us",
    "p99_us",
    "posts_per_query",
    "bytes_per_query",
    "fairness",
    "source",
    "source_sha256",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sha(value: str, label: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite {label}: {value!r}")
    return result


def integer(value: object, label: str) -> int:
    result = number(value, label)
    if not result.is_integer():
        raise ValueError(f"non-integral {label}: {value!r}")
    return int(result)


def jain_fairness(values: list[float]) -> float:
    if not values or any(value < 0 for value in values):
        raise ValueError("fairness inputs must be non-negative and non-empty")
    denominator = len(values) * sum(value * value for value in values)
    if denominator == 0:
        raise ValueError("cannot compute fairness for an all-zero vector")
    return sum(values) ** 2 / denominator


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing measurement JSON: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid measurement JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"measurement JSON is not an object: {path}")
    return value


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"refusing existing source record: {path}")
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append_csv(csv_path: Path, source_path: Path, record: dict[str, object]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    root = csv_path.parent.resolve()
    source = source_path.resolve()
    try:
        relative = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("source record must be inside the CSV campaign directory") from exc
    metrics = record["metrics"]
    assert isinstance(metrics, dict)
    row: dict[str, object] = {
        "campaign_id": record["campaign_id"],
        "protocol_fingerprint": record["protocol_fingerprint"],
        "dataset": record["dataset"],
        "system": record["system"],
        "cn_count": record["cn_count"],
        "repeat": record["repeat"],
        "binary_sha256": record["binary_sha256"],
        "query_canonical_sha256": record["query_canonical_sha256"],
        "groundtruth_canonical_sha256": record["groundtruth_canonical_sha256"],
        **{
            key: "" if value is None else value
            for key, value in metrics.items()
        },
        "source": relative,
        "source_sha256": file_sha256(source_path),
    }
    if tuple(row) != FIELDS:
        raise ValueError(f"internal CSV schema drift: {tuple(row)}")

    existing: list[dict[str, str]] = []
    if csv_path.exists():
        with csv_path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != FIELDS:
                raise ValueError(f"existing raw CSV schema drift: {csv_path}")
            existing = list(reader)
    key = (str(row["dataset"]), str(row["system"]), str(row["cn_count"]), str(row["repeat"]))
    if any(
        (old["dataset"], old["system"], old["cn_count"], old["repeat"]) == key
        for old in existing
    ):
        raise ValueError(f"duplicate raw campaign cell: {key}")
    with csv_path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not existing and handle.tell() == 0:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def common_record(
    *,
    campaign_id: str,
    protocol_fingerprint: str,
    dataset: str,
    system: str,
    cn_count: int,
    repeat: int,
    binary_sha256: str,
    query_sha256: str,
    groundtruth_sha256: str,
    metrics: dict[str, object],
    raw_measurements: list[dict[str, object]],
) -> dict[str, object]:
    if not campaign_id.strip():
        raise ValueError("campaign ID must be non-empty")
    if dataset not in {"SIFT1M", "DEEP1M", "GIST1M"}:
        raise ValueError(f"unsupported multi-CN dataset: {dataset}")
    if system not in {"SHINE", "SlabWalk", "d-HNSW"}:
        raise ValueError(f"unsupported multi-CN system: {system}")
    if cn_count not in (1, 2, 3) or repeat not in range(5):
        raise ValueError("CN count or repeat is outside the fixed campaign matrix")
    return {
        "kind": "vldb_multicn_raw_source",
        "campaign_id": campaign_id,
        "protocol_fingerprint": validate_sha(
            protocol_fingerprint, "protocol fingerprint"
        ),
        "dataset": dataset,
        "system": system,
        "cn_count": cn_count,
        "repeat": repeat,
        "binary_sha256": validate_sha(binary_sha256, "binary SHA"),
        "query_canonical_sha256": validate_sha(query_sha256, "query SHA"),
        "groundtruth_canonical_sha256": validate_sha(
            groundtruth_sha256, "ground-truth SHA"
        ),
        "metrics": metrics,
        "raw_measurements": raw_measurements,
    }


def record_graph(
    *,
    campaign_id: str,
    protocol_fingerprint: str,
    dataset: str,
    system: str,
    cn_count: int,
    repeat: int,
    binary_sha256: str,
    query_sha256: str,
    groundtruth_sha256: str,
    expected_queries: int,
    initiator_json: Path,
    client_logs: list[Path],
    source_path: Path,
    csv_path: Path,
) -> dict[str, object]:
    if system not in {"SHINE", "SlabWalk"}:
        raise ValueError("graph recorder only accepts SHINE or SlabWalk")
    if len(client_logs) != cn_count - 1:
        raise ValueError(
            f"incomplete client coverage: expected {cn_count - 1}, got {len(client_logs)}"
        )
    initiator = read_json(initiator_json)
    meta = initiator.get("meta")
    if not isinstance(meta, dict) or integer(meta.get("compute_nodes"), "compute_nodes") != cn_count:
        raise ValueError("compute-node count differs from the campaign cell")

    queries = initiator.get("queries")
    timings = initiator.get("timings")
    if not isinstance(queries, dict) or not isinstance(timings, dict):
        raise ValueError("initiator JSON is missing queries or timings")
    processed = integer(queries.get("processed"), "processed queries")
    if processed != expected_queries:
        raise ValueError(f"processed query mismatch: {processed} != {expected_queries}")
    local = queries.get("processed_local")
    expected_local_keys = {f"c{client_id}" for client_id in range(cn_count)}
    if not isinstance(local, dict) or set(local) != expected_local_keys:
        raise ValueError("initiator JSON has incomplete processed_local coverage")
    local_processed = [integer(local[f"c{i}"], f"processed_local.c{i}") for i in range(cn_count)]
    if sum(local_processed) != expected_queries:
        raise ValueError("per-CN processed counts do not sum to the logical query pool")
    local_seconds = [
        number(timings.get(f"query_c{i}"), f"query_c{i}") / 1000.0
        for i in range(cn_count)
    ]
    if any(value <= 0 for value in local_seconds):
        raise ValueError("per-CN query time must be positive")
    local_qps = [count / seconds for count, seconds in zip(local_processed, local_seconds)]

    local_samples = integer(
        queries.get("local_latency_samples"), "initiator local latency samples"
    )
    if local_samples != local_processed[0]:
        raise ValueError(
            f"initiator latency coverage {local_samples} != {local_processed[0]}"
        )
    initiator_p50 = number(queries.get("local_latency_p50_us"), "initiator p50")
    initiator_p99 = number(queries.get("local_latency_p99_us"), "initiator p99")
    if not 0 <= initiator_p50 <= initiator_p99:
        raise ValueError("initiator latency quantiles are not monotonic")

    raw_measurements: list[dict[str, object]] = [
        {
            "client_id": 0,
            "kind": "initiator_json",
            "path": str(initiator_json.resolve()),
            "sha256": file_sha256(initiator_json),
            "payload": initiator,
        }
    ]
    processed_pattern = re.compile(r"\[STATUS\]:\s*processed queries:\s*(\d+)")
    for client_id, path in enumerate(client_logs, start=1):
        if not path.is_file():
            raise ValueError(f"missing non-initiator log: {path}")
        log = path.read_text(errors="replace")
        matches = processed_pattern.findall(log)
        if not matches:
            raise ValueError(f"c{client_id} log has no processed-query completion marker")
        logged_processed = integer(matches[-1], f"c{client_id} logged processed queries")
        if logged_processed != local_processed[client_id]:
            raise ValueError(
                f"c{client_id} log coverage {logged_processed} != {local_processed[client_id]}"
            )
        raw_measurements.append(
            {
                "client_id": client_id,
                "kind": "noninitiator_stderr",
                "path": str(path.resolve()),
                "sha256": file_sha256(path),
                "processed_queries": logged_processed,
                "log": log,
            }
        )

    recall = number(queries.get("recall"), "recall")
    qps = number(queries.get("queries_per_sec"), "queries_per_sec")
    posts = number(queries.get("rdma_posts"), "rdma_posts")
    read_bytes = number(queries.get("rdma_reads_in_bytes"), "rdma_reads_in_bytes")
    if not 0 <= recall <= 1 or qps <= 0 or posts < 0 or read_bytes < 0:
        raise ValueError("invalid aggregate query metrics")
    metrics: dict[str, object] = {
        "processed_queries": processed,
        "expected_queries": expected_queries,
        "failed_queries": 0,
        "qps": qps,
        "recall": recall,
        "p50_us": initiator_p50 if cn_count == 1 else None,
        "p99_us": initiator_p99 if cn_count == 1 else None,
        "posts_per_query": posts / processed,
        "bytes_per_query": read_bytes / processed,
        "fairness": jain_fairness(local_qps),
    }
    record = common_record(
        campaign_id=campaign_id,
        protocol_fingerprint=protocol_fingerprint,
        dataset=dataset,
        system=system,
        cn_count=cn_count,
        repeat=repeat,
        binary_sha256=binary_sha256,
        query_sha256=query_sha256,
        groundtruth_sha256=groundtruth_sha256,
        metrics=metrics,
        raw_measurements=raw_measurements,
    )
    record["latency_scope"] = (
        "all_queries_single_cn"
        if cn_count == 1
        else "not_reported_cross_cn_frozen_binary_boundary"
    )
    record["initiator_local_latency"] = {
        "samples": local_samples,
        "p50_us": initiator_p50,
        "p99_us": initiator_p99,
    }
    record["per_cn_qps"] = local_qps
    atomic_json(source_path, record)
    append_csv(csv_path, source_path, record)
    return record


def record_dhnsw_parsed(
    *,
    campaign_id: str,
    protocol_fingerprint: str,
    dataset: str,
    cn_count: int,
    repeat: int,
    binary_sha256: str,
    query_sha256: str,
    groundtruth_sha256: str,
    expected_queries: int,
    clients: list[dict[str, object]],
    source_path: Path,
    csv_path: Path,
) -> dict[str, object]:
    if len(clients) != cn_count:
        raise ValueError(
            f"incomplete d-HNSW client coverage: expected {cn_count}, got {len(clients)}"
        )
    processed_values = [integer(row.get("processed_queries"), "processed_queries") for row in clients]
    expected_values = [integer(row.get("expected_queries"), "expected_queries") for row in clients]
    if processed_values != expected_values or sum(processed_values) != expected_queries:
        raise ValueError("d-HNSW client shards do not cover the logical query pool exactly")
    qps_values = [number(row.get("qps"), "client qps") for row in clients]
    recalls = [number(row.get("recall"), "client recall") for row in clients]
    if any(qps <= 0 for qps in qps_values) or any(not 0 <= recall <= 1 for recall in recalls):
        raise ValueError("invalid d-HNSW client QPS or recall")
    processed = sum(processed_values)
    recall = sum(
        value * count for value, count in zip(recalls, processed_values)
    ) / processed
    metrics: dict[str, object] = {
        "processed_queries": processed,
        "expected_queries": expected_queries,
        "failed_queries": 0,
        "qps": sum(qps_values),
        "recall": recall,
        "p50_us": None,
        "p99_us": None,
        "posts_per_query": None,
        "bytes_per_query": None,
        "fairness": jain_fairness(qps_values),
    }
    record = common_record(
        campaign_id=campaign_id,
        protocol_fingerprint=protocol_fingerprint,
        dataset=dataset,
        system="d-HNSW",
        cn_count=cn_count,
        repeat=repeat,
        binary_sha256=binary_sha256,
        query_sha256=query_sha256,
        groundtruth_sha256=groundtruth_sha256,
        metrics=metrics,
        raw_measurements=clients,
    )
    record["throughput_aggregation"] = "sum_of_concurrent_disjoint_client_shards"
    atomic_json(source_path, record)
    append_csv(csv_path, source_path, record)
    return record


def parsed_dhnsw_client(path: Path, *, ef: int, threads: int) -> dict[str, object]:
    text = path.read_text(errors="replace")
    fixed = parse_dhnsw_frontier.parse_fixed_pool_result(text, ef, threads)
    result: dict[str, object] = {
        "path": str(path.resolve()),
        "sha256": file_sha256(path),
        "processed_queries": fixed["processed_queries"],
        "expected_queries": fixed["expected_queries"],
        "failed_queries": fixed["failed_queries"],
        "wall_seconds": fixed["wall_seconds"],
        "qps": fixed["qps"],
        "recall": fixed["recall"],
        "machine_record_prefix_interleavings": fixed[
            "machine_record_prefix_interleavings"
        ],
        "log": text,
    }
    try:
        details = parse_dhnsw_frontier.parse_client_details(text, ef, threads)
    except ValueError as exc:
        result.update(
            {
                "detail_scope": "unavailable_interleaved_stdout",
                "detail_error": str(exc),
                "average_latency_us": None,
                "network_us": None,
                "compute_us": None,
                "meta_us": None,
                "deserialize_us": None,
            }
        )
        return result

    if not math.isclose(
        number(details["recall"], "client-detail recall"),
        number(fixed["recall"], "atomic fixed-pool recall"),
        rel_tol=0.0,
        abs_tol=1e-4,
    ):
        raise ValueError(
            "client-detail recall disagrees with atomic fixed-pool recall: "
            f"detail={details['recall']} atomic={fixed['recall']}"
        )
    result.update(
        {
            "detail_scope": "complete_per_thread_text",
            "detail_error": None,
            "average_latency_us": details["latency_us"],
            "network_us": details["network_us"],
            "compute_us": details["compute_us"],
            "meta_us": details["meta_us"],
            "deserialize_us": details["deserialize_us"],
        }
    )
    return result


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--protocol-fingerprint", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--cn-count", type=int, required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--query-sha256", required=True)
    parser.add_argument("--groundtruth-sha256", required=True)
    parser.add_argument("--expected-queries", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    graph = subparsers.add_parser("graph")
    add_common_arguments(graph)
    graph.add_argument("--system", choices=("SHINE", "SlabWalk"), required=True)
    graph.add_argument("--initiator-json", type=Path, required=True)
    graph.add_argument("--client-log", type=Path, action="append", default=[])

    dhnsw = subparsers.add_parser("dhnsw")
    add_common_arguments(dhnsw)
    dhnsw.add_argument("--client-log", type=Path, action="append", required=True)
    dhnsw.add_argument("--threads", type=int, required=True)
    dhnsw.add_argument("--ef", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    common = {
        "campaign_id": args.campaign_id,
        "protocol_fingerprint": args.protocol_fingerprint,
        "dataset": args.dataset,
        "cn_count": args.cn_count,
        "repeat": args.repeat,
        "binary_sha256": args.binary_sha256,
        "query_sha256": args.query_sha256,
        "groundtruth_sha256": args.groundtruth_sha256,
        "expected_queries": args.expected_queries,
        "source_path": args.source,
        "csv_path": args.csv,
    }
    if args.command == "graph":
        record = record_graph(
            **common,
            system=args.system,
            initiator_json=args.initiator_json,
            client_logs=args.client_log,
        )
    else:
        clients = [
            parsed_dhnsw_client(path, ef=args.ef, threads=args.threads)
            for path in args.client_log
        ]
        record = record_dhnsw_parsed(**common, clients=clients)
    print(json.dumps(record["metrics"], sort_keys=True))


if __name__ == "__main__":
    main()
