#!/usr/bin/env python3
"""Gate a replacement SHINE/SlabWalk frontier against certified evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from aggregate_frontier_repeats import canonical_dataset


DATASETS = (
    "SIFT1M",
    "GIST1M",
    "DEEP1M",
    "BIGANN1M",
    "SPACEV1M",
    "TURING1M",
    "TTI1M",
)
METHODS = ("SHINE", "SlabWalk")
T95_CONSERVATIVE = 2.776


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing frontier CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty frontier CSV: {path}")
    return rows


def finite(row: dict[str, str], field: str, path: Path) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid {field}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{path}: non-finite {field}")
    return value


def group_rows(
    path: Path, expected_repeats: int
) -> dict[tuple[str, str, float], list[dict[str, str]]]:
    grouped: dict[tuple[str, str, float], list[dict[str, str]]] = defaultdict(list)
    for row in read_rows(path):
        dataset = canonical_dataset(str(row.get("dataset", "")))
        method = str(row.get("method", ""))
        if dataset not in DATASETS or method not in METHODS:
            continue
        ef = finite(row, "ef", path)
        for field in (
            "recall",
            "qps",
            "p99_us",
            "posts_per_query",
            "bytes_per_query",
        ):
            finite(row, field, path)
        grouped[(dataset, method, ef)].append(row)

    for key, points in grouped.items():
        run_ids = sorted(str(point.get("run_id", "")) for point in points)
        expected_ids = [f"r{repeat}" for repeat in range(1, expected_repeats + 1)]
        if run_ids != expected_ids:
            raise ValueError(
                f"{path}: {key} must contain {expected_ids}, found {run_ids}"
            )
    return grouped


def values(points: list[dict[str, str]], field: str, path: Path) -> list[float]:
    return [finite(point, field, path) for point in points]


def ratio_interval(candidate: list[float], baseline: list[float]) -> tuple[float, float, float]:
    candidate_mean = statistics.mean(candidate)
    baseline_mean = statistics.mean(baseline)
    if candidate_mean <= 0 or baseline_mean <= 0:
        raise ValueError("ratio inputs must be positive")
    ratio = candidate_mean / baseline_mean
    candidate_se = (
        statistics.stdev(candidate) / math.sqrt(len(candidate)) / candidate_mean
        if len(candidate) > 1
        else 0.0
    )
    baseline_se = (
        statistics.stdev(baseline) / math.sqrt(len(baseline)) / baseline_mean
        if len(baseline) > 1
        else 0.0
    )
    log_half = T95_CONSERVATIVE * math.sqrt(candidate_se**2 + baseline_se**2)
    return ratio, math.exp(math.log(ratio) - log_half), math.exp(math.log(ratio) + log_half)


def compare_frontiers(
    baseline_path: Path,
    candidate_path: Path,
    *,
    expected_repeats: int = 5,
    expected_points: int = 5,
    max_recall_delta: float = 1e-9,
    max_posts_delta: float = 1e-6,
    max_bytes_delta: float = 1e-3,
    min_qps_ratio: float = 0.95,
    min_qps_ci_low: float = 0.90,
    max_p99_ratio: float = 1.10,
    max_p99_ci_high: float = 1.25,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    baseline = group_rows(baseline_path, expected_repeats)
    candidate = group_rows(candidate_path, expected_repeats)
    expected_curve_counts = {
        (dataset, method): expected_points
        for dataset in DATASETS
        for method in METHODS
    }
    observed_curve_counts: dict[tuple[str, str], int] = defaultdict(int)
    for dataset, method, _ in candidate:
        observed_curve_counts[(dataset, method)] += 1
    if dict(observed_curve_counts) != expected_curve_counts:
        raise ValueError(
            "candidate frontier matrix mismatch: "
            f"expected={expected_curve_counts} observed={dict(observed_curve_counts)}"
        )

    output: list[dict[str, object]] = []
    invariant_failures = 0
    performance_failures = 0
    for key in sorted(candidate):
        if key not in baseline:
            raise ValueError(f"baseline has no matched cell for {key}")
        candidate_points = candidate[key]
        baseline_points = baseline[key]
        recall_baseline = statistics.median(values(baseline_points, "recall", baseline_path))
        posts_baseline = statistics.median(
            values(baseline_points, "posts_per_query", baseline_path)
        )
        bytes_baseline = statistics.median(
            values(baseline_points, "bytes_per_query", baseline_path)
        )
        recall_delta = max(
            abs(value - recall_baseline)
            for value in values(candidate_points, "recall", candidate_path)
        )
        posts_delta = max(
            abs(value - posts_baseline)
            for value in values(candidate_points, "posts_per_query", candidate_path)
        )
        bytes_delta = max(
            abs(value - bytes_baseline)
            for value in values(candidate_points, "bytes_per_query", candidate_path)
        )
        qps_ratio, qps_low, qps_high = ratio_interval(
            values(candidate_points, "qps", candidate_path),
            values(baseline_points, "qps", baseline_path),
        )
        p99_ratio, p99_low, p99_high = ratio_interval(
            values(candidate_points, "p99_us", candidate_path),
            values(baseline_points, "p99_us", baseline_path),
        )
        invariant_ok = (
            recall_delta <= max_recall_delta
            and posts_delta <= max_posts_delta
            and bytes_delta <= max_bytes_delta
        )
        performance_ok = (
            qps_ratio >= min_qps_ratio
            and qps_low >= min_qps_ci_low
            and p99_ratio <= max_p99_ratio
            and p99_high <= max_p99_ci_high
        )
        invariant_failures += int(not invariant_ok)
        performance_failures += int(not performance_ok)
        output.append(
            {
                "dataset": key[0],
                "method": key[1],
                "ef": key[2],
                "repeats": expected_repeats,
                "recall_max_abs_delta": recall_delta,
                "posts_per_query_max_abs_delta": posts_delta,
                "bytes_per_query_max_abs_delta": bytes_delta,
                "qps_mean_ratio": qps_ratio,
                "qps_ratio_ci95_low": qps_low,
                "qps_ratio_ci95_high": qps_high,
                "p99_mean_ratio": p99_ratio,
                "p99_ratio_ci95_low": p99_low,
                "p99_ratio_ci95_high": p99_high,
                "invariant_ok": int(invariant_ok),
                "performance_ok": int(performance_ok),
            }
        )

    report = {
        "kind": "vldb_frontier_candidate_comparison_v1",
        "promotion_ready": invariant_failures == 0 and performance_failures == 0,
        "baseline": str(baseline_path.resolve()),
        "baseline_sha256": file_sha256(baseline_path),
        "candidate": str(candidate_path.resolve()),
        "candidate_sha256": file_sha256(candidate_path),
        "datasets": list(DATASETS),
        "methods": list(METHODS),
        "expected_repeats": expected_repeats,
        "expected_points_per_curve": expected_points,
        "compared_cells": len(output),
        "invariant_failures": invariant_failures,
        "performance_failures": performance_failures,
        "thresholds": {
            "max_recall_delta": max_recall_delta,
            "max_posts_delta": max_posts_delta,
            "max_bytes_delta": max_bytes_delta,
            "min_qps_ratio": min_qps_ratio,
            "min_qps_ci_low": min_qps_ci_low,
            "max_p99_ratio": max_p99_ratio,
            "max_p99_ci_high": max_p99_ci_high,
        },
    }
    return report, output


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=5)
    parser.add_argument("--expected-points", type=int, default=5)
    args = parser.parse_args()
    if args.out_dir.exists():
        raise ValueError(f"refusing existing comparison directory: {args.out_dir}")
    report, rows = compare_frontiers(
        args.baseline,
        args.candidate,
        expected_repeats=args.expected_repeats,
        expected_points=args.expected_points,
    )
    args.out_dir.mkdir(parents=True)
    write_csv(args.out_dir / "cells.csv", rows)
    (args.out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"compared {report['compared_cells']} cells; "
        f"promotion_ready={report['promotion_ready']}"
    )
    if not report["promotion_ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
