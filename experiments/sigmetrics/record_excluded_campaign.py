#!/usr/bin/env python3
"""Close an invalid experiment campaign without admitting any measured row."""

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


def record_exclusion(
    root: Path,
    *,
    campaign_id_override: str = "",
    status: str,
    stage: str,
    reason: str,
) -> dict[str, object]:
    root = root.resolve()
    destination = root / "campaign_failure.json"
    manifest_path = root / "campaign.json"
    if not root.is_dir():
        raise ValueError(f"missing campaign root: {root}")
    if not manifest_path.is_file() and not campaign_id_override:
        raise ValueError(f"missing campaign manifest or explicit campaign ID: {root}")
    if destination.exists():
        raise ValueError(f"campaign exclusion record already exists: {destination}")
    if not status.startswith("excluded") or not stage.strip() or not reason.strip():
        raise ValueError("exclusion status, stage, and reason must be explicit")

    manifest: dict[str, object] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid campaign manifest: {manifest_path}") from exc
    manifest_id = str(manifest.get("campaign_id", ""))
    if campaign_id_override and manifest_id and campaign_id_override != manifest_id:
        raise ValueError("explicit campaign ID does not match campaign manifest")
    campaign_id = campaign_id_override or manifest_id
    if not campaign_id:
        raise ValueError("campaign manifest has no campaign_id")

    inventory: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path == destination or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError(f"campaign inventory contains a symlink: {path}")
        inventory.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    record: dict[str, object] = {
        "campaign_id": campaign_id,
        "closed_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "failed_stage": stage,
        "reason": reason,
        "measured_rows_admitted": 0,
        "files": inventory,
    }
    payload = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".campaign_failure.", dir=root)
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
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--status", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    record = record_exclusion(
        args.campaign_root,
        campaign_id_override=args.campaign_id,
        status=args.status,
        stage=args.stage,
        reason=args.reason,
    )
    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
