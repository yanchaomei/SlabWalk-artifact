#!/usr/bin/env python3
"""Sample VmRSS/VmHWM for one frozen binary while a tmux campaign exists."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FIELDS = (
    "timestamp_utc",
    "pid",
    "starttime",
    "staged_build",
    "budget_bytes",
    "vmrss_kib",
    "vmhwm_kib",
    "vmsize_kib",
)


def _read_environment(path: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in path.read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        environment[key.decode(errors="replace")] = value.decode(errors="replace")
    return environment


def _read_status(path: Path) -> dict[str, int]:
    status: dict[str, int] = {}
    for line in path.read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:", "VmSize:")):
            key, value = line.split(":", 1)
            status[key] = int(value.strip().split()[0])
    if set(status) != {"VmRSS", "VmHWM", "VmSize"}:
        raise ValueError("incomplete process memory status")
    return status


def collect_samples(
    proc_root: Path, binary: Path, timestamp_utc: str
) -> list[dict[str, Any]]:
    """Collect one instantaneous row for every process executing binary."""

    binary = binary.resolve()
    rows: list[dict[str, Any]] = []
    for process in sorted(proc_root.glob("[0-9]*"), key=lambda path: path.name):
        try:
            if Path(os.path.realpath(process / "exe")) != binary:
                continue
            pid = int(process.name)
            raw_stat = (process / "stat").read_text()
            fields = raw_stat[raw_stat.rfind(")") + 2 :].split()
            starttime = fields[19]
            environment = _read_environment(process / "environ")
            status = _read_status(process / "status")
            rows.append(
                {
                    "timestamp_utc": timestamp_utc,
                    "pid": pid,
                    "starttime": starttime,
                    "staged_build": environment.get("SHINE_LAVD_STAGED_BUILD", ""),
                    "budget_bytes": environment.get("SHINE_LAVD_BUDGET_BYTES", ""),
                    "vmrss_kib": status["VmRSS"],
                    "vmhwm_kib": status["VmHWM"],
                    "vmsize_kib": status["VmSize"],
                }
            )
        except (
            FileNotFoundError,
            IndexError,
            PermissionError,
            ProcessLookupError,
            ValueError,
        ):
            continue
    return rows


def _tmux_exists(name: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--watch-tmux", required=True)
    parser.add_argument("--interval-s", type=float, default=1.0)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()

    binary = args.binary.resolve(strict=True)
    if not os.access(binary, os.X_OK):
        raise ValueError("RSS sampler binary is not executable")
    if not args.watch_tmux.strip() or args.interval_s <= 0.0:
        raise ValueError("RSS sampler watch target or interval is invalid")
    if args.out_root.exists():
        raise ValueError(f"refusing existing RSS output root: {args.out_root}")
    if not _tmux_exists(args.watch_tmux):
        raise ValueError(f"watched tmux session does not exist: {args.watch_tmux}")

    args.out_root.mkdir(parents=True)
    source = Path(__file__).resolve()
    source_copy = args.out_root / "sampler_source.py"
    shutil.copyfile(source, source_copy)
    binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    metadata = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "binary_path": str(binary),
        "binary_sha256": binary_sha,
        "watch_tmux": args.watch_tmux,
        "interval_s": args.interval_s,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "fields": list(FIELDS),
        "sampler_source": source_copy.name,
        "sampler_source_sha256": hashlib.sha256(source_copy.read_bytes()).hexdigest(),
    }
    metadata_path = args.out_root / "sampler.json"
    samples_path = args.out_root / "samples.csv"
    rows_written = 0
    with samples_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        handle.flush()
        while _tmux_exists(args.watch_tmux):
            timestamp = datetime.now(timezone.utc).isoformat()
            for row in collect_samples(args.proc_root, binary, timestamp):
                writer.writerow(row)
                rows_written += 1
            handle.flush()
            time.sleep(args.interval_s)
    metadata["finished_utc"] = datetime.now(timezone.utc).isoformat()
    metadata["sample_rows"] = rows_written
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    if rows_written == 0:
        raise RuntimeError("RSS sampler observed no process for the frozen binary")
    print(json.dumps(metadata, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
