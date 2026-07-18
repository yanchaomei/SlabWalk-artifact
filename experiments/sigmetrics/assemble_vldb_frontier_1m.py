#!/usr/bin/env python3
"""Atomically assemble the repeated seven-dataset 1M frontier evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DATASETS = (
    "SIFT1M",
    "GIST1M",
    "DEEP1M",
    "BIGANN1M",
    "SPACEV1M",
    "TURING1M",
    "TTI1M",
)
METHODS = ("SHINE", "SlabWalk", "d-HNSW")
RUN_RE = re.compile(r"^r([0-9]+)$")
SHA256_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measured_run_number(path: Path) -> int:
    for run_token in (path.parent.name, path.stem):
        for prefix in ("sw_", "dhnsw_"):
            if run_token.startswith(prefix):
                run_token = run_token[len(prefix) :]
                break
        match = RUN_RE.fullmatch(run_token)
        if match is not None:
            return int(match.group(1))
    raise ValueError(f"cannot infer measured run ID from {path}")


def checked_runs(paths: list[Path], expected_repeats: int, label: str) -> list[Path]:
    paths = [path for path in paths if path.is_file()]
    if len(paths) != expected_repeats:
        raise ValueError(
            f"{label}: expected {expected_repeats} measured sources, found {len(paths)}"
        )
    ordered = sorted(paths, key=measured_run_number)
    numbers = [measured_run_number(path) for path in ordered]
    if numbers != list(range(1, expected_repeats + 1)):
        raise ValueError(f"{label}: measured run IDs must be r1..r{expected_repeats}, found {numbers}")
    return ordered


def discover_sources(campaign: Path, expected_repeats: int = 5) -> dict[str, list[Path]]:
    if not (campaign / "campaign.json").is_file():
        raise ValueError(f"missing campaign manifest: {campaign / 'campaign.json'}")
    return {
        "sw": checked_runs(
            list(campaign.glob("sw_r*/slabwalk_shine_frontier_raw.csv")),
            expected_repeats,
            "1M SHINE/SlabWalk",
        ),
        "dhnsw": checked_runs(
            list(campaign.glob("dhnsw_r*/frontier.csv")),
            expected_repeats,
            "1M d-HNSW",
        ),
    }


def discover_split_sources(
    sw_campaign: Path,
    dhnsw_campaign: Path,
    expected_repeats: int = 5,
) -> dict[str, list[Path]]:
    for campaign in (sw_campaign, dhnsw_campaign):
        if not (campaign / "campaign.json").is_file():
            raise ValueError(f"missing campaign manifest: {campaign / 'campaign.json'}")
    direct_dhnsw = list(dhnsw_campaign.glob("dhnsw_r*/frontier.csv"))
    retained_dhnsw = list(dhnsw_campaign.glob("raw_sources/dhnsw/r*.csv"))
    if direct_dhnsw and retained_dhnsw:
        raise ValueError("ambiguous d-HNSW source: campaign and retained bundle coexist")
    dhnsw_sources = (
        discover_retained_dhnsw_sources(dhnsw_campaign, expected_repeats)
        if retained_dhnsw
        else checked_runs(direct_dhnsw, expected_repeats, "1M d-HNSW")
    )
    return {
        "sw": checked_runs(
            list(sw_campaign.glob("sw_r*/slabwalk_shine_frontier_raw.csv")),
            expected_repeats,
            "1M SHINE/SlabWalk",
        ),
        "dhnsw": dhnsw_sources,
    }


def verify_bundle_sha256s(root: Path) -> dict[str, str]:
    checksum_path = root / "SHA256SUMS"
    if not checksum_path.is_file():
        raise ValueError(f"retained-source bundle has no SHA256SUMS: {root}")
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(checksum_path.read_text().splitlines(), 1):
        match = SHA256_LINE_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS line {line_number}: {root}")
        digest, name = match.groups()
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in checksums:
            raise ValueError(f"unsafe or duplicate SHA256SUMS path: {name}")
        checksums[name] = digest

    actual: dict[str, Path] = {}
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file() or path == checksum_path:
            continue
        if path.is_symlink():
            raise ValueError(f"retained-source bundle contains a symlink: {path}")
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"retained-source path escapes bundle: {path}") from exc
        actual[path.relative_to(root).as_posix()] = path
    if set(checksums) != set(actual):
        raise ValueError(
            "retained-source bundle integrity set mismatch: "
            f"missing={sorted(set(actual) - set(checksums))} "
            f"extra={sorted(set(checksums) - set(actual))}"
        )
    for name, path in actual.items():
        if file_sha256(path) != checksums[name]:
            raise ValueError(f"retained-source SHA drift: {name}")
    return checksums


def discover_retained_dhnsw_sources(
    bundle: Path, expected_repeats: int
) -> list[Path]:
    provenance_path = bundle / "PROVENANCE.json"
    if not provenance_path.is_file():
        raise ValueError(f"retained-source bundle has no PROVENANCE.json: {bundle}")
    checksums = verify_bundle_sha256s(bundle)
    provenance = load_campaign_manifest(provenance_path)
    if int(provenance.get("expected_repeats", 0)) != expected_repeats:
        raise ValueError("retained d-HNSW repeat count differs from requested protocol")
    campaign_path = bundle / "campaign.json"
    if provenance.get("campaign_manifest_sha256") != file_sha256(campaign_path):
        raise ValueError("retained d-HNSW campaign manifest SHA drift")

    records = [
        record
        for record in provenance.get("retained_sources", [])
        if isinstance(record, dict) and record.get("kind") == "dhnsw"
    ]
    if len(records) != expected_repeats:
        raise ValueError(
            f"retained d-HNSW provenance expected {expected_repeats} sources, "
            f"found {len(records)}"
        )
    paths: list[Path] = []
    seen_runs: set[str] = set()
    for record in records:
        run_id = str(record.get("run_id", ""))
        relative = Path(str(record.get("retained", "")))
        if (
            RUN_RE.fullmatch(run_id) is None
            or run_id in seen_runs
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise ValueError("retained d-HNSW provenance has an unsafe run record")
        seen_runs.add(run_id)
        path = bundle / relative
        relative_name = relative.as_posix()
        expected_sha = str(record.get("sha256", ""))
        if (
            not path.is_file()
            or checksums.get(relative_name) != expected_sha
            or file_sha256(path) != expected_sha
        ):
            raise ValueError(f"retained d-HNSW provenance SHA drift: {run_id}")
        paths.append(path)
    return checked_runs(paths, expected_repeats, "retained 1M d-HNSW")


def discover_query_pools(directory: Path) -> dict[tuple[str, str], Path]:
    if not directory.is_dir():
        raise ValueError(f"missing 1M query-pool directory: {directory}")
    found: dict[tuple[str, str], Path] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid query-pool manifest: {path}") from exc
        if record.get("kind") != "query_pool_fingerprint":
            continue
        cell = (str(record.get("dataset", "")), str(record.get("method", "")))
        if cell in found:
            raise ValueError(f"duplicate 1M query-pool cell: {cell}")
        found[cell] = path
    expected = {(dataset, method) for dataset in DATASETS for method in METHODS}
    if set(found) != expected:
        raise ValueError(
            "1M query-pool matrix mismatch: "
            f"missing={sorted(expected - set(found))} extra={sorted(set(found) - expected)}"
        )
    return found


def copy_sources(
    sources: dict[str, list[Path]], staging: Path
) -> tuple[dict[str, list[Path]], list[dict[str, str]]]:
    copied: dict[str, list[Path]] = {"sw": [], "dhnsw": []}
    records: list[dict[str, str]] = []
    for kind, paths in sources.items():
        for source in paths:
            run_id = f"r{measured_run_number(source)}"
            destination = staging / "raw_sources" / kind / f"{run_id}.csv"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied[kind].append(destination)
            records.append(
                {
                    "kind": kind,
                    "run_id": run_id,
                    "source": str(source.resolve()),
                    "retained": destination.relative_to(staging).as_posix(),
                    "sha256": file_sha256(destination),
                }
            )
    return copied, records


def copy_query_pools(
    sources: dict[tuple[str, str], Path], staging: Path
) -> list[dict[str, str]]:
    destination_root = staging / "query_pools"
    destination_root.mkdir(parents=True)
    records = []
    for (dataset, method), source in sorted(sources.items()):
        destination = destination_root / source.name
        shutil.copy2(source, destination)
        records.append(
            {
                "cell": f"{dataset}/{method}",
                "source": str(source.resolve()),
                "retained": destination.relative_to(staging).as_posix(),
                "sha256": file_sha256(destination),
            }
        )
    return records


def load_campaign_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid campaign manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"campaign manifest must be a JSON object: {path}")
    return payload


def write_campaign_evidence(
    sw_campaign: Path,
    dhnsw_campaign: Path,
    staging: Path,
) -> list[dict[str, object]]:
    """Retain source manifests and describe how methods map to campaigns."""

    sw_campaign = sw_campaign.resolve()
    dhnsw_campaign = dhnsw_campaign.resolve()
    if sw_campaign == dhnsw_campaign:
        source = sw_campaign / "campaign.json"
        payload = load_campaign_manifest(source)
        destination = staging / "campaign.json"
        shutil.copy2(source, destination)
        return [
            {
                "role": "combined",
                "methods": list(METHODS),
                "campaign": str(sw_campaign),
                "campaign_id": str(payload.get("campaign_id", "")),
                "protocol_fingerprint": str(payload.get("protocol_fingerprint", "")),
                "retained": destination.relative_to(staging).as_posix(),
                "sha256": file_sha256(destination),
            }
        ]

    source_root = staging / "source_campaigns"
    source_root.mkdir()
    specs = (
        ("shine_slabwalk", ("SHINE", "SlabWalk"), sw_campaign),
        ("dhnsw", ("d-HNSW",), dhnsw_campaign),
    )
    records: list[dict[str, object]] = []
    portable_records: list[dict[str, object]] = []
    for role, methods, campaign in specs:
        source = campaign / "campaign.json"
        payload = load_campaign_manifest(source)
        campaign_id = str(payload.get("campaign_id", ""))
        if not campaign_id:
            raise ValueError(f"campaign manifest has no campaign_id: {source}")
        destination = source_root / f"{role}.json"
        shutil.copy2(source, destination)
        retained = destination.relative_to(staging).as_posix()
        manifest_sha = file_sha256(destination)
        record = {
            "role": role,
            "methods": list(methods),
            "campaign": str(campaign),
            "campaign_id": campaign_id,
            "protocol_fingerprint": str(payload.get("protocol_fingerprint", "")),
            "retained": retained,
            "sha256": manifest_sha,
        }
        records.append(record)
        portable_records.append(
            {
                "role": role,
                "methods": list(methods),
                "campaign_id": campaign_id,
                "protocol_fingerprint": record["protocol_fingerprint"],
                "manifest": retained,
                "manifest_sha256": manifest_sha,
            }
        )

    identity_material = "\n".join(
        f"{record['role']}:{record['manifest_sha256']}"
        for record in portable_records
    )
    composite_id = hashlib.sha256(identity_material.encode()).hexdigest()[:12]
    composite = {
        "schema_version": 1,
        "kind": "composite_frontier_evidence",
        "campaign_id": f"vldb-frontier-1m-composite-{composite_id}",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "method_sources": {
            "SHINE": "shine_slabwalk",
            "SlabWalk": "shine_slabwalk",
            "d-HNSW": "dhnsw",
        },
        "source_campaigns": portable_records,
    }
    (staging / "campaign.json").write_text(
        json.dumps(composite, indent=2, sort_keys=True) + "\n"
    )
    return records


def write_sha256s(root: Path) -> None:
    output = root / "SHA256SUMS"
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != output)
    output.write_text(
        "".join(
            f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        )
    )


def assemble(
    campaign: Path | None,
    query_pools: Path,
    out_dir: Path,
    aggregate_script: Path,
    *,
    expected_repeats: int = 5,
    sw_campaign: Path | None = None,
    dhnsw_campaign: Path | None = None,
) -> None:
    if out_dir.exists():
        raise ValueError(f"output already exists: {out_dir}")
    if not aggregate_script.is_file():
        raise ValueError(f"missing frontier aggregator: {aggregate_script}")
    if campaign is not None:
        if sw_campaign is not None or dhnsw_campaign is not None:
            raise ValueError("use either --campaign or the two split campaign roots")
        sw_campaign = campaign
        dhnsw_campaign = campaign
    elif sw_campaign is None or dhnsw_campaign is None:
        raise ValueError("both SHINE/SlabWalk and d-HNSW campaign roots are required")

    sources = discover_split_sources(sw_campaign, dhnsw_campaign, expected_repeats)
    query_sources = discover_query_pools(query_pools)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        copied, source_records = copy_sources(sources, staging)
        query_records = copy_query_pools(query_sources, staging)
        campaign_records = write_campaign_evidence(
            sw_campaign, dhnsw_campaign, staging
        )
        command = [sys.executable, str(aggregate_script)]
        for path in copied["sw"]:
            command.extend(("--sw", str(path)))
        for path in copied["dhnsw"]:
            command.extend(("--dhnsw", str(path)))
        command.extend(
            (
                "--expected-repeats",
                str(expected_repeats),
                "--expected-datasets",
                ",".join(DATASETS),
                "--min-points",
                "5",
                "--expected-threads",
                "10",
                "--expected-query-contexts",
                "10",
                "--expected-top-k",
                "10",
                "--query-pools",
                str(staging / "query_pools"),
                "--out-dir",
                str(staging),
            )
        )
        subprocess.run(command, check=True)
        provenance = {
            "assembled_utc": datetime.now(timezone.utc).isoformat(),
            "expected_repeats": expected_repeats,
            "expected_datasets": list(DATASETS),
            "expected_methods": list(METHODS),
            "aggregate_script": str(aggregate_script.resolve()),
            "aggregate_script_sha256": file_sha256(aggregate_script),
            "source_campaigns": campaign_records,
            "campaign_manifest_sha256": file_sha256(staging / "campaign.json"),
            "retained_sources": source_records,
            "query_pool_manifests": query_records,
        }
        if sw_campaign.resolve() == dhnsw_campaign.resolve():
            provenance["campaign"] = str(sw_campaign.resolve())
        (staging / "PROVENANCE.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        write_sha256s(staging)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--sw-campaign", type=Path)
    parser.add_argument("--dhnsw-campaign", type=Path)
    parser.add_argument("--query-pools", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=5)
    parser.add_argument(
        "--aggregate-script",
        type=Path,
        default=Path(__file__).with_name("aggregate_frontier_repeats.py"),
    )
    args = parser.parse_args()
    if args.campaign is not None and (
        args.sw_campaign is not None or args.dhnsw_campaign is not None
    ):
        parser.error("use either --campaign or --sw-campaign/--dhnsw-campaign")
    if args.campaign is None and (
        args.sw_campaign is None or args.dhnsw_campaign is None
    ):
        parser.error("provide --campaign or both split campaign roots")
    assemble(
        args.campaign,
        args.query_pools,
        args.out_dir,
        args.aggregate_script,
        expected_repeats=args.expected_repeats,
        sw_campaign=args.sw_campaign,
        dhnsw_campaign=args.dhnsw_campaign,
    )
    print(f"assembled final VLDB 1M frontier bundle: {args.out_dir}")


if __name__ == "__main__":
    main()
