#!/usr/bin/env python3
"""Bind a sealed held-out physical-design advisor into the VLDB release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

try:
    from . import validate_vldb_physical_design_advisor as advisor_validation
except ImportError:
    import validate_vldb_physical_design_advisor as advisor_validation


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIXED_THRESHOLDS = {
    "recall_min": 0.90,
    "heldout_min_qps_ratio": 0.98,
    "heldout_geomean_qps_ratio": 0.99,
}
CLAIM_BOUNDARY = (
    "strict post-hoc split over a pre-existing sealed campaign; this is an "
    "auditable offline deployment policy, not a prospective or online optimizer"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing regular {label}: {path}")
    return path


def _json(path: Path, *, kind: str, label: str) -> dict[str, object]:
    _regular(path, label)
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError(f"{label} kind mismatch: {path}")
    return payload


def _finite(payload: dict[str, object], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid physical-design advisor {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite physical-design advisor {key}")
    return value


def bind(
    main_gate: Path,
    advisor_report: Path,
    source_seal: Path,
    validation_seal: Path,
) -> dict[str, object]:
    main = _json(
        main_gate,
        kind="vldb_final_evidence_gate",
        label="main evidence gate",
    )
    if main.get("ready_for_plotting") is not True:
        raise ValueError("main evidence gate is not plot-ready")
    report = _json(
        advisor_report,
        kind="vldb_physical_design_advisor_validation",
        label="physical-design advisor report",
    )
    _regular(source_seal, "physical-design source seal")
    _regular(validation_seal, "physical-design validation seal")

    if report.get("promotion_ready") is not True or report.get(
        "promotion_failures"
    ) != []:
        raise ValueError("physical-design advisor is not promotion-ready")
    if report.get("thresholds") != FIXED_THRESHOLDS:
        raise ValueError("physical-design advisor does not use the fixed thresholds")
    if report.get("training_repeats") != [0, 1, 2] or report.get(
        "heldout_repeats"
    ) != [3, 4, 5]:
        raise ValueError("physical-design advisor split drift")
    if report.get("measured_rows") != 162 or report.get("selection_cells") != 9:
        raise ValueError("physical-design advisor matrix drift")
    if report.get("claim_boundary") != CLAIM_BOUNDARY:
        raise ValueError("physical-design advisor claim boundary drift")

    fingerprint = str(report.get("protocol_fingerprint", ""))
    if SHA256_RE.fullmatch(fingerprint) is None:
        raise ValueError("physical-design advisor protocol fingerprint is invalid")
    campaign_id = str(report.get("campaign_id", "")).strip()
    if not campaign_id:
        raise ValueError("physical-design advisor campaign ID is missing")
    ratio_min = _finite(report, "heldout_ratio_min")
    ratio_geomean = _finite(report, "heldout_ratio_geomean")
    if ratio_min < FIXED_THRESHOLDS["heldout_min_qps_ratio"] or ratio_geomean < FIXED_THRESHOLDS[
        "heldout_geomean_qps_ratio"
    ]:
        raise ValueError("physical-design advisor held-out gate is inconsistent")

    selected = report.get("selected_policies")
    if not isinstance(selected, dict) or not selected:
        raise ValueError("physical-design advisor selection counts are missing")
    allowed = {"benefit", "indeg", "hop"}
    if set(selected) - allowed:
        raise ValueError("physical-design advisor selected an unknown policy")
    if any(not isinstance(value, int) or value < 0 for value in selected.values()):
        raise ValueError("physical-design advisor selection count is invalid")
    if sum(selected.values()) != 9:
        raise ValueError("physical-design advisor selection counts do not cover nine cells")

    report_digest = sha256(advisor_report)
    main["physical_design_advisor"] = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "measured_rows": 162,
        "selection_cells": 9,
        "training_repeats": [0, 1, 2],
        "heldout_repeats": [3, 4, 5],
        "thresholds": dict(FIXED_THRESHOLDS),
        "selected_policies": dict(sorted(selected.items())),
        "heldout_ratio_min": ratio_min,
        "heldout_ratio_geomean": ratio_geomean,
        "claim_boundary": CLAIM_BOUNDARY,
        "report_sha256": report_digest,
        "source_seal_sha256": sha256(source_seal),
        "validation_seal_sha256": sha256(validation_seal),
    }
    claim_inputs = main.get("claim_input_sha256")
    if not isinstance(claim_inputs, dict):
        raise ValueError("main evidence gate has no claim-input hash map")
    claim_inputs["physical_design_advisor_report"] = report_digest
    return main


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
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
    parser.add_argument("--main-gate", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--validation-bundle", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    advisor_validation.verify_output(
        args.source_bundle,
        args.validation_bundle,
        expected_sha=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
    )
    payload = bind(
        args.main_gate,
        args.validation_bundle / "report.json",
        args.source_bundle / "SEALED.json",
        args.validation_bundle / "SEALED.json",
    )
    atomic_json(args.out, payload)
    print(f"bound physical-design advisor into main evidence gate: {args.out}")


if __name__ == "__main__":
    main()
