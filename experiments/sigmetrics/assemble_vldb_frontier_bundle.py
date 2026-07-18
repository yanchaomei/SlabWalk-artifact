#!/usr/bin/env python3
"""Atomically assemble the final three-dataset VLDB frontier evidence bundle."""

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


RUN_RE = re.compile(r"(?:^|_)(r[0-9]+)$")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_number(path: Path, *, parent: bool = False) -> int:
    raw = path.parent.name if parent else path.stem
    match = RUN_RE.search(raw)
    if match is None:
        raise ValueError(f"cannot infer measured run ID from {path}")
    return int(match.group(1)[1:])


def checked_runs(
    paths: list[Path], expected_repeats: int, label: str, *, parent: bool = False
) -> list[Path]:
    paths = [path for path in paths if path.is_file()]
    if len(paths) != expected_repeats:
        raise ValueError(
            f"{label}: expected {expected_repeats} measured sources, found {len(paths)}"
        )
    ordered = sorted(paths, key=lambda path: run_number(path, parent=parent))
    numbers = [run_number(path, parent=parent) for path in ordered]
    if len(set(numbers)) != expected_repeats:
        raise ValueError(f"{label}: duplicate measured run IDs {numbers}")
    expected = list(range(numbers[0], numbers[0] + expected_repeats))
    if numbers != expected or numbers[0] not in (0, 1):
        raise ValueError(f"{label}: non-contiguous measured run IDs {numbers}")
    return ordered


def discover_sources(
    deep_bundle: Path,
    sw_campaign: Path,
    dhnsw_campaign: Path,
    *,
    expected_repeats: int,
) -> dict[str, list[Path]]:
    return {
        "deep_sw": checked_runs(
            list((deep_bundle / "raw_sources" / "sw").glob("r*.csv")),
            expected_repeats,
            "DEEP10M SHINE/SlabWalk",
        ),
        "deep_dhnsw": checked_runs(
            list((deep_bundle / "raw_sources" / "dhnsw").glob("r*.csv")),
            expected_repeats,
            "DEEP10M d-HNSW",
        ),
        "text_sift_sw": checked_runs(
            list(sw_campaign.glob("sw_r*/slabwalk_shine_frontier_raw.csv")),
            expected_repeats,
            "TTI10M/SIFT10M SHINE/SlabWalk",
            parent=True,
        ),
        "text_sift_dhnsw": checked_runs(
            list(dhnsw_campaign.glob("r*/frontier.csv")),
            expected_repeats,
            "TTI10M/SIFT10M d-HNSW",
            parent=True,
        ),
    }


def copy_sources(
    sources: dict[str, list[Path]], staging: Path
) -> tuple[dict[str, list[Path]], list[dict[str, str]]]:
    copied: dict[str, list[Path]] = {"sw": [], "dhnsw": []}
    records: list[dict[str, str]] = []
    layout = {
        "deep_sw": ("deep10m", "sw", "sw", False),
        "deep_dhnsw": ("deep10m", "dhnsw", "dhnsw", False),
        "text_sift_sw": ("text_sift", "sw", "sw", True),
        "text_sift_dhnsw": ("text_sift", "dhnsw", "dhnsw", True),
    }
    for group, paths in sources.items():
        dataset_group, method_dir, aggregate_kind, use_parent = layout[group]
        for source in paths:
            run_id = source.parent.name if use_parent else source.stem
            if run_id.startswith("sw_"):
                run_id = run_id[3:]
            destination = (
                staging / "raw_sources" / dataset_group / method_dir / f"{run_id}.csv"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied[aggregate_kind].append(destination)
            records.append(
                {
                    "group": group,
                    "source": str(source.resolve()),
                    "retained": destination.relative_to(staging).as_posix(),
                    "sha256": file_sha256(destination),
                }
            )
    return copied, records


def load_campaign_manifest(campaign: Path) -> dict[str, object]:
    path = campaign / "campaign.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid source campaign manifest: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"source campaign manifest must be an object: {path}")
    campaign_id = str(payload.get("campaign_id", ""))
    fingerprint = str(payload.get("protocol_fingerprint", ""))
    if not campaign_id:
        raise ValueError(f"source campaign manifest has no campaign_id: {path}")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError(f"source campaign protocol fingerprint is invalid: {path}")
    return payload


def write_campaign_evidence(
    deep_sw_campaign: Path,
    deep_dhnsw_campaign: Path,
    text_sift_sw_campaign: Path,
    text_sift_dhnsw_campaign: Path,
    staging: Path,
) -> list[dict[str, object]]:
    """Retain all four source manifests and bind each dataset/method cell."""

    specs = (
        (
            "deep10m_shine_slabwalk",
            ("DEEP10M",),
            ("SHINE", "SlabWalk"),
            deep_sw_campaign,
        ),
        ("deep10m_dhnsw", ("DEEP10M",), ("d-HNSW",), deep_dhnsw_campaign),
        (
            "text_sift_shine_slabwalk",
            ("SIFT10M", "TTI10M"),
            ("SHINE", "SlabWalk"),
            text_sift_sw_campaign,
        ),
        (
            "text_sift_dhnsw",
            ("SIFT10M", "TTI10M"),
            ("d-HNSW",),
            text_sift_dhnsw_campaign,
        ),
    )
    source_root = staging / "source_campaigns"
    source_root.mkdir()
    records: list[dict[str, object]] = []
    portable_records: list[dict[str, object]] = []
    cell_sources: dict[str, str] = {}
    for role, datasets, methods, campaign in specs:
        campaign = campaign.resolve()
        source = campaign / "campaign.json"
        payload = load_campaign_manifest(campaign)
        destination = source_root / f"{role}.json"
        shutil.copy2(source, destination)
        retained = destination.relative_to(staging).as_posix()
        manifest_sha = file_sha256(destination)
        record = {
            "role": role,
            "datasets": list(datasets),
            "methods": list(methods),
            "campaign": str(campaign),
            "campaign_id": str(payload["campaign_id"]),
            "protocol_fingerprint": str(payload["protocol_fingerprint"]),
            "retained": retained,
            "sha256": manifest_sha,
        }
        records.append(record)
        portable_records.append(
            {
                "role": role,
                "datasets": list(datasets),
                "methods": list(methods),
                "campaign_id": record["campaign_id"],
                "protocol_fingerprint": record["protocol_fingerprint"],
                "manifest": retained,
                "manifest_sha256": manifest_sha,
            }
        )
        for dataset in datasets:
            for method in methods:
                cell = f"{dataset}/{method}"
                if cell in cell_sources:
                    raise ValueError(f"duplicate source campaign mapping: {cell}")
                cell_sources[cell] = role

    identity_material = "\n".join(
        f"{record['role']}:{record['manifest_sha256']}"
        for record in portable_records
    )
    composite_id = hashlib.sha256(identity_material.encode()).hexdigest()[:12]
    composite = {
        "schema_version": 2,
        "kind": "composite_frontier_evidence",
        "campaign_id": f"vldb-frontier-10m-composite-{composite_id}",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "cell_sources": cell_sources,
        "source_campaigns": portable_records,
    }
    (staging / "campaign.json").write_text(
        json.dumps(composite, indent=2, sort_keys=True) + "\n"
    )
    return records


def write_sha256s(root: Path) -> None:
    output = root / "SHA256SUMS"
    paths = sorted(
        path for path in root.rglob("*") if path.is_file() and path != output
    )
    output.write_text(
        "".join(
            f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        )
    )


def assemble(
    deep_bundle: Path,
    sw_campaign: Path,
    dhnsw_campaign: Path,
    query_pools: Path,
    out_dir: Path,
    aggregate_script: Path,
    *,
    expected_repeats: int = 5,
    deep_sw_campaign: Path | None = None,
    deep_dhnsw_campaign: Path | None = None,
) -> None:
    if out_dir.exists():
        raise ValueError(f"output already exists: {out_dir}")
    if not query_pools.is_dir():
        raise ValueError(f"missing query-pool evidence: {query_pools}")
    if not aggregate_script.is_file():
        raise ValueError(f"missing frontier aggregator: {aggregate_script}")
    if deep_sw_campaign is None or deep_dhnsw_campaign is None:
        raise ValueError("both DEEP10M source campaign roots are required")
    sources = discover_sources(
        deep_bundle,
        sw_campaign,
        dhnsw_campaign,
        expected_repeats=expected_repeats,
    )

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        copied, source_records = copy_sources(sources, staging)
        campaign_records = write_campaign_evidence(
            deep_sw_campaign,
            deep_dhnsw_campaign,
            sw_campaign,
            dhnsw_campaign,
            staging,
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
                "DEEP10M,TTI10M,SIFT10M",
                "--min-points",
                "5",
                "--expected-threads",
                "10",
                "--expected-query-contexts",
                "10",
                "--expected-top-k",
                "10",
                "--query-pools",
                str(query_pools),
                "--out-dir",
                str(staging),
            )
        )
        subprocess.run(command, check=True)

        query_records = [
            {
                "path": path.name,
                "sha256": file_sha256(path),
            }
            for path in sorted(query_pools.glob("*.json"))
        ]
        provenance = {
            "assembled_utc": datetime.now(timezone.utc).isoformat(),
            "expected_repeats": expected_repeats,
            "expected_datasets": ["DEEP10M", "TTI10M", "SIFT10M"],
            "expected_methods": ["SHINE", "SlabWalk", "d-HNSW"],
            "aggregate_script": str(aggregate_script.resolve()),
            "aggregate_script_sha256": file_sha256(aggregate_script),
            "input_roots": {
                "deep_bundle": str(deep_bundle.resolve()),
                "deep_sw_campaign": str(deep_sw_campaign.resolve()),
                "deep_dhnsw_campaign": str(deep_dhnsw_campaign.resolve()),
                "sw_campaign": str(sw_campaign.resolve()),
                "dhnsw_campaign": str(dhnsw_campaign.resolve()),
                "query_pools": str(query_pools.resolve()),
            },
            "source_campaigns": campaign_records,
            "campaign_manifest_sha256": file_sha256(staging / "campaign.json"),
            "retained_sources": source_records,
            "query_pool_manifests": query_records,
        }
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
    parser.add_argument("--deep-bundle", type=Path, required=True)
    parser.add_argument("--deep-sw-campaign", type=Path, required=True)
    parser.add_argument("--deep-dhnsw-campaign", type=Path, required=True)
    parser.add_argument("--sw-campaign", type=Path, required=True)
    parser.add_argument("--dhnsw-campaign", type=Path, required=True)
    parser.add_argument("--query-pools", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--expected-repeats", type=int, default=5)
    parser.add_argument(
        "--aggregate-script",
        type=Path,
        default=Path(__file__).with_name("aggregate_frontier_repeats.py"),
    )
    args = parser.parse_args()
    assemble(
        args.deep_bundle,
        args.sw_campaign,
        args.dhnsw_campaign,
        args.query_pools,
        args.out_dir,
        args.aggregate_script,
        expected_repeats=args.expected_repeats,
        deep_sw_campaign=args.deep_sw_campaign,
        deep_dhnsw_campaign=args.deep_dhnsw_campaign,
    )
    print(f"assembled final VLDB frontier bundle: {args.out_dir}")


if __name__ == "__main__":
    main()
