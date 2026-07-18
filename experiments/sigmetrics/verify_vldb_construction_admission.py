#!/usr/bin/env python3
"""Verify that a construction-only gate is linked to one frozen v5 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_INPUTS = {
    "promotion_report",
    "frontier_cells",
    "candidate_frontier",
    "baseline_frontier",
}
EXPECTED_METHODS = {"slabwalk", "shine"}


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


def require_sha(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"invalid {label}: {value}")


def verify_construction_admission(
    gate_path: Path,
    promotion_path: Path,
    *,
    expected_gate_sha: str,
    expected_sha_b: str,
    expected_source_tree_b: str,
) -> dict[str, Any]:
    require_sha(expected_gate_sha, "construction gate SHA")
    require_sha(expected_sha_b, "candidate binary SHA")
    require_sha(expected_source_tree_b, "candidate source-tree SHA")
    gate_path = gate_path.resolve()
    promotion_path = promotion_path.resolve()

    gate_sha = file_sha256(gate_path)
    if gate_sha != expected_gate_sha:
        raise ValueError(
            f"construction gate SHA drift: {gate_sha} != {expected_gate_sha}"
        )
    gate = load_json(gate_path, "construction gate")
    if (
        gate.get("kind") != "vldb_construction_candidate_gate_v1"
        or gate.get("construction_ready") is not True
        or gate.get("general_promotion_ready") is not False
        or gate.get("scope") != "construction_measurements_only"
        or gate.get("failures") != []
    ):
        raise ValueError("construction gate does not admit construction measurements")

    inputs = gate.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != EXPECTED_INPUTS:
        raise ValueError("construction gate input contract is incomplete")
    verified_inputs: dict[str, dict[str, str]] = {}
    for name in sorted(EXPECTED_INPUTS):
        record = inputs.get(name)
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ValueError(f"invalid {name} input record")
        recorded_path = Path(str(record["path"])).resolve()
        if name == "promotion_report" and recorded_path != promotion_path:
            raise ValueError("promotion report path drift")
        recorded_sha = str(record["sha256"])
        require_sha(recorded_sha, f"{name} SHA")
        actual_sha = file_sha256(recorded_path)
        if actual_sha != recorded_sha:
            raise ValueError(f"{name} SHA drift: {actual_sha} != {recorded_sha}")
        verified_inputs[name] = {
            "path": str(recorded_path),
            "sha256": actual_sha,
        }

    promotion = load_json(promotion_path, "promotion report")
    if (
        promotion.get("kind") != "vldb_candidate_promotion_gate_v1"
        or promotion.get("promotion_ready") is not False
        or promotion.get("failures") != ["frontier_comparison"]
    ):
        raise ValueError("original promotion failure contract changed")
    binary_ab = promotion.get("binary_ab")
    if not isinstance(binary_ab, dict) or set(binary_ab) != EXPECTED_METHODS:
        raise ValueError("promotion report lacks both binary A/B controls")
    for method in sorted(EXPECTED_METHODS):
        record = binary_ab[method]
        if record.get("ready") is not True or record.get("failures") != []:
            raise ValueError(f"{method} A/B is not ready")
        verification = record.get("verification")
        if not isinstance(verification, dict):
            raise ValueError(f"{method} A/B lacks provenance")
        if verification.get("binary_sha_b") != expected_sha_b:
            raise ValueError(f"{method} candidate binary SHA drift")
        if verification.get("source_tree_sha_b") != expected_source_tree_b:
            raise ValueError(f"{method} candidate source-tree SHA drift")

    return {
        "kind": "vldb_construction_admission_verification_v1",
        "ready": True,
        "scope": "construction_measurements_only",
        "general_promotion_ready": False,
        "construction_gate": {
            "path": str(gate_path),
            "sha256": gate_sha,
        },
        "candidate_binary_sha256": expected_sha_b,
        "candidate_source_tree_sha256": expected_source_tree_b,
        "verified_inputs": verified_inputs,
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
    parser.add_argument("--construction-gate", type=Path, required=True)
    parser.add_argument("--promotion-report", type=Path, required=True)
    parser.add_argument("--expected-gate-sha", required=True)
    parser.add_argument("--expected-sha-b", required=True)
    parser.add_argument("--expected-source-tree-b", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError(f"refusing existing admission verification: {args.out}")
    report = verify_construction_admission(
        args.construction_gate,
        args.promotion_report,
        expected_gate_sha=args.expected_gate_sha,
        expected_sha_b=args.expected_sha_b,
        expected_source_tree_b=args.expected_source_tree_b,
    )
    atomic_json(args.out, report)
    print("construction admission verified")


if __name__ == "__main__":
    main()
