#!/usr/bin/env python3
"""Record an assembler-only amendment to a worker-scaling campaign."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import amend_worker_scaling_parser as common
except ImportError:
    import amend_worker_scaling_parser as common


def amend(
    root: Path,
    tool_path: Path,
    *,
    expected_old_tool_sha: str,
) -> None:
    root = root.resolve()
    tool_path = tool_path.resolve()
    manifest_path = root / "campaign.json"
    backup_path = root / "campaign.before-assembler-amendment.json"
    amendment_path = root / "assembler_amendment.json"
    if not tool_path.is_file():
        raise ValueError(f"missing corrected assembler: {tool_path}")
    if not manifest_path.is_file():
        raise ValueError(f"missing worker campaign manifest: {manifest_path}")
    if backup_path.exists() or amendment_path.exists():
        raise ValueError("assembler amendment has already been applied")

    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign manifest has no protocol object")
    old_sha = str(protocol.get("assembler_sha256", ""))
    if old_sha != expected_old_tool_sha:
        raise ValueError(
            f"unexpected old tool SHA: {old_sha!r} != {expected_old_tool_sha}"
        )
    new_sha = common.file_sha256(tool_path)
    if new_sha == old_sha:
        raise ValueError("corrected assembler SHA is unchanged")
    campaign_id = str(manifest.get("campaign_id", ""))
    if not campaign_id:
        raise ValueError("campaign_id is empty")

    new_protocol = dict(protocol)
    new_protocol["assembler_sha256"] = new_sha
    new_manifest = dict(manifest)
    new_manifest["protocol"] = new_protocol
    new_manifest["protocol_fingerprint"] = common.protocol_fingerprint(new_protocol)
    new_manifest["assembler_amendment"] = "assembler_amendment.json"
    new_manifest_bytes = common.json_bytes(new_manifest)
    record = {
        "amended_utc": datetime.now(timezone.utc).isoformat(),
        "reason": (
            "Accept fixed-pool d-HNSW client logs as the authoritative retained "
            "detail source when the deprecated benchmark-details side file is absent; "
            "no binary, query, raw log, or measurement changed."
        ),
        "campaign_id": campaign_id,
        "protocol_key": "assembler_sha256",
        "old_tool_sha256": old_sha,
        "new_tool_sha256": new_sha,
        "original_manifest_sha256": common.bytes_sha256(original),
        "amended_manifest_sha256": common.bytes_sha256(new_manifest_bytes),
    }
    record_bytes = common.json_bytes(record)

    staging = Path(tempfile.mkdtemp(prefix=".assembler-amendment.", dir=root))
    try:
        staged_manifest = staging / "campaign.json"
        staged_record = staging / "assembler_amendment.json"
        staged_manifest.write_bytes(new_manifest_bytes)
        staged_record.write_bytes(record_bytes)
        backup_path.write_bytes(original)
        os.replace(staged_manifest, manifest_path)
        os.replace(staged_record, amendment_path)
    finally:
        for path in staging.iterdir() if staging.exists() else ():
            path.unlink(missing_ok=True)
        staging.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--assembler", type=Path, required=True)
    parser.add_argument("--expected-old-assembler-sha", required=True)
    args = parser.parse_args()
    amend(
        args.campaign_root,
        args.assembler,
        expected_old_tool_sha=args.expected_old_assembler_sha,
    )
    print(f"amended worker campaign assembler provenance: {args.campaign_root}")


if __name__ == "__main__":
    main()
