#!/usr/bin/env python3
"""Validate a deterministic materialization advisor on held-out sealed runs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from . import physical_design_advisor as advisor
    from . import summarize_vldb_materialization_policy as materialization
    from . import vldb_evidence_bundle as evidence
except ImportError:
    import physical_design_advisor as advisor
    import summarize_vldb_materialization_policy as materialization
    import vldb_evidence_bundle as evidence


DATASETS = ("DEEP1M", "SIFT1M", "GIST1M")
BUDGETS = (536870912, 1073741824, 2147483648)
POLICIES = ("benefit", "indeg", "hop")
TRAINING_REPEATS = (0, 1, 2)
HELDOUT_REPEATS = (3, 4, 5)
RECALL_MIN = 0.90
HELDOUT_MIN_QPS_RATIO = 0.98
HELDOUT_GEOMEAN_QPS_RATIO = 0.99


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _integer(value: object, label: str) -> int:
    number = _finite(value, label)
    if not number.is_integer():
        raise ValueError(f"non-integral {label}: {value!r}")
    return int(number)


def _normalize_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    if len(rows) != 162:
        raise ValueError("materialization advisor requires the complete 162-row matrix")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[str, int, int, int, str]] = set()
    for raw in rows:
        try:
            dataset = str(raw["dataset"])
            budget = _integer(raw["requested_bytes"], "requested_bytes")
            policy = str(raw["policy"])
            repeat = _integer(raw["repeat"], "repeat")
            position = _integer(raw["position"], "position")
        except KeyError as exc:
            raise ValueError("materialization advisor row is missing identity") from exc
        if (
            dataset not in DATASETS
            or budget not in BUDGETS
            or policy not in POLICIES
            or repeat not in (*TRAINING_REPEATS, *HELDOUT_REPEATS)
            or position not in range(len(POLICIES))
        ):
            raise ValueError("materialization advisor row is outside the fixed matrix")
        expected_policy = POLICIES[(position + repeat % len(POLICIES)) % len(POLICIES)]
        if policy != expected_policy:
            raise ValueError("materialization split is not position-balanced")
        key = (dataset, budget, repeat, position, policy)
        if key in seen:
            raise ValueError("materialization advisor matrix has a duplicate row")
        seen.add(key)
        qps = _finite(raw["qps"], "qps", positive=True)
        recall = _finite(raw["recall"], "recall")
        if not 0 <= recall <= 1:
            raise ValueError("materialization recall is outside [0, 1]")
        physical_bytes = _integer(raw["physical_bytes"], "physical_bytes")
        if physical_bytes > budget:
            raise ValueError("materialization row exceeds its physical byte cap")
        bytes_per_query = _finite(raw["bytes_per_query"], "bytes_per_query")
        posts_per_query = _finite(raw["posts_per_query"], "posts_per_query")
        if physical_bytes < 0 or bytes_per_query < 0 or posts_per_query < 0:
            raise ValueError("materialization resource metric is negative")
        normalized.append(
            {
                **raw,
                "dataset": dataset,
                "requested_bytes": budget,
                "policy": policy,
                "repeat": repeat,
                "position": position,
                "qps": qps,
                "recall": recall,
                "physical_bytes": physical_bytes,
                "bytes_per_query": bytes_per_query,
                "posts_per_query": posts_per_query,
            }
        )
    expected = {
        (dataset, budget, repeat, position, POLICIES[(position + repeat % 3) % 3])
        for dataset in DATASETS
        for budget in BUDGETS
        for repeat in (*TRAINING_REPEATS, *HELDOUT_REPEATS)
        for position in range(3)
    }
    if seen != expected:
        raise ValueError("materialization advisor matrix is incomplete")

    for dataset in DATASETS:
        dataset_rows = [row for row in normalized if row["dataset"] == dataset]
        for field in ("binary_sha256", "input_signature"):
            if len({str(row[field]) for row in dataset_rows}) != 1:
                raise ValueError(f"materialization {field} drifts within {dataset}")
    for dataset in DATASETS:
        for budget in BUDGETS:
            for policy in POLICIES:
                cells = [
                    row
                    for row in normalized
                    if row["dataset"] == dataset
                    and row["requested_bytes"] == budget
                    and row["policy"] == policy
                ]
                for split in (TRAINING_REPEATS, HELDOUT_REPEATS):
                    positions = sorted(
                        row["position"] for row in cells if row["repeat"] in split
                    )
                    if positions != [0, 1, 2]:
                        raise ValueError("materialization split is not position-balanced")
                for field in ("selection_hash", "physical_signature", "physical_bytes"):
                    if len({str(row[field]) for row in cells}) != 1:
                        raise ValueError(f"materialization candidate {field} drift")
    normalized.sort(
        key=lambda row: (
            DATASETS.index(str(row["dataset"])),
            int(row["requested_bytes"]),
            int(row["repeat"]),
            int(row["position"]),
        )
    )
    return normalized


def _candidate(
    cells: list[dict[str, object]],
    *,
    dataset: str,
    budget: int,
    policy: str,
) -> dict[str, object]:
    training = [row for row in cells if row["repeat"] in TRAINING_REPEATS]
    return {
        "candidate_id": policy,
        "configuration": {
            "dataset": dataset,
            "materialization_policy": policy,
            "requested_bytes": budget,
        },
        "provenance": {
            "binary_sha256": str(training[0]["binary_sha256"]),
            "input_signature": str(training[0]["input_signature"]),
            "physical_signature": str(training[0]["physical_signature"]),
            "selection_hash": str(training[0]["selection_hash"]),
            "training_repeats": list(TRAINING_REPEATS),
        },
        "qps_samples": [float(row["qps"]) for row in training],
        "recall": min(float(row["recall"]) for row in training),
        "resources": {
            "mn_bytes": max(int(row["physical_bytes"]) for row in training),
            "bytes_per_query": max(float(row["bytes_per_query"]) for row in training),
            "posts_per_query": max(float(row["posts_per_query"]) for row in training),
        },
    }


def evaluate_rows(
    rows: list[dict[str, object]],
    *,
    campaign_id: str,
    protocol_fingerprint: str,
    input_seal_sha256: str,
) -> dict[str, object]:
    normalized = _normalize_rows(rows)
    grouped: dict[tuple[str, int, str], list[dict[str, object]]] = defaultdict(list)
    for row in normalized:
        grouped[(str(row["dataset"]), int(row["requested_bytes"]), str(row["policy"]))].append(row)

    failures: set[str] = set()
    cells_out: list[dict[str, object]] = []
    ratios: list[float] = []
    selected_counts: Counter[str] = Counter()
    for dataset in DATASETS:
        for budget in BUDGETS:
            request = {
                "schema_version": 1,
                "selection_id": f"{dataset}-b{budget}",
                "constraints": {
                    "recall_min": RECALL_MIN,
                    "resources_max": {"mn_bytes": float(budget)},
                },
                "objective": "qps_ci95_low",
                "candidates": [
                    _candidate(
                        grouped[(dataset, budget, policy)],
                        dataset=dataset,
                        budget=budget,
                        policy=policy,
                    )
                    for policy in POLICIES
                ],
                "provenance": {
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": protocol_fingerprint,
                    "input_seal_sha256": input_seal_sha256,
                    "split": "repeats_0_2_train__3_5_heldout",
                },
            }
            selection = advisor.select_candidate(request)
            if not selection["selection_ready"]:
                failures.add("selection_feasibility")
                cells_out.append(
                    {
                        "dataset": dataset,
                        "requested_bytes": budget,
                        "request": request,
                        "selection": selection,
                        "selected_policy": None,
                        "heldout_candidates": [],
                        "heldout_qps_ratio": 0.0,
                    }
                )
                ratios.append(0.0)
                continue

            selected_policy = str(selection["selected"]["candidate_id"])
            selected_counts[selected_policy] += 1
            heldout_candidates = []
            for policy in POLICIES:
                heldout = [
                    row
                    for row in grouped[(dataset, budget, policy)]
                    if row["repeat"] in HELDOUT_REPEATS
                ]
                feasible = (
                    min(float(row["recall"]) for row in heldout) >= RECALL_MIN
                    and max(int(row["physical_bytes"]) for row in heldout) <= budget
                )
                heldout_candidates.append(
                    {
                        "policy": policy,
                        "feasible": feasible,
                        "qps_mean": statistics.mean(float(row["qps"]) for row in heldout),
                        "qps_samples": [float(row["qps"]) for row in heldout],
                        "recall_min": min(float(row["recall"]) for row in heldout),
                        "mn_bytes_max": max(int(row["physical_bytes"]) for row in heldout),
                    }
                )
            feasible_heldout = [row for row in heldout_candidates if row["feasible"]]
            selected_heldout = next(
                row for row in heldout_candidates if row["policy"] == selected_policy
            )
            if not selected_heldout["feasible"]:
                failures.add("selected_heldout_feasibility")
            if not feasible_heldout:
                failures.add("no_heldout_feasible_oracle")
                ratio = 0.0
                oracle = None
            else:
                oracle = min(
                    feasible_heldout,
                    key=lambda row: (-row["qps_mean"], row["policy"]),
                )
                ratio = (
                    selected_heldout["qps_mean"] / oracle["qps_mean"]
                    if selected_heldout["feasible"]
                    else 0.0
                )
            ratios.append(ratio)
            if ratio < HELDOUT_MIN_QPS_RATIO:
                failures.add("heldout_cell_ratio")
            cells_out.append(
                {
                    "dataset": dataset,
                    "requested_bytes": budget,
                    "request": request,
                    "selection": selection,
                    "selected_policy": selected_policy,
                    "oracle_policy": None if oracle is None else oracle["policy"],
                    "selected_heldout_qps_mean": selected_heldout["qps_mean"],
                    "oracle_heldout_qps_mean": None if oracle is None else oracle["qps_mean"],
                    "heldout_qps_ratio": ratio,
                    "selected_heldout_feasible": selected_heldout["feasible"],
                    "heldout_candidates": heldout_candidates,
                }
            )

    ratio_min = min(ratios)
    ratio_geomean = (
        math.exp(statistics.mean(math.log(value) for value in ratios))
        if all(value > 0 for value in ratios)
        else 0.0
    )
    if ratio_geomean < HELDOUT_GEOMEAN_QPS_RATIO:
        failures.add("heldout_geomean_ratio")
    return {
        "schema_version": 1,
        "kind": "vldb_physical_design_advisor_validation",
        "campaign_id": campaign_id,
        "protocol_fingerprint": protocol_fingerprint,
        "input_seal_sha256": input_seal_sha256,
        "measured_rows": len(normalized),
        "selection_cells": len(cells_out),
        "training_repeats": list(TRAINING_REPEATS),
        "heldout_repeats": list(HELDOUT_REPEATS),
        "thresholds": {
            "recall_min": RECALL_MIN,
            "heldout_min_qps_ratio": HELDOUT_MIN_QPS_RATIO,
            "heldout_geomean_qps_ratio": HELDOUT_GEOMEAN_QPS_RATIO,
        },
        "selected_policies": dict(sorted(selected_counts.items())),
        "heldout_ratio_min": ratio_min,
        "heldout_ratio_geomean": ratio_geomean,
        "promotion_ready": not failures,
        "promotion_failures": sorted(failures),
        "claim_boundary": (
            "strict post-hoc split over a pre-existing sealed campaign; this is "
            "an auditable offline deployment policy, not a prospective or online optimizer"
        ),
        "cells": cells_out,
    }


def _read_runs(path: Path) -> list[dict[str, object]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def validate_bundle(
    bundle: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, object]:
    bundle = bundle.resolve()
    source_validation = materialization.validate_bundle(
        bundle,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    campaign = json.loads((bundle / "campaign.json").read_text())
    report = evaluate_rows(
        _read_runs(bundle / "runs.csv"),
        campaign_id=str(campaign["campaign_id"]),
        protocol_fingerprint=str(campaign["protocol_fingerprint"]),
        input_seal_sha256=file_sha256(bundle / "SEALED.json"),
    )
    report["input_validation"] = source_validation
    report["input_runs_sha256"] = file_sha256(bundle / "runs.csv")
    report["advisor_sha256"] = file_sha256(Path(advisor.__file__).resolve())
    report["validator_sha256"] = file_sha256(Path(__file__).resolve())
    return report


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _write_cells(path: Path, report: dict[str, object]) -> None:
    rows = []
    for cell in report["cells"]:
        rows.append(
            {
                "dataset": cell["dataset"],
                "requested_bytes": cell["requested_bytes"],
                "selected_policy": cell["selected_policy"],
                "oracle_policy": cell.get("oracle_policy"),
                "selected_heldout_qps_mean": cell.get("selected_heldout_qps_mean"),
                "oracle_heldout_qps_mean": cell.get("oracle_heldout_qps_mean"),
                "heldout_qps_ratio": cell["heldout_qps_ratio"],
                "selected_heldout_feasible": cell.get("selected_heldout_feasible"),
            }
        )
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_derived_files(root: Path, report: dict[str, object]) -> None:
    _write_json(root / "report.json", report)
    _write_cells(root / "heldout.csv", report)
    for cell in report["cells"]:
        stem = f"{str(cell['dataset']).lower()}_b{int(cell['requested_bytes'])}"
        _write_json(root / "requests" / f"{stem}.json", cell["request"])
        _write_json(root / "selections" / f"{stem}.json", cell["selection"])
    protocol = {
        "source_campaign_id": report["campaign_id"],
        "source_protocol_fingerprint": report["protocol_fingerprint"],
        "source_seal_sha256": report["input_seal_sha256"],
        "source_runs_sha256": report["input_runs_sha256"],
        "training_repeats": report["training_repeats"],
        "heldout_repeats": report["heldout_repeats"],
        "thresholds": report["thresholds"],
        "advisor_sha256": report["advisor_sha256"],
        "validator_sha256": report["validator_sha256"],
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_json(
        root / "campaign.json",
        {
            "campaign_id": f"vldb-physical-design-advisor-{report['campaign_id']}",
            "campaign_uuid": f"physical-design-{fingerprint[:32]}",
            "protocol_fingerprint": fingerprint,
            "protocol": protocol,
        },
    )


def build_output(
    bundle: Path,
    output: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, object]:
    output = output.resolve()
    if output.exists():
        raise ValueError(f"refusing existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = validate_bundle(
        bundle,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temporary:
        staging = Path(temporary)
        _write_derived_files(staging, report)
        evidence.seal_bundle(staging, staging / "campaign.json")
        os.rename(staging, output)
    return report


def verify_output(
    bundle: Path,
    output: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, object]:
    output = output.resolve()
    evidence.verify_bundle(output)
    expected = validate_bundle(
        bundle,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    stored = json.loads((output / "report.json").read_text())
    if stored != expected:
        raise ValueError("sealed advisor report differs from recomputation")
    with tempfile.TemporaryDirectory() as temporary:
        reference = Path(temporary)
        _write_derived_files(reference, expected)
        expected_files = {
            path.relative_to(reference).as_posix(): file_sha256(path)
            for path in reference.rglob("*")
            if path.is_file()
        }
        actual_files = {
            path.relative_to(output).as_posix(): file_sha256(path)
            for path in output.rglob("*")
            if path.is_file() and path.name not in {"SHA256SUMS", "SEALED.json"}
        }
        if actual_files != expected_files:
            raise ValueError("sealed advisor derivatives differ from recomputation")
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        child = commands.add_parser(name)
        child.add_argument("--bundle", type=Path, required=True)
        child.add_argument("--out", type=Path, required=True)
        child.add_argument("--expected-sha", required=True)
        child.add_argument("--expected-compute-host", required=True)
    args = parser.parse_args()
    function = build_output if args.command == "build" else verify_output
    report = function(
        args.bundle,
        args.out,
        expected_sha=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
