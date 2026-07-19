#!/usr/bin/env python3
"""Assemble the final manuscript-facing claim surface from gated summaries."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import summarize_vldb_headlines as headline_summary
from publication_metadata import normalize_publication_paths, publication_timestamp


DATASETS = ("DEEP10M", "SIFT10M", "TTI10M")
CACHE_RATIOS = {"off": 0, "c5": 5, "c20": 20, "c50": 50}
COLOCATION_INLINE_CODES = {"full": 32, "24": 24, "16": 16, "8": 8, "4": 4, "1": 1}
LAYOUTS = ("legacy", "fixed", "variable")
MN_COUNTS = (1, 3, 5)
BUILD_DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
BUILD_SCALING_10M_EFS = {"DEEP10M": 48, "SIFT10M": 64, "TTI10M": 100}
BUILD_SCALING_STAGE_FIELDS = (
    "lavd_build_fetch_share_pct",
    "lavd_build_parse_share_pct",
    "lavd_build_rank_share_pct",
    "lavd_build_encode_share_pct",
    "lavd_build_metadata_share_pct",
    "lavd_build_materialize_share_pct",
)
WORKER_METHODS = ("SHINE", "SlabWalk", "d-HNSW")
WORKER_COUNTS = (1, 8, 16, 40)
BUDGET_FRACTIONS = {
    "f05": 0.05,
    "f10": 0.10,
    "f25": 0.25,
    "f50": 0.50,
    "f75": 0.75,
    "full": 1.0,
}
RESIDENT_MODES = ("remote", "resident")
RESIDENT_EFS = (50, 100, 200)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GATE_KIND = "vldb_final_evidence_gate"
GATED_SOURCE_NAMES = (
    "cache_summary",
    "colocation_summary",
    "budget_summary",
    "resident_summary",
    "profile_summary",
    "resource_summary",
    "resource_runs",
    "worker_runs",
    "rdma_runs",
    "robustness_runs",
    "topology_summary",
    "lifecycle_refresh",
    "lifecycle_tti",
    "build_summary",
    "build_scaling_10m_summary",
    "physical_design_advisor_report",
)
GATE_HASH_FIELDS = {
    "headline_source_summary": ("frontier", "summary_sha256"),
    "cache_summary": ("cache_control", "summary_sha256"),
    "colocation_summary": ("colocation_control", "summary_sha256"),
    "budget_summary": ("mechanism_controls", "budget_summary_sha256"),
    "resident_summary": ("mechanism_controls", "resident_summary_sha256"),
    "profile_summary": ("query_profile", "summary_sha256"),
    "resource_summary": ("resource_ledger", "summary_sha256"),
    "resource_runs": ("resource_ledger", "runs_sha256"),
    "worker_runs": ("worker_scaling", "runs_sha256"),
    "rdma_runs": ("model_controls", "runs_sha256"),
    "robustness_runs": ("robustness", "runs_sha256"),
    "topology_summary": ("topology_control", "summary_sha256"),
    "lifecycle_refresh": ("lifecycle_controls", "refresh_sha256"),
    "lifecycle_tti": ("lifecycle_controls", "tti_sha256"),
    "build_summary": ("build_cost", "summary_sha256"),
    "build_scaling_10m_summary": ("build_scaling_10m", "summary_sha256"),
    "physical_design_advisor_report": (
        "physical_design_advisor",
        "report_sha256",
    ),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_sha256(value: object, label: str) -> str:
    digest = str(value).strip()
    if SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"invalid {label}: {digest!r}")
    return digest


def load_gate(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ValueError(f"missing evidence gate: {path}")
    try:
        gate = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence gate: {path}") from exc
    if not isinstance(gate, dict):
        raise ValueError(f"invalid evidence gate: {path}")
    if gate.get("kind") != GATE_KIND:
        raise ValueError(
            f"evidence gate kind mismatch: expected {GATE_KIND!r}, "
            f"found {gate.get('kind')!r}"
        )
    if gate.get("ready_for_plotting") is not True:
        raise ValueError("evidence gate is not ready_for_plotting")
    recorded = gate.get("claim_input_sha256")
    if not isinstance(recorded, dict):
        raise ValueError("evidence gate is missing claim_input_sha256")
    required_names = set(GATE_HASH_FIELDS)
    missing = sorted(required_names - set(recorded))
    if missing:
        raise ValueError(f"evidence gate is missing claim input hashes: {missing}")
    for name in sorted(required_names):
        recorded_sha = require_sha256(recorded[name], f"evidence gate {name} SHA")
        section, field = GATE_HASH_FIELDS[name]
        section_report = gate.get(section)
        if not isinstance(section_report, dict):
            raise ValueError(f"evidence gate is missing {section} report")
        report_sha = require_sha256(
            section_report.get(field), f"evidence gate {section}.{field}"
        )
        if recorded_sha != report_sha:
            raise ValueError(f"evidence gate claim hash mismatch for {name}")
    return gate, sha256(path)


def campaign_identities(gate: dict[str, Any]) -> dict[str, dict[str, str]]:
    identities = gate.get("campaign_identities")
    if not isinstance(identities, dict):
        raise ValueError("evidence gate is missing campaign identities")
    output: dict[str, dict[str, str]] = {}
    for section in ("colocation_control", "mechanism_controls"):
        identity = identities.get(section)
        report = gate.get(section)
        if not isinstance(identity, dict) or not isinstance(report, dict):
            raise ValueError(f"evidence gate is missing {section} campaign identity")
        campaign_id = str(identity.get("campaign_id", "")).strip()
        if not campaign_id:
            raise ValueError(f"evidence gate {section} campaign ID is missing")
        fingerprint = require_sha256(
            identity.get("protocol_fingerprint"),
            f"evidence gate {section} protocol fingerprint",
        )
        if (
            report.get("campaign_id") != campaign_id
            or report.get("protocol_fingerprint") != fingerprint
        ):
            raise ValueError(f"evidence gate {section} campaign identity mismatch")
        output[section] = {
            "campaign_id": campaign_id,
            "protocol_fingerprint": fingerprint,
        }
    return output


def verify_gated_sources(
    sources: dict[str, Path], gate: dict[str, Any]
) -> dict[str, str]:
    recorded = gate["claim_input_sha256"]
    observed: dict[str, str] = {}
    for name in GATED_SOURCE_NAMES:
        path = sources[name]
        if not path.is_file():
            raise ValueError(f"missing claim input: {path}")
        actual = sha256(path)
        expected = str(recorded[name])
        if actual != expected:
            raise ValueError(
                f"{name} SHA mismatch against evidence gate: "
                f"{actual} != {expected}"
            )
        observed[name] = actual
    return observed


def verify_frontier_summary(path: Path, gate: dict[str, Any]) -> str:
    if not path.is_file():
        raise ValueError(f"missing frontier summary: {path}")
    actual = sha256(path)
    expected = str(gate["claim_input_sha256"]["headline_source_summary"])
    if actual != expected:
        raise ValueError(
            "frontier_summary SHA mismatch against evidence gate: "
            f"{actual} != {expected}"
        )
    return actual


def read_headline_payload(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ValueError(f"missing headline candidates: {path}")
    try:
        payload = path.read_bytes()
        headline = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid headline candidates: {path}") from exc
    if not isinstance(headline, dict):
        raise ValueError(f"invalid headline candidates: {path}")
    return headline, hashlib.sha256(payload).hexdigest()


def verify_headline_provenance(
    headline: dict[str, Any], gate: dict[str, Any], gate_sha256: str
) -> None:
    if headline.get("gate_sha256") != gate_sha256:
        raise ValueError(
            "headline gate SHA mismatch: "
            f"{headline.get('gate_sha256')!r} != {gate_sha256!r}"
        )
    expected_summary = gate["claim_input_sha256"]["headline_source_summary"]
    if headline.get("summary_sha256") != expected_summary:
        raise ValueError(
            "headline source summary SHA mismatch against evidence gate: "
            f"{headline.get('summary_sha256')!r} != {expected_summary!r}"
        )


def canonical_headline_payload(headline: dict[str, Any]) -> bytes:
    created_utc = headline.get("created_utc")
    if not isinstance(created_utc, str) or not created_utc.strip():
        raise ValueError("headline created_utc is invalid")
    try:
        created = datetime.fromisoformat(created_utc)
    except ValueError as exc:
        raise ValueError("headline created_utc is invalid") from exc
    if created.tzinfo is None:
        raise ValueError("headline created_utc is invalid")

    comparable = dict(headline)
    comparable["created_utc"] = "<production-generated>"
    try:
        return json.dumps(
            comparable,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("headline payload is not canonical JSON") from exc


def verify_recomputed_headline(
    headline: dict[str, Any],
    *,
    frontier_summary: Path,
    gate: Path,
    path_root: Path | None = None,
) -> None:
    expected = headline_summary.derive(frontier_summary, gate)
    if path_root is not None:
        expected = normalize_publication_paths(expected, path_root)
    if canonical_headline_payload(headline) != canonical_headline_payload(expected):
        raise ValueError(
            "headline payload mismatch against recomputed frontier summary"
        )


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing summary: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty summary: {path}")
    return rows


def number(row: dict[str, str], field: str, source: Path) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{source}: invalid numeric field {field}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite numeric field {field}")
    return value


def integer(row: dict[str, str], field: str, source: Path) -> int:
    value = number(row, field, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral field {field}")
    return int(value)


def parse_bool(value: str, source: Path, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{source}: invalid boolean field {field}")


def load_headline(
    path: Path, *, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    if payload is None:
        payload, _ = read_headline_payload(path)
    data = payload
    if data.get("kind") != "vldb_headline_candidates":
        raise ValueError("headline candidate kind mismatch")
    datasets = data.get("datasets")
    ranges = data.get("headline_ranges")
    if not isinstance(datasets, dict) or set(datasets) != set(DATASETS):
        raise ValueError("headline dataset matrix mismatch")
    if not isinstance(ranges, dict):
        raise ValueError("headline ranges missing")
    matched = ranges.get("matched_datasets")
    if (
        not isinstance(matched, list)
        or not matched
        or len(set(matched)) != len(matched)
        or any(name not in DATASETS for name in matched)
    ):
        raise ValueError("headline matched-dataset list is invalid")
    recall_tolerance = data.get("recall_tolerance")
    if (
        not isinstance(recall_tolerance, (int, float))
        or not math.isfinite(float(recall_tolerance))
        or float(recall_tolerance) < 0
    ):
        raise ValueError("headline recall tolerance is invalid")
    recall_floor = data.get("recall_floor")
    if (
        not isinstance(recall_floor, (int, float))
        or not math.isfinite(float(recall_floor))
        or not 0 <= float(recall_floor) <= 1
    ):
        raise ValueError("headline recall floor is invalid")
    result = {
        "matched_datasets": matched,
        "recall_floor": float(recall_floor),
        "recall_tolerance": float(recall_tolerance),
    }
    for field in (
        "high_recall_qps_speedup_min",
        "high_recall_qps_speedup_max",
        "high_recall_post_reduction_min",
        "high_recall_post_reduction_max",
    ):
        value = ranges.get(field)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f"invalid headline range {field}")
        result[field] = float(value)
    high_pairs: dict[str, dict[str, float]] = {}
    for dataset in DATASETS:
        pair = datasets[dataset].get("high_recall_matched_pair")
        expected = dataset in matched
        if not expected:
            if pair is not None:
                raise ValueError(f"{dataset}: unlisted high-recall matched pair")
            continue
        if not isinstance(pair, dict):
            raise ValueError(f"{dataset}: missing high-recall matched pair")
        values: dict[str, float] = {}
        for field in (
            "ef",
            "shine_recall",
            "slabwalk_recall",
            "recall_delta",
            "qps_speedup",
            "post_reduction",
        ):
            value = pair.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{dataset}: invalid high-recall field {field}")
            values[field] = float(value)
        if pair.get("recall_matched") is not True:
            raise ValueError(f"{dataset}: high-recall pair is not marked recall-matched")
        if not all(0 <= values[field] <= 1 for field in ("shine_recall", "slabwalk_recall")):
            raise ValueError(f"{dataset}: invalid high-recall recall value")
        observed_delta = values["slabwalk_recall"] - values["shine_recall"]
        if not math.isclose(values["recall_delta"], observed_delta, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"{dataset}: inconsistent high-recall delta")
        if abs(observed_delta) > float(recall_tolerance) + 1e-12:
            raise ValueError(f"{dataset}: high-recall pair exceeds recall tolerance")
        if min(values["shine_recall"], values["slabwalk_recall"]) + 1e-12 < float(recall_floor):
            raise ValueError(f"{dataset}: high-recall pair is below recall floor")
        if values["ef"] <= 0:
            raise ValueError(f"{dataset}: invalid high-recall ef")
        if values["qps_speedup"] <= 1.0 + 1e-12 or values["post_reduction"] <= 1.0 + 1e-12:
            raise ValueError(f"{dataset}: high-recall pair does not show positive improvement")
        high_pairs[dataset] = values

    if matched != [dataset for dataset in DATASETS if dataset in high_pairs]:
        raise ValueError("headline matched-dataset order is invalid")
    expected_ranges = {
        "high_recall_qps_speedup_min": min(pair["qps_speedup"] for pair in high_pairs.values()),
        "high_recall_qps_speedup_max": max(pair["qps_speedup"] for pair in high_pairs.values()),
        "high_recall_post_reduction_min": min(pair["post_reduction"] for pair in high_pairs.values()),
        "high_recall_post_reduction_max": max(pair["post_reduction"] for pair in high_pairs.values()),
    }
    for field, expected in expected_ranges.items():
        if not math.isclose(result[field], expected, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"headline range {field} does not match retained pairs")
    result["high_recall_matched_pairs"] = high_pairs

    result["dhnsw_max_recall"] = {}
    for dataset in DATASETS:
        row = datasets[dataset].get("dhnsw_max_recall")
        if not isinstance(row, dict):
            raise ValueError(f"{dataset}: missing d-HNSW maximum-recall point")
        values = {}
        for field in ("ef", "recall", "qps"):
            value = row.get(field)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{dataset}: invalid d-HNSW field {field}")
            values[field] = float(value)
        if not 0 <= values["recall"] <= 1 or values["qps"] <= 0:
            raise ValueError(f"{dataset}: invalid d-HNSW maximum-recall point")
        result["dhnsw_max_recall"][dataset] = values
    return result


def load_cache(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_condition = {row.get("condition", ""): row for row in rows}
    if len(rows) != 4 or set(by_condition) != set(CACHE_RATIOS):
        raise ValueError("cache-control matrix mismatch")
    output: dict[str, Any] = {"conditions": {}}
    for condition, expected_ratio in CACHE_RATIOS.items():
        row = by_condition[condition]
        if integer(row, "n", path) != 5:
            raise ValueError("cache control requires five repeats per condition")
        if integer(row, "cache_ratio_pct", path) != expected_ratio:
            raise ValueError(f"cache ratio mismatch for {condition}")
        current = {
            field: number(row, field, path)
            for field in (
                "qps_mean",
                "qps_ci95",
                "qps_change_vs_off_pct",
                "recall_mean",
                "posts_per_query_mean",
                "post_reduction_vs_off_pct",
            )
        }
        current["all_repeats_below_off_min"] = parse_bool(
            row.get("all_repeats_below_off_min", ""), path, "all_repeats_below_off_min"
        )
        output["conditions"][condition] = current
    output["cache_50"] = output["conditions"]["c50"]
    if output["cache_50"]["qps_change_vs_off_pct"] >= 0:
        raise ValueError("cache QPS direction mismatch")
    if output["cache_50"]["post_reduction_vs_off_pct"] <= 0:
        raise ValueError("cache post direction mismatch")
    return output


def load_profile(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    if len(rows) != 1:
        raise ValueError("query profile must contain exactly one row")
    row = rows[0]
    expected = {
        "dataset": "SIFT1M",
        "method": "SHINE-derived",
        "threads": 1,
        "query_contexts": 1,
        "coroutines": 8,
        "ef": 100,
        "top_k": 10,
        "query_rows": 200000,
        "lost_samples": 0,
    }
    for field, value in expected.items():
        actual: object = row.get(field)
        if isinstance(value, int):
            actual = integer(row, field, path)
        if actual != value:
            raise ValueError(f"query-profile protocol mismatch for {field}")
    distance = number(row, "distance_self_percent", path)
    samples = integer(row, "samples", path)
    if not 0 < distance < 100 or samples <= 0:
        raise ValueError("invalid query-profile sample summary")
    return {
        "distance_self_percent": distance,
        "samples": samples,
        "qps": number(row, "qps", path),
        "posts_per_query": number(row, "posts_per_query", path),
        "bytes_per_query": number(row, "bytes_per_query", path),
    }


def load_colocation(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_degree = {row.get("degree", ""): row for row in rows}
    if len(rows) != 6 or set(by_degree) != set(COLOCATION_INLINE_CODES):
        raise ValueError("co-location control matrix mismatch")
    output: dict[str, Any] = {"degrees": {}}
    for degree, expected_inline in COLOCATION_INLINE_CODES.items():
        row = by_degree[degree]
        if integer(row, "n", path) != 5:
            raise ValueError("co-location control requires five repeats per degree")
        if integer(row, "inline_codes", path) != expected_inline:
            raise ValueError(f"co-location inline-code mismatch for {degree}")
        output["degrees"][degree] = {
            field: number(row, field, path)
            for field in (
                "qps_mean",
                "qps_ci95",
                "qps_change_vs_full_pct",
                "recall_mean",
                "recall_ci95",
                "posts_per_query_mean",
                "posts_per_query_ci95",
                "post_increase_vs_full_pct",
                "bytes_per_query_mean",
                "bytes_per_query_ci95",
                "byte_change_vs_full_pct",
                "p99_us_mean",
                "p99_us_ci95",
            )
        }
    output["full"] = output["degrees"]["full"]
    output["degree_1"] = output["degrees"]["1"]
    if output["degree_1"]["qps_change_vs_full_pct"] >= 0:
        raise ValueError("co-location QPS direction mismatch")
    if output["degree_1"]["post_increase_vs_full_pct"] <= 0:
        raise ValueError("co-location post direction mismatch")
    if output["degree_1"]["byte_change_vs_full_pct"] <= 0:
        raise ValueError("co-location byte direction mismatch")
    recalls = [point["recall_mean"] for point in output["degrees"].values()]
    output["recall_mean_min"] = min(recalls)
    output["recall_mean_max"] = max(recalls)
    output["recall_mean_span"] = max(recalls) - min(recalls)
    return output


def load_materialization_budget(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_key = {row.get("key", ""): row for row in rows}
    if len(rows) != 6 or set(by_key) != set(BUDGET_FRACTIONS):
        raise ValueError("materialization-budget matrix mismatch")
    output: dict[str, Any] = {"fractions": {}}
    for key, expected_fraction in BUDGET_FRACTIONS.items():
        row = by_key[key]
        if integer(row, "n", path) != 5:
            raise ValueError("materialization budget requires five repeats per fraction")
        fraction = number(row, "materialized_fraction", path)
        if not math.isclose(fraction, expected_fraction, rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"materialization fraction mismatch for {key}")
        point = {
            field: number(row, field, path)
            for field in (
                "qps_mean",
                "qps_ci95",
                "qps_change_vs_full_pct",
                "materialized_bytes_mean",
                "materialized_bytes_ci95",
                "materialized_byte_fraction_vs_full",
                "recall_mean",
                "recall_ci95",
                "posts_per_query_mean",
                "posts_per_query_ci95",
                "bytes_per_query_mean",
                "bytes_per_query_ci95",
                "p99_us_mean",
                "p99_us_ci95",
                "registered_bytes_mean",
                "actual_write_bytes_mean",
                "budget_map_bytes_mean",
            )
        }
        point["materialized_fraction"] = fraction
        if point["materialized_bytes_mean"] <= 0:
            raise ValueError(f"invalid materialized byte count for {key}")
        if key == "full" and point["budget_map_bytes_mean"] != 0:
            raise ValueError("full materialization unexpectedly has a budget map")
        if key != "full" and point["budget_map_bytes_mean"] <= 0:
            raise ValueError(f"partial materialization is missing a budget map for {key}")
        output["fractions"][key] = point
    byte_counts = [
        output["fractions"][key]["materialized_bytes_mean"]
        for key in BUDGET_FRACTIONS
    ]
    if any(right <= left for left, right in zip(byte_counts, byte_counts[1:])):
        raise ValueError("materialization-budget bytes are not monotone")
    output["fraction_05"] = output["fractions"]["f05"]
    output["full"] = output["fractions"]["full"]
    recalls = [point["recall_mean"] for point in output["fractions"].values()]
    output["recall_mean_span"] = max(recalls) - min(recalls)
    return output


def load_physical_design_advisor(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing physical-design advisor report: {path}")
    try:
        report = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid physical-design advisor report: {path}") from exc
    if (
        not isinstance(report, dict)
        or report.get("kind") != "vldb_physical_design_advisor_validation"
    ):
        raise ValueError("physical-design advisor report kind mismatch")
    if report.get("promotion_ready") is not True or report.get(
        "promotion_failures"
    ) != []:
        raise ValueError("physical-design advisor report is not promotion-ready")
    thresholds = report.get("thresholds")
    expected_thresholds = {
        "recall_min": 0.90,
        "heldout_min_qps_ratio": 0.98,
        "heldout_geomean_qps_ratio": 0.99,
    }
    if thresholds != expected_thresholds:
        raise ValueError("physical-design advisor threshold drift")
    if report.get("training_repeats") != [0, 1, 2] or report.get(
        "heldout_repeats"
    ) != [3, 4, 5]:
        raise ValueError("physical-design advisor split drift")
    if report.get("measured_rows") != 162 or report.get("selection_cells") != 9:
        raise ValueError("physical-design advisor matrix drift")
    selected = report.get("selected_policies")
    if (
        not isinstance(selected, dict)
        or set(selected) - {"benefit", "indeg", "hop"}
        or any(not isinstance(count, int) or count < 0 for count in selected.values())
        or sum(selected.values()) != 9
    ):
        raise ValueError("physical-design advisor selection-count drift")
    ratio_min = report.get("heldout_ratio_min")
    ratio_geomean = report.get("heldout_ratio_geomean")
    if not isinstance(ratio_min, (int, float)) or not math.isfinite(float(ratio_min)):
        raise ValueError("invalid physical-design advisor minimum ratio")
    if not isinstance(ratio_geomean, (int, float)) or not math.isfinite(
        float(ratio_geomean)
    ):
        raise ValueError("invalid physical-design advisor geometric-mean ratio")
    if float(ratio_min) < 0.98 or float(ratio_geomean) < 0.99:
        raise ValueError("physical-design advisor held-out gate is inconsistent")
    return {
        "campaign_id": str(report.get("campaign_id", "")),
        "protocol_fingerprint": str(report.get("protocol_fingerprint", "")),
        "measured_rows": 162,
        "selection_cells": 9,
        "training_repeats": [0, 1, 2],
        "heldout_repeats": [3, 4, 5],
        "thresholds": expected_thresholds,
        "selected_policies": dict(sorted(selected.items())),
        "heldout_ratio_min": float(ratio_min),
        "heldout_ratio_geomean": float(ratio_geomean),
        "claim_boundary": str(report.get("claim_boundary", "")),
    }


def load_resident_upper(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_cell: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        key = (row.get("mode", ""), integer(row, "ef", path))
        if key in by_cell:
            raise ValueError(f"duplicate resident-upper cell {key}")
        by_cell[key] = row
    expected = {(mode, ef) for mode in RESIDENT_MODES for ef in RESIDENT_EFS}
    if len(rows) != 6 or set(by_cell) != expected:
        raise ValueError("resident-upper-graph matrix mismatch")
    output: dict[str, Any] = {"cells": {}}
    fields = (
        "qps_mean",
        "qps_ci95",
        "qps_change_vs_remote_pct",
        "posts_upnav_per_query_mean",
        "posts_upnav_per_query_ci95",
        "upnav_reduction_vs_remote_pct",
        "recall_mean",
        "recall_ci95",
        "posts_per_query_mean",
        "posts_per_query_ci95",
        "bytes_per_query_mean",
        "bytes_per_query_ci95",
        "p99_us_mean",
        "p99_us_ci95",
        "upper_nodes_mean",
        "upper_bytes_mean",
        "upper_build_ms_mean",
    )
    for ef in RESIDENT_EFS:
        pair: dict[str, Any] = {}
        for mode in RESIDENT_MODES:
            row = by_cell[(mode, ef)]
            if integer(row, "n", path) != 5:
                raise ValueError("resident upper graph requires five repeats per cell")
            point = {field: number(row, field, path) for field in fields}
            if mode == "resident":
                if (
                    point["posts_upnav_per_query_mean"] != 0
                    or not math.isclose(
                        point["upnav_reduction_vs_remote_pct"], 100.0,
                        rel_tol=0,
                        abs_tol=1e-9,
                    )
                    or point["upper_nodes_mean"] <= 0
                    or point["upper_bytes_mean"] <= 0
                    or point["upper_build_ms_mean"] <= 0
                ):
                    raise ValueError("resident upper graph invariant mismatch")
            elif (
                point["posts_upnav_per_query_mean"] <= 0
                or point["upper_nodes_mean"] != 0
                or point["upper_bytes_mean"] != 0
                or point["upper_build_ms_mean"] != 0
            ):
                raise ValueError("remote upper graph invariant mismatch")
            pair[mode] = point
        output["cells"][str(ef)] = pair
        output[f"ef{ef}"] = pair
    if output["ef100"]["resident"]["qps_change_vs_remote_pct"] <= 0:
        raise ValueError("resident QPS direction mismatch")
    return output


def resource_point(row: dict[str, str], source: Path) -> dict[str, float]:
    return {
        "recall": number(row, "recall_mean", source),
        "qps": number(row, "qps_mean", source),
        "qps_ci95": number(row, "qps_ci95", source),
        "bytes_per_query": number(row, "query_read_bytes_per_query_mean", source),
        "wrs_per_query": number(row, "query_read_wrs_per_query_mean", source),
        "submits_per_query": number(row, "query_read_submits_per_query_mean", source),
        "read_bytes_gini": number(row, "read_bytes_gini_mean", source),
        "sidecar_gib": number(row, "materialized_sidecar_bytes_mean", source) / 1024**3,
        "storage_amplification": number(row, "storage_amplification_mean", source),
        "max_mn_rss_gib": number(row, "mn_peak_rss_max_kib_mean", source) / 1024**2,
    }


def load_resource(path: Path, runs_path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    cells: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        if row.get("dataset", "").lower() != "gist1m":
            raise ValueError("resource ledger must contain only GIST1M")
        layout = row.get("layout", "")
        mns = integer(row, "memory_nodes", path)
        key = (layout, mns)
        if key in cells:
            raise ValueError(f"duplicate resource cell {key}")
        cells[key] = row
    expected = {(layout, mns) for layout in LAYOUTS for mns in MN_COUNTS}
    if set(cells) != expected:
        raise ValueError("resource matrix mismatch")
    if any(integer(row, "n", path) != 5 for row in cells.values()):
        raise ValueError("resource ledger requires five repeats per cell")
    points = {key: resource_point(row, path) for key, row in cells.items()}

    raw_cells: dict[tuple[str, int], list[dict[str, str]]] = {}
    for row in load_csv(runs_path):
        if row.get("dataset", "").lower() != "gist1m":
            raise ValueError("resource runs must contain only GIST1M")
        key = (row.get("layout", ""), integer(row, "memory_nodes", runs_path))
        raw_cells.setdefault(key, []).append(row)
    if set(raw_cells) != expected:
        raise ValueError("resource-run matrix mismatch")
    for key, raw_rows in raw_cells.items():
        layout, memory_nodes = key
        repeats = sorted(integer(row, "repeat", runs_path) for row in raw_rows)
        if repeats != list(range(5)):
            raise ValueError(f"resource runs require repeats 0--4 for {key}")
        vector_counts = {integer(row, "num_vectors", runs_path) for row in raw_rows}
        if len(vector_counts) != 1 or next(iter(vector_counts)) <= 0:
            raise ValueError(f"resource runs disagree on vector count for {key}")
        num_vectors = next(iter(vector_counts))
        means = {
            field: statistics.mean(number(row, field, runs_path) for row in raw_rows)
            for field in (
                "measured_authoritative_index_bytes",
                "registered_sidecar_bytes",
                "materialized_sidecar_bytes",
                "actual_sidecar_write_bytes",
                "cn_peak_rss_kib",
            )
        }
        if (
            means["measured_authoritative_index_bytes"] <= 0
            or means["materialized_sidecar_bytes"] <= 0
            or means["registered_sidecar_bytes"] < means["materialized_sidecar_bytes"]
            or means["actual_sidecar_write_bytes"] <= 0
            or means["cn_peak_rss_kib"] <= 0
        ):
            raise ValueError(f"invalid resource accounting for {key}")
        sidecar_gib = means["materialized_sidecar_bytes"] / 1024**3
        if not math.isclose(sidecar_gib, points[key]["sidecar_gib"], rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"resource runs and summary disagree for {key}")
        points[key].update({
            "num_vectors": num_vectors,
            "authoritative_gib": means["measured_authoritative_index_bytes"] / 1024**3,
            "registered_gib": means["registered_sidecar_bytes"] / 1024**3,
            "actual_write_gib": means["actual_sidecar_write_bytes"] / 1024**3,
            "cn_peak_rss_gib": means["cn_peak_rss_kib"] / 1024**2,
            "cn_address_map_bytes": 8 * (num_vectors + memory_nodes)
            if layout == "variable"
            else 0,
            "cn_address_map_gib": (
                8 * (num_vectors + memory_nodes) / 1024**3
                if layout == "variable"
                else 0.0
            ),
            "cn_address_map_formula": "8*(N+S)" if layout == "variable" else "0",
        })
    variable = {mns: points[("variable", mns)] for mns in MN_COUNTS}
    return {
        "five_mn": {layout: points[(layout, 5)] for layout in LAYOUTS},
        "variable_scale": {
            "qps_1mn": variable[1]["qps"],
            "qps_5mn": variable[5]["qps"],
            "max_mn_rss_gib_1mn": variable[1]["max_mn_rss_gib"],
            "max_mn_rss_gib_5mn": variable[5]["max_mn_rss_gib"],
            "max_read_bytes_gini": max(point["read_bytes_gini"] for point in variable.values()),
        },
    }


def load_build(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_dataset = {row.get("dataset", ""): row for row in rows}
    if len(rows) != 3 or set(by_dataset) != set(BUILD_DATASETS):
        raise ValueError("build-cost dataset matrix mismatch")
    output = {}
    for dataset in BUILD_DATASETS:
        row = by_dataset[dataset]
        if integer(row, "repeats", path) != 5:
            raise ValueError("build cost requires five repeats per dataset")
        output[dataset] = {
            "build_mean_s": number(row, "build_mean_s", path),
            "build_ci95_half_s": number(row, "build_ci95_half_s", path),
            "build_peak_rss_mean_gib": number(row, "build_peak_rss_mean_gib", path),
            "region_gb": number(row, "region_gb", path),
        }
    return output


def load_build_scaling_10m(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_dataset = {row.get("dataset", ""): row for row in rows}
    if len(rows) != len(DATASETS) or set(by_dataset) != set(DATASETS):
        raise ValueError("10M build-scaling dataset matrix mismatch")
    output = {}
    for dataset in DATASETS:
        row = by_dataset[dataset]
        if integer(row, "n", path) != 5:
            raise ValueError("10M build scaling requires five repeats per dataset")
        if integer(row, "canonical_ef", path) != BUILD_SCALING_10M_EFS[dataset]:
            raise ValueError(f"{dataset}: 10M build-scaling search width mismatch")
        point = {
            "canonical_ef": BUILD_SCALING_10M_EFS[dataset],
            "build_mean_s": number(row, "build_mean_s", path),
            "build_ci95_half_s": number(row, "build_ci95_half_s", path),
            "resident_build_mean_s": number(row, "resident_build_mean_s", path),
            "registered_mean_gib": number(row, "registered_mean_gib", path),
            "materialized_mean_gib": number(row, "materialized_mean_gib", path),
            "stage_share_pct": {
                field.removeprefix("lavd_build_").removesuffix("_share_pct"): number(
                    row, field, path
                )
                for field in BUILD_SCALING_STAGE_FIELDS
            },
        }
        if (
            point["build_mean_s"] <= 0
            or point["build_ci95_half_s"] < 0
            or point["resident_build_mean_s"] <= 0
            or point["registered_mean_gib"] <= 0
            or point["materialized_mean_gib"] <= 0
            or point["materialized_mean_gib"] > point["registered_mean_gib"]
        ):
            raise ValueError(f"{dataset}: invalid 10M build-scaling value")
        stage_sum = sum(point["stage_share_pct"].values())
        if (
            any(value < 0 or value > 100 for value in point["stage_share_pct"].values())
            or stage_sum < 95
            or stage_sum > 100.0001
        ):
            raise ValueError(f"{dataset}: invalid 10M build-stage accounting")
        output[dataset] = point
    return output


def load_worker_scaling(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    cells: dict[tuple[str, int], list[dict[str, str]]] = {}
    for row in rows:
        if row.get("dataset", "") != "DEEP1M":
            raise ValueError("worker scaling must contain only DEEP1M")
        method = row.get("method", "")
        workers = integer(row, "workers", path)
        key = (method, workers)
        cells.setdefault(key, []).append(row)
    expected = {
        (method, workers)
        for method in WORKER_METHODS
        for workers in WORKER_COUNTS
    }
    if set(cells) != expected or len(rows) != len(expected) * 5:
        raise ValueError("worker-scaling matrix mismatch")

    output: dict[str, Any] = {"cells": {}}
    for method in WORKER_METHODS:
        method_cells: dict[str, Any] = {}
        for workers in WORKER_COUNTS:
            current = cells[(method, workers)]
            repeats = sorted(integer(row, "repeat", path) for row in current)
            if repeats != list(range(5)):
                raise ValueError(
                    f"worker scaling requires repeats 0--4 for {method}/{workers}"
                )
            qps = [number(row, "qps", path) for row in current]
            recalls = [number(row, "recall", path) for row in current]
            if any(value <= 0 for value in qps) or any(
                value < 0 or value > 1 for value in recalls
            ):
                raise ValueError(f"invalid worker-scaling values for {method}/{workers}")
            method_cells[str(workers)] = {
                "qps_mean": statistics.mean(qps),
                "recall_mean": statistics.mean(recalls),
            }
        output["cells"][method] = method_cells

    slabwalk = output["cells"]["SlabWalk"]
    qps_1 = slabwalk["1"]["qps_mean"]
    qps_40 = slabwalk["40"]["qps_mean"]
    output["slabwalk"] = {
        "qps_1_worker": qps_1,
        "qps_40_workers": qps_40,
        "throughput_gain": qps_40 / qps_1,
        "recall_mean": statistics.mean(
            slabwalk[str(workers)]["recall_mean"] for workers in WORKER_COUNTS
        ),
    }
    return output


def repeated_mean(
    rows: list[dict[str, str]],
    path: Path,
    metric: str,
    *,
    repeat_field: str,
    repeat_values: list[int],
    **filters: object,
) -> float:
    selected = [
        row
        for row in rows
        if all(str(row.get(key, "")) == str(wanted) for key, wanted in filters.items())
    ]
    repeats = sorted(integer(row, repeat_field, path) for row in selected)
    if repeats != repeat_values:
        label = ", ".join(f"{key}={value}" for key, value in filters.items())
        raise ValueError(f"{path}: expected five repeats for {label}")
    values = [number(row, metric, path) for row in selected]
    return statistics.mean(values)


def load_rdma_controls(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    repeats = list(range(1, 6))

    def mean(metric: str, sweep: str, **filters: object) -> float:
        return repeated_mean(
            rows,
            path,
            metric,
            repeat_field="rep",
            repeat_values=repeats,
            sweep=sweep,
            **filters,
        )

    payload_64 = {
        "avg_us": mean("avg_us", "payload_latency", size=64),
        "p99_us": mean("p99_us", "payload_latency", size=64),
    }
    payload_4096 = {
        "avg_us": mean("avg_us", "payload_latency", size=4096),
        "p99_us": mean("p99_us", "payload_latency", size=4096),
    }
    mtu_1024 = mean("avg_us", "mtu_latency", mtu=1024)
    mtu_4096 = mean("avg_us", "mtu_latency", mtu=4096)
    numa_0 = mean("avg_us", "numa_latency", client_numa=0, server_numa=0)
    numa_1 = mean("avg_us", "numa_latency", client_numa=1, server_numa=1)
    qp1_cq1 = mean("msg_rate_mpps", "qp_cq_msg_rate", qps=1, cq_mod=1)
    qp2_cq16 = mean("msg_rate_mpps", "qp_cq_msg_rate", qps=2, cq_mod=16)
    outs1 = mean("msg_rate_mpps", "outs_msg_rate", outs=1)
    outs16 = mean("msg_rate_mpps", "outs_msg_rate", outs=16)
    positive_measurements = (
        payload_64["avg_us"],
        payload_64["p99_us"],
        payload_4096["avg_us"],
        payload_4096["p99_us"],
        mtu_1024,
        mtu_4096,
        numa_0,
        numa_1,
        qp1_cq1,
        qp2_cq16,
        outs1,
        outs16,
    )
    if any(value <= 0 for value in positive_measurements):
        raise ValueError("invalid RDMA control claim")

    output = {
        "payload_64": payload_64,
        "payload_4096": payload_4096,
        "payload_avg_span": payload_4096["avg_us"] / payload_64["avg_us"],
        "payload_p99_span": payload_4096["p99_us"] / payload_64["p99_us"],
        "qp1_cq1_mops": qp1_cq1,
        "qp2_cq16_mops": qp2_cq16,
        "mtu_mean_span": max(mtu_1024, mtu_4096) / min(mtu_1024, mtu_4096),
        "numa_mean_difference_pct": 100.0 * abs(numa_1 - numa_0) / min(numa_0, numa_1),
        "outs1_mops": outs1,
        "outs16_mops": outs16,
    }
    return output


def load_robustness_controls(path: Path) -> dict[str, Any]:
    rows = [row for row in load_csv(path) if row.get("run_kind") == "measure"]
    repeats = list(range(5))

    def mean(metric: str, factor: str, value: str) -> float:
        return repeated_mean(
            rows,
            path,
            metric,
            repeat_field="repeat",
            repeat_values=repeats,
            factor=factor,
            value=value,
        )

    coroutines = {
        value: {
            "qps": mean("qps", "coroutines", value),
            "p99_us": mean("p99_us", "coroutines", value),
        }
        for value in ("1", "4", "16")
    }
    top_k_qps = [mean("qps", "top_k", value) for value in ("1", "10", "50", "100")]
    uniform_qps = mean("qps", "query_distribution", "uniform")
    zipf_qps = mean("qps", "query_distribution", "zipf1.0")
    uniform_p99 = mean("p99_us", "query_distribution", "uniform")
    zipf_p99 = mean("p99_us", "query_distribution", "zipf1.0")
    return {
        "coroutines_1": coroutines["1"],
        "coroutines_4": coroutines["4"],
        "coroutines_16": coroutines["16"],
        "qps_gain_4_to_16_pct": 100.0 * (
            coroutines["16"]["qps"] / coroutines["4"]["qps"] - 1.0
        ),
        "top_k_qps_span_pct": 100.0 * (max(top_k_qps) - min(top_k_qps)) / max(top_k_qps),
        "zipf_qps_difference_pct": 100.0 * abs(zipf_qps - uniform_qps) / uniform_qps,
        "zipf_p99_difference_pct": 100.0 * abs(zipf_p99 - uniform_p99) / uniform_p99,
    }


def load_topology_summary(path: Path) -> dict[str, Any]:
    rows = load_csv(path)
    by_topology = {row.get("topology", ""): row for row in rows}
    if len(rows) != 2 or set(by_topology) != {"loopback", "remote"}:
        raise ValueError("topology summary matrix mismatch")
    output: dict[str, Any] = {}
    for topology, row in by_topology.items():
        if integer(row, "n", path) != 5:
            raise ValueError("topology summary requires five repeats")
        point = {
            "qps": number(row, "qps_mean", path),
            "recall": number(row, "recall_mean", path),
            "latency_us": number(row, "latency_us_mean", path),
            "network_us": number(row, "network_us_mean", path),
        }
        if point["qps"] <= 0 or not 0 <= point["recall"] <= 1:
            raise ValueError("invalid topology summary claim")
        output[topology] = point
    if not math.isclose(
        output["loopback"]["recall"], output["remote"]["recall"], rel_tol=0, abs_tol=1e-6
    ):
        raise ValueError("topology control changed recall")
    return output


def load_lifecycle_boundaries(refresh_path: Path, tti_path: Path) -> dict[str, Any]:
    refresh_rows = load_csv(refresh_path)
    if len(refresh_rows) < 2:
        raise ValueError("incomplete lifecycle refresh matrix")
    amplifications = [
        number(row, "write_amp_blocks_per_insert", refresh_path) for row in refresh_rows
    ]
    recalls = [number(row, "recall", refresh_path) for row in refresh_rows]
    if (
        any(row.get("byte_identical") != "PASS" for row in refresh_rows)
        or max(recalls) - min(recalls) > 1e-9
    ):
        raise ValueError("lifecycle refresh correctness mismatch")

    config_keys = {
        "fp32 baseline": "fp32",
        "sq8 Slabs": "sq8",
        "RaBitQ-2 Slabs": "rabitq2",
        "RaBitQ-4 Slabs": "rabitq4",
    }
    tti_rows = {
        row.get("config", ""): row
        for row in load_csv(tti_path)
        if row.get("config", "") in config_keys and integer(row, "threads", tti_path) == 1
    }
    if set(tti_rows) != set(config_keys):
        raise ValueError("TTI lifecycle boundary matrix mismatch")
    tti = {}
    for config, key in config_keys.items():
        row = tti_rows[config]
        point = {
            "qps": number(row, "qps", tti_path),
            "recall": number(row, "recall", tti_path),
            "posts_per_query": number(row, "posts_per_query", tti_path),
        }
        if point["qps"] <= 0 or point["posts_per_query"] <= 0 or not 0 <= point["recall"] <= 1:
            raise ValueError("invalid TTI lifecycle boundary claim")
        tti[key] = point
    return {
        "refresh": {
            "min_records_per_node": min(amplifications),
            "max_records_per_node": max(amplifications),
            "recall": statistics.mean(recalls),
        },
        "tti": tti,
    }


def assemble(
    *,
    gate: Path,
    frontier_summary: Path,
    headline: Path,
    cache_summary: Path,
    colocation_summary: Path,
    budget_summary: Path,
    resident_summary: Path,
    profile_summary: Path,
    resource_summary: Path,
    resource_runs: Path,
    worker_runs: Path,
    rdma_runs: Path,
    robustness_runs: Path,
    topology_summary: Path,
    lifecycle_refresh: Path,
    lifecycle_tti: Path,
    build_summary: Path,
    build_scaling_10m_summary: Path,
    physical_design_advisor_report: Path,
    out: Path,
    path_root: Path | None = None,
) -> None:
    sources = {
        "headline": headline,
        "frontier_summary": frontier_summary,
        "cache_summary": cache_summary,
        "colocation_summary": colocation_summary,
        "budget_summary": budget_summary,
        "resident_summary": resident_summary,
        "profile_summary": profile_summary,
        "resource_summary": resource_summary,
        "resource_runs": resource_runs,
        "worker_runs": worker_runs,
        "rdma_runs": rdma_runs,
        "robustness_runs": robustness_runs,
        "topology_summary": topology_summary,
        "lifecycle_refresh": lifecycle_refresh,
        "lifecycle_tti": lifecycle_tti,
        "build_summary": build_summary,
        "build_scaling_10m_summary": build_scaling_10m_summary,
        "physical_design_advisor_report": physical_design_advisor_report,
    }
    gate_report, gate_digest = load_gate(gate)
    observed_source_sha = verify_gated_sources(sources, gate_report)
    observed_source_sha["frontier_summary"] = verify_frontier_summary(
        frontier_summary, gate_report
    )
    headline_payload, headline_digest = read_headline_payload(headline)
    frontier_claims = load_headline(headline, payload=headline_payload)
    verify_headline_provenance(headline_payload, gate_report, gate_digest)
    verify_recomputed_headline(
        headline_payload,
        frontier_summary=frontier_summary,
        gate=gate,
        path_root=path_root,
    )
    observed_source_sha["headline"] = headline_digest
    identities = campaign_identities(gate_report)
    report = {
        "kind": "vldb_manuscript_claims",
        "created_utc": publication_timestamp(),
        "gate_sha256": gate_digest,
        "campaign_identities": identities,
        "source_sha256": observed_source_sha,
        "frontier": frontier_claims,
        "cache_control": load_cache(cache_summary),
        "colocation_control": load_colocation(colocation_summary),
        "materialization_budget": load_materialization_budget(budget_summary),
        "resident_upper_graph": load_resident_upper(resident_summary),
        "query_profile": load_profile(profile_summary),
        "resource_ledger": load_resource(resource_summary, resource_runs),
        "worker_scaling": load_worker_scaling(worker_runs),
        "rdma_controls": load_rdma_controls(rdma_runs),
        "robustness_controls": load_robustness_controls(robustness_runs),
        "topology_control": load_topology_summary(topology_summary),
        "lifecycle_boundaries": load_lifecycle_boundaries(
            lifecycle_refresh, lifecycle_tti
        ),
        "build_cost": load_build(build_summary),
        "build_scaling_10m": load_build_scaling_10m(build_scaling_10m_summary),
        "physical_design_advisor": load_physical_design_advisor(
            physical_design_advisor_report
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--frontier-summary", type=Path, required=True)
    parser.add_argument("--headline", type=Path, required=True)
    parser.add_argument("--cache-summary", type=Path, required=True)
    parser.add_argument("--colocation-summary", type=Path, required=True)
    parser.add_argument("--budget-summary", type=Path, required=True)
    parser.add_argument("--resident-summary", type=Path, required=True)
    parser.add_argument("--profile-summary", type=Path, required=True)
    parser.add_argument("--resource-summary", type=Path, required=True)
    parser.add_argument("--resource-runs", type=Path, required=True)
    parser.add_argument("--worker-runs", type=Path, required=True)
    parser.add_argument("--rdma-runs", type=Path, required=True)
    parser.add_argument("--robustness-runs", type=Path, required=True)
    parser.add_argument("--topology-summary", type=Path, required=True)
    parser.add_argument("--lifecycle-refresh", type=Path, required=True)
    parser.add_argument("--lifecycle-tti", type=Path, required=True)
    parser.add_argument("--build-summary", type=Path, required=True)
    parser.add_argument("--build-scaling-10m-summary", type=Path, required=True)
    parser.add_argument("--physical-design-advisor-report", type=Path, required=True)
    parser.add_argument("--path-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    assemble(
        gate=args.gate,
        frontier_summary=args.frontier_summary,
        headline=args.headline,
        cache_summary=args.cache_summary,
        colocation_summary=args.colocation_summary,
        budget_summary=args.budget_summary,
        resident_summary=args.resident_summary,
        profile_summary=args.profile_summary,
        resource_summary=args.resource_summary,
        resource_runs=args.resource_runs,
        worker_runs=args.worker_runs,
        rdma_runs=args.rdma_runs,
        robustness_runs=args.robustness_runs,
        topology_summary=args.topology_summary,
        lifecycle_refresh=args.lifecycle_refresh,
        lifecycle_tti=args.lifecycle_tti,
        build_summary=args.build_summary,
        build_scaling_10m_summary=args.build_scaling_10m_summary,
        physical_design_advisor_report=args.physical_design_advisor_report,
        out=args.out,
        path_root=args.path_root,
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
