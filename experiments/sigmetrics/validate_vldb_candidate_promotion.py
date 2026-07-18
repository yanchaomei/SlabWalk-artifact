#!/usr/bin/env python3
"""Gate a frontier replacement with independent SlabWalk and SHINE A/Bs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import verify_vldb_binary_ab as binary_ab


METHODS = ("slabwalk", "shine")
EXPECTED_DATASETS = {"slabwalk": "GIST1M", "shine": "DEEP1M"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def load_baseline_p99(path: Path) -> float:
    try:
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as exc:
        raise ValueError(f"invalid A/B summary: {path}") from exc
    baseline = [row for row in rows if row.get("variant") == "A"]
    if len(baseline) != 1:
        raise ValueError(f"A/B summary must contain one variant A row: {path}")
    try:
        value = float(baseline[0]["p99_us_mean"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"A/B summary has invalid baseline P99: {path}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"A/B summary has non-positive baseline P99: {path}")
    return value


def evaluate_ab(
    method: str,
    verification: dict[str, Any],
    comparison: dict[str, Any],
    baseline_p99_us: float,
    *,
    expected_sha_a: str,
    expected_sha_b: str,
    expected_source_tree_a: str,
    expected_source_tree_b: str,
    expected_repeats: int = 6,
    min_qps_ratio_low: float = 0.95,
    max_p99_ratio_high: float = 1.10,
    max_recall_delta: float = 1e-9,
    max_posts_delta: float = 1e-6,
    max_bytes_delta: float = 1e-3,
) -> dict[str, Any]:
    failures: list[str] = []
    exact = (
        ("recall", max_recall_delta),
        ("posts_per_query", max_posts_delta),
        ("bytes_per_query", max_bytes_delta),
    )
    if (
        verification.get("method") != method
        or verification.get("dataset") != EXPECTED_DATASETS[method]
        or verification.get("capture_build_metrics") is not False
        or int(verification.get("paired_repeats", 0)) != expected_repeats
        or int(verification.get("run_count", 0)) != 2 * expected_repeats
    ):
        failures.append("verification_contract")
    if verification.get("binary_sha_a") != expected_sha_a:
        failures.append("baseline_binary")
    if verification.get("binary_sha_b") != expected_sha_b:
        failures.append("candidate_binary")
    if verification.get("source_tree_sha_a") != expected_source_tree_a:
        failures.append("baseline_source_tree")
    if verification.get("source_tree_sha_b") != expected_source_tree_b:
        failures.append("candidate_source_tree")
    if int(comparison.get("paired_repeats", 0)) != expected_repeats:
        failures.append("paired_repeats")
    order = comparison.get("order_stratified", {})
    if not isinstance(order, dict) or any(
        int(order.get(name, {}).get("n", 0)) != expected_repeats // 2
        for name in ("AB", "BA")
    ):
        failures.append("position_balance")

    for metric, tolerance in exact:
        mean = abs(float(comparison.get(f"paired_{metric}_delta_B_minus_A_mean", math.inf)))
        ci = abs(float(comparison.get(f"paired_{metric}_delta_B_minus_A_ci95", math.inf)))
        if not math.isfinite(mean) or not math.isfinite(ci) or mean > tolerance or ci > tolerance:
            failures.append(f"{metric}_invariant")

    qps_mean = float(comparison.get("paired_qps_speedup_B_over_A_mean", -math.inf))
    qps_ci = abs(float(comparison.get("paired_qps_speedup_B_over_A_ci95", math.inf)))
    qps_low = qps_mean - qps_ci
    if not math.isfinite(qps_low) or qps_low < min_qps_ratio_low:
        failures.append("qps_regression")

    p99_delta = float(comparison.get("paired_p99_us_delta_B_minus_A_mean", math.inf))
    p99_ci = abs(float(comparison.get("paired_p99_us_delta_B_minus_A_ci95", math.inf)))
    p99_high = (baseline_p99_us + p99_delta + p99_ci) / baseline_p99_us
    if not math.isfinite(p99_high) or p99_high > max_p99_ratio_high:
        failures.append("p99_regression")

    return {
        "method": method,
        "ready": not failures,
        "failures": failures,
        "paired_repeats": expected_repeats,
        "qps_ratio_ci95_low": qps_low,
        "p99_ratio_ci95_high": p99_high,
        "baseline_p99_us": baseline_p99_us,
        "thresholds": {
            "min_qps_ratio_ci95_low": min_qps_ratio_low,
            "max_p99_ratio_ci95_high": max_p99_ratio_high,
            "max_recall_delta": max_recall_delta,
            "max_posts_delta": max_posts_delta,
            "max_bytes_delta": max_bytes_delta,
        },
    }


def evaluate(
    frontier: dict[str, Any],
    ab_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    frontier_ready = (
        frontier.get("kind") == "vldb_frontier_candidate_comparison_v1"
        and frontier.get("promotion_ready") is True
        and int(frontier.get("invariant_failures", -1)) == 0
        and int(frontier.get("performance_failures", -1)) == 0
    )
    failures = [] if frontier_ready else ["frontier_comparison"]
    failures.extend(
        f"{method}_ab" for method in METHODS if not ab_reports[method]["ready"]
    )
    return {
        "kind": "vldb_candidate_promotion_gate_v1",
        "promotion_ready": not failures,
        "failures": failures,
        "frontier": frontier,
        "binary_ab": ab_reports,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontier-comparison", type=Path, required=True)
    parser.add_argument("--slabwalk-ab", type=Path, required=True)
    parser.add_argument("--shine-ab", type=Path, required=True)
    parser.add_argument("--expected-sha-a", required=True)
    parser.add_argument("--expected-sha-b", required=True)
    parser.add_argument("--expected-source-tree-a", required=True)
    parser.add_argument("--expected-source-tree-b", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"refusing existing promotion report: {args.out}")

    ab_reports: dict[str, dict[str, Any]] = {}
    for method, root in (("slabwalk", args.slabwalk_ab), ("shine", args.shine_ab)):
        verification = binary_ab.validate_bundle(
            root,
            expected_sha_a=args.expected_sha_a,
            expected_sha_b=args.expected_sha_b,
            expected_compute_host=args.expected_compute_host,
        )
        comparison = load_json(root / "comparison.json", f"{method} comparison")
        method_report = evaluate_ab(
            method,
            verification,
            comparison,
            load_baseline_p99(root / "summary.csv"),
            expected_sha_a=args.expected_sha_a,
            expected_sha_b=args.expected_sha_b,
            expected_source_tree_a=args.expected_source_tree_a,
            expected_source_tree_b=args.expected_source_tree_b,
        )
        method_report["verification"] = verification
        method_report["comparison_sha256"] = file_sha256(
            root / "comparison.json"
        )
        method_report["summary_sha256"] = file_sha256(root / "summary.csv")
        ab_reports[method] = method_report
    frontier = load_json(args.frontier_comparison, "frontier comparison")
    frontier["report_sha256"] = file_sha256(args.frontier_comparison)
    report = evaluate(frontier, ab_reports)
    atomic_json(args.out, report)
    print(f"candidate promotion_ready={report['promotion_ready']}")
    if not report["promotion_ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
