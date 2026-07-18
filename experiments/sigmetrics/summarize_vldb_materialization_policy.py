#!/usr/bin/env python3
"""Validate and summarize exact-byte Slab materialization campaigns."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

_explicit_evidence_module = os.environ.get("VLDB_EVIDENCE_BUNDLE_MODULE")
if _explicit_evidence_module:
    _module_path = Path(_explicit_evidence_module).resolve()
    _module_spec = importlib.util.spec_from_file_location(
        "_vldb_evidence_bundle_frozen", _module_path
    )
    if _module_spec is None or _module_spec.loader is None:
        raise ImportError(f"cannot load evidence module: {_module_path}")
    evidence_bundle = importlib.util.module_from_spec(_module_spec)
    sys.modules[_module_spec.name] = evidence_bundle
    _module_spec.loader.exec_module(evidence_bundle)
else:
    try:
        from . import vldb_evidence_bundle as evidence_bundle
    except ImportError:
        import vldb_evidence_bundle as evidence_bundle


POLICY_PREFIX = "LAVD_MATERIALIZATION_POLICY "
PHYSICAL_PREFIX = "LAVD_PHYSICAL_ACCOUNTING "
PUBLICATION_PREFIX = "LAVD_BUILD_PUBLICATION "
PHYSICAL_HASH_VERSION = 2
PHYSICAL_HASH_ALGORITHM = "fnv1a64"
PHYSICAL_HASH_SCOPE = "field_scoped_physical_artifacts"
PHYSICAL_HASH_SCOPES = {
    "header_hash_scope": "replicated_header_source_bytes",
    "descriptor_hash_scope": "descriptor_slice_of_replicated_header",
    "map_hash_scope": "global_budget_map_source_bytes",
    "offset_table_hash_scope": "per_mn_offset_table_source_bytes",
    "record_payload_hash_scope": "per_mn_record_payload_source_bytes",
    "selected_uid_hash_scope": "global_selected_uid_u32le_sequence",
}
PHYSICAL_BUDGET_MAP_OWNER_MN = 0
PHYSICAL_HASH_FIELDS = (
    "header_hash",
    "descriptor_hash",
    "map_hash",
    "offset_table_hash",
    "record_payload_hash",
    "selected_uid_hash",
)
REPLICATED_HASH_FIELDS = (
    "header_hash",
    "descriptor_hash",
    "map_hash",
    "selected_uid_hash",
)
PHYSICAL_ABI_FIELDS = (
    "max_record_bytes",
    "max_degree",
    "colocated_degree",
    "slot_only",
    "budget_map_required",
    "record_layout",
    "scoring_code",
    "scoring_bits",
)
HEX64_RE = re.compile(r"^[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUMMARY_METRICS = (
    "qps",
    "recall",
    "p50_us",
    "p95_us",
    "p99_us",
    "posts_per_query",
    "bytes_per_query",
    "build_total_ms",
    "build_rank_ms",
    "build_materialize_ms",
    "record_write_posts",
    "build_record_assemble_ms",
    "build_record_publish_ms",
)


def _json_payloads(path: Path, prefix: str) -> list[dict[str, Any]]:
    payloads = []
    for line in path.read_text().splitlines():
        if line.startswith(prefix):
            payloads.append(json.loads(line[len(prefix) :]))
    return payloads


def _physical_hash_identity(physical: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(physical, key=lambda shard: int(shard.get("mn", -1)))
    num_mns = {int(shard.get("num_mns", -1)) for shard in ordered}
    if num_mns != {len(ordered)} or [int(shard.get("mn", -1)) for shard in ordered] != list(
        range(len(ordered))
    ):
        raise ValueError("incomplete or duplicate physical MN accounting")
    if {int(shard.get("descriptor_version", -1)) for shard in ordered} != {3}:
        raise ValueError("unsupported physical descriptor version")
    if {int(shard.get("hash_version", -1)) for shard in ordered} != {
        PHYSICAL_HASH_VERSION
    }:
        raise ValueError("unsupported physical hash version")
    if {str(shard.get("hash_algorithm", "")) for shard in ordered} != {
        PHYSICAL_HASH_ALGORITHM
    }:
        raise ValueError("unsupported physical hash algorithm")
    if {str(shard.get("hash_scope", "")) for shard in ordered} != {
        PHYSICAL_HASH_SCOPE
    }:
        raise ValueError("unsupported physical hash scope")
    for field, expected in PHYSICAL_HASH_SCOPES.items():
        if {str(shard.get(field, "")) for shard in ordered} != {expected}:
            raise ValueError(f"unsupported physical {field}")
    owner_mns = {int(shard.get("budget_map_owner_mn", -1)) for shard in ordered}
    if owner_mns != {PHYSICAL_BUDGET_MAP_OWNER_MN}:
        raise ValueError("unsupported physical budget_map_owner_mn")
    for shard in ordered:
        missing_abi = [field for field in PHYSICAL_ABI_FIELDS if field not in shard]
        if missing_abi:
            raise ValueError(
                "missing physical ABI fields: " + ",".join(missing_abi)
            )
        for field in PHYSICAL_HASH_FIELDS:
            value = str(shard.get(field, ""))
            if not HEX64_RE.fullmatch(value):
                raise ValueError(f"missing or malformed physical {field}")
    abi = {
        (
            int(shard["max_record_bytes"]),
            int(shard["max_degree"]),
            int(shard["colocated_degree"]),
            bool(shard["slot_only"]),
            bool(shard["budget_map_required"]),
            str(shard["record_layout"]),
            str(shard["scoring_code"]),
            int(shard["scoring_bits"]),
        )
        for shard in ordered
    }
    if len(abi) != 1:
        raise ValueError("physical ABI drift across MNs")
    map_required = bool(ordered[0]["budget_map_required"])
    budget_map_bytes = [int(shard.get("budget_map_bytes", -1)) for shard in ordered]
    if any(value < 0 for value in budget_map_bytes):
        raise ValueError("missing physical budget_map_bytes")
    if map_required:
        if budget_map_bytes[PHYSICAL_BUDGET_MAP_OWNER_MN] <= 0 or any(
            value != 0
            for mn, value in enumerate(budget_map_bytes)
            if mn != PHYSICAL_BUDGET_MAP_OWNER_MN
        ):
            raise ValueError("budget map is not confined to its declared owner MN")
    elif any(value != 0 for value in budget_map_bytes):
        raise ValueError("budget map bytes exist while the map is disabled")
    replicated = {}
    for field in REPLICATED_HASH_FIELDS:
        values = {str(shard[field]) for shard in ordered}
        if len(values) != 1:
            raise ValueError(f"replicated physical {field} drift across MNs")
        replicated[field] = values.pop()
    signature = hashlib.sha256(
        json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "physical_hash_version": PHYSICAL_HASH_VERSION,
        "physical_hash_algorithm": PHYSICAL_HASH_ALGORITHM,
        "physical_hash_scope": PHYSICAL_HASH_SCOPE,
        **PHYSICAL_HASH_SCOPES,
        "budget_map_owner_mn": PHYSICAL_BUDGET_MAP_OWNER_MN,
        **replicated,
        "offset_table_hashes": ";".join(
            str(shard["offset_table_hash"]) for shard in ordered
        ),
        "record_payload_hashes": ";".join(
            str(shard["record_payload_hash"]) for shard in ordered
        ),
        "physical_signature": signature,
    }


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"expected finite {name}")
    if positive and number <= 0.0:
        raise ValueError(f"expected positive {name}")
    if not positive and number < 0.0:
        raise ValueError(f"expected non-negative {name}")
    return number


def load_cell(
    result_path: Path,
    stderr_path: Path,
    *,
    dataset: str,
    repeat: int,
    position: int,
    expected_policy: str,
    expected_budget: int,
    binary_sha256: str,
    input_signature: str,
    source_tree_sha256: str,
    compute_host: str,
) -> dict[str, Any]:
    result = json.loads(result_path.read_text())
    if not re.fullmatch(r"[0-9a-f]{64}", input_signature):
        raise ValueError("missing or malformed input signature")
    if not re.fullmatch(r"[0-9a-f]{64}", source_tree_sha256):
        raise ValueError("missing or malformed source-tree SHA")
    if not compute_host.strip():
        raise ValueError("missing compute host")
    policies = _json_payloads(stderr_path, POLICY_PREFIX)
    physical = _json_payloads(stderr_path, PHYSICAL_PREFIX)
    publications = _json_payloads(stderr_path, PUBLICATION_PREFIX)
    if len(policies) != 1:
        raise ValueError(f"expected one materialization policy record, got {len(policies)}")
    if not physical:
        raise ValueError("missing physical accounting records")
    if len(publications) != 1:
        raise ValueError(
            f"expected one build publication record, got {len(publications)}"
        )

    policy = policies[0]
    publication = publications[0]
    if policy.get("version") != 1:
        raise ValueError("unsupported materialization policy version")
    if policy.get("policy") != expected_policy:
        raise ValueError("materialization policy mismatch")
    if int(policy.get("requested_bytes", -1)) != expected_budget:
        raise ValueError("materialization byte budget mismatch")
    if publication.get("version") != 1:
        raise ValueError("unsupported build publication version")
    if publication.get("mode") not in {"serial", "staged"}:
        raise ValueError("unsupported build publication mode")
    if int(publication.get("records", -1)) != int(policy["selected_records"]):
        raise ValueError("selection/publication record-count mismatch")
    record_write_posts = int(publication.get("record_write_posts", -1))
    if record_write_posts <= 0 or record_write_posts > int(policy["selected_records"]):
        raise ValueError("invalid record-publication post count")
    if publication["mode"] == "serial" and record_write_posts != int(
        policy["selected_records"]
    ):
        raise ValueError("serial publication must issue one post per record")
    if publication["mode"] == "staged" and int(
        publication.get("staging_bytes", 0)
    ) <= 0:
        raise ValueError("staged publication is missing its bounded buffer size")
    rank_workers_recorded = int("rank_workers" in policy)
    rank_workers = int(policy.get("rank_workers", 1))
    if rank_workers <= 0:
        raise ValueError("invalid rank-worker count")
    physical_identity = _physical_hash_identity(physical)

    physical_bytes = sum(int(shard["materialized_bytes"]) for shard in physical)
    physical_record_bytes = sum(int(shard["record_bytes"]) for shard in physical)
    physical_fixed_bytes = sum(
        int(shard["header_bytes"])
        + int(shard["budget_map_bytes"])
        + int(shard["placement_padding_bytes"])
        + int(shard["offset_table_bytes"])
        for shard in physical
    )
    for shard in physical:
        expected_write_bytes = (
            int(shard["header_bytes"])
            + int(shard["budget_map_bytes"])
            + int(shard["offset_table_bytes"])
            + int(shard["record_bytes"])
        )
        if int(shard["actual_write_bytes"]) != expected_write_bytes:
            raise ValueError("planned/actual writer-byte mismatch")

    if (
        physical_bytes != int(policy["admitted_bytes"])
        or physical_record_bytes != int(policy["record_bytes"])
        or physical_fixed_bytes != int(policy["fixed_bytes"])
    ):
        raise ValueError("planner/writer physical-byte mismatch")
    if physical_bytes > expected_budget:
        raise ValueError("physical materialization exceeds requested cap")
    if expected_budget - physical_bytes != int(policy["unused_bytes"]):
        raise ValueError("unused-byte accounting mismatch")

    queries = result["queries"]
    processed = int(queries["processed"])
    if processed <= 0 or processed != int(result["num_queries"]):
        raise ValueError("incomplete fixed query pool")
    recall = _finite(queries["recall"], "recall", positive=True)
    if not 0.0 < recall <= 1.0:
        raise ValueError("invalid recall")
    result_hash_version = int(queries.get("local_result_hash_version", -1))
    if result_hash_version != 1:
        raise ValueError("unsupported or missing query-result hash version")
    result_hash = int(queries.get("local_result_hash", -1))
    if result_hash < 0:
        raise ValueError("missing query-result hash")

    qps = _finite(queries["queries_per_sec"], "qps", positive=True)
    p50_us = _finite(queries.get("local_latency_p50_us", 0.0), "p50_us")
    p95_us = _finite(queries.get("local_latency_p95_us", 0.0), "p95_us")
    p99_us = _finite(queries.get("local_latency_p99_us", 0.0), "p99_us")
    posts_per_query = _finite(
        float(queries["rdma_posts"]) / processed, "posts_per_query"
    )
    bytes_per_query = _finite(
        float(queries["rdma_reads_in_bytes"]) / processed, "bytes_per_query"
    )
    timings = result.get("timings", {})
    build_total_ms = _finite(timings.get("lavd_build_multi", 0.0), "build_total_ms")
    build_rank_ms = _finite(timings.get("lavd_build_rank", 0.0), "build_rank_ms")
    build_materialize_ms = _finite(
        timings.get("lavd_build_materialize", 0.0), "build_materialize_ms"
    )
    build_record_assemble_ms = _finite(
        timings.get("lavd_build_record_assemble", 0.0),
        "build_record_assemble_ms",
    )
    build_record_publish_ms = _finite(
        timings.get("lavd_build_record_publish", 0.0),
        "build_record_publish_ms",
    )
    return {
        "dataset": dataset,
        "repeat": int(repeat),
        "position": int(position),
        "policy": expected_policy,
        "binary_sha256": binary_sha256,
        "input_signature": input_signature,
        "source_tree_sha256": source_tree_sha256,
        "compute_host": compute_host,
        "requested_bytes": expected_budget,
        "fixed_bytes": int(policy["fixed_bytes"]),
        "record_bytes": int(policy["record_bytes"]),
        "admitted_bytes": int(policy["admitted_bytes"]),
        "physical_bytes": physical_bytes,
        "unused_bytes": int(policy["unused_bytes"]),
        "selected_records": int(policy["selected_records"]),
        "total_records": int(policy["total_records"]),
        "selection_hash": int(policy["selection_hash"]),
        "result_hash_version": result_hash_version,
        "result_hash": result_hash,
        **physical_identity,
        "total_benefit": int(policy["total_benefit"]),
        "build_mode": str(publication["mode"]),
        "build_workers": int(publication["workers"]),
        "rank_workers": rank_workers,
        "rank_workers_recorded": rank_workers_recorded,
        "staging_bytes": int(publication["staging_bytes"]),
        "record_write_posts": record_write_posts,
        "processed": processed,
        "qps": qps,
        "recall": recall,
        "p50_us": p50_us,
        "p95_us": p95_us,
        "p99_us": p99_us,
        "posts_per_query": posts_per_query,
        "bytes_per_query": bytes_per_query,
        "build_total_ms": build_total_ms,
        "build_rank_ms": build_rank_ms,
        "build_materialize_ms": build_materialize_ms,
        "build_record_assemble_ms": build_record_assemble_ms,
        "build_record_publish_ms": build_record_publish_ms,
        "result_json": str(result_path),
        "stderr": str(stderr_path),
    }


def _ci95(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    tcrit = {
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
    }.get(len(values) - 1, 1.96)
    return tcrit * statistics.stdev(values) / math.sqrt(len(values))


def _validate_protocol_matrix(
    rows: list[dict[str, Any]], protocol: dict[str, Any]
) -> None:
    datasets = [str(value) for value in protocol.get("datasets", [])]
    policies = [str(value) for value in protocol.get("policies", [])]
    budgets = [int(value) for value in protocol.get("budget_bytes", [])]
    repeats = int(protocol.get("repeats", 0))
    campaign_kind = str(protocol.get("campaign_kind", "smoke"))
    build_threads = int(protocol.get("build_threads", 0))
    compute_host = str(protocol.get("compute_host", "")).strip()
    staged = protocol.get("staged_build")
    if (
        not datasets
        or len(set(datasets)) != len(datasets)
        or not policies
        or len(set(policies)) != len(policies)
        or not budgets
        or len(set(budgets)) != len(budgets)
        or repeats <= 0
        or campaign_kind not in {"formal", "smoke"}
        or build_threads <= 0
        or not compute_host
        or not isinstance(staged, bool)
    ):
        raise ValueError("invalid materialization campaign protocol")
    if campaign_kind == "formal" and repeats % len(policies) != 0:
        raise ValueError(
            "formal materialization campaign must be position-balanced"
        )

    expected = set()
    for dataset in datasets:
        for budget in budgets:
            for repeat in range(repeats):
                rotation = repeat % len(policies)
                for position in range(len(policies)):
                    policy = policies[(position + rotation) % len(policies)]
                    expected.add((dataset, budget, repeat, position, policy))
    actual = {
        (
            str(row["dataset"]),
            int(row["requested_bytes"]),
            int(row["repeat"]),
            int(row["position"]),
            str(row["policy"]),
        )
        for row in rows
    }
    if actual != expected or len(rows) != len(expected):
        raise ValueError("incomplete or duplicate materialization campaign matrix")

    expected_mode = "staged" if staged else "serial"
    expected_build_workers = build_threads if staged else 1
    for row in rows:
        if str(row.get("compute_host", "")).strip() != compute_host:
            raise ValueError("materialization compute host drift")
        if str(row["build_mode"]) != expected_mode:
            raise ValueError("observed build mode does not match campaign protocol")
        if int(row["build_workers"]) != expected_build_workers:
            raise ValueError("observed build worker count does not match campaign protocol")
        if int(row.get("rank_workers_recorded", 0)) != 1:
            raise ValueError("rank-worker identity was not recorded")
        if int(row["rank_workers"]) != build_threads:
            raise ValueError("observed rank worker count does not match campaign protocol")

    for dataset in datasets:
        cells = [row for row in rows if str(row["dataset"]) == dataset]
        logical_fields = (
            "binary_sha256",
            "input_signature",
            "source_tree_sha256",
            "result_hash_version",
            "result_hash",
            "processed",
            "recall",
        )
        for field in logical_fields:
            if len({str(row[field]) for row in cells}) != 1:
                label = "query-result hash" if field in {"result_hash", "result_hash_version"} else field
                raise ValueError(f"{label} drift across materialization policies")


def summarize_rows(
    rows: Iterable[dict[str, Any]], *, protocol: dict[str, Any]
) -> list[dict[str, Any]]:
    rows = list(rows)
    _validate_protocol_matrix(rows, protocol)
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["dataset"]), int(row["requested_bytes"]), str(row["policy"]))].append(row)

    summaries = []
    for (dataset, requested_bytes, policy), cells in sorted(groups.items()):
        hashes = {str(cell["binary_sha256"]) for cell in cells}
        input_signatures = {str(cell["input_signature"]) for cell in cells}
        source_tree_hashes = {str(cell["source_tree_sha256"]) for cell in cells}
        compute_hosts = {str(cell["compute_host"]) for cell in cells}
        selections = {str(cell["selection_hash"]) for cell in cells}
        physical = {int(cell["physical_bytes"]) for cell in cells}
        build_modes = {str(cell["build_mode"]) for cell in cells}
        build_workers = {int(cell["build_workers"]) for cell in cells}
        rank_workers = {int(cell["rank_workers"]) for cell in cells}
        rank_workers_recorded = {
            int(cell["rank_workers_recorded"]) for cell in cells
        }
        result_hash_versions = {int(cell["result_hash_version"]) for cell in cells}
        result_hashes = {int(cell["result_hash"]) for cell in cells}
        physical_hash_versions = {
            int(cell["physical_hash_version"]) for cell in cells
        }
        physical_hash_algorithms = {
            str(cell["physical_hash_algorithm"]) for cell in cells
        }
        physical_hash_scopes = {str(cell["physical_hash_scope"]) for cell in cells}
        field_hash_scopes = {
            field: {str(cell[field]) for cell in cells}
            for field in PHYSICAL_HASH_SCOPES
        }
        budget_map_owner_mns = {
            int(cell["budget_map_owner_mn"]) for cell in cells
        }
        physical_signatures = {str(cell["physical_signature"]) for cell in cells}
        if (
            len(hashes) != 1
            or len(input_signatures) != 1
            or len(source_tree_hashes) != 1
            or len(compute_hosts) != 1
            or len(selections) != 1
            or len(physical) != 1
            or len(build_modes) != 1
            or len(build_workers) != 1
            or len(rank_workers) != 1
            or len(rank_workers_recorded) != 1
            or len(result_hash_versions) != 1
            or len(result_hashes) != 1
            or len(physical_hash_versions) != 1
            or len(physical_hash_algorithms) != 1
            or len(physical_hash_scopes) != 1
            or any(len(values) != 1 for values in field_hash_scopes.values())
            or len(budget_map_owner_mns) != 1
            or len(physical_signatures) != 1
        ):
            raise ValueError("immutable cell identity drift across repeats")
        record: dict[str, Any] = {
            "dataset": dataset,
            "requested_bytes": requested_bytes,
            "policy": policy,
            "n": len(cells),
            "binary_sha256": hashes.pop(),
            "input_signature": input_signatures.pop(),
            "source_tree_sha256": source_tree_hashes.pop(),
            "compute_host": compute_hosts.pop(),
            "selection_hash": selections.pop(),
            "result_hash_version": result_hash_versions.pop(),
            "result_hash": result_hashes.pop(),
            "physical_hash_version": physical_hash_versions.pop(),
            "physical_hash_algorithm": physical_hash_algorithms.pop(),
            "physical_hash_scope": physical_hash_scopes.pop(),
            **{
                field: values.pop()
                for field, values in field_hash_scopes.items()
            },
            "budget_map_owner_mn": budget_map_owner_mns.pop(),
            "physical_signature": physical_signatures.pop(),
            "physical_bytes": physical.pop(),
            "selected_records": int(cells[0]["selected_records"]),
            "build_mode": build_modes.pop(),
            "build_workers": build_workers.pop(),
            "rank_workers": rank_workers.pop(),
            "rank_workers_recorded": rank_workers_recorded.pop(),
            "staging_bytes": int(cells[0]["staging_bytes"]),
        }
        for metric in SUMMARY_METRICS:
            values = [_finite(cell[metric], metric, positive=(metric == "qps")) for cell in cells]
            record[f"{metric}_mean"] = statistics.mean(values)
            record[f"{metric}_ci95"] = _ci95(values)
        summaries.append(record)
    return summaries


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not rows:
        raise ValueError("refusing empty semantic comparison")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return list(csv.DictReader(buffer))


def _equivalent_summary_rows(
    stored: list[dict[str, str]], recomputed: list[dict[str, str]]
) -> bool:
    """Compare summaries while tolerating only cross-runtime float ULP noise."""
    if len(stored) != len(recomputed):
        return False
    for stored_row, recomputed_row in zip(stored, recomputed):
        if list(stored_row) != list(recomputed_row):
            return False
        for field, stored_value in stored_row.items():
            recomputed_value = recomputed_row[field]
            if stored_value == recomputed_value:
                continue
            if not (field.endswith("_mean") or field.endswith("_ci95")):
                return False
            try:
                left = float(stored_value)
                right = float(recomputed_value)
            except ValueError:
                return False
            if not (math.isfinite(left) and math.isfinite(right)):
                return False
            tolerance = (
                8.0
                * sys.float_info.epsilon
                * max(1.0, abs(left), abs(right))
            )
            if abs(left - right) > tolerance:
                return False
    return True


def _read_csv_rows(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing sealed {label}: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty sealed {label}: {path}")
    return rows


def _path_within(root: Path, raw: str, label: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise ValueError(f"{label} path escapes materialization cell")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} path escapes materialization cell") from error
    return resolved


def _relocated_row_artifact(
    bundle: Path, raw: str, artifact: Path, label: str
) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        candidate = (bundle / path).resolve()
        if candidate == artifact.resolve():
            return candidate
    relative = artifact.resolve().relative_to(bundle.resolve())
    if len(path.parts) >= len(relative.parts) and tuple(
        path.parts[-len(relative.parts) :]
    ) == relative.parts:
        return artifact.resolve()
    raise ValueError(f"{label} path does not identify its sealed artifact")


def _run_key(row: dict[str, Any]) -> tuple[str, int, int, int, str]:
    return (
        str(row["dataset"]),
        int(row["requested_bytes"]),
        int(row["repeat"]),
        int(row["position"]),
        str(row["policy"]),
    )


def _normalized_run_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized = [
        {
            key: value
            for key, value in row.items()
            if key not in {"result_json", "stderr"}
        }
        for row in rows
    ]
    return sorted(
        normalized,
        key=lambda row: (
            row["dataset"],
            int(row["requested_bytes"]),
            int(row["repeat"]),
            int(row["position"]),
            row["policy"],
        ),
    )


def validate_bundle(
    root: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, Any]:
    """Recompute a sealed materialization-policy campaign after relocation."""

    root = root.resolve()
    expected_compute_host = expected_compute_host.strip()
    if not SHA256_RE.fullmatch(expected_sha):
        raise ValueError("expected binary SHA must contain 64 lowercase hex digits")
    if not expected_compute_host:
        raise ValueError("materialization compute host drift: missing expected host")
    evidence_bundle.verify_bundle(root)

    campaign_path = root / "campaign.json"
    campaign = json.loads(campaign_path.read_text())
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("sealed materialization campaign is missing its protocol")
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if fingerprint != campaign.get("protocol_fingerprint"):
        raise ValueError("sealed materialization protocol fingerprint drift")

    datasets = protocol.get("datasets")
    policies = protocol.get("policies")
    budgets = protocol.get("budget_bytes")
    repeats = int(protocol.get("repeats", 0))
    warmups = int(protocol.get("warmups", -1))
    campaign_kind = str(protocol.get("campaign_kind", ""))
    compute_host = str(protocol.get("compute_host", "")).strip()
    source = protocol.get("source", {})
    source_tree_sha = str(source.get("tree_sha256", ""))
    input_signatures = protocol.get("input_signatures")
    if (
        protocol.get("binary_sha256") != expected_sha
        or protocol.get("memory_node_binary_sha256") != expected_sha
    ):
        raise ValueError("sealed materialization binary SHA drift")
    if compute_host != expected_compute_host:
        raise ValueError("sealed materialization compute host drift")
    if (
        not isinstance(datasets, list)
        or not datasets
        or len(set(datasets)) != len(datasets)
        or not isinstance(policies, list)
        or not policies
        or len(set(policies)) != len(policies)
        or not isinstance(budgets, list)
        or not budgets
        or len(set(budgets)) != len(budgets)
        or repeats <= 0
        or warmups < 0
        or campaign_kind not in {"formal", "smoke"}
        or not isinstance(input_signatures, dict)
        or not SHA256_RE.fullmatch(source_tree_sha)
    ):
        raise ValueError("sealed materialization protocol is unsupported")
    if set(policies) - {"benefit", "indeg", "hop"}:
        raise ValueError("sealed materialization protocol has an unsupported policy")
    if campaign_kind == "formal" and repeats % len(policies) != 0:
        raise ValueError("sealed formal materialization protocol is not position-balanced")
    if any(
        not isinstance(value, int) or value <= 0
        for value in budgets
    ):
        raise ValueError("sealed materialization protocol has an invalid budget")
    if set(input_signatures) != set(datasets) or any(
        not SHA256_RE.fullmatch(str(input_signatures[dataset]))
        for dataset in datasets
    ):
        raise ValueError("sealed materialization input-signature map is invalid")

    expected_cells: set[tuple[str, int, str, int, int, str]] = set()
    for dataset in datasets:
        for budget in budgets:
            for warmup in range(warmups):
                for position, policy in enumerate(policies):
                    expected_cells.add(
                        (str(dataset), int(budget), "w", warmup, position, str(policy))
                    )
            for repeat in range(repeats):
                rotation = repeat % len(policies)
                for position in range(len(policies)):
                    policy = policies[(position + rotation) % len(policies)]
                    expected_cells.add(
                        (str(dataset), int(budget), "r", repeat, position, str(policy))
                    )

    raw_campaigns = sorted((root / "raw").glob("**/campaign.json"))
    actual_cells: set[tuple[str, int, str, int, int, str]] = set()
    recomputed: list[dict[str, Any]] = []
    artifacts_by_key: dict[
        tuple[str, int, int, int, str], tuple[Path, Path]
    ] = {}
    for raw_campaign_path in raw_campaigns:
        cell_root = raw_campaign_path.parent.resolve()
        raw_campaign = json.loads(raw_campaign_path.read_text())
        if int(raw_campaign.get("schema_version", -1)) != 2:
            raise ValueError("unsupported materialization cell provenance schema")
        if (
            raw_campaign.get("campaign_id") != campaign.get("campaign_id")
            or raw_campaign.get("campaign_uuid") != campaign.get("campaign_uuid")
            or raw_campaign.get("protocol_fingerprint")
            != campaign.get("protocol_fingerprint")
        ):
            raise ValueError("materialization cell parent identity drift")
        dataset = str(raw_campaign.get("dataset", ""))
        budget = int(raw_campaign.get("budget_bytes", -1))
        kind = str(raw_campaign.get("kind", ""))
        repeat = int(raw_campaign.get("repeat", -1))
        position = int(raw_campaign.get("position", -1))
        policy = str(raw_campaign.get("policy", ""))
        cell_key = (dataset, budget, kind, repeat, position, policy)
        if cell_key in actual_cells:
            raise ValueError("duplicate materialization cell provenance")
        actual_cells.add(cell_key)
        executables = raw_campaign.get("executables", {})
        compute_node = executables.get("compute_node", {})
        memory_node = executables.get("memory_node", {})
        if (
            compute_node.get("sha256") != expected_sha
            or memory_node.get("sha256") != expected_sha
        ):
            raise ValueError("materialization cell executable provenance drift")
        if str(compute_node.get("host", "")).strip() != expected_compute_host:
            raise ValueError("materialization compute host drift in cell provenance")
        if raw_campaign.get("input_signature") != input_signatures.get(dataset):
            raise ValueError("materialization cell input signature drift")
        checks = raw_campaign.get("input_verification", {})
        if set(checks) != {"pre_run", "post_run"} or checks[
            "pre_run"
        ] != checks["post_run"]:
            raise ValueError("materialization cell input verification drift")

        artifact_records = raw_campaign.get("artifacts", {})
        expected_artifacts = {
            "compute_stdout",
            "compute_stderr",
            "memory_node_stdout",
            "memory_node_stderr",
        }
        if set(artifact_records) != expected_artifacts:
            raise ValueError("materialization cell artifact provenance is incomplete")
        artifacts: dict[str, Path] = {}
        for name in sorted(expected_artifacts):
            record = artifact_records[name]
            artifact = _path_within(cell_root, str(record.get("path", "")), name)
            digest = str(record.get("sha256", ""))
            if (
                not SHA256_RE.fullmatch(digest)
                or not artifact.is_file()
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest
            ):
                raise ValueError(f"materialization cell artifact drift: {name}")
            artifacts[name] = artifact
        phasef_sha = raw_campaign.get("phasef_sha256")
        if phasef_sha:
            phasef = cell_root / "phasef.sorted.csv"
            if (
                not SHA256_RE.fullmatch(str(phasef_sha))
                or not phasef.is_file()
                or hashlib.sha256(phasef.read_bytes()).hexdigest() != phasef_sha
            ):
                raise ValueError("materialization Phase-F artifact drift")
        if kind == "r":
            row = load_cell(
                artifacts["compute_stdout"],
                artifacts["compute_stderr"],
                dataset=dataset,
                repeat=repeat,
                position=position,
                expected_policy=policy,
                expected_budget=budget,
                binary_sha256=expected_sha,
                input_signature=str(input_signatures[dataset]),
                source_tree_sha256=source_tree_sha,
                compute_host=expected_compute_host,
            )
            key = _run_key(row)
            if key in artifacts_by_key:
                raise ValueError("duplicate measured materialization row")
            artifacts_by_key[key] = (
                artifacts["compute_stdout"],
                artifacts["compute_stderr"],
            )
            recomputed.append(row)

    if actual_cells != expected_cells or len(raw_campaigns) != len(expected_cells):
        raise ValueError("incomplete or duplicate sealed materialization cell matrix")

    recomputed.sort(key=_run_key)
    stored_runs = _read_csv_rows(root / "runs.csv", "materialization runs")
    stored_by_key = {_run_key(row): row for row in stored_runs}
    if len(stored_by_key) != len(stored_runs) or set(stored_by_key) != set(
        artifacts_by_key
    ):
        raise ValueError("sealed materialization runs matrix drift")
    for key, stored in stored_by_key.items():
        result_artifact, stderr_artifact = artifacts_by_key[key]
        _relocated_row_artifact(
            root, stored.get("result_json", ""), result_artifact, "result_json"
        )
        _relocated_row_artifact(
            root, stored.get("stderr", ""), stderr_artifact, "stderr"
        )
    recomputed_csv = _csv_rows(recomputed)
    if _normalized_run_rows(stored_runs) != _normalized_run_rows(recomputed_csv):
        raise ValueError("semantic runs mismatch in sealed materialization bundle")

    summaries = summarize_rows(recomputed, protocol=protocol)
    stored_summary = _read_csv_rows(
        root / "summary.csv", "materialization summary"
    )
    if not _equivalent_summary_rows(stored_summary, _csv_rows(summaries)):
        raise ValueError("semantic summary mismatch in sealed materialization bundle")

    return {
        "kind": "vldb_materialization_policy_validation_v1",
        "campaign_id": str(campaign.get("campaign_id", "")),
        "campaign_kind": campaign_kind,
        "binary_sha256": expected_sha,
        "source_tree_sha256": source_tree_sha,
        "compute_host": expected_compute_host,
        "datasets": datasets,
        "policies": policies,
        "budget_bytes": budgets,
        "repeats": repeats,
        "warmups": warmups,
        "measured_cells": len(recomputed),
        "summary_sha256": hashlib.sha256(
            (root / "summary.csv").read_bytes()
        ).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    cell = subparsers.add_parser("cell")
    cell.add_argument("--result", type=Path, required=True)
    cell.add_argument("--stderr", type=Path, required=True)
    cell.add_argument("--dataset", required=True)
    cell.add_argument("--repeat", type=int, required=True)
    cell.add_argument("--position", type=int, required=True)
    cell.add_argument("--policy", required=True)
    cell.add_argument("--budget", type=int, required=True)
    cell.add_argument("--binary-sha", required=True)
    cell.add_argument("--input-signature", required=True)
    cell.add_argument("--source-tree-sha", required=True)
    cell.add_argument("--compute-host", required=True)
    cell.add_argument("--out", type=Path, required=True)

    aggregate = subparsers.add_parser("summary")
    aggregate.add_argument("--runs", type=Path, required=True)
    aggregate.add_argument("--campaign", type=Path, required=True)
    aggregate.add_argument("--out", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expected-sha", required=True)
    verify.add_argument("--expected-compute-host", required=True)
    args = parser.parse_args()

    if args.command == "cell":
        row = load_cell(
            args.result,
            args.stderr,
            dataset=args.dataset,
            repeat=args.repeat,
            position=args.position,
            expected_policy=args.policy,
            expected_budget=args.budget,
            binary_sha256=args.binary_sha,
            input_signature=args.input_signature,
            source_tree_sha256=args.source_tree_sha,
            compute_host=args.compute_host,
        )
        _append_row(args.out, row)
    elif args.command == "summary":
        with args.runs.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        campaign = json.loads(args.campaign.read_text())
        protocol = campaign.get("protocol")
        if not isinstance(protocol, dict):
            raise ValueError("campaign is missing its protocol")
        _write_csv(args.out, summarize_rows(rows, protocol=protocol))
    else:
        report = validate_bundle(
            args.bundle,
            expected_sha=args.expected_sha,
            expected_compute_host=args.expected_compute_host,
        )
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
