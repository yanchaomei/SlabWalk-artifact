#!/usr/bin/env python3
"""Record a fail-closed d-HNSW runner amendment for a worker campaign."""

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
    runner_path: Path,
    *,
    expected_old_runner_sha: str,
) -> None:
    root = root.resolve()
    runner_path = runner_path.resolve()
    manifest_path = root / "campaign.json"
    backup_path = root / "campaign.before-dhnsw-runner-amendment.json"
    amendment_path = root / "dhnsw_runner_amendment.json"
    if not runner_path.is_file():
        raise ValueError(f"missing corrected d-HNSW runner: {runner_path}")
    if not manifest_path.is_file():
        raise ValueError(f"missing worker campaign manifest: {manifest_path}")
    if backup_path.exists() or amendment_path.exists():
        raise ValueError("d-HNSW runner amendment has already been applied")

    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign manifest has no protocol object")
    old_sha = str(protocol.get("dhnsw_runner_sha256", ""))
    if old_sha != expected_old_runner_sha:
        raise ValueError(
            f"unexpected old runner SHA: {old_sha!r} != {expected_old_runner_sha}"
        )
    new_sha = common.file_sha256(runner_path)
    if new_sha == old_sha:
        raise ValueError("corrected d-HNSW runner SHA is unchanged")
    campaign_id = str(manifest.get("campaign_id", ""))
    if not campaign_id:
        raise ValueError("campaign_id is empty")

    new_protocol = dict(protocol)
    new_protocol["dhnsw_runner_sha256"] = new_sha
    new_manifest = dict(manifest)
    new_manifest["protocol"] = new_protocol
    new_manifest["protocol_fingerprint"] = common.protocol_fingerprint(new_protocol)
    new_manifest["dhnsw_runner_amendment"] = "dhnsw_runner_amendment.json"
    new_manifest_bytes = common.json_bytes(new_manifest)
    record = {
        "amended_utc": datetime.now(timezone.utc).isoformat(),
        "reason": (
            "Create the released client's aggregate-output directory, remove stale "
            "side files before every EF point, and fail closed when a successful "
            "client does not publish fresh aggregate details. Existing measurements "
            "are unchanged; subsequent runs retain an unambiguous detail record."
        ),
        "campaign_id": campaign_id,
        "protocol_key": "dhnsw_runner_sha256",
        "old_tool_sha256": old_sha,
        "new_tool_sha256": new_sha,
        "original_manifest_sha256": common.bytes_sha256(original),
        "amended_manifest_sha256": common.bytes_sha256(new_manifest_bytes),
    }

    staging = Path(tempfile.mkdtemp(prefix=".dhnsw-runner-amendment.", dir=root))
    try:
        staged_manifest = staging / "campaign.json"
        staged_record = staging / "dhnsw_runner_amendment.json"
        staged_manifest.write_bytes(new_manifest_bytes)
        staged_record.write_bytes(common.json_bytes(record))
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
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--expected-old-runner-sha", required=True)
    args = parser.parse_args()
    amend(
        args.campaign_root,
        args.runner,
        expected_old_runner_sha=args.expected_old_runner_sha,
    )
    print(f"amended worker campaign d-HNSW runner: {args.campaign_root}")


if __name__ == "__main__":
    main()
