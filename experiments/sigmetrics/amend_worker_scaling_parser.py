#!/usr/bin/env python3
"""Reparse a worker campaign after a non-measurement parser correction."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


WORKER_DIR = re.compile(r"^w([1-9][0-9]*)$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def protocol_fingerprint(protocol: dict[str, object]) -> str:
    payload = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def one_csv_row(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"missing frontier CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise ValueError(f"expected one frontier row in {path}, found {len(rows)}")
    return rows[0]


def discover_runs(root: Path, expected_workers: set[int]) -> list[tuple[Path, int]]:
    runs: list[tuple[Path, int]] = []
    for path in sorted((root / "raw" / "dhnsw").glob("w*/*")):
        if not path.is_dir() or not (path / "deep1M_ef200_client.log").is_file():
            continue
        match = WORKER_DIR.fullmatch(path.parent.name)
        if match is None:
            raise ValueError(f"invalid worker directory: {path.parent}")
        workers = int(match.group(1))
        if workers not in expected_workers:
            raise ValueError(f"unexpected worker count {workers} in {path}")
        runs.append((path, workers))
    if not runs:
        raise ValueError(f"no d-HNSW worker logs found under {root}")
    return runs


def amend(
    root: Path,
    parser_path: Path,
    *,
    expected_old_parser_sha: str,
) -> None:
    root = root.resolve()
    parser_path = parser_path.resolve()
    manifest_path = root / "campaign.json"
    manifest_before_path = root / "campaign.before-parser-amendment.json"
    amendment_path = root / "parser_amendment.json"
    if not parser_path.is_file():
        raise ValueError(f"missing corrected parser: {parser_path}")
    if not manifest_path.is_file():
        raise ValueError(f"missing worker campaign manifest: {manifest_path}")
    if manifest_before_path.exists() or amendment_path.exists():
        raise ValueError("parser amendment has already been applied")

    original_manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(original_manifest_bytes)
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign manifest has no protocol object")
    old_parser_sha = str(protocol.get("dhnsw_parser_sha256", ""))
    if old_parser_sha != expected_old_parser_sha:
        raise ValueError(
            f"unexpected old parser SHA: {old_parser_sha!r} != {expected_old_parser_sha}"
        )
    new_parser_sha = file_sha256(parser_path)
    if new_parser_sha == old_parser_sha:
        raise ValueError("corrected parser SHA is unchanged")
    campaign_id = str(manifest.get("campaign_id", ""))
    binary_sha = str(protocol.get("dhnsw_client_binary_sha256", ""))
    expected_workers = {int(value) for value in protocol.get("workers", [])}
    if not campaign_id or len(binary_sha) != 64 or not expected_workers:
        raise ValueError("campaign identity, binary SHA, or worker matrix is invalid")

    runs = discover_runs(root, expected_workers)
    staging = Path(tempfile.mkdtemp(prefix=".parser-amendment.", dir=root))
    records: list[dict[str, object]] = []
    staged_outputs: list[tuple[Path, Path, Path]] = []
    try:
        for run_dir, workers in runs:
            original = run_dir / "frontier.csv"
            original_row = one_csv_row(original)
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
            reparsed_row = one_csv_row(staged)
            if (
                reparsed_row.get("status") != "ok"
                or reparsed_row.get("campaign_id") != campaign_id
                or reparsed_row.get("binary_sha256") != binary_sha
                or int(reparsed_row.get("threads", "0")) != workers
                or int(reparsed_row.get("processed_queries", "0")) != 10000
            ):
                raise ValueError(f"corrected parser did not close {relative}")
            if original_row.get("status") == "ok" and original_row != reparsed_row:
                changed = sorted(
                    key
                    for key in set(original_row) | set(reparsed_row)
                    if original_row.get(key) != reparsed_row.get(key)
                )
                raise ValueError(
                    f"measurement drift in previously valid row {relative}: {changed}"
                )
            backup = run_dir / "frontier.before-parser-amendment.csv"
            if backup.exists():
                raise ValueError(f"frontier backup already exists: {backup}")
            records.append(
                {
                    "run": relative.as_posix(),
                    "workers": workers,
                    "original_status": original_row.get("status", ""),
                    "original_frontier_sha256": file_sha256(original),
                    "reparsed_frontier_sha256": file_sha256(staged),
                    "client_log_sha256": file_sha256(
                        run_dir / "deep1M_ef200_client.log"
                    ),
                }
            )
            staged_outputs.append((original, backup, staged))

        new_protocol = dict(protocol)
        new_protocol["dhnsw_parser_sha256"] = new_parser_sha
        new_manifest = dict(manifest)
        new_manifest["protocol"] = new_protocol
        new_manifest["protocol_fingerprint"] = protocol_fingerprint(new_protocol)
        new_manifest["parser_amendment"] = "parser_amendment.json"
        new_manifest_bytes = json_bytes(new_manifest)
        amendment = {
            "amended_utc": datetime.now(timezone.utc).isoformat(),
            "reason": (
                "Tolerate asynchronous reporter records spliced into d-HNSW "
                "per-thread benchmark lines; no binary, query, or measured run changed."
            ),
            "campaign_id": campaign_id,
            "old_parser_sha256": old_parser_sha,
            "new_parser_sha256": new_parser_sha,
            "original_manifest_sha256": bytes_sha256(original_manifest_bytes),
            "amended_manifest_sha256": bytes_sha256(new_manifest_bytes),
            "reparsed_runs": records,
        }
        amendment_bytes = json_bytes(amendment)

        manifest_before_path.write_bytes(original_manifest_bytes)
        for original, backup, staged in staged_outputs:
            shutil.copy2(original, backup)
            os.replace(staged, original)
        manifest_tmp = staging / "campaign.json"
        manifest_tmp.write_bytes(new_manifest_bytes)
        os.replace(manifest_tmp, manifest_path)
        amendment_tmp = staging / "parser_amendment.json"
        amendment_tmp.write_bytes(amendment_bytes)
        os.replace(amendment_tmp, amendment_path)
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
    print(f"amended worker campaign parser provenance: {args.campaign_root}")


if __name__ == "__main__":
    main()
