#!/usr/bin/env python3
"""Recompute a sealed single-method binary/configuration A/B campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    from . import summarize_vldb_materialization_policy as materialization
    from . import vldb_evidence_bundle as evidence_bundle
except ImportError:
    import summarize_vldb_materialization_policy as materialization
    import vldb_evidence_bundle as evidence_bundle


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUMMARY_METRICS = (
    "qps",
    "recall",
    "p50_us",
    "p95_us",
    "p99_us",
    "sq8_prefix_rejections_per_query",
    "posts_per_query",
    "bytes_per_query",
    "record_write_posts",
    "physical_bytes",
    "build_total_ms",
    "build_rank_ms",
    "build_materialize_ms",
    "build_assemble_ms",
    "build_publish_ms",
)
STATIC_SUMMARY_FIELDS = (
    "compute_host",
    "binary_sha256",
    "mn_binary_sha256",
    "input_signature",
    "result_hash_version",
    "build_mode",
    "build_workers",
    "rank_workers",
    "rank_workers_recorded",
    "staging_bytes",
    "selection_hash",
    "descriptor_version",
    "physical_signature",
    "budget_map_required",
    "result_hash",
)
RAW_FLOAT_FIELDS = {
    "qps": "qps",
    "recall": "recall",
    "p50_us": "p50_us",
    "p95_us": "p95_us",
    "p99_us": "p99_us",
    "posts_per_query": "posts_per_query",
    "bytes_per_query": "bytes_per_query",
    "build_total_ms": "build_total_ms",
    "build_rank_ms": "build_rank_ms",
    "build_materialize_ms": "build_materialize_ms",
    "build_assemble_ms": "build_record_assemble_ms",
    "build_publish_ms": "build_record_publish_ms",
}
RAW_INT_FIELDS = {
    "repeat": "repeat",
    "position": "position",
    "result_hash_version": "result_hash_version",
    "result_hash": "result_hash",
    "processed": "processed",
    "build_workers": "build_workers",
    "rank_workers": "rank_workers",
    "rank_workers_recorded": "rank_workers_recorded",
    "staging_bytes": "staging_bytes",
    "record_write_posts": "record_write_posts",
    "selection_hash": "selection_hash",
    "physical_bytes": "physical_bytes",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing sealed {label}: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty sealed {label}: {path}")
    return rows


def _parse_env(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for assignment in raw.split():
        if "=" not in assignment:
            raise ValueError("malformed variant environment in runs.csv")
        key, value = assignment.split("=", 1)
        if not key or key in parsed:
            raise ValueError("duplicate variant environment key in runs.csv")
        parsed[key] = value
    return dict(sorted(parsed.items()))


def _relocated_artifact(root: Path, child: Path, raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        candidate = (root / path).resolve()
        try:
            candidate.relative_to(child.resolve())
        except ValueError as error:
            raise ValueError(f"{label} path escapes its A/B child") from error
        return candidate
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve()
    except ValueError:
        if len(path.parts) >= 2 and path.parts[-2] == child.name:
            return (child / path.name).resolve()
        raise ValueError(f"{label} path does not identify its relocated child artifact")


def _same_number(actual: Any, expected: Any, label: str) -> None:
    left = float(actual)
    right = float(expected)
    if not math.isfinite(left) or not math.isfinite(right):
        raise ValueError(f"non-finite {label}")
    scale = max(abs(left), abs(right), 1.0)
    absolute = max(8.0 * sys.float_info.epsilon * scale, 1e-12)
    if not math.isclose(left, right, rel_tol=1e-12, abs_tol=absolute):
        raise ValueError(f"sealed {label} does not match its raw artifact")


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


def _single(values: Iterable[Any], label: str) -> Any:
    distinct = set(values)
    if len(distinct) != 1:
        raise ValueError(f"{label} drift within A/B variant")
    return distinct.pop()


def _verify_provenance(
    provenance_path: Path,
    *,
    child: Path,
    row: dict[str, str],
    expected_sha: str,
    expected_compute_host: str,
    expected_method: str,
    result_path: Path,
    stderr_path: Path,
) -> None:
    if not provenance_path.is_file():
        raise ValueError("missing A/B execution provenance")
    if _sha256(provenance_path) != row["execution_provenance_sha256"]:
        raise ValueError("A/B execution provenance digest drift")
    provenance = json.loads(provenance_path.read_text())
    if int(provenance.get("schema_version", -1)) != 1:
        raise ValueError("unsupported A/B execution provenance schema")
    if (
        str(provenance.get("dataset")) != row["dataset"]
        or str(provenance.get("method")) != expected_method
        or str(provenance.get("input_signature")) != row["input_signature"]
    ):
        raise ValueError("A/B execution provenance input identity drift")
    checks = provenance.get("input_verification", {})
    if set(checks) != {"pre_run", "post_run"} or checks["pre_run"] != checks["post_run"]:
        raise ValueError("A/B pre/post input verification drift")
    executables = provenance.get("executables", {})
    compute = executables.get("compute_node", {})
    memory_nodes = executables.get("memory_nodes", [])
    if (
        compute.get("sha256") != expected_sha
        or str(compute.get("host", "")).strip() != expected_compute_host
        or not memory_nodes
        or any(node.get("sha256") != expected_sha for node in memory_nodes)
    ):
        raise ValueError("A/B executable provenance drift")
    artifacts = provenance.get("artifacts", {})
    required = {
        "compute_stdout": result_path,
        "compute_stderr": stderr_path,
        "memory_node_stdout": None,
        "memory_node_stderr": None,
    }
    if set(artifacts) != set(required):
        raise ValueError("A/B execution artifact provenance is incomplete")
    for name, expected_path in required.items():
        record = artifacts[name]
        artifact = (child / str(record.get("path", ""))).resolve()
        try:
            artifact.relative_to(child.resolve())
        except ValueError as error:
            raise ValueError("A/B execution artifact escapes its child") from error
        if not artifact.is_file() or _sha256(artifact) != record.get("sha256"):
            raise ValueError(f"A/B execution artifact hash drift: {name}")
        if expected_path is not None and artifact != expected_path.resolve():
            raise ValueError(f"A/B row does not reference {name}")


def _recompute_row(
    root: Path,
    row: dict[str, str],
    *,
    protocol: dict[str, Any],
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, Any]:
    repeat = int(row["repeat"])
    position = int(row["position"])
    variant = row["variant"]
    child = (root / f"r{repeat}_{position}_{variant}").resolve()
    if not child.is_dir():
        raise ValueError(f"missing A/B child: {child.name}")
    evidence_bundle.verify_bundle(child)

    result_path = _relocated_artifact(root, child, row["json"], "result JSON")
    campaign_path = _relocated_artifact(root, child, row["campaign"], "campaign")
    provenance_path = _relocated_artifact(
        root, child, row["execution_provenance"], "execution provenance"
    )
    stderr_candidates = [
        path for path in child.glob("*.err") if not path.name.endswith(".mn.err")
    ]
    if len(stderr_candidates) != 1:
        raise ValueError("A/B child must contain one compute stderr artifact")
    stderr_path = stderr_candidates[0].resolve()
    if not campaign_path.is_file() or not result_path.is_file():
        raise ValueError("A/B row references a missing child artifact")

    variant_protocol = protocol["variants"][variant]
    environment = variant_protocol["environment"]
    if _parse_env(row["variant_env"]) != environment:
        raise ValueError("A/B row environment differs from protocol")

    _verify_provenance(
        provenance_path,
        child=child,
        row=row,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
        expected_method=str(protocol["method"]),
        result_path=result_path,
        stderr_path=stderr_path,
    )
    capture_build_metrics = protocol["capture_build_metrics"] is True
    if capture_build_metrics:
        try:
            policy = str(environment["SHINE_LAVD_HOTSET"])
            budget = int(environment["SHINE_LAVD_BUDGET_BYTES"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "A/B protocol lacks materialization policy or byte cap"
            ) from error
        recomputed = materialization.load_cell(
            result_path,
            stderr_path,
            dataset=row["dataset"],
            repeat=repeat,
            position=position,
            expected_policy=policy,
            expected_budget=budget,
            binary_sha256=expected_sha,
            input_signature=row["input_signature"],
            source_tree_sha256=str(
                variant_protocol.get("source", protocol["source"])["tree_sha256"]
            ),
            compute_host=expected_compute_host,
        )
    else:
        result = json.loads(result_path.read_text())
        queries = result.get("queries")
        if not isinstance(queries, dict):
            raise ValueError("query-only A/B result has no query metrics")
        processed = int(queries.get("processed", 0))
        expected_queries = int(protocol.get("query_pool_size", 0))
        if (
            processed != expected_queries
            or processed != int(result.get("num_queries", 0))
            or processed <= 0
        ):
            raise ValueError("query-only A/B fixed query pool is incomplete")
        result_hash = int(queries.get("local_result_hash", 0))
        result_hash_version = int(queries.get("local_result_hash_version", 0))
        if protocol.get("require_query_invariants") is True and result_hash == 0:
            raise ValueError("query-only A/B result hash is missing")
        if result_hash_version <= 0:
            raise ValueError("query-only A/B result hash version is missing")
        recomputed = {
            "repeat": repeat,
            "position": position,
            "dataset": row["dataset"],
            "binary_sha256": expected_sha,
            "input_signature": row["input_signature"],
            "result_hash_version": result_hash_version,
            "result_hash": result_hash,
            "processed": processed,
            "qps": float(queries.get("queries_per_sec", 0.0)),
            "recall": float(queries.get("recall", 0.0)),
            "p50_us": float(queries.get("local_latency_p50_us", 0.0)),
            "p95_us": float(queries.get("local_latency_p95_us", 0.0)),
            "p99_us": float(queries.get("local_latency_p99_us", 0.0)),
            "posts_per_query": float(queries.get("rdma_posts", 0.0)) / processed,
            "bytes_per_query": float(queries.get("rdma_reads_in_bytes", 0.0))
            / processed,
            "build_mode": "",
            "build_workers": 0,
            "rank_workers": 0,
            "rank_workers_recorded": 0,
            "staging_bytes": 0,
            "record_write_posts": 0,
            "selection_hash": 0,
            "physical_bytes": 0,
            "build_total_ms": 0.0,
            "build_rank_ms": 0.0,
            "build_materialize_ms": 0.0,
            "build_record_assemble_ms": 0.0,
            "build_record_publish_ms": 0.0,
            "physical_signature": "",
        }
        for field in (
            "qps",
            "recall",
            "p50_us",
            "p95_us",
            "p99_us",
            "posts_per_query",
            "bytes_per_query",
        ):
            value = float(recomputed[field])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"query-only A/B result has invalid {field}")
        if float(recomputed["qps"]) <= 0 or float(recomputed["recall"]) <= 0:
            raise ValueError("query-only A/B result has invalid QPS or recall")

    for csv_field, raw_field in RAW_INT_FIELDS.items():
        if int(row[csv_field]) != int(recomputed[raw_field]):
            raise ValueError(f"sealed {csv_field} does not match its raw artifact")
    for csv_field, raw_field in RAW_FLOAT_FIELDS.items():
        _same_number(row[csv_field], recomputed[raw_field], csv_field)
    for field in ("dataset", "binary_sha256", "input_signature", "build_mode"):
        if str(row[field]) != str(recomputed[field]):
            raise ValueError(f"sealed {field} does not match its raw artifact")
    if row["physical_signature"] != recomputed["physical_signature"]:
        raise ValueError("sealed physical_signature does not match its raw artifact")

    if capture_build_metrics:
        physical = materialization._json_payloads(
            stderr_path, materialization.PHYSICAL_PREFIX
        )
        descriptor_versions = {int(shard["descriptor_version"]) for shard in physical}
        map_states = {int(bool(shard["budget_map_required"])) for shard in physical}
        if descriptor_versions != {int(row["descriptor_version"])}:
            raise ValueError("sealed descriptor_version does not match its raw artifact")
        if map_states != {int(row["budget_map_required"])}:
            raise ValueError("sealed budget_map_required does not match its raw artifact")
    elif int(row["descriptor_version"]) != 0 or int(row["budget_map_required"]) != 0:
        raise ValueError("query-only A/B row contains unexpected build metadata")

    result = json.loads(result_path.read_text())
    queries = result["queries"]
    prefix_rejections = float(queries.get("local_sq8_prefix_rejections", 0.0))
    observed_rejections = prefix_rejections / int(recomputed["processed"])
    _same_number(
        row["sq8_prefix_rejections_per_query"],
        observed_rejections,
        "sq8_prefix_rejections_per_query",
    )
    return {
        **row,
        **{field: float(row[field]) for field in SUMMARY_METRICS},
        "repeat": repeat,
        "position": position,
    }


def _summaries(rows: list[dict[str, Any]], repeats: int) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for variant in ("A", "B"):
        cells = [row for row in rows if row["variant"] == variant]
        if len(cells) != repeats or sorted(row["repeat"] for row in cells) != list(
            range(repeats)
        ):
            raise ValueError(f"incomplete A/B variant {variant}")
        record: dict[str, Any] = {
            "dataset": _single((row["dataset"] for row in cells), "dataset"),
            "variant": variant,
            "label": _single((row["label"] for row in cells), "label"),
            "n": len(cells),
        }
        for field in STATIC_SUMMARY_FIELDS:
            record[field] = _single((row[field] for row in cells), field)
        for metric in SUMMARY_METRICS:
            values = [float(row[metric]) for row in cells]
            record[f"{metric}_mean"] = statistics.mean(values)
            record[f"{metric}_ci95"] = "" if len(values) < 2 else _ci95(values)
        summaries.append(record)
    return summaries


def _paired_comparison(
    rows: list[dict[str, Any]], summaries: list[dict[str, Any]], repeats: int
) -> dict[str, Any]:
    paired: list[dict[str, Any]] = []
    for repeat in range(repeats):
        cell = {row["variant"]: row for row in rows if row["repeat"] == repeat}
        if set(cell) != {"A", "B"}:
            raise ValueError(f"repeat {repeat} is not a complete A/B pair")
        a, b = cell["A"], cell["B"]
        for field in (
            "compute_host",
            "input_signature",
            "result_hash_version",
            "result_hash",
            "selection_hash",
            "physical_bytes",
            "physical_signature",
            "rank_workers",
            "rank_workers_recorded",
            "descriptor_version",
            "budget_map_required",
        ):
            if a[field] != b[field]:
                raise ValueError(f"A/B invariant changed: {field}")
        for field in ("recall", "posts_per_query", "bytes_per_query"):
            _same_number(a[field], b[field], f"A/B {field}")
        paired.append(
            {
                "order": "AB" if repeat % 2 == 0 else "BA",
                "qps_delta": b["qps"] - a["qps"],
                "qps_speedup": b["qps"] / a["qps"],
                "recall_delta": b["recall"] - a["recall"],
                "posts_delta": b["posts_per_query"] - a["posts_per_query"],
                "bytes_delta": b["bytes_per_query"] - a["bytes_per_query"],
                "p99_us_delta": b["p99_us"] - a["p99_us"],
                "sq8_prefix_rejections_per_query_delta": (
                    b["sq8_prefix_rejections_per_query"]
                    - a["sq8_prefix_rejections_per_query"]
                ),
                "build_speedup": (
                    a["build_total_ms"] / b["build_total_ms"]
                    if b["build_total_ms"] > 0
                    else 0.0
                ),
                "rank_speedup": (
                    a["build_rank_ms"] / b["build_rank_ms"]
                    if b["build_rank_ms"] > 0
                    else 0.0
                ),
                "materialize_speedup": (
                    a["build_materialize_ms"] / b["build_materialize_ms"]
                    if b["build_materialize_ms"] > 0
                    else 0.0
                ),
            }
        )

    def paired_summary(metric: str, cells: list[dict[str, Any]] = paired) -> tuple[float | None, float | None]:
        values = [float(row[metric]) for row in cells]
        if not values:
            return None, None
        return statistics.mean(values), None if len(values) < 2 else _ci95(values)

    public_names = {
        "qps_delta": "qps_delta_B_minus_A",
        "qps_speedup": "qps_speedup_B_over_A",
        "recall_delta": "recall_delta_B_minus_A",
        "posts_delta": "posts_per_query_delta_B_minus_A",
        "bytes_delta": "bytes_per_query_delta_B_minus_A",
        "p99_us_delta": "p99_us_delta_B_minus_A",
        "sq8_prefix_rejections_per_query_delta": (
            "sq8_prefix_rejections_per_query_delta_B_minus_A"
        ),
        "build_speedup": "build_speedup_A_over_B",
        "rank_speedup": "rank_speedup_A_over_B",
        "materialize_speedup": "materialize_speedup_A_over_B",
    }
    order_stratified: dict[str, Any] = {}
    for order in ("AB", "BA"):
        cells = [row for row in paired if row["order"] == order]
        record: dict[str, Any] = {"n": len(cells)}
        for metric, public_name in public_names.items():
            mean, ci = paired_summary(metric, cells)
            record[f"{public_name}_mean"] = mean
            record[f"{public_name}_ci95"] = ci
        order_stratified[order] = record

    means = {row["variant"]: row for row in summaries}
    comparison: dict[str, Any] = {
        "dataset": means["A"]["dataset"],
        "compute_host": means["A"]["compute_host"],
        "qps_speedup_B_over_A": means["B"]["qps_mean"] / means["A"]["qps_mean"],
        "recall_delta_B_minus_A": means["B"]["recall_mean"] - means["A"]["recall_mean"],
        "posts_per_query_delta_B_minus_A": (
            means["B"]["posts_per_query_mean"]
            - means["A"]["posts_per_query_mean"]
        ),
        "bytes_per_query_delta_B_minus_A": (
            means["B"]["bytes_per_query_mean"]
            - means["A"]["bytes_per_query_mean"]
        ),
        "p99_us_delta_B_minus_A": means["B"]["p99_us_mean"] - means["A"]["p99_us_mean"],
        "sq8_prefix_rejections_per_query_delta_B_minus_A": (
            means["B"]["sq8_prefix_rejections_per_query_mean"]
            - means["A"]["sq8_prefix_rejections_per_query_mean"]
        ),
        "paired_repeats": len(paired),
        "order_stratified": order_stratified,
        "record_write_posts_B_over_A": (
            means["B"]["record_write_posts_mean"]
            / means["A"]["record_write_posts_mean"]
            if means["A"]["record_write_posts_mean"] > 0
            else 0.0
        ),
    }
    for metric, public_name in public_names.items():
        mean, ci = paired_summary(metric)
        comparison[f"paired_{public_name}_mean"] = mean
        comparison[f"paired_{public_name}_ci95"] = ci
    return comparison


def _compare_tree(actual: Any, expected: Any, label: str) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            raise ValueError(f"sealed {label} schema differs from recomputation")
        for key in expected:
            _compare_tree(actual[key], expected[key], f"{label}.{key}")
    elif expected is None or actual is None:
        if actual is not expected:
            raise ValueError(f"sealed {label} differs from recomputation")
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        _same_number(actual, expected, label)
    elif str(actual) != str(expected):
        raise ValueError(f"sealed {label} differs from recomputation")


def _compare_summary_csv(
    stored: list[dict[str, str]], expected: list[dict[str, Any]]
) -> None:
    by_variant = {row["variant"]: row for row in stored}
    if set(by_variant) != {"A", "B"}:
        raise ValueError("sealed A/B summary has an invalid variant set")
    for record in expected:
        actual = by_variant[record["variant"]]
        if set(actual) != set(record):
            raise ValueError("sealed A/B summary schema differs from recomputation")
        for field, value in record.items():
            if value == "":
                if actual[field] != "":
                    raise ValueError(f"sealed summary.{field} differs from recomputation")
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                _same_number(actual[field], value, f"summary.{field}")
            elif str(actual[field]) != str(value):
                raise ValueError(f"sealed summary.{field} differs from recomputation")


def validate_bundle(
    root: Path,
    *,
    expected_sha_a: str,
    expected_sha_b: str,
    expected_compute_host: str,
) -> dict[str, Any]:
    """Verify hashes, raw artifacts, matrix balance, and all derived outputs."""

    root = root.resolve()
    for label, digest in (("A", expected_sha_a), ("B", expected_sha_b)):
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"expected binary SHA {label} must be lowercase hex")
    expected_compute_host = expected_compute_host.strip()
    if not expected_compute_host:
        raise ValueError("expected compute host must not be empty")
    evidence_bundle.verify_bundle(root)

    campaign = json.loads((root / "campaign.json").read_text())
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("sealed A/B campaign lacks a protocol")
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if fingerprint != campaign.get("protocol_fingerprint"):
        raise ValueError("sealed A/B protocol fingerprint drift")
    repeats = int(protocol.get("repeats", 0))
    campaign_kind = str(protocol.get("campaign_kind", ""))
    variants = protocol.get("variants")
    source = protocol.get("source", {})
    capture_build_metrics = protocol.get("capture_build_metrics")
    method = str(protocol.get("method", ""))
    source_identity_version = int(protocol.get("source_identity_version", 1))
    if (
        repeats <= 0
        or campaign_kind not in {"formal", "smoke"}
        or (campaign_kind == "formal" and repeats % 2 != 0)
        or not isinstance(variants, dict)
        or set(variants) != {"A", "B"}
        or protocol.get("compute_host") != expected_compute_host
        or protocol.get("measurement_mode") != "complete_fixed_query_pool"
        or not isinstance(capture_build_metrics, bool)
        or method not in {"slabwalk", "shine"}
        or (capture_build_metrics and method != "slabwalk")
        or protocol.get("compute_recall") is not True
        or protocol.get("require_query_invariants") is not True
        or int(protocol.get("query_tile", -1)) != 1
        or not SHA256_RE.fullmatch(str(source.get("tree_sha256", "")))
        or source_identity_version not in {1, 2}
    ):
        raise ValueError("sealed A/B protocol is unsupported")
    expected_shas = {"A": expected_sha_a, "B": expected_sha_b}
    for variant, expected_sha in expected_shas.items():
        record = variants[variant]
        variant_source = record.get("source", source)
        if (
            record.get("sha256") != expected_sha
            or not isinstance(record.get("environment"), dict)
            or int(record.get("query_contexts", -1)) < 0
            or record.get("expected_build_mode") not in {"serial", "staged"}
            or int(record.get("expected_build_workers", 0)) <= 0
            or not isinstance(variant_source, dict)
            or not SHA256_RE.fullmatch(
                str(variant_source.get("tree_sha256", ""))
            )
            or int(variant_source.get("file_count", 0)) <= 0
            or not isinstance(variant_source.get("tree_scope"), list)
            or not variant_source.get("tree_scope")
            or (
                source_identity_version == 2
                and (
                    not str(variant_source.get("root", "")).strip()
                    or variant_source.get("layout")
                    not in {"repository", "graphbeyond_project"}
                )
            )
        ):
            raise ValueError(f"sealed A/B variant {variant} protocol drift")
    expected_order = ["AB" if repeat % 2 == 0 else "BA" for repeat in range(repeats)]
    if protocol.get("order") != expected_order:
        raise ValueError("sealed A/B order schedule drift")

    runs = _read_csv(root / "runs.csv", "A/B runs")
    if len(runs) != 2 * repeats:
        raise ValueError("incomplete sealed A/B run matrix")
    recomputed: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for row in runs:
        repeat = int(row["repeat"])
        position = int(row["position"])
        variant = row["variant"]
        key = (repeat, position, variant)
        if key in seen:
            raise ValueError("duplicate sealed A/B cell")
        seen.add(key)
        expected_variant = "A" if repeat % 2 == position else "B"
        build_contract_matches = (
            row["build_mode"] == variants[variant]["expected_build_mode"]
            and int(row["build_workers"])
            == int(variants[variant]["expected_build_workers"])
            if capture_build_metrics
            else row["build_mode"] == "" and int(row["build_workers"]) == 0
        )
        if (
            repeat not in range(repeats)
            or position not in {0, 1}
            or variant != expected_variant
            or row["status"] != "ok"
            or row["dataset"] != protocol.get("dataset")
            or row["label"] != variants[variant]["label"]
            or row["compute_host"] != expected_compute_host
            or row["binary_sha256"] != expected_shas[variant]
            or row["mn_binary_sha256"] != expected_shas[variant]
            or int(row["query_contexts"]) != int(variants[variant]["query_contexts"])
            or int(row["compute_recall"]) != int(bool(protocol.get("compute_recall")))
            or int(row["query_tile"]) != int(protocol.get("query_tile", -1))
            or not build_contract_matches
        ):
            raise ValueError("sealed A/B row differs from protocol")
        recomputed.append(
            _recompute_row(
                root,
                row,
                protocol=protocol,
                expected_sha=expected_shas[variant],
                expected_compute_host=expected_compute_host,
            )
        )

    summaries = _summaries(recomputed, repeats)
    _compare_summary_csv(
        _read_csv(root / "summary.csv", "A/B summary"), summaries
    )
    comparison = _paired_comparison(recomputed, summaries, repeats)
    stored_comparison = json.loads((root / "comparison.json").read_text())
    _compare_tree(stored_comparison, comparison, "comparison")
    return {
        "root": str(root),
        "run_count": len(recomputed),
        "paired_repeats": repeats,
        "binary_sha_a": expected_sha_a,
        "binary_sha_b": expected_sha_b,
        "compute_host": expected_compute_host,
        "dataset": protocol["dataset"],
        "method": method,
        "capture_build_metrics": capture_build_metrics,
        "source_identity_version": source_identity_version,
        "source_tree_sha_a": variants["A"].get("source", source)[
            "tree_sha256"
        ],
        "source_tree_sha_b": variants["B"].get("source", source)[
            "tree_sha256"
        ],
        "paired_build_speedup_A_over_B_mean": comparison[
            "paired_build_speedup_A_over_B_mean"
        ],
        "paired_materialize_speedup_A_over_B_mean": comparison[
            "paired_materialize_speedup_A_over_B_mean"
        ],
        "paired_qps_speedup_B_over_A_mean": comparison[
            "paired_qps_speedup_B_over_A_mean"
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-sha-a", required=True)
    parser.add_argument("--expected-sha-b", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    args = parser.parse_args()
    report = validate_bundle(
        args.root,
        expected_sha_a=args.expected_sha_a,
        expected_sha_b=args.expected_sha_b,
        expected_compute_host=args.expected_compute_host,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
