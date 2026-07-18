#!/usr/bin/env python3
"""Validate and summarize the formal Slab co-location-degree control."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import statistics
import uuid
from pathlib import Path
from typing import Any


DEGREES = ("full", "24", "16", "8", "4", "1")
INLINE_CODES = {"full": 32, "24": 24, "16": 16, "8": 8, "4": 4, "1": 1}
T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SELFTEST_RE = re.compile(
    r"\[LAVD\]\[selftest\]\s+checked=64\s+fails=(\d+)\s+coloc_d=(\d+)\s+(PASS|FAIL)"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_bundle_file(root: Path, path: Path) -> None:
    """Require a regular source file whose full path stays inside the campaign."""
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = path.relative_to(root)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: expected bundle-contained regular file") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{path}: expected bundle-contained regular file")
    if not path.is_file():
        raise ValueError(f"{path}: expected bundle-contained regular file")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def ci95(values: list[float]) -> float:
    if len(values) < 2:
        raise ValueError("95% CI requires at least two values")
    critical = T95.get(len(values), 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def require_number(obj: dict[str, Any], key: str, source: Path) -> float:
    try:
        value = float(obj[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{source}: missing or invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def require_integer(obj: dict[str, Any], key: str, source: Path) -> int:
    value = require_number(obj, key, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral {key}")
    return int(value)


def require_sha(value: Any, label: str) -> str:
    text = str(value)
    if not SHA_RE.fullmatch(text):
        raise ValueError(f"invalid {label}: {value!r}")
    return text


def load_campaign(root: Path, expected_sha: str) -> tuple[dict[str, Any], str, str]:
    path = root / "campaign.json"
    if not path.exists():
        raise ValueError(f"missing co-location campaign: {path}")
    require_bundle_file(root, path)
    campaign = json.loads(path.read_text())
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("co-location campaign is missing protocol")
    exact = {
        "binary_sha256": expected_sha,
        "dataset": "DEEP1M",
        "degrees": list(DEGREES),
        "inline_codes": INLINE_CODES,
        "m_max0": 32,
        "code": "sq8",
        "repeats": 5,
        "warmups": 1,
        "threads": 10,
        "query_contexts": 10,
        "coroutines": 2,
        "ef_search": 200,
        "top_k": 10,
        "query_suffix": "uniform",
        "queries_per_run": 10000,
    }
    for key, expected in exact.items():
        if protocol.get(key) != expected:
            raise ValueError(
                f"co-location protocol mismatch for {key}: "
                f"{protocol.get(key)!r} != {expected!r}"
            )
    for key in (
        "binary_sha256",
        "index_dump_sha256",
        "query_sha256",
        "groundtruth_sha256",
        "runner_sha256",
        "summarizer_sha256",
        "fingerprint_tool_sha256",
    ):
        require_sha(protocol.get(key), f"co-location protocol {key}")
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if campaign.get("protocol_fingerprint") != fingerprint:
        raise ValueError("co-location protocol fingerprint mismatch")
    campaign_id = str(campaign.get("campaign_id", "")).strip()
    if not campaign_id:
        raise ValueError("co-location campaign ID is missing")
    return protocol, campaign_id, fingerprint


def option(command: list[str], flag: str, source: Path) -> str:
    if command.count(flag) != 1:
        raise ValueError(f"{source}: expected exactly one {flag}")
    index = command.index(flag)
    if index + 1 >= len(command):
        raise ValueError(f"{source}: missing value after {flag}")
    return command[index + 1]


def validate_command(command: Any, source: Path) -> None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"{source}: command must be a string list")
    exact = {
        "--lavd": "8",
        "--threads": "10",
        "--query-contexts": "10",
        "--coroutines": "2",
        "--ef-search": "200",
        "--ef-construction": "100",
        "--m": "16",
        "--k": "10",
        "--query-suffix": "uniform",
        "--lavd-region-bytes": "6442450944",
    }
    for flag, expected in exact.items():
        actual = option(command, flag, source)
        if actual != expected:
            raise ValueError(f"{source}: {flag} mismatch: {actual!r}")
    if "--load-index" not in command or "--store-index" in command:
        raise ValueError(f"{source}: co-location command must load the fixed index")
    if "--cache" in command or "--cache-ratio" in command:
        raise ValueError(f"{source}: co-location command must not enable generic cache")


def validate_environment(environment: Any, degree: str, source: Path) -> None:
    expected = {
        "SHINE_CRANE": "1",
        "GB_BITMAP_DEDUP": "1",
        "SHINE_LAVD_HOT_COLD_BATCH": "1",
        "SHINE_LAVD_SELFTEST": "1",
        "SHINE_LAVD_COLOC_SELFTEST": "1",
        "GB_QUERY_LATENCY": "1",
    }
    if degree != "full":
        expected["SHINE_LAVD_COLOC_DEGREE"] = degree
    if environment != expected:
        if isinstance(environment, dict) and environment.get(
            "SHINE_LAVD_COLOC_DEGREE"
        ) != expected.get("SHINE_LAVD_COLOC_DEGREE"):
            raise ValueError(f"{source}: co-location degree environment mismatch")
        raise ValueError(f"{source}: co-location environment drift")


def load_cell(
    root: Path,
    degree: str,
    run_kind: str,
    repeat: int,
    campaign_id: str,
    fingerprint: str,
    expected_sha: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if degree not in DEGREES:
        raise ValueError(f"unknown co-location degree: {degree}")
    cell = root / "raw" / degree / f"{run_kind}_r{repeat}"
    required = (
        cell / "manifest.json",
        cell / "cn.json",
        cell / "cn.err",
        cell / "mn" / "mn.err",
        cell / "mn" / "status",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"missing co-location cell files: {missing}")
    for path in required:
        require_bundle_file(root, path)
    if (cell / "mn" / "status").read_text().strip() != "0":
        raise ValueError(f"{cell}: memory-node status is not zero")

    manifest_path = cell / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected_manifest = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "degree": degree,
        "run_kind": run_kind,
        "repeat": repeat,
        "binary_sha256": expected_sha,
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            if key == "binary_sha256":
                raise ValueError(f"{manifest_path}: binary SHA mismatch")
            raise ValueError(f"{manifest_path}: {key} mismatch")
    protocol, loaded_campaign_id, loaded_fingerprint = load_campaign(root, expected_sha)
    if loaded_campaign_id != campaign_id or loaded_fingerprint != fingerprint:
        raise ValueError(f"{manifest_path}: campaign identity mismatch")
    expected_inputs = {
        "cn_binary": expected_sha,
        "mn_binary": expected_sha,
        "index_dump": protocol["index_dump_sha256"],
        "query": protocol["query_sha256"],
        "groundtruth": protocol["groundtruth_sha256"],
    }
    observed_inputs = manifest.get("observed_inputs")
    if observed_inputs != expected_inputs:
        raise ValueError(f"{manifest_path}: observed input SHA mismatch")
    for label, value in expected_inputs.items():
        require_sha(value, f"{manifest_path} observed input {label}")
    validate_command(manifest.get("command"), manifest_path)
    validate_environment(manifest.get("environment"), degree, manifest_path)

    cn_err_path = cell / "cn.err"
    selftests = SELFTEST_RE.findall(cn_err_path.read_text())
    if len(selftests) != 1:
        raise ValueError(f"{cn_err_path}: expected one layout selftest record")
    fails, coloc_d, outcome = selftests[0]
    if fails != "0" or outcome != "PASS" or int(coloc_d) != INLINE_CODES[degree]:
        raise ValueError(f"{cn_err_path}: layout selftest failed or mismatched")

    cn_path = cell / "cn.json"
    data = json.loads(cn_path.read_text())
    meta = data.get("meta")
    queries = data.get("queries")
    if not isinstance(meta, dict) or not isinstance(queries, dict):
        raise ValueError(f"{cn_path}: missing meta/queries object")
    expected_meta = {
        "dataset": "deep1m",
        "compute_threads": 10,
        "coroutines_per_thread": 2,
        "memory_nodes": 1,
        "query_suffix": "uniform",
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise ValueError(f"{cn_path}: metadata mismatch for {key}")
    if require_integer(data, "query_contexts", cn_path) != 10:
        raise ValueError(f"{cn_path}: query-context mismatch")
    if require_integer(data, "num_queries", cn_path) != 10000:
        raise ValueError(f"{cn_path}: query-count mismatch")
    if str(data.get("distance")) != "squared_l2":
        raise ValueError(f"{cn_path}: distance mismatch")
    if require_integer(queries, "processed", cn_path) != 10000:
        raise ValueError(f"{cn_path}: processed-query mismatch")
    if require_integer(queries, "local_latency_samples", cn_path) != 10000:
        raise ValueError(f"{cn_path}: latency-sample mismatch")
    qps = require_number(queries, "queries_per_sec", cn_path)
    recall = require_number(queries, "recall", cn_path)
    posts = require_number(queries, "rdma_posts", cn_path)
    bytes_read = require_number(queries, "rdma_reads_in_bytes", cn_path)
    if qps <= 0 or posts <= 0 or bytes_read <= 0 or not 0 <= recall <= 1:
        raise ValueError(f"{cn_path}: invalid query result")

    inventory = [
        {"path": str(path.relative_to(root)), "sha256": sha256(path)}
        for path in required
    ]
    row = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "dataset": "DEEP1M",
        "degree": degree,
        "inline_codes": INLINE_CODES[degree],
        "inline_code_fraction": INLINE_CODES[degree] / 32.0,
        "run_kind": run_kind,
        "repeat": repeat,
        "binary_sha256": expected_sha,
        "threads": 10,
        "query_contexts": 10,
        "coroutines": 2,
        "ef": 200,
        "top_k": 10,
        "processed": 10000,
        "recall": recall,
        "qps": qps,
        "posts_per_query": posts / 10000.0,
        "bytes_per_query": bytes_read / 10000.0,
        "p50_us": require_number(queries, "local_latency_p50_us", cn_path),
        "p95_us": require_number(queries, "local_latency_p95_us", cn_path),
        "p99_us": require_number(queries, "local_latency_p99_us", cn_path),
        "source_json": str(cn_path.relative_to(root)),
        "source_json_sha256": sha256(cn_path),
        "source_manifest": str(manifest_path.relative_to(root)),
        "source_manifest_sha256": sha256(manifest_path),
    }
    return row, inventory


def load_runs(root: Path, expected_sha: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    protocol, campaign_id, fingerprint = load_campaign(root, expected_sha)
    query_manifest = root / "query_pools" / "deep1m_slabwalk.json"
    if not query_manifest.exists():
        raise ValueError(f"missing co-location query-pool manifest: {query_manifest}")
    require_bundle_file(root, query_manifest)
    query = json.loads(query_manifest.read_text())
    if (
        query.get("kind") != "query_pool_fingerprint"
        or query.get("dataset") != "DEEP1M"
        or query.get("method") != "SlabWalk"
        or query.get("metric") != "l2"
        or query.get("limit") != 10000
        or query.get("query", {}).get("rows") != 10000
        or query.get("groundtruth", {}).get("rows") != 10000
    ):
        raise ValueError("co-location query-pool manifest mismatch")
    require_sha(query.get("query", {}).get("canonical_sha256"), "DEEP1M query pool")
    require_sha(
        query.get("groundtruth", {}).get("canonical_ids_sha256"),
        "DEEP1M ground-truth pool",
    )

    measured: list[dict[str, Any]] = []
    inventory: list[dict[str, str]] = []
    for degree in DEGREES:
        for run_kind, repeats in (
            ("warmup", int(protocol["warmups"])),
            ("measure", int(protocol["repeats"])),
        ):
            for repeat in range(repeats):
                row, source_files = load_cell(
                    root,
                    degree,
                    run_kind,
                    repeat,
                    campaign_id,
                    fingerprint,
                    expected_sha,
                )
                inventory.extend(source_files)
                if run_kind == "measure":
                    measured.append(row)
    counts = {degree: 0 for degree in DEGREES}
    for row in measured:
        counts[str(row["degree"])] += 1
    if counts != {degree: 5 for degree in DEGREES}:
        raise ValueError(f"incomplete co-location matrix: {counts}")
    provenance = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "campaign_sha256": sha256(root / "campaign.json"),
        "query_pool_manifest": str(query_manifest.relative_to(root)),
        "query_pool_manifest_sha256": sha256(query_manifest),
        "retained_cells": 36,
        "retained_source_files": inventory,
    }
    return measured, provenance


def summarize(root: Path, out: Path, expected_sha: str) -> dict[str, Any]:
    if out.exists():
        raise ValueError(f"refusing existing co-location summary: {out}")
    runs, provenance = load_runs(root, expected_sha)
    full = [row for row in runs if row["degree"] == "full"]
    full_qps = statistics.mean(float(row["qps"]) for row in full)
    full_posts = statistics.mean(float(row["posts_per_query"]) for row in full)
    full_bytes = statistics.mean(float(row["bytes_per_query"]) for row in full)
    summary: list[dict[str, Any]] = []
    for degree in DEGREES:
        rows = [row for row in runs if row["degree"] == degree]
        qps = [float(row["qps"]) for row in rows]
        recall = [float(row["recall"]) for row in rows]
        posts = [float(row["posts_per_query"]) for row in rows]
        bytes_per_query = [float(row["bytes_per_query"]) for row in rows]
        p99 = [float(row["p99_us"]) for row in rows]
        qps_mean = statistics.mean(qps)
        posts_mean = statistics.mean(posts)
        bytes_mean = statistics.mean(bytes_per_query)
        summary.append({
            "degree": degree,
            "inline_codes": INLINE_CODES[degree],
            "inline_code_fraction": INLINE_CODES[degree] / 32.0,
            "n": len(rows),
            "qps_mean": qps_mean,
            "qps_ci95": ci95(qps),
            "qps_change_vs_full_pct": 100.0 * (qps_mean / full_qps - 1.0),
            "recall_mean": statistics.mean(recall),
            "recall_ci95": ci95(recall),
            "posts_per_query_mean": posts_mean,
            "posts_per_query_ci95": ci95(posts),
            "post_increase_vs_full_pct": 100.0 * (posts_mean / full_posts - 1.0),
            "bytes_per_query_mean": bytes_mean,
            "bytes_per_query_ci95": ci95(bytes_per_query),
            "byte_change_vs_full_pct": 100.0 * (bytes_mean / full_bytes - 1.0),
            "p99_us_mean": statistics.mean(p99),
            "p99_us_ci95": ci95(p99),
        })

    staging = out.with_name(f".{out.name}.tmp-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        write_csv(staging / "runs.csv", runs)
        write_csv(staging / "summary.csv", summary)
        provenance["summarizer_sha256"] = sha256(Path(__file__).resolve())
        (staging / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        recall_means = [float(row["recall_mean"]) for row in summary]
        report = {
            "measured_runs": len(runs),
            "measured_cells": len(summary),
            "retained_cells": provenance["retained_cells"],
            "recall_mean_span": max(recall_means) - min(recall_means),
            "runs_sha256": sha256(staging / "runs.csv"),
            "summary_sha256": sha256(staging / "summary.csv"),
            "provenance_sha256": sha256(staging / "provenance.json"),
        }
        (staging / "validation.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        staging.rename(out)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-binary-sha", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = summarize(args.campaign, args.out, args.expected_binary_sha)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
