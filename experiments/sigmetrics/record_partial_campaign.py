#!/usr/bin/env python3
"""Partition a partially successful campaign into admitted and excluded cells."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_partition(
    root: Path,
    *,
    admitted_cells: list[str],
    admitted_rows: int,
    excluded_cells: list[str],
    failed_stage: str,
    reason: str,
) -> dict[str, object]:
    root = root.resolve()
    manifest_path = root / "campaign.json"
    destination = root / "campaign_partition.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing campaign manifest: {manifest_path}")
    if destination.exists():
        raise ValueError(f"campaign partition already exists: {destination}")
    admitted = sorted({cell.strip() for cell in admitted_cells if cell.strip()})
    excluded = sorted({cell.strip() for cell in excluded_cells if cell.strip()})
    if not admitted or not excluded or set(admitted) & set(excluded):
        raise ValueError("admitted and excluded cells must be nonempty and disjoint")
    if admitted_rows <= 0 or not failed_stage.strip() or not reason.strip():
        raise ValueError("admitted rows, failed stage, and reason must be explicit")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid campaign manifest: {manifest_path}") from exc
    campaign_id = str(manifest.get("campaign_id", ""))
    if not campaign_id:
        raise ValueError("campaign manifest has no campaign_id")

    inventory: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path == destination or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError(f"campaign inventory contains a symlink: {path}")
        inventory.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        })
    record: dict[str, object] = {
        "campaign_id": campaign_id,
        "closed_utc": datetime.now(timezone.utc).isoformat(),
        "status": "partial_success",
        "admitted_cells": admitted,
        "measured_rows_admitted": admitted_rows,
        "excluded_cells": excluded,
        "failed_stage": failed_stage,
        "reason": reason,
        "files": inventory,
    }
    payload = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".campaign_partition.", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--admitted-cell", action="append", default=[])
    parser.add_argument("--admitted-rows", type=int, required=True)
    parser.add_argument("--excluded-cell", action="append", default=[])
    parser.add_argument("--failed-stage", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    record = record_partition(
        args.campaign_root,
        admitted_cells=args.admitted_cell,
        admitted_rows=args.admitted_rows,
        excluded_cells=args.excluded_cell,
        failed_stage=args.failed_stage,
        reason=args.reason,
    )
    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
