#!/usr/bin/env python3
"""Move an incomplete worker-scaling run into a content-addressed audit trail."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import amend_worker_scaling_parser as common
except ImportError:
    import amend_worker_scaling_parser as common


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUN_PATH = re.compile(r"^raw/dhnsw/(w[1-9][0-9]*)/(warmup[0-9]+|r[0-9]+)$")


def frontier_is_successful(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    return len(rows) == 1 and rows[0].get("status") == "ok"


def archive_failed_run(
    root: Path,
    relative_run: Path,
    *,
    archive_name: str,
    reason: str,
) -> None:
    root = root.resolve()
    relative_text = relative_run.as_posix()
    match = RUN_PATH.fullmatch(relative_text)
    if match is None:
        raise ValueError(f"invalid worker run path: {relative_text}")
    if not SAFE_NAME.fullmatch(archive_name):
        raise ValueError(f"invalid archive name: {archive_name}")
    if not reason.strip():
        raise ValueError("archive reason is empty")
    manifest_path = root / "campaign.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing campaign manifest: {manifest_path}")
    campaign = json.loads(manifest_path.read_text())
    campaign_id = str(campaign.get("campaign_id", ""))
    if not campaign_id:
        raise ValueError("campaign_id is empty")

    source = root / relative_run
    if not source.is_dir():
        raise ValueError(f"missing failed run directory: {source}")
    if frontier_is_successful(source / "frontier.csv"):
        raise ValueError(f"refusing to archive a successful run: {source}")

    worker, run_id = match.groups()
    destination_relative = (
        Path("failed_runs") / "dhnsw" / worker / f"{run_id}-{archive_name}"
    )
    destination = root / destination_relative
    record_path = root / f"failed_run_archive_{worker}_{run_id}_{archive_name}.json"
    if destination.exists() or record_path.exists():
        raise ValueError("failed-run archive already exists")

    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"failed run contains unsupported symlink: {path}")
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": common.file_sha256(path),
                }
            )
    if not files:
        raise ValueError("failed run has no retained files")
    record = {
        "archived_utc": datetime.now(timezone.utc).isoformat(),
        "campaign_id": campaign_id,
        "reason": reason.strip(),
        "source": relative_text,
        "archive": destination_relative.as_posix(),
        "campaign_manifest_sha256": common.file_sha256(manifest_path),
        "files": files,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".failed-run-archive.", dir=root))
    try:
        staged_record = staging / record_path.name
        staged_record.write_bytes(common.json_bytes(record))
        os.replace(source, destination)
        os.replace(staged_record, record_path)
    finally:
        for path in staging.iterdir() if staging.exists() else ():
            path.unlink(missing_ok=True)
        staging.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--archive-name", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    archive_failed_run(
        args.campaign_root,
        args.run,
        archive_name=args.archive_name,
        reason=args.reason,
    )
    print(f"archived incomplete worker run: {args.run}")


if __name__ == "__main__":
    main()
