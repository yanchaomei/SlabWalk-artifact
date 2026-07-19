#!/usr/bin/env python3
"""Seal and verify a completed VLDB multi-CN campaign without rewriting it."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
SYSTEMS = ("SHINE", "SlabWalk", "d-HNSW")
CN_COUNTS = (1, 2, 3)
REPEATS = 5
TOOL_ROLES = (
    "assembler",
    "dhnsw_parser",
    "query_fingerprinter",
    "recorder",
    "runner",
)
HARNESS_ROLES = (*TOOL_ROLES, "sealer")
INVENTORY_NAME = "MULTICN_SHA256SUMS"
SEAL_NAME = "MULTICN_SEALED.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or unsafe {label}: {path}")
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _csv(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or unsafe {label}: {path}")
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing {label} header")
        rows = list(reader)
    if not rows:
        raise ValueError(f"empty {label}")
    return rows


def _contained(root: Path, raw: str, label: str) -> Path:
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{label} path escapes campaign root")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} path escapes campaign root") from exc
    return resolved


def _assert_no_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"campaign tree contains a symlink: {path}")


def _validate_sha(value: object, label: str) -> str:
    digest = str(value)
    if SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"invalid {label}")
    return digest


def _validate_campaign(root: Path) -> dict[str, Any]:
    campaign_path = root / "campaign.json"
    campaign = _json(campaign_path, "campaign manifest")
    if campaign.get("kind") != "vldb_multicn_campaign":
        raise ValueError("unsupported multi-CN campaign kind")
    campaign_id = str(campaign.get("campaign_id", "")).strip()
    if not campaign_id:
        raise ValueError("multi-CN campaign ID is missing")
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("multi-CN campaign protocol is missing")
    fingerprint = _validate_sha(
        campaign.get("protocol_fingerprint"), "protocol fingerprint"
    )
    observed = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if observed != fingerprint:
        raise ValueError("multi-CN protocol fingerprint drift")
    if tuple(campaign.get("datasets", [])) != DATASETS:
        raise ValueError("multi-CN dataset matrix drift")
    if tuple(campaign.get("systems", [])) != SYSTEMS:
        raise ValueError("multi-CN system matrix drift")
    if tuple(campaign.get("cn_counts", [])) != CN_COUNTS:
        raise ValueError("multi-CN count matrix drift")
    if int(campaign.get("repeats", 0)) != REPEATS:
        raise ValueError("multi-CN repeat contract drift")
    tools = campaign.get("tool_sha256")
    if not isinstance(tools, dict) or tuple(sorted(tools)) != tuple(
        sorted(TOOL_ROLES)
    ):
        raise ValueError("multi-CN tool identity is incomplete")
    for role in TOOL_ROLES:
        _validate_sha(tools[role], f"{role} SHA")
    if protocol.get("tool_sha256") != tools:
        raise ValueError("multi-CN protocol does not bind the tool identity")
    return campaign


def _validate_harness(root: Path, campaign: dict[str, Any]) -> str:
    harness_root = root / "harness"
    manifest_path = harness_root / "harness.json"
    manifest = _json(manifest_path, "harness manifest")
    entries = manifest.get("entries")
    if manifest.get("schema_version") != 1 or not isinstance(entries, dict):
        raise ValueError("unsupported harness manifest")
    if tuple(sorted(entries)) != tuple(sorted(HARNESS_ROLES)):
        raise ValueError("harness role set is incomplete")
    expected_files = {manifest_path.resolve()}
    for role in HARNESS_ROLES:
        record = entries.get(role)
        if not isinstance(record, dict):
            raise ValueError(f"malformed harness entry: {role}")
        path = _contained(harness_root, str(record.get("path", "")), "harness")
        expected_files.add(path)
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing harness file: {role}")
        digest = _validate_sha(record.get("sha256"), f"harness {role} SHA")
        if file_sha256(path) != digest:
            raise ValueError(f"harness {role} SHA mismatch")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ValueError(f"harness {role} size mismatch")
        if role in TOOL_ROLES and digest != campaign["tool_sha256"][role]:
            raise ValueError(f"harness {role} SHA drift from campaign protocol")
    actual_files = {
        path.resolve() for path in harness_root.iterdir() if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("harness directory contains untracked files")
    return file_sha256(manifest_path)


def _run_key(row: dict[str, str], label: str) -> tuple[str, str, int, int]:
    try:
        key = (
            row["dataset"],
            row["system"],
            int(row["cn_count"]),
            int(row["repeat"]),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"malformed {label} identity") from exc
    return key


def _validate_runs(
    root: Path, campaign: dict[str, Any]
) -> tuple[list[dict[str, str]], set[tuple[str, str, int, int]]]:
    runs = _csv(root / "runs.csv", "raw run matrix")
    expected = {
        (dataset, system, cn_count, repeat)
        for dataset in DATASETS
        for system in SYSTEMS
        for cn_count in CN_COUNTS
        for repeat in range(REPEATS)
    }
    keys = [_run_key(row, "raw run matrix") for row in runs]
    if len(runs) != len(expected) or len(set(keys)) != len(keys) or set(keys) != expected:
        raise ValueError("raw run matrix is incomplete or duplicated")
    seen_sources: set[Path] = set()
    for row, key in zip(runs, keys):
        if row.get("campaign_id") != campaign["campaign_id"]:
            raise ValueError("raw run campaign identity drift")
        if row.get("protocol_fingerprint") != campaign["protocol_fingerprint"]:
            raise ValueError("raw run protocol identity drift")
        source = _contained(root, row.get("source", ""), "source")
        if not source.is_file() or source.is_symlink() or source in seen_sources:
            raise ValueError("raw run source is missing, unsafe, or reused")
        seen_sources.add(source)
        expected_sha = _validate_sha(row.get("source_sha256"), "source SHA")
        if file_sha256(source) != expected_sha:
            raise ValueError("raw run source SHA mismatch")
        payload = _json(source, "raw run source")
        for field, value in (
            ("campaign_id", campaign["campaign_id"]),
            ("protocol_fingerprint", campaign["protocol_fingerprint"]),
            ("dataset", key[0]),
            ("system", key[1]),
            ("cn_count", key[2]),
            ("repeat", key[3]),
        ):
            if payload.get(field) != value:
                raise ValueError(f"raw run source identity drift: {field}")
    if len(seen_sources) != 135:
        raise ValueError("raw run source inventory is incomplete")
    return runs, expected


def _validate_summary(
    root: Path,
    campaign: dict[str, Any],
    raw_runs: list[dict[str, str]],
    expected_keys: set[tuple[str, str, int, int]],
) -> dict[str, Any]:
    summary_root = root / "summary"
    summary_runs = _csv(summary_root / "runs.csv", "normalized run matrix")
    summary_keys = [_run_key(row, "normalized run matrix") for row in summary_runs]
    if len(summary_runs) != 135 or set(summary_keys) != expected_keys:
        raise ValueError("normalized run matrix is incomplete or duplicated")
    raw_by_key = {_run_key(row, "raw run matrix"): row for row in raw_runs}
    for row, key in zip(summary_runs, summary_keys):
        raw = raw_by_key[key]
        for field in (
            "campaign_id",
            "protocol_fingerprint",
            "source",
            "source_sha256",
        ):
            if row.get(field) != raw.get(field):
                raise ValueError(f"normalized run identity drift: {field}")

    summaries = _csv(summary_root / "summary.csv", "summary cell matrix")
    summary_cells: set[tuple[str, str, int]] = set()
    for row in summaries:
        try:
            key = (row["dataset"], row["system"], int(row["cn_count"]))
            count = int(row["n"])
        except (KeyError, ValueError) as exc:
            raise ValueError("malformed summary cell matrix") from exc
        if count != REPEATS:
            raise ValueError("summary cell repeat count drift")
        summary_cells.add(key)
    expected_cells = {
        (dataset, system, cn_count)
        for dataset in DATASETS
        for system in SYSTEMS
        for cn_count in CN_COUNTS
    }
    if len(summaries) != 27 or summary_cells != expected_cells:
        raise ValueError("summary cell matrix is incomplete or duplicated")

    gate = _json(summary_root / "gate.json", "promotion gate")
    if gate.get("kind") != "vldb_multicn_promotion_gate":
        raise ValueError("unsupported multi-CN promotion gate")
    for field in ("campaign_id", "protocol_fingerprint"):
        if gate.get(field) != campaign[field]:
            raise ValueError(f"promotion gate {field} drift")
    if not isinstance(gate.get("promotion_ready"), bool):
        raise ValueError("promotion gate decision is missing")
    if int(gate.get("measured_rows", -1)) != 135:
        raise ValueError("promotion gate measured-row count drift")
    if int(gate.get("cells", -1)) != 27:
        raise ValueError("promotion gate cell count drift")
    if int(gate.get("source_files_verified", -1)) != 135:
        raise ValueError("promotion gate source count drift")
    failures = gate.get("promotion_failures")
    if not isinstance(failures, list) or bool(failures) == bool(
        gate["promotion_ready"]
    ):
        raise ValueError("promotion gate failure list contradicts its decision")

    derived_campaign = _json(summary_root / "campaign.json", "summary campaign")
    if derived_campaign.get("campaign_id") != campaign["campaign_id"]:
        raise ValueError("summary campaign identity drift")
    if derived_campaign.get("protocol_fingerprint") != campaign[
        "protocol_fingerprint"
    ]:
        raise ValueError("summary campaign protocol drift")
    if derived_campaign.get("input_manifest_sha256") != file_sha256(
        root / "campaign.json"
    ):
        raise ValueError("summary campaign input-manifest SHA drift")
    if derived_campaign.get("input_runs_sha256") != file_sha256(root / "runs.csv"):
        raise ValueError("summary campaign input-runs SHA drift")
    return gate


def _validate_tree(root: Path) -> dict[str, Any]:
    _assert_no_symlinks(root)
    campaign = _validate_campaign(root)
    harness_sha = _validate_harness(root, campaign)
    raw_runs, expected_keys = _validate_runs(root, campaign)
    gate = _validate_summary(root, campaign, raw_runs, expected_keys)
    runner_log = root / "runner.log"
    if not runner_log.is_file() or runner_log.is_symlink() or runner_log.stat().st_size == 0:
        raise ValueError("completed campaign is missing its runner log")
    return {
        "campaign": campaign,
        "gate": gate,
        "harness_manifest_sha256": harness_sha,
        "runner_log_sha256": file_sha256(runner_log),
    }


def _inventory_paths(root: Path) -> list[Path]:
    excluded = {(root / INVENTORY_NAME).resolve(), (root / SEAL_NAME).resolve()}
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.resolve() not in excluded
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_inventory(root: Path) -> tuple[Path, int]:
    inventory = root / INVENTORY_NAME
    payload = "".join(
        f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
        for path in _inventory_paths(root)
    ).encode()
    _atomic_write(inventory, payload)
    return inventory, len(payload.splitlines())


def _verify_inventory(root: Path) -> int:
    inventory = root / INVENTORY_NAME
    if not inventory.is_file() or inventory.is_symlink():
        raise ValueError("missing or unsafe multi-CN inventory")
    entries: dict[Path, str] = {}
    for line_number, line in enumerate(inventory.read_text().splitlines(), start=1):
        if "  " not in line:
            raise ValueError(f"malformed inventory line {line_number}")
        digest, raw = line.split("  ", 1)
        _validate_sha(digest, "inventory SHA")
        path = _contained(root, raw, "inventory")
        if path in entries:
            raise ValueError("duplicate inventory path")
        entries[path] = digest
    actual = {path.resolve() for path in _inventory_paths(root)}
    if set(entries) != actual:
        raise ValueError("inventory does not cover the complete campaign tree")
    for path, digest in entries.items():
        if not path.is_file() or path.is_symlink() or file_sha256(path) != digest:
            raise ValueError(f"inventory SHA mismatch: {path}")
    return len(entries)


def seal_campaign(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"missing campaign root: {root}")
    if (root / INVENTORY_NAME).exists() or (root / SEAL_NAME).exists():
        raise ValueError(f"campaign is already sealed: {root}")
    validated = _validate_tree(root)
    inventory, file_count = _write_inventory(root)
    campaign = validated["campaign"]
    gate = validated["gate"]
    record: dict[str, Any] = {
        "schema_version": 1,
        "kind": "vldb_multicn_campaign_seal",
        "campaign_id": campaign["campaign_id"],
        "protocol_fingerprint": campaign["protocol_fingerprint"],
        "campaign_sha256": file_sha256(root / "campaign.json"),
        "gate_sha256": file_sha256(root / "summary" / "gate.json"),
        "harness_manifest_sha256": validated["harness_manifest_sha256"],
        "runner_log_sha256": validated["runner_log_sha256"],
        "promotion_ready": gate["promotion_ready"],
        "promotion_failures": gate["promotion_failures"],
        "measured_rows": gate["measured_rows"],
        "cells": gate["cells"],
        "source_files_verified": gate["source_files_verified"],
        "inventory": INVENTORY_NAME,
        "inventory_sha256": file_sha256(inventory),
        "sealed_file_count": file_count,
        "sealed_utc": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(
        root / SEAL_NAME,
        (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(),
    )
    verify_campaign(root)
    return record


def verify_campaign(root: Path) -> dict[str, Any]:
    root = root.resolve()
    record = _json(root / SEAL_NAME, "multi-CN campaign seal")
    if record.get("schema_version") != 1 or record.get("kind") != (
        "vldb_multicn_campaign_seal"
    ):
        raise ValueError("unsupported multi-CN campaign seal")
    inventory = root / INVENTORY_NAME
    if record.get("inventory") != INVENTORY_NAME or record.get(
        "inventory_sha256"
    ) != file_sha256(inventory):
        raise ValueError("multi-CN inventory identity drift")
    file_count = _verify_inventory(root)
    if int(record.get("sealed_file_count", -1)) != file_count:
        raise ValueError("sealed file count drift")
    validated = _validate_tree(root)
    campaign = validated["campaign"]
    gate = validated["gate"]
    expected = {
        "campaign_id": campaign["campaign_id"],
        "protocol_fingerprint": campaign["protocol_fingerprint"],
        "campaign_sha256": file_sha256(root / "campaign.json"),
        "gate_sha256": file_sha256(root / "summary" / "gate.json"),
        "harness_manifest_sha256": validated["harness_manifest_sha256"],
        "runner_log_sha256": validated["runner_log_sha256"],
        "promotion_ready": gate["promotion_ready"],
        "promotion_failures": gate["promotion_failures"],
        "measured_rows": gate["measured_rows"],
        "cells": gate["cells"],
        "source_files_verified": gate["source_files_verified"],
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise ValueError(f"sealed {field} drift")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("seal", "verify"):
        child = commands.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    report = seal_campaign(args.root) if args.command == "seal" else verify_campaign(args.root)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
