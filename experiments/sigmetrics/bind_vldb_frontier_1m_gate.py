#!/usr/bin/env python3
"""Bind the validated 1M frontier gate into the main VLDB release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path, kind: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence gate: {path}") from exc
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError(f"evidence gate kind mismatch: {path}")
    if payload.get("ready_for_plotting") is not True:
        raise ValueError(f"evidence gate is not plot-ready: {path}")
    return payload


def bind(main_gate: Path, frontier_1m_gate: Path) -> dict[str, object]:
    main = load(main_gate, "vldb_final_evidence_gate")
    frontier = load(frontier_1m_gate, "vldb_frontier_1m_gate")
    for key in (
        "datasets",
        "methods",
        "expected_repeats",
        "measured_rows",
        "summary_rows",
        "query_pool_cells",
        "campaign_id",
        "raw_sha256",
        "summary_sha256",
    ):
        if key not in frontier:
            raise ValueError(f"1M frontier gate is missing {key}")
    main["frontier_1m"] = {
        key: frontier[key]
        for key in (
            "datasets",
            "methods",
            "expected_repeats",
            "measured_rows",
            "summary_rows",
            "query_pool_cells",
            "campaign_id",
            "raw_sha256",
            "summary_sha256",
        )
    }
    main["frontier_1m"]["gate_sha256"] = sha256(frontier_1m_gate)
    claim_inputs = main.get("claim_input_sha256")
    if not isinstance(claim_inputs, dict):
        raise ValueError("main evidence gate has no claim-input hash map")
    claim_inputs["frontier_1m_summary"] = frontier["summary_sha256"]
    claim_inputs["frontier_1m_gate"] = sha256(frontier_1m_gate)
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
    parser.add_argument("--frontier-1m-gate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    atomic_json(args.out, bind(args.main_gate, args.frontier_1m_gate))
    print(f"bound 1M frontier into main evidence gate: {args.out}")


if __name__ == "__main__":
    main()
