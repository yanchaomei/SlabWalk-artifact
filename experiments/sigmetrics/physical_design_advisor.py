#!/usr/bin/env python3
"""Choose a measured physical-design point under explicit resource constraints."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import tempfile
from pathlib import Path
from typing import Any


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("advisor input is not canonical finite JSON") from exc


def _finite(value: object, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {label}: {value!r}")
    if positive and number <= 0:
        raise ValueError(f"non-positive {label}: {value!r}")
    return number


def _ci95(values: list[float]) -> float:
    if len(values) < 2:
        raise ValueError("QPS objective requires at least two samples")
    critical = T_975.get(len(values) - 1, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def _validate_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("unsupported physical-design request schema")
    selection_id = str(request.get("selection_id", "")).strip()
    if not selection_id:
        raise ValueError("selection_id must be non-empty")
    if request.get("objective") != "qps_ci95_low":
        raise ValueError("objective must be qps_ci95_low")

    constraints = request.get("constraints")
    if not isinstance(constraints, dict) or set(constraints) != {
        "recall_min",
        "resources_max",
    }:
        raise ValueError("constraints must contain recall_min and resources_max")
    recall_min = _finite(constraints["recall_min"], "recall_min")
    if not 0 <= recall_min <= 1:
        raise ValueError("recall_min must be in [0, 1]")
    resource_limits = constraints["resources_max"]
    if not isinstance(resource_limits, dict):
        raise ValueError("resources_max must be a JSON object")
    normalized_limits: dict[str, float] = {}
    for name, value in resource_limits.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("resource names must be non-empty strings")
        limit = _finite(value, f"resource limit {name}")
        if limit < 0:
            raise ValueError(f"resource limit {name} must be non-negative")
        normalized_limits[name] = limit

    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("advisor request requires at least one candidate")
    provenance = request.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("request provenance must be a JSON object")

    normalized_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "candidate_id",
        "configuration",
        "provenance",
        "qps_samples",
        "recall",
        "resources",
    }
    for raw in candidates:
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError("malformed physical-design candidate")
        candidate_id = str(raw["candidate_id"]).strip()
        if not candidate_id:
            raise ValueError("candidate_id must be non-empty")
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate: {candidate_id}")
        seen.add(candidate_id)
        if not isinstance(raw["configuration"], dict):
            raise ValueError(f"{candidate_id}: configuration must be an object")
        if not isinstance(raw["provenance"], dict):
            raise ValueError(f"{candidate_id}: provenance must be an object")
        samples_raw = raw["qps_samples"]
        if not isinstance(samples_raw, list) or len(samples_raw) < 2:
            raise ValueError(
                f"{candidate_id}: QPS objective requires at least two samples"
            )
        samples = [
            _finite(value, f"{candidate_id} qps sample", positive=True)
            for value in samples_raw
        ]
        recall = _finite(raw["recall"], f"{candidate_id} recall")
        if not 0 <= recall <= 1:
            raise ValueError(f"{candidate_id}: recall must be in [0, 1]")
        resources_raw = raw["resources"]
        if not isinstance(resources_raw, dict):
            raise ValueError(f"{candidate_id}: resources must be an object")
        resources: dict[str, float] = {}
        for name, value in resources_raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{candidate_id}: invalid resource name")
            observed = _finite(value, f"{candidate_id} resource {name}")
            if observed < 0:
                raise ValueError(f"{candidate_id}: negative resource {name}")
            resources[name] = observed
        normalized_candidates.append(
            {
                "candidate_id": candidate_id,
                "configuration": raw["configuration"],
                "provenance": raw["provenance"],
                "qps_samples": samples,
                "recall": recall,
                "resources": resources,
            }
        )
    return {
        "selection_id": selection_id,
        "recall_min": recall_min,
        "resource_limits": normalized_limits,
        "candidates": normalized_candidates,
        "provenance": provenance,
    }


def select_candidate(request: dict[str, Any]) -> dict[str, Any]:
    normalized = _validate_request(request)
    evaluated: list[dict[str, Any]] = []
    for candidate in normalized["candidates"]:
        samples = candidate["qps_samples"]
        mean = statistics.mean(samples)
        ci = _ci95(samples)
        rejections: list[str] = []
        if candidate["recall"] < normalized["recall_min"]:
            rejections.append("recall_below_target")
        for name, limit in sorted(normalized["resource_limits"].items()):
            if name not in candidate["resources"]:
                rejections.append(f"missing_resource:{name}")
            elif candidate["resources"][name] > limit:
                rejections.append(f"resource_exceeds:{name}")
        evaluated.append(
            {
                **candidate,
                "qps_n": len(samples),
                "qps_mean": mean,
                "qps_ci95": ci,
                "qps_ci95_low": mean - ci,
                "qps_ci95_high": mean + ci,
                "feasible": not rejections,
                "rejection_reasons": rejections,
            }
        )
    evaluated.sort(key=lambda row: row["candidate_id"])
    feasible = [row for row in evaluated if row["feasible"]]
    selected = None
    failures: list[str] = []
    if feasible:
        winner = min(
            feasible,
            key=lambda row: (
                -row["qps_ci95_low"],
                -row["qps_mean"],
                row["candidate_id"],
            ),
        )
        selected = {
            key: winner[key]
            for key in (
                "candidate_id",
                "configuration",
                "provenance",
                "recall",
                "resources",
                "qps_n",
                "qps_mean",
                "qps_ci95",
                "qps_ci95_low",
                "qps_ci95_high",
            )
        }
    else:
        failures.append("no_feasible_candidate")
    return {
        "schema_version": 1,
        "kind": "slabwalk_physical_design_selection",
        "selection_id": normalized["selection_id"],
        "selection_ready": selected is not None,
        "failures": failures,
        "objective": "qps_ci95_low",
        "selection_rule": (
            "maximize two-sided Student-t 95% QPS lower bound, then mean QPS, "
            "then lexical candidate_id"
        ),
        "constraints": {
            "recall_min": normalized["recall_min"],
            "resources_max": normalized["resource_limits"],
        },
        "provenance": normalized["provenance"],
        "input_sha256": hashlib.sha256(_canonical(request)).hexdigest(),
        "advisor_sha256": file_sha256(Path(__file__).resolve()),
        "candidates": evaluated,
        "selected": selected,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_file(input_path: Path, output_path: Path) -> dict[str, Any]:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file() or input_path.is_symlink():
        raise ValueError(f"missing or unsafe advisor input: {input_path}")
    if output_path.exists():
        raise ValueError(f"refusing existing output: {output_path}")
    try:
        request = json.loads(input_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid advisor input JSON: {input_path}") from exc
    report = select_candidate(request)
    _atomic_write(
        output_path,
        (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(),
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_file(args.input, args.out), sort_keys=True))


if __name__ == "__main__":
    main()
