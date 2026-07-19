#!/usr/bin/env python3
"""Validate and summarize the fixed-pool VLDB multi-CN scaling campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable


DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
SYSTEMS = ("SHINE", "SlabWalk", "d-HNSW")
CN_COUNTS = (1, 2, 3)
REPEATS = 5
EXPECTED_QUERY_COUNTS = {"SIFT1M": 10_000, "DEEP1M": 10_000, "GIST1M": 1_000}
SLABWALK_MIN_3CN_SCALE_CI_LOW = 2.30
SLABWALK_MAX_RECALL_DRIFT = 0.005
SLABWALK_MIN_MULTICN_FAIRNESS_CI_LOW = 0.95
TOOL_SHA_KEYS = (
    "assembler",
    "dhnsw_parser",
    "query_fingerprinter",
    "recorder",
    "runner",
)

T_975 = {
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
}

REQUIRED_FIELDS = (
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

SUMMARY_METRICS = (
    "qps",
    "recall",
    "p50_us",
    "p99_us",
    "posts_per_query",
    "bytes_per_query",
    "fairness",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_sha(value: object, label: str) -> str:
    text = str(value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"invalid {label}: {text!r}")
    return text


def finite_number(value: object, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite {label}: {value!r}")
    return result


def integer(value: object, label: str) -> int:
    result = finite_number(value, label)
    if not result.is_integer():
        raise ValueError(f"non-integral {label}: {value!r}")
    return int(result)


def optional_number(value: object, label: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return finite_number(value, label)


def t_ci_half(values: Iterable[float]) -> float:
    sample = list(values)
    if len(sample) < 2:
        return 0.0
    critical = T_975.get(len(sample) - 1, 1.96)
    return critical * statistics.stdev(sample) / math.sqrt(len(sample))


def summarize_values(values: list[float]) -> dict[str, float | int]:
    mean = statistics.mean(values)
    ci = t_ci_half(values)
    return {
        "n": len(values),
        "mean": mean,
        "median": statistics.median(values),
        "ci95": ci,
        "ci95_low": mean - ci,
        "ci95_high": mean + ci,
        "min": min(values),
        "max": max(values),
    }


def read_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"missing campaign manifest: {path}")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid campaign manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("campaign manifest must be a JSON object")
    if manifest.get("kind") != "vldb_multicn_campaign":
        raise ValueError("campaign kind must be vldb_multicn_campaign")
    if not str(manifest.get("campaign_id", "")).strip():
        raise ValueError("campaign_id must be non-empty")
    validate_sha(manifest.get("protocol_fingerprint"), "protocol fingerprint")
    validate_sha(manifest.get("slabwalk_binary_sha256"), "SlabWalk binary SHA")
    validate_sha(manifest.get("dhnsw_binary_sha256"), "d-HNSW binary SHA")
    validate_sha(
        manifest.get("dhnsw_runtime_manifest_sha256"),
        "d-HNSW runtime manifest SHA",
    )
    tool_sha256 = manifest.get("tool_sha256")
    if not isinstance(tool_sha256, dict) or set(tool_sha256) != set(TOOL_SHA_KEYS):
        raise ValueError(f"tool SHA keys must be exactly {TOOL_SHA_KEYS}")
    for name in TOOL_SHA_KEYS:
        validate_sha(tool_sha256[name], f"{name} tool SHA")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign protocol must be a JSON object")
    encoded_protocol = json.dumps(
        protocol, sort_keys=True, separators=(",", ":")
    ).encode()
    observed_fingerprint = hashlib.sha256(encoded_protocol).hexdigest()
    if observed_fingerprint != manifest["protocol_fingerprint"]:
        raise ValueError("protocol fingerprint does not match campaign protocol")
    if protocol.get("tool_sha256") != tool_sha256:
        raise ValueError("tool SHA identity is not bound to campaign protocol")
    if tuple(manifest.get("datasets", [])) != DATASETS:
        raise ValueError(f"dataset matrix must be exactly {DATASETS}")
    if tuple(manifest.get("systems", [])) != SYSTEMS:
        raise ValueError(f"system matrix must be exactly {SYSTEMS}")
    if tuple(manifest.get("cn_counts", [])) != CN_COUNTS:
        raise ValueError(f"CN matrix must be exactly {CN_COUNTS}")
    if int(manifest.get("repeats", 0)) != REPEATS:
        raise ValueError(f"campaign requires exactly {REPEATS} repeats")
    expected_queries = manifest.get("expected_queries")
    if expected_queries != EXPECTED_QUERY_COUNTS:
        raise ValueError(
            "expected_queries must bind the standard logical query pools: "
            f"{EXPECTED_QUERY_COUNTS}"
        )
    return manifest


def read_raw(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing raw multi-CN CSV: {path}")
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        missing = sorted(set(REQUIRED_FIELDS) - set(reader.fieldnames))
        if missing:
            raise ValueError(f"raw CSV is missing fields: {missing}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"empty raw multi-CN CSV: {path}")
    return rows


def checked_source(raw_csv: Path, row: dict[str, str]) -> tuple[Path, dict[str, object]]:
    relative = Path(row["source"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"invalid source path: {relative}")
    root = raw_csv.parent.resolve()
    source = (raw_csv.parent / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source escapes campaign directory: {relative}") from exc
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"missing or unsafe source file: {relative}")
    expected = validate_sha(row["source_sha256"], "source SHA")
    observed = file_sha256(source)
    if observed != expected:
        raise ValueError(
            f"source SHA mismatch for {relative}: {observed} != {expected}"
        )
    try:
        payload = json.loads(source.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid source JSON: {relative}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"source JSON must be an object: {relative}")
    if payload.get("kind") != "vldb_multicn_raw_source":
        raise ValueError(f"invalid source record kind: {relative}")
    for field, expected_value in (
        ("campaign_id", row["campaign_id"]),
        ("protocol_fingerprint", row["protocol_fingerprint"]),
        ("dataset", row["dataset"]),
        ("system", row["system"]),
        ("cn_count", integer(row["cn_count"], "cn_count")),
        ("repeat", integer(row["repeat"], "repeat")),
        ("binary_sha256", row["binary_sha256"]),
        ("query_canonical_sha256", row["query_canonical_sha256"]),
        (
            "groundtruth_canonical_sha256",
            row["groundtruth_canonical_sha256"],
        ),
    ):
        if payload.get(field) != expected_value:
            raise ValueError(f"source identity mismatch for {relative}: {field}")
    return source, payload


def verify_source_metrics(
    payload: dict[str, object], expected: dict[str, float | int | None], label: str
) -> None:
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"{label}: missing source metrics")
    for key, expected_value in expected.items():
        observed = metrics.get(key)
        if expected_value is None:
            if observed is not None:
                raise ValueError(f"{label}: source metric mismatch for {key}")
            continue
        if isinstance(expected_value, int):
            if integer(observed, f"{label} source metric {key}") != expected_value:
                raise ValueError(f"{label}: source metric mismatch for {key}")
            continue
        observed_number = finite_number(observed, f"{label} source metric {key}")
        if not math.isclose(
            observed_number, expected_value, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(f"{label}: source metric mismatch for {key}")


def normalize_rows(
    manifest: dict[str, object], raw_csv: Path, raw_rows: list[dict[str, str]]
) -> tuple[list[dict[str, object]], int]:
    expected_keys = {
        (dataset, system, cn_count, repeat)
        for dataset in DATASETS
        for system in SYSTEMS
        for cn_count in CN_COUNTS
        for repeat in range(REPEATS)
    }
    seen_keys: set[tuple[str, str, int, int]] = set()
    seen_sources: set[Path] = set()
    query_hashes: dict[str, set[str]] = defaultdict(set)
    groundtruth_hashes: dict[str, set[str]] = defaultdict(set)
    normalized: list[dict[str, object]] = []

    campaign_id = str(manifest["campaign_id"])
    fingerprint = str(manifest["protocol_fingerprint"])
    slab_sha = str(manifest["slabwalk_binary_sha256"])
    dhnsw_sha = str(manifest["dhnsw_binary_sha256"])

    for index, raw in enumerate(raw_rows, start=2):
        label = f"{raw_csv}:{index}"
        if raw["campaign_id"] != campaign_id:
            raise ValueError(f"{label}: campaign ID drift")
        if raw["protocol_fingerprint"] != fingerprint:
            raise ValueError(f"{label}: protocol fingerprint drift")
        dataset = raw["dataset"]
        system = raw["system"]
        cn_count = integer(raw["cn_count"], f"{label} cn_count")
        repeat = integer(raw["repeat"], f"{label} repeat")
        key = (dataset, system, cn_count, repeat)
        if key not in expected_keys:
            raise ValueError(f"{label}: unexpected matrix cell {key}")
        if key in seen_keys:
            raise ValueError(f"{label}: duplicate matrix cell {key}")
        seen_keys.add(key)

        expected_binary = dhnsw_sha if system == "d-HNSW" else slab_sha
        binary = validate_sha(raw["binary_sha256"], f"{label} binary SHA")
        if binary != expected_binary:
            raise ValueError(f"{label}: binary SHA mismatch for {system}")
        query_sha = validate_sha(
            raw["query_canonical_sha256"], f"{label} query canonical SHA"
        )
        gt_sha = validate_sha(
            raw["groundtruth_canonical_sha256"],
            f"{label} ground-truth canonical SHA",
        )
        query_hashes[dataset].add(query_sha)
        groundtruth_hashes[dataset].add(gt_sha)

        processed = integer(raw["processed_queries"], f"{label} processed_queries")
        expected = integer(raw["expected_queries"], f"{label} expected_queries")
        failed = integer(raw["failed_queries"], f"{label} failed_queries")
        dataset_expected = int(manifest["expected_queries"][dataset])
        if processed != expected or expected != dataset_expected or failed != 0:
            raise ValueError(
                f"{label}: fixed-pool query accounting mismatch "
                f"({processed}/{expected}, failed={failed})"
            )

        qps = finite_number(raw["qps"], f"{label} qps")
        recall = finite_number(raw["recall"], f"{label} recall")
        fairness = finite_number(raw["fairness"], f"{label} fairness")
        if qps <= 0 or not 0 <= recall <= 1 or not 0 <= fairness <= 1:
            raise ValueError(f"{label}: invalid QPS, recall, or fairness")
        p50 = optional_number(raw["p50_us"], f"{label} p50_us")
        p99 = optional_number(raw["p99_us"], f"{label} p99_us")
        posts = optional_number(raw["posts_per_query"], f"{label} posts_per_query")
        read_bytes = optional_number(raw["bytes_per_query"], f"{label} bytes_per_query")
        if (p50 is None) != (p99 is None):
            raise ValueError(f"{label}: latency quantiles must be both present or both absent")
        if system != "d-HNSW":
            if posts is None or read_bytes is None:
                raise ValueError(f"{label}: graph-preserving resource metrics are incomplete")
            if cn_count == 1 and (p50 is None or p99 is None):
                raise ValueError(f"{label}: single-CN latency metrics are incomplete")
            if cn_count > 1 and (p50 is not None or p99 is not None):
                raise ValueError(
                    f"{label}: cross-CN latency is unavailable from the frozen binary"
                )
        elif any(value is not None for value in (p50, p99, posts, read_bytes)):
            raise ValueError(f"{label}: unsupported d-HNSW metrics must remain absent")
        if p50 is not None and p99 is not None and not 0 <= p50 <= p99:
            raise ValueError(f"{label}: non-monotonic latency quantiles")
        for value, metric in ((posts, "posts_per_query"), (read_bytes, "bytes_per_query")):
            if value is not None and value < 0:
                raise ValueError(f"{label}: negative {metric}")

        source, source_payload = checked_source(raw_csv, raw)
        if system != "d-HNSW":
            expected_latency_scope = (
                "all_queries_single_cn"
                if cn_count == 1
                else "not_reported_cross_cn_frozen_binary_boundary"
            )
            if source_payload.get("latency_scope") != expected_latency_scope:
                raise ValueError(f"{label}: source latency scope mismatch")
        verify_source_metrics(
            source_payload,
            {
                "processed_queries": processed,
                "expected_queries": expected,
                "failed_queries": failed,
                "qps": qps,
                "recall": recall,
                "p50_us": p50,
                "p99_us": p99,
                "posts_per_query": posts,
                "bytes_per_query": read_bytes,
                "fairness": fairness,
            },
            label,
        )
        if source in seen_sources:
            raise ValueError(f"{label}: source file reused across matrix cells")
        seen_sources.add(source)

        normalized.append(
            {
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
                "dataset": dataset,
                "system": system,
                "cn_count": cn_count,
                "repeat": repeat,
                "binary_sha256": binary,
                "query_canonical_sha256": query_sha,
                "groundtruth_canonical_sha256": gt_sha,
                "processed_queries": processed,
                "expected_queries": expected,
                "failed_queries": failed,
                "qps": qps,
                "recall": recall,
                "p50_us": "" if p50 is None else p50,
                "p99_us": "" if p99 is None else p99,
                "posts_per_query": "" if posts is None else posts,
                "bytes_per_query": "" if read_bytes is None else read_bytes,
                "fairness": fairness,
                "source": raw["source"],
                "source_sha256": raw["source_sha256"],
            }
        )

    if seen_keys != expected_keys:
        missing = sorted(expected_keys - seen_keys)
        extra = sorted(seen_keys - expected_keys)
        raise ValueError(f"incomplete matrix or repeat set: missing={missing}, extra={extra}")
    for dataset in DATASETS:
        if len(query_hashes[dataset]) != 1:
            raise ValueError(f"{dataset}: query canonical SHA differs across systems")
        if len(groundtruth_hashes[dataset]) != 1:
            raise ValueError(f"{dataset}: ground-truth canonical SHA differs across systems")
    normalized.sort(
        key=lambda row: (
            DATASETS.index(str(row["dataset"])),
            SYSTEMS.index(str(row["system"])),
            int(row["cn_count"]),
            int(row["repeat"]),
        )
    )
    return normalized, len(seen_sources)


def build_summary(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for dataset in DATASETS:
        for system in SYSTEMS:
            for cn_count in CN_COUNTS:
                cell = [
                    row
                    for row in rows
                    if row["dataset"] == dataset
                    and row["system"] == system
                    and row["cn_count"] == cn_count
                ]
                record: dict[str, object] = {
                    "dataset": dataset,
                    "system": system,
                    "cn_count": cn_count,
                    "n": len(cell),
                }
                for metric in SUMMARY_METRICS:
                    values = [
                        float(row[metric])
                        for row in cell
                        if row[metric] != "" and row[metric] is not None
                    ]
                    if not values:
                        for suffix in (
                            "mean",
                            "median",
                            "ci95",
                            "ci95_low",
                            "ci95_high",
                            "min",
                            "max",
                        ):
                            record[f"{metric}_{suffix}"] = ""
                        continue
                    stats = summarize_values(values)
                    for suffix, value in stats.items():
                        if suffix != "n":
                            record[f"{metric}_{suffix}"] = value

                one_cn = [
                    row
                    for row in rows
                    if row["dataset"] == dataset
                    and row["system"] == system
                    and row["cn_count"] == 1
                ]
                paired = {
                    int(row["repeat"]): float(row["qps"])
                    for row in one_cn
                }
                ratios = [
                    float(row["qps"]) / paired[int(row["repeat"])]
                    for row in cell
                ]
                scale = summarize_values(ratios)
                for suffix, value in scale.items():
                    if suffix != "n":
                        record[f"qps_scale_from_1cn_{suffix}"] = value
                record["scaling_efficiency_mean"] = float(scale["mean"]) / cn_count
                output.append(record)
    return output


def build_gate(
    manifest: dict[str, object],
    rows: list[dict[str, object]],
    summary: list[dict[str, object]],
    source_count: int,
) -> dict[str, object]:
    failures: list[str] = []
    dataset_checks: list[dict[str, object]] = []
    for dataset in DATASETS:
        scale_row = next(
            row
            for row in summary
            if row["dataset"] == dataset
            and row["system"] == "SlabWalk"
            and row["cn_count"] == 3
        )
        scale_low = float(scale_row["qps_scale_from_1cn_ci95_low"])
        scale_mean = float(scale_row["qps_scale_from_1cn_mean"])
        if scale_low < SLABWALK_MIN_3CN_SCALE_CI_LOW:
            failures.append(
                f"SlabWalk {dataset} 3-CN scale CI lower bound {scale_low:.3f} "
                f"is below {SLABWALK_MIN_3CN_SCALE_CI_LOW:.2f}"
            )

        one_recall = {
            int(row["repeat"]): float(row["recall"])
            for row in rows
            if row["dataset"] == dataset
            and row["system"] == "SlabWalk"
            and row["cn_count"] == 1
        }
        three_recall = {
            int(row["repeat"]): float(row["recall"])
            for row in rows
            if row["dataset"] == dataset
            and row["system"] == "SlabWalk"
            and row["cn_count"] == 3
        }
        recall_drifts = [
            abs(three_recall[repeat] - one_recall[repeat])
            for repeat in range(REPEATS)
        ]
        max_recall_drift = max(recall_drifts)
        if max_recall_drift > SLABWALK_MAX_RECALL_DRIFT:
            failures.append(
                f"SlabWalk {dataset} max 1-to-3-CN recall drift "
                f"{max_recall_drift:.6f} exceeds {SLABWALK_MAX_RECALL_DRIFT:.3f}"
            )

        fairness_checks: dict[str, float] = {}
        for cn_count in (2, 3):
            fairness_row = next(
                row
                for row in summary
                if row["dataset"] == dataset
                and row["system"] == "SlabWalk"
                and row["cn_count"] == cn_count
            )
            fairness_low = float(fairness_row["fairness_ci95_low"])
            fairness_checks[str(cn_count)] = fairness_low
            if fairness_low < SLABWALK_MIN_MULTICN_FAIRNESS_CI_LOW:
                failures.append(
                    f"SlabWalk {dataset} {cn_count}-CN fairness CI lower bound "
                    f"{fairness_low:.3f} is below "
                    f"{SLABWALK_MIN_MULTICN_FAIRNESS_CI_LOW:.2f}"
                )

        dataset_checks.append(
            {
                "dataset": dataset,
                "slabwalk_3cn_scale_mean": scale_mean,
                "slabwalk_3cn_scale_ci95_low": scale_low,
                "slabwalk_max_recall_drift": max_recall_drift,
                "slabwalk_fairness_ci95_low": fairness_checks,
            }
        )

    return {
        "kind": "vldb_multicn_promotion_gate",
        "campaign_id": manifest["campaign_id"],
        "protocol_fingerprint": manifest["protocol_fingerprint"],
        "promotion_ready": not failures,
        "promotion_failures": failures,
        "measured_rows": len(rows),
        "cells": len(summary),
        "source_files_verified": source_count,
        "thresholds": {
            "slabwalk_min_3cn_scale_ci95_low": SLABWALK_MIN_3CN_SCALE_CI_LOW,
            "slabwalk_max_recall_drift": SLABWALK_MAX_RECALL_DRIFT,
            "slabwalk_min_multicn_fairness_ci95_low": (
                SLABWALK_MIN_MULTICN_FAIRNESS_CI_LOW
            ),
        },
        "dataset_checks": dataset_checks,
        "dhnsw_role": (
            "reported as a measured partition-fetch endpoint; its recall is not "
            "silently matched or used to waive the SlabWalk scaling gate"
        ),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def assemble(manifest_path: Path, raw_csv: Path, out_dir: Path) -> dict[str, object]:
    manifest_path = manifest_path.resolve()
    raw_csv = raw_csv.resolve()
    manifest = read_manifest(manifest_path)
    rows, source_count = normalize_rows(manifest, raw_csv, read_raw(raw_csv))
    summary = build_summary(rows)
    gate = build_gate(manifest, rows, summary, source_count)

    if out_dir.exists():
        raise ValueError(f"refusing existing output directory: {out_dir}")
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{out_dir.name}.staging.", dir=out_dir.parent
    ) as temporary:
        staging = Path(temporary)
        write_csv(staging / "runs.csv", rows)
        write_csv(staging / "summary.csv", summary)
        write_json(staging / "gate.json", gate)
        write_json(
            staging / "campaign.json",
            {
                **manifest,
                "input_manifest": str(manifest_path),
                "input_manifest_sha256": file_sha256(manifest_path),
                "input_runs": str(raw_csv),
                "input_runs_sha256": file_sha256(raw_csv),
            },
        )
        os.rename(staging, out_dir)

    return {
        "promotion_ready": gate["promotion_ready"],
        "promotion_failures": gate["promotion_failures"],
        "measured_rows": len(rows),
        "cells": len(summary),
        "source_files_verified": source_count,
        "out_dir": str(out_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = assemble(args.manifest, args.raw, args.out)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
