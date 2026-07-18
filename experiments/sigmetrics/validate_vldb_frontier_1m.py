#!/usr/bin/env python3
"""Validate the publication-grade seven-dataset 1M frontier bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

import aggregate_frontier_repeats as aggregate
from assemble_vldb_frontier_1m import DATASETS, METHODS
from publication_metadata import publication_timestamp


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIMENSIONS = {
    "SIFT1M": 128,
    "GIST1M": 960,
    "DEEP1M": 96,
    "BIGANN1M": 128,
    "SPACEV1M": 100,
    "TURING1M": 100,
    "TTI1M": 200,
}
METRICS = {dataset: "ip" if dataset == "TTI1M" else "l2" for dataset in DATASETS}
GRAPH_QUERY_FORMATS = {
    "SIFT1M": "fbin",
    "GIST1M": "fbin",
    "DEEP1M": "fbin",
    "BIGANN1M": "u8bin",
    "SPACEV1M": "i8bin",
    "TURING1M": "fbin",
    "TTI1M": "fbin",
}
INDEX_REGION_BYTES = 4 * 1024**3
SLAB_REGION_BYTES = {
    "SIFT1M": 5 * 1024**3,
    "GIST1M": 9 * 1024**3,
    "DEEP1M": 4 * 1024**3,
    "BIGANN1M": 5 * 1024**3,
    "SPACEV1M": 4 * 1024**3,
    "TURING1M": 4 * 1024**3,
    "TTI1M": 8 * 1024**3,
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing 1M frontier CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty 1M frontier CSV: {path}")
    return rows


def required(row: dict[str, object], key: str, source: Path) -> str:
    value = str(row.get(key, "")).strip()
    if not value:
        raise ValueError(f"{source}: missing {key}")
    return value


def finite(row: dict[str, object], key: str, source: Path) -> float:
    try:
        value = float(required(row, key, source))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def integer(row: dict[str, object], key: str, source: Path) -> int:
    value = finite(row, key, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral {key}")
    return int(value)


def validate_sha(value: str, label: str) -> None:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def validate_manifest(root: Path) -> int:
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise ValueError(f"missing bundle SHA manifest: {manifest}")
    expected: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not relative or relative in expected:
            raise ValueError(f"invalid SHA256SUMS line: {line!r}")
        validate_sha(digest, f"SHA256SUMS digest for {relative}")
        expected[relative] = digest
    actual = {
        path.relative_to(root).as_posix(): file_sha256(path)
        for path in root.rglob("*")
        if path.is_file() and path != manifest
    }
    if set(actual) != set(expected):
        raise ValueError(
            "bundle SHA manifest file matrix mismatch: "
            f"missing={sorted(set(expected) - set(actual))} "
            f"extra={sorted(set(actual) - set(expected))}"
        )
    mismatches = [name for name in expected if actual[name] != expected[name]]
    if mismatches:
        raise ValueError(f"bundle SHA mismatch: {mismatches}")
    return len(actual)


def validate_query_pools(directory: Path) -> dict[str, object]:
    grouped: dict[tuple[str, str], tuple[Path, dict[str, object]]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid 1M query-pool manifest: {path}") from exc
        if record.get("kind") != "query_pool_fingerprint":
            continue
        cell = (str(record.get("dataset", "")), str(record.get("method", "")))
        if cell in grouped:
            raise ValueError(f"duplicate 1M query-pool cell: {cell}")
        grouped[cell] = (path, record)
    expected = {(dataset, method) for dataset in DATASETS for method in METHODS}
    if set(grouped) != expected:
        raise ValueError(
            "1M query-pool matrix mismatch: "
            f"missing={sorted(expected - set(grouped))} extra={sorted(set(grouped) - expected)}"
        )

    links: dict[str, dict[str, str]] = {}
    for (dataset, method), (path, record) in sorted(grouped.items()):
        if str(record.get("metric", "")) != METRICS[dataset]:
            raise ValueError(f"{path}: metric mismatch for {dataset}")
        query = record.get("query")
        groundtruth = record.get("groundtruth")
        if not isinstance(query, dict) or not isinstance(groundtruth, dict):
            raise ValueError(f"{path}: missing query or ground-truth record")
        if (
            int(query.get("rows", 0)) != 10000
            or int(groundtruth.get("rows", 0)) != 10000
            or int(query.get("dim", 0)) != DIMENSIONS[dataset]
            or int(groundtruth.get("k", 0)) < 10
        ):
            raise ValueError(f"{path}: invalid 10K query-pool shape for {dataset}")
        expected_query_format = (
            "fvecs" if method == "d-HNSW" else GRAPH_QUERY_FORMATS[dataset]
        )
        expected_gt_format = "ivecs" if method == "d-HNSW" else "bin"
        if str(query.get("format", "")) != expected_query_format:
            raise ValueError(f"{path}: unexpected physical query format")
        if str(groundtruth.get("format", "")) != expected_gt_format:
            raise ValueError(f"{path}: unexpected physical ground-truth format")
        for obj, key in (
            (query, "canonical_sha256"),
            (query, "file_sha256"),
            (groundtruth, "canonical_ids_sha256"),
            (groundtruth, "file_sha256"),
        ):
            validate_sha(str(obj.get(key, "")), f"{path.name} {key}")
        links[f"{dataset}/{method}"] = {
            "query_pool_manifest": path.name,
            "query_pool_manifest_sha256": file_sha256(path),
            "query_path": required(query, "path", path),
            "groundtruth_path": required(groundtruth, "path", path),
            "query_canonical_sha256": str(query["canonical_sha256"]),
            "groundtruth_canonical_sha256": str(
                groundtruth["canonical_ids_sha256"]
            ),
            "query_file_sha256": str(query["file_sha256"]),
            "groundtruth_file_sha256": str(groundtruth["file_sha256"]),
        }

    for dataset in DATASETS:
        query_hashes = {
            links[f"{dataset}/{method}"]["query_canonical_sha256"]
            for method in METHODS
        }
        gt_hashes = {
            links[f"{dataset}/{method}"]["groundtruth_canonical_sha256"]
            for method in METHODS
        }
        if len(query_hashes) != 1 or len(gt_hashes) != 1:
            raise ValueError(f"query-pool content mismatch for {dataset}")
    return {"measured_cells": len(grouped), "links": links}


def validate_retained_source(root: Path, relative: str, digest: str) -> None:
    validate_sha(digest, f"retained source {relative}")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"retained source path must stay inside bundle: {relative}")
    candidate = root / path
    if not candidate.is_file() or file_sha256(candidate) != digest:
        raise ValueError(f"retained source link mismatch: {relative}")


def load_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def validate_campaign_protocol(
    campaign: dict[str, object], label: str
) -> dict[str, object]:
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError(f"{label} is missing protocol")
    if (
        int(protocol.get("repeats", 0)) != 5
        or int(protocol.get("threads", 0)) != 10
        or int(protocol.get("query_contexts", 0)) != 10
        or int(protocol.get("top_k", 0)) != 10
        or protocol.get("measurement_mode") != "fixed_query_pool"
    ):
        raise ValueError(f"{label} protocol does not match the publication contract")
    return protocol


def validate_campaign_binding(
    bundle: Path,
    campaign: dict[str, object],
    provenance: dict[str, object],
    campaigns: dict[tuple[str, str], set[str]],
    expected_slabwalk_sha: str,
) -> dict[str, object]:
    campaign_path = bundle / "campaign.json"
    campaign_id = str(campaign.get("campaign_id", ""))
    if not campaign_id:
        raise ValueError("1M campaign manifest has no campaign_id")

    manifest_sha = str(provenance.get("campaign_manifest_sha256", ""))
    if manifest_sha:
        validate_sha(manifest_sha, "1M campaign manifest SHA")
        if file_sha256(campaign_path) != manifest_sha:
            raise ValueError("1M campaign manifest SHA mismatch")

    if campaign.get("kind") != "composite_frontier_evidence":
        protocol = validate_campaign_protocol(campaign, "1M campaign manifest")
        binary_sha = str(protocol.get("gb_binary_sha256", ""))
        if binary_sha and binary_sha != expected_slabwalk_sha:
            raise ValueError("1M legacy campaign SlabWalk binary SHA mismatch")
        observed_campaigns = set().union(*campaigns.values())
        if observed_campaigns != {campaign_id}:
            raise ValueError("1M campaign manifest is not linked to measured rows")
        return {
            "campaign_id": campaign_id,
            "campaign_mode": "legacy_single_campaign",
            "source_campaigns_verified": 1,
        }

    if not manifest_sha:
        raise ValueError("composite 1M provenance is missing campaign manifest SHA")
    expected_method_sources = {
        "SHINE": "shine_slabwalk",
        "SlabWalk": "shine_slabwalk",
        "d-HNSW": "dhnsw",
    }
    if campaign.get("method_sources") != expected_method_sources:
        raise ValueError("composite 1M method-to-source map mismatch")

    manifest_sources = campaign.get("source_campaigns")
    provenance_sources = provenance.get("source_campaigns")
    if not isinstance(manifest_sources, list) or not isinstance(provenance_sources, list):
        raise ValueError("composite 1M source-campaign records are missing")
    manifest_by_role = {
        str(record.get("role", "")): record
        for record in manifest_sources
        if isinstance(record, dict)
    }
    provenance_by_role = {
        str(record.get("role", "")): record
        for record in provenance_sources
        if isinstance(record, dict)
    }
    expected_roles = {
        "shine_slabwalk": ("SHINE", "SlabWalk"),
        "dhnsw": ("d-HNSW",),
    }
    if (
        len(manifest_sources) != len(expected_roles)
        or len(provenance_sources) != len(expected_roles)
        or set(manifest_by_role) != set(expected_roles)
        or set(provenance_by_role) != set(expected_roles)
    ):
        raise ValueError("composite 1M source-campaign role mismatch")

    source_campaign_ids: dict[str, str] = {}
    for role, methods in expected_roles.items():
        entry = manifest_by_role[role]
        retained_record = provenance_by_role[role]
        if tuple(entry.get("methods", ())) != methods or tuple(
            retained_record.get("methods", ())
        ) != methods:
            raise ValueError(f"composite 1M methods mismatch for {role}")

        source_campaign_id = str(entry.get("campaign_id", ""))
        protocol_fingerprint = str(entry.get("protocol_fingerprint", ""))
        retained = str(entry.get("manifest", ""))
        retained_sha = str(entry.get("manifest_sha256", ""))
        if not source_campaign_id:
            raise ValueError(f"composite 1M campaign ID missing for {role}")
        validate_sha(
            protocol_fingerprint,
            f"composite 1M {role} protocol fingerprint",
        )
        validate_sha(retained_sha, f"composite 1M {role} manifest SHA")
        for field, expected in (
            ("campaign_id", source_campaign_id),
            ("protocol_fingerprint", protocol_fingerprint),
            ("retained", retained),
            ("sha256", retained_sha),
        ):
            if str(retained_record.get(field, "")) != expected:
                raise ValueError(
                    f"composite 1M provenance mismatch for {role}/{field}"
                )

        validate_retained_source(bundle, retained, retained_sha)
        retained_campaign = load_json_object(
            bundle / retained, f"composite 1M {role} campaign manifest"
        )
        if (
            str(retained_campaign.get("campaign_id", "")) != source_campaign_id
            or str(retained_campaign.get("protocol_fingerprint", ""))
            != protocol_fingerprint
        ):
            raise ValueError(f"composite 1M retained identity mismatch for {role}")
        source_protocol = validate_campaign_protocol(
            retained_campaign, f"composite 1M {role} campaign"
        )
        if role == "shine_slabwalk" and str(
            source_protocol.get("gb_binary_sha256", "")
        ) != expected_slabwalk_sha:
            raise ValueError("composite 1M SlabWalk source binary SHA mismatch")

        observed_ids = set().union(
            *(
                campaign_ids
                for (dataset, method), campaign_ids in campaigns.items()
                if method in methods
            )
        )
        if observed_ids != {source_campaign_id}:
            raise ValueError(
                f"composite 1M row binding mismatch for {role}: "
                f"{sorted(observed_ids)}"
            )
        source_campaign_ids[role] = source_campaign_id

    return {
        "campaign_id": campaign_id,
        "campaign_mode": "composite",
        "source_campaigns_verified": len(source_campaign_ids),
        "source_campaign_ids": source_campaign_ids,
    }


def validate_summary(
    path: Path, raw_rows: list[dict[str, object]], expected_repeats: int
) -> int:
    actual_rows = read_csv(path)
    expected_rows = aggregate.summarize(raw_rows, expected_repeats)

    def key(row: dict[str, object]) -> tuple[str, str, float]:
        return str(row["dataset"]), str(row["method"]), float(row["ef"])

    actual = {key(row): row for row in actual_rows}
    expected = {key(row): row for row in expected_rows}
    if len(actual) != len(actual_rows) or set(actual) != set(expected):
        raise ValueError("1M frontier summary point matrix differs from raw evidence")
    for cell, wanted in expected.items():
        got = actual[cell]
        for field, value in wanted.items():
            observed = got.get(field, "")
            if isinstance(value, (int, float)):
                try:
                    parsed = float(observed)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"1M summary has invalid {field} for {cell}") from exc
                if not math.isfinite(parsed) or not math.isclose(
                    parsed, float(value), rel_tol=1e-9, abs_tol=1e-9
                ):
                    raise ValueError(f"1M summary mismatch for {cell}/{field}")
            elif str(observed) != str(value):
                raise ValueError(f"1M summary mismatch for {cell}/{field}")
    return len(actual_rows)


def validate(bundle: Path, expected_slabwalk_sha: str) -> dict[str, object]:
    validate_sha(expected_slabwalk_sha, "expected SlabWalk binary SHA")
    if not bundle.is_dir():
        raise ValueError(f"missing 1M frontier bundle: {bundle}")
    manifest_files = validate_manifest(bundle)
    query_report = validate_query_pools(bundle / "query_pools")
    raw_path = bundle / "frontier_repeated_raw.csv"
    rows: list[dict[str, object]] = list(read_csv(raw_path))
    aggregate.validate_protocol(rows, 10, 10, 10)
    aggregate.validate_measurement_metrics(rows)
    summaries = aggregate.summarize(rows, 5)
    aggregate.validate_matrix(summaries, list(DATASETS), 5)

    grouped: dict[tuple[str, str, float], list[str]] = defaultdict(list)
    campaigns: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        dataset = required(row, "dataset", raw_path)
        method = required(row, "method", raw_path)
        if dataset not in DATASETS or method not in METHODS:
            raise ValueError(f"unexpected 1M frontier row: {dataset}/{method}")
        ef = finite(row, "ef", raw_path)
        if integer(row, "expected_queries", raw_path) != 10000:
            raise ValueError(f"{dataset}/{method}/ef={ef}: expected exactly 10000 queries")
        binary = required(row, "binary_sha256", raw_path)
        validate_sha(binary, f"{dataset}/{method} binary SHA")
        if method != "d-HNSW" and binary != expected_slabwalk_sha:
            raise ValueError(f"{dataset}/{method}: frozen binary SHA mismatch")
        if method == "SHINE":
            if (
                required(row, "variant", raw_path) != "shine_path"
                or integer(row, "lavd_bits", raw_path) != 0
                or integer(row, "index_region_bytes", raw_path)
                != INDEX_REGION_BYTES
                or integer(row, "lavd_region_bytes", raw_path) != 0
            ):
                raise ValueError(f"{dataset}/{method}: object-native layout contract mismatch")
        elif method == "SlabWalk":
            layout_env = required(row, "layout_env", raw_path)
            if (
                required(row, "variant", raw_path) != "slabwalk_expansion"
                or integer(row, "lavd_bits", raw_path) != 8
                or integer(row, "index_region_bytes", raw_path)
                != INDEX_REGION_BYTES
                or integer(row, "lavd_region_bytes", raw_path)
                != SLAB_REGION_BYTES[dataset]
                or (dataset == "GIST1M")
                != ("SHINE_LAVD_RABITQ_B=2" in layout_env)
            ):
                raise ValueError(f"{dataset}/{method}: Slab layout contract mismatch")
        link = query_report["links"][f"{dataset}/{method}"]
        for field in aggregate.QUERY_POOL_LINK_FIELDS:
            if required(row, field, raw_path) != str(link[field]):
                raise ValueError(f"query-pool link mismatch for {dataset}/{method}/{field}")
        validate_retained_source(
            bundle,
            required(row, "source", raw_path),
            required(row, "source_sha256", raw_path),
        )
        run_id = required(row, "run_id", raw_path)
        grouped[(dataset, method, ef)].append(run_id)
        campaigns[(dataset, method)].add(required(row, "campaign_id", raw_path))
    for cell, run_ids in grouped.items():
        if sorted(run_ids) != ["r1", "r2", "r3", "r4", "r5"]:
            raise ValueError(f"1M frontier repeat mismatch for {cell}: {sorted(run_ids)}")
    for dataset in DATASETS:
        if campaigns[(dataset, "SHINE")] != campaigns[(dataset, "SlabWalk")]:
            raise ValueError(f"SHINE/SlabWalk campaign mismatch for {dataset}")

    summary_path = bundle / "frontier_summary.csv"
    summary_rows = validate_summary(summary_path, rows, 5)
    campaign_path = bundle / "campaign.json"
    provenance_path = bundle / "PROVENANCE.json"
    campaign = load_json_object(campaign_path, "1M campaign manifest")
    provenance = load_json_object(provenance_path, "1M provenance")
    if (
        tuple(provenance.get("expected_datasets", ())) != DATASETS
        or tuple(provenance.get("expected_methods", ())) != METHODS
        or int(provenance.get("expected_repeats", 0)) != 5
    ):
        raise ValueError("1M provenance contract mismatch")
    campaign_report = validate_campaign_binding(
        bundle,
        campaign,
        provenance,
        campaigns,
        expected_slabwalk_sha,
    )
    return {
        "kind": "vldb_frontier_1m_gate",
        "ready_for_plotting": True,
        "validated_utc": publication_timestamp(),
        "expected_slabwalk_sha256": expected_slabwalk_sha,
        "datasets": list(DATASETS),
        "methods": list(METHODS),
        "expected_repeats": 5,
        "measured_rows": len(rows),
        "summary_rows": summary_rows,
        "query_pool_cells": query_report["measured_cells"],
        "manifest_files": manifest_files,
        **campaign_report,
        "raw_sha256": file_sha256(raw_path),
        "summary_sha256": file_sha256(summary_path),
        "campaign_sha256": file_sha256(campaign_path),
        "provenance_sha256": file_sha256(provenance_path),
    }


def atomic_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_report_path(bundle: Path, output: Path) -> None:
    resolved_bundle = bundle.resolve()
    resolved_output = output.resolve()
    try:
        resolved_output.relative_to(resolved_bundle)
    except ValueError:
        pass
    else:
        raise ValueError("validation report must be outside the sealed bundle")
    if output.exists():
        raise ValueError(f"refusing existing validation report: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-slabwalk-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    validate_report_path(args.bundle, args.out)
    report = validate(args.bundle, args.expected_slabwalk_sha)
    atomic_json(args.out, report)
    print(
        "VLDB 1M frontier is plot-ready: "
        f"{report['measured_rows']} raw rows, {report['summary_rows']} points"
    )


if __name__ == "__main__":
    main()
