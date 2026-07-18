#!/usr/bin/env python3
"""Admit construction-only measurements after one isolated cross-date tail."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any, Iterable


EXPECTED_RUN_IDS = [f"r{repeat}" for repeat in range(1, 6)]


def as_float(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite numeric field")
    return result


def as_int(value: Any) -> int:
    return int(float(value))


def cell_key(row: dict[str, Any]) -> tuple[str, str, float]:
    return str(row.get("dataset", "")), str(row.get("method", "")), as_float(
        row.get("ef")
    )


def rows_for_cell(
    rows: Iterable[dict[str, Any]], cell: tuple[str, str, float]
) -> list[dict[str, Any]]:
    return [row for row in rows if cell_key(row) == cell]


def tail_control(
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    allowed_cell: tuple[str, str, float],
    *,
    tail_ratio: float = 1.50,
    normal_ratio: float = 1.10,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    candidate = rows_for_cell(candidate_rows, allowed_cell)
    baseline = rows_for_cell(baseline_rows, allowed_cell)
    candidate_ids = sorted(str(row.get("run_id", "")) for row in candidate)
    baseline_ids = sorted(str(row.get("run_id", "")) for row in baseline)
    repeat_contract = (
        candidate_ids == EXPECTED_RUN_IDS and baseline_ids == EXPECTED_RUN_IDS
    )
    if not repeat_contract:
        failures.append("tail_repeat_contract")

    try:
        candidate_p99 = {
            str(row["run_id"]): as_float(row["p99_us"]) for row in candidate
        }
        baseline_p99 = [as_float(row["p99_us"]) for row in baseline]
        baseline_median = statistics.median(baseline_p99)
    except (KeyError, TypeError, ValueError, statistics.StatisticsError):
        candidate_p99 = {}
        baseline_median = math.nan
        failures.append("tail_measurements")

    tail_ids: list[str] = []
    normal_ids: list[str] = []
    if math.isfinite(baseline_median) and baseline_median > 0:
        tail_ids = sorted(
            run_id
            for run_id, value in candidate_p99.items()
            if value > baseline_median * tail_ratio
        )
        normal_ids = sorted(run_id for run_id in candidate_p99 if run_id not in tail_ids)
        normal_values = [candidate_p99[run_id] for run_id in normal_ids]
        isolated = (
            repeat_contract
            and len(tail_ids) == 1
            and len(normal_ids) == 4
            and all(value <= baseline_median * normal_ratio for value in normal_values)
        )
        if not isolated:
            failures.append("isolated_tail")
        if tail_ids != ["r1"]:
            failures.append("retained_tail_identity")
    else:
        failures.append("isolated_tail")

    report = {
        "allowed_cell": {
            "dataset": allowed_cell[0],
            "method": allowed_cell[1],
            "ef": allowed_cell[2],
        },
        "baseline_p99_us_median": baseline_median,
        "candidate_p99_us_by_run": candidate_p99,
        "tail_ratio": tail_ratio,
        "normal_ratio": normal_ratio,
        "tail_run_ids": tail_ids,
        "normal_run_ids": normal_ids,
        "normal_run_count": len(normal_ids),
    }
    return report, failures


def evaluate_construction_candidate(
    promotion: dict[str, Any],
    cells: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    *,
    allowed_cell: tuple[str, str, float],
) -> dict[str, Any]:
    failures: list[str] = []
    frontier = promotion.get("frontier", {})
    binary_ab = promotion.get("binary_ab", {})

    if (
        promotion.get("kind") != "vldb_candidate_promotion_gate_v1"
        or promotion.get("promotion_ready") is not False
        or promotion.get("failures") != ["frontier_comparison"]
    ):
        failures.append("original_promotion_contract")
    if (
        not isinstance(frontier, dict)
        or frontier.get("kind") != "vldb_frontier_candidate_comparison_v1"
        or frontier.get("promotion_ready") is not False
        or as_int(frontier.get("compared_cells", -1)) != 70
        or len(cells) != 70
        or as_int(frontier.get("performance_failures", -1)) != 1
    ):
        failures.append("frontier_failure_shape")
    if as_int(frontier.get("invariant_failures", -1)) != 0 or any(
        as_int(row.get("invariant_ok", 0)) != 1 for row in cells
    ):
        failures.append("query_work_invariants")

    failed_cells = [row for row in cells if as_int(row.get("performance_ok", 0)) != 1]
    allowed_row = failed_cells[0] if len(failed_cells) == 1 else None
    if allowed_row is None or cell_key(allowed_row) != allowed_cell:
        if "frontier_failure_shape" not in failures:
            failures.append("frontier_failure_shape")

    thresholds = frontier.get("thresholds", {})
    if allowed_row is not None:
        qps_ok = (
            as_float(allowed_row.get("qps_mean_ratio"))
            >= as_float(thresholds.get("min_qps_ratio"))
            and as_float(allowed_row.get("qps_ratio_ci95_low"))
            >= as_float(thresholds.get("min_qps_ci_low"))
        )
        p99_failed = (
            as_float(allowed_row.get("p99_mean_ratio"))
            > as_float(thresholds.get("max_p99_ratio"))
            or as_float(allowed_row.get("p99_ratio_ci95_high"))
            > as_float(thresholds.get("max_p99_ci_high"))
        )
        if not qps_ok or not p99_failed:
            failures.append("allowed_cell_not_p99_only")

    for method in ("slabwalk", "shine"):
        method_report = binary_ab.get(method, {}) if isinstance(binary_ab, dict) else {}
        if method_report.get("ready") is not True or method_report.get("failures") != []:
            failures.append(f"{method}_ab")

    tail, tail_failures = tail_control(
        candidate_rows, baseline_rows, allowed_cell
    )
    failures.extend(tail_failures)
    failures = list(dict.fromkeys(failures))
    return {
        "kind": "vldb_construction_candidate_gate_v1",
        "construction_ready": not failures,
        "general_promotion_ready": False,
        "scope": "construction_measurements_only",
        "failures": failures,
        "allowed_cross_date_failure": tail["allowed_cell"],
        "tail_control": tail,
        "binary_ab": {
            method: {
                "ready": binary_ab.get(method, {}).get("ready"),
                "failures": binary_ab.get(method, {}).get("failures"),
            }
            for method in ("slabwalk", "shine")
        },
    }


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"non-empty CSV required: {path}")
    return rows


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    parser.add_argument("--promotion-report", type=Path, required=True)
    parser.add_argument("--frontier-cells", type=Path, required=True)
    parser.add_argument("--candidate-frontier", type=Path, required=True)
    parser.add_argument("--baseline-frontier", type=Path, required=True)
    parser.add_argument("--allowed-dataset", default="GIST1M")
    parser.add_argument("--allowed-method", default="SlabWalk")
    parser.add_argument("--allowed-ef", type=float, default=100.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"refusing existing construction gate: {args.out}")

    inputs = {
        "promotion_report": args.promotion_report,
        "frontier_cells": args.frontier_cells,
        "candidate_frontier": args.candidate_frontier,
        "baseline_frontier": args.baseline_frontier,
    }
    report = evaluate_construction_candidate(
        load_json(args.promotion_report),
        load_csv(args.frontier_cells),
        load_csv(args.candidate_frontier),
        load_csv(args.baseline_frontier),
        allowed_cell=(args.allowed_dataset, args.allowed_method, args.allowed_ef),
    )
    report["inputs"] = {
        name: {"path": str(path.resolve()), "sha256": file_sha256(path)}
        for name, path in inputs.items()
    }
    atomic_json(args.out, report)
    print(f"construction_ready={report['construction_ready']}")
    if not report["construction_ready"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
