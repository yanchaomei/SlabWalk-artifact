#!/usr/bin/env python3
"""Chain a parser amendment after revalidating every retained worker run."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import amend_worker_scaling_parser as common
except ImportError:
    import amend_worker_scaling_parser as common


def amend(
    root: Path,
    parser_path: Path,
    *,
    expected_old_parser_sha: str,
) -> None:
    root = root.resolve()
    parser_path = parser_path.resolve()
    manifest_path = root / "campaign.json"
    backup_path = root / "campaign.before-parser-amendment-v2.json"
    amendment_path = root / "parser_amendment_v2.json"
    if not parser_path.is_file():
        raise ValueError(f"missing corrected parser: {parser_path}")
    if not manifest_path.is_file():
        raise ValueError(f"missing worker campaign manifest: {manifest_path}")
    if backup_path.exists() or amendment_path.exists():
        raise ValueError("parser amendment v2 has already been applied")

    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign manifest has no protocol object")
    old_sha = str(protocol.get("dhnsw_parser_sha256", ""))
    if old_sha != expected_old_parser_sha:
        raise ValueError(
            f"unexpected old parser SHA: {old_sha!r} != {expected_old_parser_sha}"
        )
    new_sha = common.file_sha256(parser_path)
    if new_sha == old_sha:
        raise ValueError("corrected parser SHA is unchanged")
    campaign_id = str(manifest.get("campaign_id", ""))
    binary_sha = str(protocol.get("dhnsw_client_binary_sha256", ""))
    expected_workers = {int(value) for value in protocol.get("workers", [])}
    if not campaign_id or len(binary_sha) != 64 or not expected_workers:
        raise ValueError("campaign identity, binary SHA, or worker matrix is invalid")

    runs = common.discover_runs(root, expected_workers)
    staging = Path(tempfile.mkdtemp(prefix=".parser-amendment-v2.", dir=root))
    revalidated = []
    try:
        for run_dir, workers in runs:
            original_path = run_dir / "frontier.csv"
            original_row = common.one_csv_row(original_path)
            if original_row.get("status") != "ok":
                raise ValueError(f"retained run is not valid before amendment: {run_dir}")
            relative = run_dir.relative_to(root)
            staged = staging / relative / "frontier.csv"
            staged.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    sys.executable,
                    str(parser_path),
                    "--result-dir",
                    str(run_dir),
                    "--datasets",
                    "deep1M",
                    "--ef-list",
                    "200",
                    "--duration",
                    "20",
                    "--threads",
                    str(workers),
                    "--campaign-id",
                    campaign_id,
                    "--binary-sha256",
                    binary_sha,
                    "--out",
                    str(staged),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            reparsed_row = common.one_csv_row(staged)
            if original_row != reparsed_row:
                changed = sorted(
                    key
                    for key in set(original_row) | set(reparsed_row)
                    if original_row.get(key) != reparsed_row.get(key)
                )
                raise ValueError(f"measurement drift in {relative}: {changed}")
            revalidated.append(
                {
                    "run": relative.as_posix(),
                    "workers": workers,
                    "row_unchanged": True,
                    "frontier_sha256": common.file_sha256(original_path),
                    "client_log_sha256": common.file_sha256(
                        run_dir / "deep1M_ef200_client.log"
                    ),
                }
            )

        new_protocol = dict(protocol)
        new_protocol["dhnsw_parser_sha256"] = new_sha
        new_manifest = dict(manifest)
        new_manifest["protocol"] = new_protocol
        new_manifest["protocol_fingerprint"] = common.protocol_fingerprint(new_protocol)
        new_manifest["parser_amendment_v2"] = "parser_amendment_v2.json"
        new_manifest_bytes = common.json_bytes(new_manifest)
        record = {
            "amended_utc": datetime.now(timezone.utc).isoformat(),
            "reason": (
                "Preserve protocol sentinels when a malformed color CSI prefix is "
                "spliced immediately before FRONTIER output. Every retained result "
                "was reparsed byte-for-field with no measurement change."
            ),
            "campaign_id": campaign_id,
            "old_parser_sha256": old_sha,
            "new_parser_sha256": new_sha,
            "original_manifest_sha256": common.bytes_sha256(original_manifest),
            "amended_manifest_sha256": common.bytes_sha256(new_manifest_bytes),
            "revalidated_runs": revalidated,
        }

        staged_manifest = staging / "campaign.json"
        staged_record = staging / "parser_amendment_v2.json"
        staged_manifest.write_bytes(new_manifest_bytes)
        staged_record.write_bytes(common.json_bytes(record))
        backup_path.write_bytes(original_manifest)
        os.replace(staged_manifest, manifest_path)
        os.replace(staged_record, amendment_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--parser", type=Path, required=True)
    parser.add_argument("--expected-old-parser-sha", required=True)
    args = parser.parse_args()
    amend(
        args.campaign_root,
        args.parser,
        expected_old_parser_sha=args.expected_old_parser_sha,
    )
    print(f"amended worker campaign parser v2: {args.campaign_root}")


if __name__ == "__main__":
    main()
