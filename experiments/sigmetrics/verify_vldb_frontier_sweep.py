#!/usr/bin/env python3
"""Independently reparse and verify a SlabWalk/SHINE frontier evidence bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
METHOD_VARIANTS = {
    "SHINE": "shine_path",
    "SlabWalk": "slabwalk_expansion",
}
INPUT_ROLE_ORDER = ("query", "groundtruth", "index_dump")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def contained(root: Path, raw: str, label: str) -> Path:
    require(bool(raw), f"missing {label} path")
    relative = Path(raw)
    require(not relative.is_absolute(), f"{label} path must be bundle-relative")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} path escapes bundle root") from error
    return resolved


def integer(row: dict[str, str], field: str) -> int:
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid integer field {field}") from error


def number(row: dict[str, str], field: str) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric field {field}") from error
    require(math.isfinite(value), f"non-finite numeric field {field}")
    return value


def same_number(actual: float, recorded: float, label: str) -> None:
    require(
        math.isclose(actual, recorded, rel_tol=1e-12, abs_tol=1e-9),
        f"raw JSON {label} differs from frontier CSV",
    )


def verify_harness(root: Path, campaign: dict[str, Any]) -> None:
    record = campaign.get("harness")
    require(isinstance(record, dict), "campaign harness identity is missing")
    manifest = contained(root, str(record.get("manifest", "")), "harness manifest")
    expected_sha = str(record.get("manifest_sha256", ""))
    require(SHA256_RE.fullmatch(expected_sha) is not None, "invalid harness SHA")
    require(manifest.is_file(), "missing frozen harness manifest")
    require(sha256_file(manifest) == expected_sha, "frozen harness manifest drift")
    payload = json.loads(manifest.read_text())
    require(payload.get("schema_version") == 1, "unsupported harness schema")
    entries = payload.get("entries")
    require(isinstance(entries, dict) and entries, "empty frozen harness")
    expected_paths = {manifest.resolve()}
    for role, item in entries.items():
        require(isinstance(item, dict), f"invalid harness entry {role}")
        path = contained(manifest.parent, str(item.get("path", "")), "harness entry")
        expected_paths.add(path)
        require(path.is_file(), f"missing frozen harness entry {role}")
        require(path.stat().st_size == int(item.get("bytes", -1)), "harness size drift")
        require(sha256_file(path) == item.get("sha256"), "harness content drift")
    actual_paths = {path.resolve() for path in manifest.parent.iterdir() if path.is_file()}
    require(actual_paths == expected_paths, "untracked file in frozen harness")


def load_input_signatures(
    root: Path, expected_datasets: set[str], compute_host: str
) -> tuple[dict[str, str], dict[str, str]]:
    path = root / "input_manifest.tsv"
    require(path.is_file(), "missing input_manifest.tsv")
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        require(
            reader.fieldnames == ["dataset", "role", "host", "path", "bytes", "sha256"],
            "unexpected input manifest schema",
        )
        rows = list(reader)
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        dataset = row["dataset"]
        role = row["role"]
        require(dataset in expected_datasets, "unexpected input-manifest dataset")
        require(role in INPUT_ROLE_ORDER, "unexpected input-manifest role")
        require(role not in grouped.setdefault(dataset, {}), "duplicate input-manifest role")
        require(SHA256_RE.fullmatch(row["sha256"]) is not None, "invalid input SHA")
        try:
            size = int(row["bytes"])
        except ValueError as error:
            raise ValueError("invalid input byte count") from error
        require(size > 0 and bool(row["path"]), "empty frontier input")
        grouped[dataset][role] = {
            "dataset": dataset,
            "role": role,
            "host": row["host"],
            "path": row["path"],
            "bytes": size,
            "sha256": row["sha256"],
        }
    require(set(grouped) == expected_datasets, "input-manifest dataset coverage mismatch")
    signatures: dict[str, str] = {}
    memory_hosts: dict[str, str] = {}
    for dataset, records in grouped.items():
        require(set(records) == set(INPUT_ROLE_ORDER), "input role coverage mismatch")
        require(records["query"]["host"] == compute_host, "query host mismatch")
        require(records["groundtruth"]["host"] == compute_host, "groundtruth host mismatch")
        ordered = [records[role] for role in INPUT_ROLE_ORDER]
        signatures[dataset] = canonical_sha256(ordered)
        memory_hosts[dataset] = records["index_dump"]["host"]
    return signatures, memory_hosts


def verify_execution_manifest(
    root: Path,
    row: dict[str, str],
    expected_binary_sha: str,
    required_artifacts: set[str],
) -> None:
    path = contained(root, row["execution_manifest"], "execution manifest")
    require(path.is_file(), "missing execution manifest")
    payload = json.loads(path.read_text())
    require(payload.get("schema_version") == 1, "unsupported execution schema")
    require(payload.get("campaign_id") == row["campaign_id"], "execution campaign mismatch")
    cell = payload.get("cell")
    require(isinstance(cell, dict), "missing execution cell identity")
    for field in ("dataset", "method", "variant", "input_signature"):
        require(cell.get(field) == row[field], f"execution cell {field} mismatch")
    raw_path = contained(root, row["json"], "raw JSON")
    require(cell.get("tag") == raw_path.stem, "execution tag mismatch")
    compute = payload.get("compute_process")
    memory = payload.get("memory_process")
    require(isinstance(compute, dict) and isinstance(memory, dict), "missing process identity")
    for process, host, label in (
        (compute, row["compute_host"], "compute"),
        (memory, row["memory_host"], "memory"),
    ):
        require(process.get("host") == host, f"{label} host mismatch")
        require(process.get("binary_sha256") == expected_binary_sha, f"{label} SHA mismatch")
        require(process.get("identity_verified") is True, f"{label} identity was not verified")
        require(int(process.get("pid", 0)) > 0, f"invalid {label} PID")
        require(int(process.get("proc_starttime", 0)) > 0, f"invalid {label} start time")
        require(bool(process.get("executable")), f"missing {label} executable path")
    require(payload.get("exit_code") == 0, "frontier cell exited unsuccessfully")
    require(
        not payload.get("identity_failure_reason"),
        "frontier cell recorded an identity probe failure",
    )
    artifacts = payload.get("artifacts")
    require(isinstance(artifacts, list) and artifacts, "empty execution artifact inventory")
    seen: set[str] = set()
    for artifact in artifacts:
        require(isinstance(artifact, dict), "invalid execution artifact")
        relative = str(artifact.get("path", ""))
        require(relative not in seen, "duplicate execution artifact")
        seen.add(relative)
        artifact_path = contained(root, relative, "execution artifact")
        require(artifact_path.is_file(), "missing execution artifact")
        require(artifact_path.stat().st_size == int(artifact.get("bytes", -1)), "artifact size drift")
        require(sha256_file(artifact_path) == artifact.get("sha256"), "artifact SHA drift")
    require(required_artifacts <= seen, "row artifacts are absent from execution manifest")


def verify_frontier_bundle(
    root: Path,
    *,
    expected_binary_sha: str,
    expected_campaign_id: str,
    expected_run_id: str,
    expected_run_kind: str,
    expected_datasets: set[str],
    expected_threads: int,
    expected_query_contexts: int,
    expected_coroutines: int,
    expected_trace: bool,
    min_points: int,
) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), "missing frontier bundle")
    require(SHA256_RE.fullmatch(expected_binary_sha) is not None, "invalid expected binary SHA")
    require(expected_datasets and min_points > 0, "empty frontier expectation")
    campaign_path = root / "campaign.json"
    require(campaign_path.is_file(), "missing campaign.json")
    campaign = json.loads(campaign_path.read_text())
    require(campaign.get("schema_version") == 2, "unsupported frontier campaign schema")
    require(campaign.get("campaign_id") == expected_campaign_id, "campaign ID mismatch")
    require(bool(campaign.get("campaign_uuid")), "campaign UUID is missing")
    protocol = campaign.get("protocol")
    require(isinstance(protocol, dict), "campaign protocol is missing")
    require(canonical_sha256(protocol) == campaign.get("protocol_fingerprint"), "campaign fingerprint mismatch")
    protocol_expectations = {
        "binary_sha256": expected_binary_sha,
        "run_id": expected_run_id,
        "run_kind": expected_run_kind,
        "trace": expected_trace,
        "measurement_mode": "fixed_query_pool",
        "workers": expected_threads,
        "query_contexts": expected_query_contexts,
        "coroutines": expected_coroutines,
        "top_k": 10,
    }
    for field, expected in protocol_expectations.items():
        require(protocol.get(field) == expected, f"campaign protocol {field} mismatch")
    require(
        len(protocol.get("datasets", [])) == len(expected_datasets)
        and set(protocol.get("datasets", [])) == expected_datasets,
        "campaign dataset mismatch",
    )
    compute_host = str(protocol.get("compute_host", ""))
    require(bool(compute_host), "campaign compute host is missing")
    tcp_port = int(protocol.get("tcp_port", 0))
    require(tcp_port > 0, "invalid campaign TCP port")
    require(protocol.get("method_order_offset") in {0, 1}, "invalid method-order offset")
    require(
        protocol.get("minimum_frontier_points") == min_points,
        "minimum frontier-point contract mismatch",
    )
    verify_harness(root, campaign)
    input_signatures, memory_hosts = load_input_signatures(
        root, expected_datasets, compute_host
    )

    csv_path = root / "slabwalk_shine_frontier_raw.csv"
    require(csv_path.is_file(), "missing frontier CSV")
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    require(bool(rows), "empty frontier CSV")
    cells: dict[tuple[str, str, int], dict[str, str]] = {}
    for row in rows:
        dataset = row.get("dataset", "")
        method = row.get("method", "")
        require(dataset in expected_datasets, "unexpected frontier dataset")
        require(method in METHOD_VARIANTS, "unexpected frontier method")
        require(row.get("variant") == METHOD_VARIANTS[method], "method variant mismatch")
        require(row.get("status") == "ok", "non-ok frontier row")
        require(row.get("campaign_id") == expected_campaign_id, "row campaign mismatch")
        require(row.get("binary_sha256") == expected_binary_sha, "row binary SHA mismatch")
        require(row.get("mn_binary_sha256") == expected_binary_sha, "row MN binary SHA mismatch")
        require(row.get("input_signature") == input_signatures[dataset], "input signature mismatch")
        require(row.get("compute_host") == compute_host, "row compute host mismatch")
        require(row.get("memory_host") == memory_hosts[dataset], "row memory host mismatch")
        require(row.get("run_id") == expected_run_id, "row run ID mismatch")
        require(row.get("run_kind") == expected_run_kind, "row run kind mismatch")
        require(row.get("trace") == ("1" if expected_trace else "0"), "row trace mismatch")
        require(row.get("measurement_mode") == "fixed_query_pool", "measurement mode mismatch")
        require(integer(row, "threads") == expected_threads, "thread count mismatch")
        require(integer(row, "query_contexts") == expected_query_contexts, "query context mismatch")
        require(integer(row, "coroutines") == expected_coroutines, "coroutine count mismatch")
        require(integer(row, "top_k") == 10, "top-k mismatch")
        require(row.get("metric") in {"l2", "ip"}, "invalid distance metric")
        ef = integer(row, "ef")
        require(ef > 0, "invalid ef")
        key = (dataset, method, ef)
        require(key not in cells, "duplicate frontier cell")
        cells[key] = row
        processed = integer(row, "processed")
        expected_queries = integer(row, "expected_queries")
        require(processed == expected_queries and processed > 0, "incomplete query pool")
        require(integer(row, "failed_queries") == 0, "failed frontier queries")
        recall = number(row, "recall")
        qps = number(row, "qps")
        p50 = number(row, "p50_us")
        p95 = number(row, "p95_us")
        p99 = number(row, "p99_us")
        posts = number(row, "posts_per_q")
        read_bytes = number(row, "bytes_per_q")
        require(0 <= recall <= 1 and qps > 0, "invalid recall or qps")
        require(0 <= p50 <= p95 <= p99, "invalid tail-latency ordering")
        require(posts >= 0 and read_bytes >= 0, "invalid remote accounting")
        raw_path = contained(root, row.get("json", ""), "raw JSON")
        err_path = contained(root, row.get("stderr", ""), "stderr")
        require(raw_path.is_file() and err_path.is_file(), "missing raw frontier artifact")
        required_artifacts = {row["json"], row["stderr"]}
        if expected_trace:
            trace_path = contained(root, row.get("trace_csv", ""), "trace CSV")
            require(trace_path.is_file(), "missing requested frontier trace")
            required_artifacts.add(row["trace_csv"])
        else:
            require(not row.get("trace_csv"), "unexpected frontier trace")
        verify_execution_manifest(
            root, row, expected_binary_sha, required_artifacts
        )
        raw = json.loads(raw_path.read_text())
        require(int(raw.get("query_contexts", -1)) == expected_query_contexts, "raw query-context mismatch")
        raw_queries = raw.get("queries")
        require(isinstance(raw_queries, dict), "raw queries object is missing")
        raw_processed = int(raw_queries.get("processed", -1))
        raw_expected = int(raw.get("num_queries", -1))
        require(raw_processed == processed and raw_expected == expected_queries, "raw query count mismatch")
        require(int(raw_queries.get("local_latency_samples", -1)) == processed, "raw latency samples mismatch")
        same_number(float(raw_queries["recall"]), recall, "recall")
        same_number(float(raw_queries["queries_per_sec"]), qps, "qps")
        same_number(float(raw_queries["local_latency_p50_us"]), p50, "p50")
        same_number(float(raw_queries["local_latency_p95_us"]), p95, "p95")
        same_number(float(raw_queries["local_latency_p99_us"]), p99, "p99")
        same_number(float(raw_queries.get("rdma_posts", 0)) / processed, posts, "posts/query")
        same_number(
            float(raw_queries.get("rdma_reads_in_bytes", 0)) / processed,
            read_bytes,
            "bytes/query",
        )
        row_protocol = {
            "binary_sha256": expected_binary_sha,
            "input_signature": row["input_signature"],
            "compute_host": row["compute_host"],
            "memory_host": row["memory_host"],
            "mn_binary_sha256": row["mn_binary_sha256"],
            "dataset": dataset,
            "method": method,
            "variant": row["variant"],
            "threads": expected_threads,
            "query_contexts": expected_query_contexts,
            "coroutines": expected_coroutines,
            "top_k": 10,
            "metric": row["metric"],
            "measurement_mode": "fixed_query_pool",
            "latency_mode": "thread_local_steady_clock",
            "tcp_port": tcp_port,
            "expected_queries": expected_queries,
            "ef": ef,
            "m": integer(row, "m"),
            "efc": integer(row, "efc"),
            "query_suffix": row["query_suffix"],
            "lavd": integer(row, "lavd"),
            "index_region_bytes": integer(row, "index_region_bytes"),
            "lavd_region_bytes": integer(row, "lavd_region_bytes"),
            "env": row["env"],
        }
        require(
            canonical_sha256(row_protocol) == row.get("protocol_fingerprint"),
            "row protocol fingerprint mismatch",
        )

    setup_fields = (
        "input_signature",
        "compute_host",
        "memory_host",
        "threads",
        "query_contexts",
        "coroutines",
        "top_k",
        "metric",
        "ef",
        "m",
        "efc",
        "query_suffix",
        "index_region_bytes",
    )
    point_counts: set[int] = set()
    for dataset in expected_datasets:
        method_efs = {
            method: {ef for ds, candidate, ef in cells if ds == dataset and candidate == method}
            for method in METHOD_VARIANTS
        }
        require(method_efs["SHINE"] == method_efs["SlabWalk"], "unmatched method EF sets")
        require(len(method_efs["SHINE"]) >= min_points, "insufficient frontier points")
        point_counts.add(len(method_efs["SHINE"]))
        for ef in method_efs["SHINE"]:
            shine = cells[(dataset, "SHINE", ef)]
            slab = cells[(dataset, "SlabWalk", ef)]
            require(
                all(shine[field] == slab[field] for field in setup_fields),
                "unmatched SHINE/SlabWalk protocol cell",
            )
    require(len(point_counts) == 1, "datasets use inconsistent frontier point counts")
    return {
        "schema_version": 1,
        "campaign_id": expected_campaign_id,
        "binary_sha256": expected_binary_sha,
        "datasets": sorted(expected_datasets),
        "methods": sorted(METHOD_VARIANTS),
        "points_per_method": next(iter(point_counts)),
        "rows": len(rows),
        "status": "verified",
    }


def parse_datasets(raw: str) -> set[str]:
    datasets = {value.strip() for value in raw.split(",") if value.strip()}
    if not datasets:
        raise argparse.ArgumentTypeError("expected datasets cannot be empty")
    return datasets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-binary-sha", required=True)
    parser.add_argument("--expected-campaign-id", required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-run-kind", required=True)
    parser.add_argument("--expected-datasets", type=parse_datasets, required=True)
    parser.add_argument("--expected-threads", type=int, required=True)
    parser.add_argument("--expected-query-contexts", type=int, required=True)
    parser.add_argument("--expected-coroutines", type=int, required=True)
    parser.add_argument("--expected-trace", choices=("0", "1"), required=True)
    parser.add_argument("--min-points", type=int, default=5)
    args = parser.parse_args()
    result = verify_frontier_bundle(
        args.root,
        expected_binary_sha=args.expected_binary_sha,
        expected_campaign_id=args.expected_campaign_id,
        expected_run_id=args.expected_run_id,
        expected_run_kind=args.expected_run_kind,
        expected_datasets=args.expected_datasets,
        expected_threads=args.expected_threads,
        expected_query_contexts=args.expected_query_contexts,
        expected_coroutines=args.expected_coroutines,
        expected_trace=args.expected_trace == "1",
        min_points=args.min_points,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
