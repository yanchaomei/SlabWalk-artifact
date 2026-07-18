#!/usr/bin/env python3
"""Bind one-second /proc RSS samples to sealed binary A/B child processes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from . import verify_vldb_binary_ab as binary_ab
except ImportError:
    import verify_vldb_binary_ab as binary_ab


def _ci95(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    tcrit = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
    }.get(len(values) - 1, 1.96)
    return tcrit * statistics.stdev(values) / math.sqrt(len(values))


def _positive_int(value: Any, label: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"expected positive {label}")
    return parsed


def _convert_sample(row: dict[str, Any]) -> dict[str, Any]:
    required = {
        "timestamp_utc",
        "pid",
        "starttime",
        "staged_build",
        "budget_bytes",
        "vmrss_kib",
        "vmhwm_kib",
        "vmsize_kib",
    }
    if set(row) != required or not str(row["timestamp_utc"]).strip():
        raise ValueError("RSS sample schema drift")
    converted = dict(row)
    converted["pid"] = _positive_int(row["pid"], "sample PID")
    converted["starttime"] = str(row["starttime"])
    converted["staged_build"] = int(row["staged_build"])
    if converted["staged_build"] not in {0, 1}:
        raise ValueError("invalid staged-build sample flag")
    for field in ("budget_bytes", "vmrss_kib", "vmhwm_kib", "vmsize_kib"):
        converted[field] = _positive_int(row[field], field)
    if (
        converted["vmhwm_kib"] < converted["vmrss_kib"]
        or converted["vmsize_kib"] < converted["vmrss_kib"]
    ):
        raise ValueError("invalid RSS sample ordering")
    return converted


def correlate_samples(
    samples: Iterable[dict[str, Any]],
    cells: Iterable[dict[str, Any]],
    *,
    min_serial: int,
    min_staged: int,
    min_samples_per_process: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join samples to provenance by the PID/starttime anti-reuse key."""

    if min_serial <= 0 or min_staged <= 0 or min_samples_per_process <= 0:
        raise ValueError("RSS coverage thresholds must be positive")
    cells = [dict(cell) for cell in cells]
    cell_map: dict[str, dict[str, Any]] = {}
    for cell in cells:
        key = str(cell["starttime"])
        if not key or key in cell_map:
            raise ValueError("duplicate or missing A/B process-starttime provenance")
        cell_map[key] = cell

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_sample in samples:
        sample = _convert_sample(raw_sample)
        key = sample["starttime"]
        if key not in cell_map:
            raise ValueError("RSS sample is not bound to a sealed A/B child process")
        cell = cell_map[key]
        if (
            sample["staged_build"] != int(cell["staged_build"])
            or sample["budget_bytes"] != int(cell["budget_bytes"])
        ):
            raise ValueError("RSS sample configuration drift from A/B provenance")
        grouped[key].append(sample)
    if not grouped:
        raise ValueError("empty RSS sidecar")

    runs: list[dict[str, Any]] = []
    for key, process_samples in grouped.items():
        cell = cell_map[key]
        process_samples.sort(key=lambda row: str(row["timestamp_utc"]))
        timestamps = [str(row["timestamp_utc"]) for row in process_samples]
        hwm = [int(row["vmhwm_kib"]) for row in process_samples]
        pids = {int(row["pid"]) for row in process_samples}
        if len(process_samples) < min_samples_per_process:
            raise ValueError("RSS process has insufficient samples")
        if len(pids) != 1 or len(set(timestamps)) != len(timestamps) or any(
            left > right for left, right in zip(hwm, hwm[1:])
        ):
            raise ValueError("RSS sample PID/time/HWM sequence drift")
        pid = pids.pop()
        if "pid" in cell and int(cell["pid"]) != pid:
            raise ValueError("RSS sample PID differs from declared cell PID")
        runs.append(
            {
                "repeat": int(cell["repeat"]),
                "position": int(cell["position"]),
                "variant": str(cell["variant"]),
                "label": str(cell["label"]),
                "pid": pid,
                "starttime": key,
                "staged_build": int(cell["staged_build"]),
                "budget_bytes": int(cell["budget_bytes"]),
                "sample_count": len(process_samples),
                "first_sample_utc": timestamps[0],
                "last_sample_utc": timestamps[-1],
                "peak_vmrss_kib": max(int(row["vmrss_kib"]) for row in process_samples),
                "peak_vmhwm_kib": max(hwm),
                "peak_vmsize_kib": max(int(row["vmsize_kib"]) for row in process_samples),
            }
        )
    runs.sort(key=lambda row: (int(row["repeat"]), int(row["position"])))

    summary: list[dict[str, Any]] = []
    required = {"A": min_serial, "B": min_staged}
    for variant in ("A", "B"):
        variant_runs = [row for row in runs if row["variant"] == variant]
        if len(variant_runs) < required[variant]:
            raise ValueError(
                f"RSS {variant} coverage {len(variant_runs)} is below {required[variant]}"
            )
        values = [float(row["peak_vmhwm_kib"]) for row in variant_runs]
        rss_values = [float(row["peak_vmrss_kib"]) for row in variant_runs]
        summary.append(
            {
                "variant": variant,
                "label": variant_runs[0]["label"],
                "n": len(variant_runs),
                "peak_vmhwm_mean_kib": statistics.mean(values),
                "peak_vmhwm_median_kib": statistics.median(values),
                "peak_vmhwm_stdev_kib": statistics.stdev(values) if len(values) > 1 else 0.0,
                "peak_vmhwm_ci95_kib": _ci95(values),
                "peak_vmhwm_min_kib": min(values),
                "peak_vmhwm_max_kib": max(values),
                "peak_vmrss_mean_kib": statistics.mean(rss_values),
                "peak_vmrss_ci95_kib": _ci95(rss_values),
            }
        )
    return runs, summary


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty CSV: {path}")
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing empty output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_ab_cells(root: Path) -> list[dict[str, Any]]:
    """Extract PID/starttime and configuration identity from sealed children."""

    root = root.resolve()
    campaign = json.loads((root / "campaign.json").read_text())
    protocol = campaign["protocol"]
    rows = _read_csv(root / "runs.csv")
    cells: list[dict[str, Any]] = []
    for row in rows:
        variant = row["variant"]
        child = root / f"r{row['repeat']}_{row['position']}_{variant}"
        provenance_files = list(child.glob("*.provenance.json"))
        if len(provenance_files) != 1:
            raise ValueError("A/B child must have one execution provenance file")
        provenance_path = provenance_files[0]
        if hashlib.sha256(provenance_path.read_bytes()).hexdigest() != row[
            "execution_provenance_sha256"
        ]:
            raise ValueError("A/B execution provenance digest drift")
        provenance = json.loads(provenance_path.read_text())
        compute = provenance["executables"]["compute_node"]
        environment = protocol["variants"][variant]["environment"]
        cells.append(
            {
                "repeat": int(row["repeat"]),
                "position": int(row["position"]),
                "variant": variant,
                "label": row["label"],
                "starttime": str(compute["pid_starttime"]),
                "staged_build": int(environment["SHINE_LAVD_STAGED_BUILD"]),
                "budget_bytes": _positive_int(
                    environment["SHINE_LAVD_BUDGET_BYTES"], "provenance budget"
                ),
            }
        )
    return cells


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ab-bundle", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--sampler-metadata", type=Path, required=True)
    parser.add_argument("--sampler-source", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    parser.add_argument("--min-serial", type=int, default=1)
    parser.add_argument("--min-staged", type=int, default=1)
    parser.add_argument("--min-samples-per-process", type=int, default=10)
    parser.add_argument("--out-runs", type=Path, required=True)
    parser.add_argument("--out-summary", type=Path, required=True)
    parser.add_argument("--out-report", type=Path, required=True)
    parser.add_argument("--out-campaign", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    args = parser.parse_args()

    binary_ab.validate_bundle(
        args.ab_bundle,
        expected_sha_a=args.expected_sha,
        expected_sha_b=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
    )
    metadata = json.loads(args.sampler_metadata.read_text())
    if (
        metadata.get("binary_sha256") != args.expected_sha
        or metadata.get("host") != args.expected_compute_host
        or float(metadata.get("interval_s", 0.0)) != 1.0
        or "started_utc" not in metadata
        or "finished_utc" not in metadata
    ):
        raise ValueError("RSS sampler metadata drift")
    if not args.sampler_source.is_file() or not args.campaign_id.strip():
        raise ValueError("RSS sampler source or campaign identity is missing")
    sidecar_root = args.sampler_metadata.parent.resolve()
    for path in (
        args.samples,
        args.sampler_source,
        args.out_runs,
        args.out_summary,
        args.out_report,
        args.out_campaign,
    ):
        try:
            path.resolve().relative_to(sidecar_root)
        except ValueError as error:
            raise ValueError("RSS sidecar artifact escapes its evidence root") from error
    runs, summary = correlate_samples(
        _read_csv(args.samples),
        load_ab_cells(args.ab_bundle),
        min_serial=args.min_serial,
        min_staged=args.min_staged,
        min_samples_per_process=args.min_samples_per_process,
    )
    _write_csv(args.out_runs, runs)
    _write_csv(args.out_summary, summary)
    ab_campaign = json.loads((args.ab_bundle / "campaign.json").read_text())
    ab_manifest = args.ab_bundle / "SHA256SUMS"
    protocol = {
        "kind": "vldb_rss_sidecar_v1",
        "measurement": "one_second_proc_status_sampling",
        "compute_host": args.expected_compute_host,
        "binary_sha256": args.expected_sha,
        "ab_campaign_id": ab_campaign["campaign_id"],
        "ab_campaign_uuid": ab_campaign["campaign_uuid"],
        "ab_protocol_fingerprint": ab_campaign["protocol_fingerprint"],
        "ab_root_manifest_sha256": hashlib.sha256(ab_manifest.read_bytes()).hexdigest(),
        "sampler_interval_s": 1.0,
        "sampler_metadata_sha256": hashlib.sha256(
            args.sampler_metadata.read_bytes()
        ).hexdigest(),
        "sampler_source_sha256": hashlib.sha256(
            args.sampler_source.read_bytes()
        ).hexdigest(),
        "samples_sha256": hashlib.sha256(args.samples.read_bytes()).hexdigest(),
        "min_serial_processes": args.min_serial,
        "min_staged_processes": args.min_staged,
        "min_samples_per_process": args.min_samples_per_process,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    campaign = {
        "campaign_id": args.campaign_id,
        "campaign_uuid": str(uuid.uuid4()),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }
    args.out_campaign.write_text(json.dumps(campaign, indent=2, sort_keys=True) + "\n")
    report = {
        "kind": "vldb_rss_sidecar_report_v1",
        "campaign_id": args.campaign_id,
        "protocol_fingerprint": fingerprint,
        "sample_rows": len(_read_csv(args.samples)),
        "processes": len(runs),
        "serial_processes": sum(row["variant"] == "A" for row in runs),
        "staged_processes": sum(row["variant"] == "B" for row in runs),
        "runs_sha256": hashlib.sha256(args.out_runs.read_bytes()).hexdigest(),
        "summary_sha256": hashlib.sha256(args.out_summary.read_bytes()).hexdigest(),
    }
    args.out_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
