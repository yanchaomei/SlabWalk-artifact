#!/usr/bin/env python3
"""Validate and summarize the formal SHINE cache-control campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import statistics
import uuid
from pathlib import Path
from typing import Any


CONDITIONS = ("off", "c5", "c20", "c50")
RATIOS = {"off": 0, "c5": 5, "c20": 20, "c50": 50}
T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def load_campaign(root: Path, expected_sha: str) -> tuple[dict[str, Any], str, str]:
    path = root / "campaign.json"
    if not path.is_file():
        raise ValueError(f"missing cache-control campaign: {path}")
    campaign = json.loads(path.read_text())
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("cache-control campaign is missing protocol")
    exact = {
        "binary_sha256": expected_sha,
        "dataset": "SIFT1M",
        "conditions": list(CONDITIONS),
        "repeats": 5,
        "warmups": 1,
        "threads": 1,
        "query_contexts": 1,
        "coroutines": 8,
        "ef_search": 100,
        "top_k": 10,
        "query_suffix": "uniform",
        "queries_per_run": 10000,
    }
    for key, expected in exact.items():
        if protocol.get(key) != expected:
            raise ValueError(
                f"cache-control protocol mismatch for {key}: "
                f"{protocol.get(key)!r} != {expected!r}"
            )
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if campaign.get("protocol_fingerprint") != fingerprint:
        raise ValueError("cache-control protocol fingerprint mismatch")
    campaign_id = str(campaign.get("campaign_id", "")).strip()
    if not campaign_id:
        raise ValueError("cache-control campaign ID is missing")
    return protocol, campaign_id, fingerprint


def validate_command(command: Any, condition: str, source: Path) -> None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"{source}: command must be a string list")
    if "--lavd" not in command or command[command.index("--lavd") + 1] != "0":
        raise ValueError(f"{source}: cache control must use --lavd 0")
    has_cache = "--cache" in command
    if condition == "off":
        if has_cache or "--cache-ratio" in command:
            raise ValueError(f"{source}: cache-off command enables cache")
        return
    if not has_cache or "--cache-ratio" not in command:
        raise ValueError(f"{source}: cached command is missing cache flags")
    ratio = command[command.index("--cache-ratio") + 1]
    if ratio != str(RATIOS[condition]):
        raise ValueError(f"{source}: cache-ratio mismatch")


def load_cell(
    root: Path,
    condition: str,
    run_kind: str,
    repeat: int,
    campaign_id: str,
    fingerprint: str,
    expected_sha: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    cell = root / "raw" / condition / f"{run_kind}_r{repeat}"
    required = (
        cell / "manifest.json",
        cell / "cn.json",
        cell / "cn.err",
        cell / "mn" / "mn.err",
        cell / "mn" / "status",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"missing cache-control cell files: {missing}")
    if (cell / "mn" / "status").read_text().strip() != "0":
        raise ValueError(f"{cell}: memory-node status is not zero")

    manifest_path = cell / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected_manifest = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "condition": condition,
        "run_kind": run_kind,
        "repeat": repeat,
        "binary_sha256": expected_sha,
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            if key == "binary_sha256":
                raise ValueError(f"{manifest_path}: binary SHA mismatch")
            raise ValueError(f"{manifest_path}: {key} mismatch")
    validate_command(manifest.get("command"), condition, manifest_path)

    cn_path = cell / "cn.json"
    data = json.loads(cn_path.read_text())
    meta = data.get("meta")
    queries = data.get("queries")
    cache = data.get("cache")
    if not isinstance(meta, dict) or not isinstance(queries, dict) or not isinstance(cache, dict):
        raise ValueError(f"{cn_path}: missing meta/queries/cache object")
    expected_meta = {
        "dataset": "sift1m",
        "compute_threads": 1,
        "coroutines_per_thread": 8,
        "memory_nodes": 1,
        "query_suffix": "uniform",
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise ValueError(f"{cn_path}: metadata mismatch for {key}")
    if require_integer(data, "query_contexts", cn_path) != 1:
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
    ratio = int(cache.get("cache_size_ratio", 0))
    if ratio != RATIOS[condition]:
        raise ValueError(f"{cn_path}: reported cache ratio mismatch")
    hits = require_number(cache, "hits_total", cn_path)
    misses = require_number(cache, "misses_total", cn_path)
    if condition == "off" and hits != 0:
        raise ValueError(f"{cn_path}: cache-off run reports hits")

    inventory = [
        {"path": str(path.relative_to(root)), "sha256": sha256(path)}
        for path in required
    ]
    row = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "dataset": "SIFT1M",
        "condition": condition,
        "cache_ratio_pct": RATIOS[condition],
        "run_kind": run_kind,
        "repeat": repeat,
        "binary_sha256": expected_sha,
        "threads": 1,
        "query_contexts": 1,
        "coroutines": 8,
        "ef": 100,
        "top_k": 10,
        "processed": 10000,
        "recall": recall,
        "qps": qps,
        "posts_per_query": posts / 10000.0,
        "bytes_per_query": bytes_read / 10000.0,
        "cache_hits": hits,
        "cache_misses": misses,
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
    query_manifest = root / "query_pools" / "sift1m_shine.json"
    if not query_manifest.is_file():
        raise ValueError(f"missing cache-control query-pool manifest: {query_manifest}")
    query = json.loads(query_manifest.read_text())
    if (
        query.get("dataset") != "SIFT1M"
        or query.get("method") != "SHINE"
        or query.get("metric") != "l2"
        or query.get("query", {}).get("rows") != 10000
        or query.get("groundtruth", {}).get("rows") != 10000
    ):
        raise ValueError("cache-control query-pool manifest mismatch")

    measured: list[dict[str, Any]] = []
    inventory: list[dict[str, str]] = []
    for condition in CONDITIONS:
        for run_kind, repeats in (("warmup", int(protocol["warmups"])), ("measure", int(protocol["repeats"]))):
            for repeat in range(repeats):
                row, source_files = load_cell(
                    root, condition, run_kind, repeat, campaign_id,
                    fingerprint, expected_sha,
                )
                inventory.extend(source_files)
                if run_kind == "measure":
                    measured.append(row)
    counts = {condition: 0 for condition in CONDITIONS}
    for row in measured:
        counts[str(row["condition"])] += 1
    if counts != {condition: 5 for condition in CONDITIONS}:
        raise ValueError(f"incomplete cache-control matrix: {counts}")
    provenance = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "campaign_sha256": sha256(root / "campaign.json"),
        "query_pool_manifest": str(query_manifest.relative_to(root)),
        "query_pool_manifest_sha256": sha256(query_manifest),
        "retained_cells": 24,
        "retained_source_files": inventory,
    }
    return measured, provenance


def summarize(root: Path, out: Path, expected_sha: str) -> dict[str, Any]:
    if out.exists():
        raise ValueError(f"refusing existing cache-control summary: {out}")
    runs, provenance = load_runs(root, expected_sha)
    off = [row for row in runs if row["condition"] == "off"]
    off_qps = statistics.mean(float(row["qps"]) for row in off)
    off_posts = statistics.mean(float(row["posts_per_query"]) for row in off)
    summary: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        rows = [row for row in runs if row["condition"] == condition]
        qps = [float(row["qps"]) for row in rows]
        recall = [float(row["recall"]) for row in rows]
        posts = [float(row["posts_per_query"]) for row in rows]
        bytes_per_query = [float(row["bytes_per_query"]) for row in rows]
        p99 = [float(row["p99_us"]) for row in rows]
        hits = [float(row["cache_hits"]) for row in rows]
        qps_mean = statistics.mean(qps)
        posts_mean = statistics.mean(posts)
        summary.append({
            "condition": condition,
            "cache_ratio_pct": RATIOS[condition],
            "n": len(rows),
            "qps_mean": qps_mean,
            "qps_ci95": ci95(qps),
            "qps_change_vs_off_pct": 100.0 * (qps_mean / off_qps - 1.0),
            "recall_mean": statistics.mean(recall),
            "recall_ci95": ci95(recall),
            "posts_per_query_mean": posts_mean,
            "posts_per_query_ci95": ci95(posts),
            "post_reduction_vs_off_pct": 100.0 * (1.0 - posts_mean / off_posts),
            "bytes_per_query_mean": statistics.mean(bytes_per_query),
            "p99_us_mean": statistics.mean(p99),
            "p99_us_ci95": ci95(p99),
            "cache_hits_mean": statistics.mean(hits),
            "all_repeats_below_off_min": condition == "off" or max(qps) < min(float(row["qps"]) for row in off),
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
        report = {
            "measured_runs": len(runs),
            "measured_cells": len(summary),
            "retained_cells": provenance["retained_cells"],
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
