#!/usr/bin/env python3
"""Block VLDB plotting until the final measured evidence matrix is complete."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable

try:
    from . import aggregate_frontier_repeats as aggregate_frontier
    from . import assemble_vldb_10m_build_scaling as build_scaling_10m_assembler
    from . import assemble_vldb_query_profile as query_profile_assembler
    from . import assemble_vldb_lifecycle_controls as lifecycle_controls
    from . import summarize_slab_build_cost as build_cost_summary
    from . import summarize_vldb_cache_control as cache_control_summary
    from . import summarize_vldb_colocation_control as colocation_control_summary
    from . import summarize_vldb_mechanism_controls as mechanism_control_summary
    from . import summarize_vldb_resource_ledger as resource_ledger_summary
    from . import vldb_evidence_bundle
    from .publication_metadata import (
        normalize_publication_paths,
        publication_timestamp,
    )
except ImportError:
    import aggregate_frontier_repeats as aggregate_frontier
    import assemble_vldb_10m_build_scaling as build_scaling_10m_assembler
    import assemble_vldb_query_profile as query_profile_assembler
    import assemble_vldb_lifecycle_controls as lifecycle_controls
    import summarize_slab_build_cost as build_cost_summary
    import summarize_vldb_cache_control as cache_control_summary
    import summarize_vldb_colocation_control as colocation_control_summary
    import summarize_vldb_mechanism_controls as mechanism_control_summary
    import summarize_vldb_resource_ledger as resource_ledger_summary
    import vldb_evidence_bundle
    from publication_metadata import normalize_publication_paths, publication_timestamp


FRONTIER_DATASETS = ("DEEP10M", "TTI10M", "SIFT10M")
FRONTIER_METHODS = ("SHINE", "SlabWalk", "d-HNSW")
QUERY_POOL_DIMENSIONS = {"DEEP10M": 96, "SIFT10M": 128, "TTI10M": 200}
QUERY_POOL_METRICS = {"DEEP10M": "l2", "SIFT10M": "l2", "TTI10M": "ip"}
ROBUSTNESS_CELLS = {
    ("workers", value) for value in ("1", "8", "16", "40")
} | {
    ("coroutines", value) for value in ("1", "2", "4", "8", "16")
} | {
    ("top_k", value) for value in ("1", "10", "50", "100")
} | {
    ("query_distribution", value) for value in ("uniform", "zipf1.0")
} | {
    ("latency_instrumentation", value) for value in ("off", "on")
}
WORKER_SCALING_METHODS = ("SHINE", "d-HNSW", "SlabWalk")
WORKER_SCALING_WORKERS = (1, 8, 16, 40)
RESOURCE_LAYOUTS = ("legacy", "fixed", "variable")
RESOURCE_MN_COUNTS = (1, 3, 5)
BUILD_COST_DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
BUILD_COST_ADMISSION_INPUTS = {
    "promotion_report",
    "frontier_cells",
    "candidate_frontier",
    "baseline_frontier",
}
INDEX_CONSTRUCTION_DATASETS = {
    "sift10m": ("SIFT10M", 128, "l2"),
    "tti10m": ("TTI10M", 200, "ip"),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def model_control_cells() -> dict[tuple[object, ...], str]:
    cells: dict[tuple[object, ...], str] = {}

    def add(
        sweep: str,
        label: str,
        tool: str,
        size: int,
        mtu: int,
        outstanding: int,
        qps: int,
        cq_mod: int,
        client_numa: int,
        server_numa: int,
    ) -> None:
        cells[
            (
                sweep,
                tool,
                size,
                mtu,
                outstanding,
                qps,
                cq_mod,
                client_numa,
                server_numa,
            )
        ] = label

    for size in (64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384):
        add("payload_latency", f"{size}B", "ib_read_lat", size, 4096, 16, 1, 1, 1, 1)
    for mtu in (1024, 2048, 4096):
        add("mtu_latency", f"MTU {mtu}", "ib_read_lat", 256, mtu, 16, 1, 1, 1, 1)
    for numa in (0, 1):
        add("numa_latency", f"preferred {numa}", "ib_read_lat", 256, 4096, 16, 1, 1, numa, numa)
    for outstanding in (1, 4, 16):
        add("outs_msg_rate", f"outs {outstanding}", "ib_read_bw", 256, 4096, outstanding, 1, 1, 1, 1)
    for qps in (1, 2, 4, 8):
        for cq_mod in (1, 16):
            add(
                "qp_cq_msg_rate",
                f"{qps}QP CQ{cq_mod}",
                "ib_read_bw",
                256,
                4096,
                16,
                qps,
                cq_mod,
                1,
                1,
            )
    return cells


MODEL_CONTROL_CELLS = model_control_cells()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing evidence CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty evidence CSV: {path}")
    return rows


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # Some filesystems do not support fsync on directory descriptors.
            pass
    finally:
        os.close(descriptor)


def _write_gate_atomically(path: Path, report: dict[str, object]) -> None:
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
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_frontier_summary(
    directory: Path, raw_rows: list[dict[str, str]], expected_repeats: int
) -> tuple[Path, str]:
    summary_path = directory / "frontier_summary.csv"
    actual_rows = read_csv(summary_path)
    expected_rows = aggregate_frontier.summarize(raw_rows, expected_repeats)

    def row_key(row: dict[str, object]) -> tuple[str, str, float]:
        return str(row["dataset"]), str(row["method"]), float(row["ef"])

    actual = {row_key(row): row for row in actual_rows}
    expected = {row_key(row): row for row in expected_rows}
    if len(actual) != len(actual_rows) or set(actual) != set(expected):
        raise ValueError("frontier summary mismatch: point matrix differs from raw evidence")
    for key, wanted in expected.items():
        got = actual[key]
        for field, value in wanted.items():
            actual_value = got.get(field, "")
            if isinstance(value, (int, float)):
                try:
                    parsed = float(actual_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"frontier summary mismatch for {key}: invalid {field}"
                    ) from exc
                if not math.isfinite(parsed) or not math.isclose(
                    parsed, float(value), rel_tol=1e-9, abs_tol=1e-9
                ):
                    raise ValueError(
                        f"frontier summary mismatch for {key}: {field}={actual_value} "
                        f"expected {value}"
                    )
            elif str(actual_value) != str(value):
                raise ValueError(
                    f"frontier summary mismatch for {key}: {field}={actual_value!r} "
                    f"expected {value!r}"
                )
    return summary_path, file_sha256(summary_path)


def required(row: dict[str, str], key: str, source: Path) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"{source}: missing {key}")
    return value


def finite(row: dict[str, str], key: str, source: Path) -> float:
    try:
        value = float(required(row, key, source))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def integer(row: dict[str, str], key: str, source: Path) -> int:
    value = finite(row, key, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral {key}")
    return int(value)


def validate_sha(value: str, label: str) -> None:
    if SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")


def validate_campaign_identity(
    *,
    label: str,
    observed_campaign_id: object,
    observed_protocol_fingerprint: object,
    expected_campaign_id: str | None,
    expected_protocol_fingerprint: str | None,
) -> tuple[str, str]:
    campaign_id = str(observed_campaign_id).strip()
    protocol_fingerprint = str(observed_protocol_fingerprint).strip()
    if not campaign_id:
        raise ValueError(f"{label} campaign ID is missing")
    validate_sha(protocol_fingerprint, f"{label} observed protocol fingerprint")
    if (expected_campaign_id is None) != (expected_protocol_fingerprint is None):
        raise ValueError(
            f"expected {label} campaign ID and protocol fingerprint "
            "must be supplied together"
        )
    if expected_campaign_id is None or expected_protocol_fingerprint is None:
        return campaign_id, protocol_fingerprint

    expected_campaign_id = expected_campaign_id.strip()
    if not expected_campaign_id:
        raise ValueError(f"expected {label} campaign ID is empty")
    validate_sha(
        expected_protocol_fingerprint,
        f"expected {label} protocol fingerprint",
    )
    if campaign_id != expected_campaign_id:
        raise ValueError(
            f"{label} campaign ID mismatch: "
            f"{campaign_id!r} != {expected_campaign_id!r}"
        )
    if protocol_fingerprint != expected_protocol_fingerprint:
        raise ValueError(
            f"{label} protocol fingerprint mismatch: "
            f"{protocol_fingerprint} != {expected_protocol_fingerprint}"
        )
    return campaign_id, protocol_fingerprint


def validate_retained_source(
    directory: Path, source_value: str, expected_sha: str, label: str
) -> Path:
    source = Path(source_value)
    if source.is_absolute():
        raise ValueError(f"{label}: source path must be relative to the frontier bundle")
    root = directory.resolve()
    candidate = (directory / source).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}: source path escapes the frontier bundle") from exc
    if not candidate.is_file():
        raise ValueError(f"{label}: missing retained source {source_value}")
    actual_sha = file_sha256(candidate)
    if actual_sha != expected_sha:
        raise ValueError(
            f"{label}: source SHA mismatch for {source_value}: "
            f"{actual_sha} != {expected_sha}"
        )
    return candidate


def load_json_object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"missing {label}: {path}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def validate_frontier_campaign_provenance(
    directory: Path,
    campaigns: dict[tuple[str, str], set[str]],
    *,
    required: bool = False,
) -> dict[str, object]:
    campaign_path = directory / "campaign.json"
    provenance_path = directory / "PROVENANCE.json"
    if not campaign_path.exists() and not provenance_path.exists():
        if required:
            raise ValueError("frontier campaign provenance is required")
        return {
            "mode": "unbound_fixture",
            "source_campaigns_verified": 0,
            "inventory_files_verified": 0,
        }
    if not campaign_path.is_file() or not provenance_path.is_file():
        raise ValueError("frontier campaign and provenance manifests must be retained together")

    try:
        inventory = vldb_evidence_bundle.verify_manifest(
            directory / "SHA256SUMS"
        )
    except ValueError as exc:
        raise ValueError(f"frontier SHA256SUMS validation failed: {exc}") from exc

    campaign = load_json_object(campaign_path, "frontier campaign manifest")
    provenance = load_json_object(provenance_path, "frontier provenance manifest")
    manifest_sha = str(provenance.get("campaign_manifest_sha256", ""))
    validate_sha(manifest_sha, "frontier campaign manifest SHA")
    if file_sha256(campaign_path) != manifest_sha:
        raise ValueError("frontier campaign manifest SHA mismatch")

    if campaign.get("kind") != "composite_frontier_evidence":
        campaign_id = str(campaign.get("campaign_id", ""))
        if not campaign_id:
            raise ValueError("legacy frontier campaign ID is missing")
        observed = set().union(*campaigns.values()) if campaigns else set()
        if observed != {campaign_id}:
            raise ValueError(
                f"legacy frontier campaign binding mismatch: {sorted(observed)}"
            )
        return {
            "mode": "legacy_single_campaign",
            "campaign_id": campaign_id,
            "campaign_manifest_sha256": manifest_sha,
            "source_campaigns_verified": 1,
            "inventory_files_verified": len(inventory),
        }

    if campaign.get("schema_version") == 2:
        manifest_sources = campaign.get("source_campaigns")
        provenance_sources = provenance.get("source_campaigns")
        cell_sources = campaign.get("cell_sources")
        if (
            not isinstance(manifest_sources, list)
            or not isinstance(provenance_sources, list)
            or not isinstance(cell_sources, dict)
        ):
            raise ValueError("invalid cell-scoped frontier source manifests")
        if not all(
            isinstance(cell, str) and isinstance(role, str)
            for cell, role in cell_sources.items()
        ):
            raise ValueError("invalid cell-scoped frontier source map")
        expected_cells = {
            f"{dataset}/{method}" for dataset, method in campaigns
        }
        if set(cell_sources) != expected_cells:
            raise ValueError("cell-scoped frontier source map mismatch")
        manifest_by_role = {
            str(item.get("role", "")): item
            for item in manifest_sources
            if isinstance(item, dict)
        }
        provenance_by_role = {
            str(item.get("role", "")): item
            for item in provenance_sources
            if isinstance(item, dict)
        }
        if (
            len(manifest_by_role) != len(manifest_sources)
            or len(provenance_by_role) != len(provenance_sources)
            or not manifest_by_role
            or set(manifest_by_role) != set(provenance_by_role)
            or set(cell_sources.values()) != set(manifest_by_role)
        ):
            raise ValueError("cell-scoped frontier source role mismatch")

        identities: dict[str, str] = {}
        for role, entry in manifest_by_role.items():
            retained_record = provenance_by_role[role]
            datasets = entry.get("datasets")
            methods = entry.get("methods")
            if (
                not isinstance(datasets, list)
                or not datasets
                or not all(isinstance(value, str) and value for value in datasets)
                or len(set(datasets)) != len(datasets)
                or not isinstance(methods, list)
                or not methods
                or not all(isinstance(value, str) and value for value in methods)
                or len(set(methods)) != len(methods)
                or retained_record.get("datasets") != datasets
                or retained_record.get("methods") != methods
            ):
                raise ValueError(f"cell-scoped frontier dimensions mismatch for {role}")
            role_cells = {
                f"{dataset}/{method}" for dataset in datasets for method in methods
            }
            mapped_cells = {
                cell for cell, mapped_role in cell_sources.items() if mapped_role == role
            }
            if role_cells != mapped_cells:
                raise ValueError(f"cell-scoped frontier cell set mismatch for {role}")

            campaign_id = str(entry.get("campaign_id", ""))
            fingerprint = str(entry.get("protocol_fingerprint", ""))
            retained = str(entry.get("manifest", ""))
            retained_sha = str(entry.get("manifest_sha256", ""))
            if not campaign_id:
                raise ValueError(f"cell-scoped frontier campaign ID missing for {role}")
            validate_sha(
                fingerprint, f"cell-scoped frontier {role} protocol fingerprint"
            )
            validate_sha(retained_sha, f"cell-scoped frontier {role} manifest SHA")
            expected_provenance = {
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
                "retained": retained,
                "sha256": retained_sha,
            }
            for field, value in expected_provenance.items():
                if str(retained_record.get(field, "")) != value:
                    raise ValueError(
                        f"cell-scoped frontier provenance mismatch for {role}/{field}"
                    )
            retained_path = validate_retained_source(
                directory,
                retained,
                retained_sha,
                f"cell-scoped frontier {role} campaign",
            )
            retained_manifest = load_json_object(
                retained_path, f"cell-scoped frontier {role} retained campaign"
            )
            if (
                str(retained_manifest.get("campaign_id", "")) != campaign_id
                or str(retained_manifest.get("protocol_fingerprint", ""))
                != fingerprint
            ):
                raise ValueError(
                    f"cell-scoped frontier retained identity mismatch for {role}"
                )
            observed_ids = set().union(
                *(
                    campaigns[(dataset, method)]
                    for dataset in datasets
                    for method in methods
                )
            )
            if observed_ids != {campaign_id}:
                raise ValueError(
                    f"cell-scoped frontier row binding mismatch for {role}: "
                    f"{sorted(observed_ids)}"
                )
            identities[role] = campaign_id

        composite_id = str(campaign.get("campaign_id", ""))
        if not composite_id:
            raise ValueError("cell-scoped composite campaign ID is missing")
        return {
            "mode": "composite_cells",
            "campaign_id": composite_id,
            "campaign_manifest_sha256": manifest_sha,
            "source_campaigns_verified": len(identities),
            "campaign_ids": identities,
            "inventory_files_verified": len(inventory),
        }

    expected_method_sources = {
        "SHINE": "shine_slabwalk",
        "SlabWalk": "shine_slabwalk",
        "d-HNSW": "dhnsw",
    }
    if campaign.get("method_sources") != expected_method_sources:
        raise ValueError("composite frontier method-to-source map mismatch")
    manifest_sources = campaign.get("source_campaigns")
    provenance_sources = provenance.get("source_campaigns")
    if not isinstance(manifest_sources, list) or not isinstance(provenance_sources, list):
        raise ValueError("invalid composite frontier source-campaign lists")
    manifest_by_role = {
        str(item.get("role", "")): item
        for item in manifest_sources
        if isinstance(item, dict)
    }
    provenance_by_role = {
        str(item.get("role", "")): item
        for item in provenance_sources
        if isinstance(item, dict)
    }
    expected_roles = {
        "shine_slabwalk": ("SHINE", "SlabWalk"),
        "dhnsw": ("d-HNSW",),
    }
    if set(manifest_by_role) != set(expected_roles) or set(provenance_by_role) != set(
        expected_roles
    ):
        raise ValueError("composite frontier source-campaign role mismatch")

    identities: dict[str, str] = {}
    for role, methods in expected_roles.items():
        entry = manifest_by_role[role]
        retained_record = provenance_by_role[role]
        if tuple(entry.get("methods", [])) != methods or tuple(
            retained_record.get("methods", [])
        ) != methods:
            raise ValueError(f"composite frontier methods mismatch for {role}")
        campaign_id = str(entry.get("campaign_id", ""))
        fingerprint = str(entry.get("protocol_fingerprint", ""))
        retained = str(entry.get("manifest", ""))
        retained_sha = str(entry.get("manifest_sha256", ""))
        if not campaign_id:
            raise ValueError(f"composite frontier campaign ID missing for {role}")
        validate_sha(fingerprint, f"composite frontier {role} protocol fingerprint")
        validate_sha(retained_sha, f"composite frontier {role} manifest SHA")
        expected_provenance = {
            "campaign_id": campaign_id,
            "protocol_fingerprint": fingerprint,
            "retained": retained,
            "sha256": retained_sha,
        }
        for field, value in expected_provenance.items():
            if str(retained_record.get(field, "")) != value:
                raise ValueError(
                    f"composite frontier provenance mismatch for {role}/{field}"
                )
        retained_path = validate_retained_source(
            directory,
            retained,
            retained_sha,
            f"composite frontier {role} campaign",
        )
        retained_manifest = load_json_object(
            retained_path, f"composite frontier {role} retained campaign"
        )
        if (
            str(retained_manifest.get("campaign_id", "")) != campaign_id
            or str(retained_manifest.get("protocol_fingerprint", "")) != fingerprint
        ):
            raise ValueError(f"composite frontier retained identity mismatch for {role}")
        observed_ids = set().union(
            *(
                values
                for (dataset, method), values in campaigns.items()
                if method in methods
            )
        )
        if observed_ids != {campaign_id}:
            raise ValueError(
                f"composite frontier row binding mismatch for {role}: "
                f"{sorted(observed_ids)}"
            )
        identities[role] = campaign_id

    return {
        "mode": "composite",
        "campaign_manifest_sha256": manifest_sha,
        "source_campaigns_verified": len(identities),
        "campaign_ids": identities,
        "inventory_files_verified": len(inventory),
    }


def validate_index_construction(
    directory: Path, expected_slabwalk_sha: str
) -> dict[str, object]:
    """Validate topology-preserving 10M HNSW construction and conversion."""

    validate_sha(expected_slabwalk_sha, "expected SlabWalk SHA")
    reports: dict[str, dict[str, object]] = {}

    def load_object(path: Path) -> dict[str, object]:
        if not path.is_file():
            raise ValueError(f"missing index-construction evidence: {path}")
        try:
            obj = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid index-construction JSON: {path}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"index-construction JSON must be an object: {path}")
        return obj

    def exact_int(obj: dict[str, object], key: str, expected: int, path: Path) -> None:
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise ValueError(
                f"{path}: invalid {key}={value!r}; expected {expected}"
            )

    def positive_number(obj: dict[str, object], key: str, path: Path) -> float:
        value = obj.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: invalid {key}")
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError(f"{path}: non-positive {key}")
        return result

    for slug, (dataset, dim, space) in INDEX_CONSTRUCTION_DATASETS.items():
        root = directory / slug
        for name in ("build.rc", "conversion.rc", "pipeline.rc"):
            path = root / name
            if not path.is_file() or path.read_text().strip() != "0":
                raise ValueError(f"{dataset}: nonzero or missing {name}")

        build_path = root / "build.json"
        conversion_path = root / "conversion.json"
        campaign_path = root / "campaign.json"
        build = load_object(build_path)
        conversion = load_object(conversion_path)
        campaign = load_object(campaign_path)

        if build.get("status") != "complete":
            raise ValueError(f"{dataset}: HNSW build is not complete")
        for obj, path in (
            (build, build_path),
            (conversion, conversion_path),
            (campaign, campaign_path),
        ):
            exact_int(obj, "count", 10_000_000, path)
            exact_int(obj, "dim", dim, path)
            exact_int(obj, "m", 16, path)
            exact_int(obj, "ef_construction", 100, path)
        exact_int(build, "completed", 10_000_000, build_path)
        exact_int(build, "random_seed", 47, build_path)
        exact_int(campaign, "random_seed", 47, campaign_path)
        if build.get("space") != space or campaign.get("space") != space:
            raise ValueError(f"{dataset}: distance-space mismatch")
        if build.get("hnswlib_version") != "0.8.0":
            raise ValueError(f"{dataset}: unsupported hnswlib version")
        if build.get("label_policy") != "external_label_equals_source_row_id":
            raise ValueError(f"{dataset}: label policy is not row-ID preserving")

        if campaign.get("frozen_graphbeyond_binary_sha256") != expected_slabwalk_sha:
            raise ValueError(f"{dataset}: frozen binary SHA mismatch")
        for key in (
            "builder_sha256",
            "converter_sha256",
            "converter_source_sha256",
        ):
            value = campaign.get(key)
            if not isinstance(value, str):
                raise ValueError(f"{dataset}: missing {key}")
            validate_sha(value, f"{dataset} {key}")

        if conversion.get("converter") != "convert_hnswlib_dump-v1":
            raise ValueError(f"{dataset}: converter identity mismatch")
        if conversion.get("source_format") != "hnswlib-0.8.0-native-64le":
            raise ValueError(f"{dataset}: source format mismatch")
        if conversion.get("format") != "graphbeyond-hnsw-single-mn-v1":
            raise ValueError(f"{dataset}: output format mismatch")
        if conversion.get("graph_preserved") is not True:
            raise ValueError(f"{dataset}: conversion is not graph-preserved")
        if conversion.get("post_write_validation") != (
            "full_graph_payload_and_pointers"
        ):
            raise ValueError(f"{dataset}: full post-write validation is missing")
        if conversion.get("deleted_nodes_accepted") is not False:
            raise ValueError(f"{dataset}: deleted-node conversion is not allowed")
        exact_int(conversion, "max_m0", 32, conversion_path)

        source_sha = build.get("source_sha256")
        hnsw_sha = build.get("output_sha256")
        conversion_source_sha = conversion.get("source_sha256")
        dump_sha = conversion.get("output_sha256")
        for value, label in (
            (source_sha, "source SHA"),
            (hnsw_sha, "hnswlib SHA"),
            (conversion_source_sha, "converter source SHA"),
            (dump_sha, "dump SHA"),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{dataset}: missing {label}")
            validate_sha(value, f"{dataset} {label}")
        if hnsw_sha != conversion_source_sha:
            raise ValueError(f"{dataset}: converter source SHA breaks builder link")

        source_bytes = int(positive_number(build, "source_bytes", build_path))
        hnsw_bytes = int(positive_number(build, "output_bytes", build_path))
        if conversion.get("source_bytes") != hnsw_bytes:
            raise ValueError(f"{dataset}: converter source byte count mismatch")
        dump_bytes = int(
            positive_number(conversion, "output_bytes", conversion_path)
        )
        wall_seconds = positive_number(build, "wall_seconds", build_path)
        peak_rss_bytes = int(
            positive_number(build, "peak_rss_bytes", build_path)
        )
        positive_number(build, "threads", build_path)

        reports[dataset] = {
            "source_sha256": source_sha,
            "hnswlib_sha256": hnsw_sha,
            "dump_sha256": dump_sha,
            "source_bytes": source_bytes,
            "hnswlib_bytes": hnsw_bytes,
            "dump_bytes": dump_bytes,
            "build_wall_seconds": wall_seconds,
            "build_peak_rss_bytes": peak_rss_bytes,
            "build_manifest_sha256": file_sha256(build_path),
            "conversion_manifest_sha256": file_sha256(conversion_path),
            "campaign_manifest_sha256": file_sha256(campaign_path),
        }

    return {
        "measured_cells": len(reports),
        "datasets": reports,
        "directory": str(directory),
    }


def expect_repeat_set(
    rows: Iterable[dict[str, str]], key: str, expected_repeats: int, label: str
) -> None:
    values = sorted(integer(row, key, Path(label)) for row in rows)
    expected = list(range(expected_repeats))
    if values != expected:
        raise ValueError(f"{label}: repeat set {values} != {expected}")


def validate_recomputed_csv(
    path: Path,
    expected_rows: list[dict[str, object]],
    key_fields: tuple[str, ...],
    label: str,
    *,
    path_fields: tuple[str, ...] = (),
    path_root: Path | None = None,
) -> None:
    actual_rows = read_csv(path)

    if path_fields and path_root is None:
        raise ValueError(f"{label}: path_root is required for path-valued fields")

    def path_candidates(value: object) -> set[Path]:
        candidate = Path(str(value))
        if candidate.is_absolute():
            return {candidate.resolve()}
        if ".." in candidate.parts:
            return set()
        assert path_root is not None
        root = path_root.resolve()
        bases = (Path.cwd().resolve(), root, *root.parents)
        return {(base / candidate).resolve() for base in bases}

    def equivalent_path_lists(actual_value: object, expected_value: object) -> bool:
        actual_paths = str(actual_value).split(";")
        expected_paths = str(expected_value).split(";")
        return len(actual_paths) == len(expected_paths) and all(
            path_candidates(current) & path_candidates(wanted)
            for current, wanted in zip(actual_paths, expected_paths)
        )

    def key(row: dict[str, object]) -> tuple[str, ...]:
        return tuple(str(row[field]) for field in key_fields)

    actual = {key(row): row for row in actual_rows}
    expected = {key(row): row for row in expected_rows}
    if (
        len(actual) != len(actual_rows)
        or len(expected) != len(expected_rows)
        or set(actual) != set(expected)
    ):
        raise ValueError(f"{label} mismatch: row matrix differs from raw evidence")
    for row_key, wanted in expected.items():
        got = actual[row_key]
        if set(got) != set(wanted):
            raise ValueError(f"{label} mismatch for {row_key}: columns differ")
        for field, value in wanted.items():
            actual_value = got.get(field, "")
            if field in path_fields:
                if not equivalent_path_lists(actual_value, value):
                    raise ValueError(
                        f"{label} mismatch for {row_key}: {field}={actual_value!r} "
                        f"does not identify {value!r}"
                    )
            elif isinstance(value, (int, float)):
                try:
                    parsed = float(actual_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{label} mismatch for {row_key}: invalid {field}"
                    ) from exc
                if not math.isfinite(parsed) or not math.isclose(
                    parsed, float(value), rel_tol=1e-9, abs_tol=1e-9
                ):
                    raise ValueError(
                        f"{label} mismatch for {row_key}: {field}={actual_value} "
                        f"expected {value}"
                    )
            elif str(actual_value) != str(value):
                raise ValueError(
                    f"{label} mismatch for {row_key}: {field}={actual_value!r} "
                    f"expected {value!r}"
                )


def validate_build_cost(
    directory: Path,
    expected_sha: str,
    expected_source_tree_sha: str | None = None,
    expected_admission_gate_sha: str | None = None,
    expected_admission_scope: str | None = None,
) -> dict[str, object]:
    """Recompute the complete derived-structure build matrix from raw runs."""
    campaign_path = directory / "raw" / "campaign.json"
    if not campaign_path.is_file():
        raise ValueError("missing build-cost campaign manifest")
    try:
        campaign = json.loads(campaign_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid build-cost campaign manifest") from exc
    if campaign.get("kind") != "vldb_build_cost_bundle_v1":
        raise ValueError("build-cost evidence must be an assembled final bundle")
    campaign_sha = required(campaign, "binary_sha256", campaign_path)
    validate_sha(campaign_sha, "build-cost binary SHA")
    if campaign_sha != expected_sha:
        raise ValueError(
            f"build-cost binary SHA {campaign_sha} does not match {expected_sha}"
        )
    source_tree_sha = campaign.get("source_tree_sha256")
    if expected_source_tree_sha is not None:
        validate_sha(expected_source_tree_sha, "expected build-cost source tree SHA")
        if source_tree_sha != expected_source_tree_sha:
            raise ValueError("build-cost source tree SHA does not match expected source")
    if source_tree_sha is not None:
        source_tree_sha = str(source_tree_sha)
        validate_sha(source_tree_sha, "build-cost source tree SHA")
    if (expected_admission_gate_sha is None) != (expected_admission_scope is None):
        raise ValueError("expected build-cost admission SHA and scope must be set together")
    if expected_admission_gate_sha is not None:
        validate_sha(
            expected_admission_gate_sha,
            "expected build-cost admission gate SHA",
        )
    script_sha = required(campaign, "script_sha256", campaign_path)
    validate_sha(script_sha, "build-cost script SHA")
    summary_script_sha = required(campaign, "summary_script_sha256", campaign_path)
    validate_sha(summary_script_sha, "build-cost summary script SHA")
    if campaign.get("script_role") != "bundle_assembler":
        raise ValueError("build-cost script role must identify the bundle assembler")
    if int(campaign.get("repeats", 0)) != 5:
        raise ValueError("build-cost campaign must use five repeats")
    if list(campaign.get("datasets", [])) != list(BUILD_COST_DATASETS):
        raise ValueError("build-cost campaign dataset matrix mismatch")
    if int(campaign.get("builder_threads", 0)) != 20:
        raise ValueError("build-cost campaign must record 20 builder threads")
    if (
        int(campaign.get("query_threads", 0)) != 1
        or int(campaign.get("query_coroutines", 0)) != 1
        or campaign.get("layout") != "packed_fixed"
        or campaign.get("measurement") != "derived_build_only"
    ):
        raise ValueError("invalid build-cost campaign protocol")

    provenance_value = required(campaign, "provenance_path", campaign_path)
    provenance_sha = required(campaign, "provenance_sha256", campaign_path)
    validate_sha(provenance_sha, "build-cost provenance SHA")
    provenance_path = validate_retained_source(
        directory,
        provenance_value,
        provenance_sha,
        "build-cost provenance",
    )
    try:
        provenance = json.loads(provenance_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid build-cost provenance manifest") from exc
    if provenance.get("kind") != "vldb_build_cost_provenance_v1":
        raise ValueError("invalid build-cost provenance kind")
    assembler = provenance.get("assembler", {})
    if not isinstance(assembler, dict) or assembler.get("sha256") != script_sha:
        raise ValueError("build-cost assembler SHA does not match campaign")
    summarizer = provenance.get("summarizer", {})
    if (
        not isinstance(summarizer, dict)
        or summarizer.get("sha256") != summary_script_sha
    ):
        raise ValueError("build-cost summarizer SHA does not match campaign")

    campaign_admission = campaign.get("admission")
    provenance_admission = provenance.get("admission")
    admission_gate_sha: str | None = None
    admission_inputs_verified = 0
    if campaign_admission is None and provenance_admission is None:
        if expected_admission_gate_sha is not None:
            raise ValueError("build-cost bundle lacks required construction admission")
    else:
        if not isinstance(campaign_admission, dict) or not isinstance(
            provenance_admission, dict
        ):
            raise ValueError("build-cost admission provenance is incomplete")
        for record, label in (
            (campaign_admission, "campaign"),
            (provenance_admission, "provenance"),
        ):
            if (
                record.get("kind") != "vldb_construction_candidate_gate_v1"
                or record.get("scope") != "construction_measurements_only"
                or record.get("construction_ready") is not True
                or record.get("general_promotion_ready") is not False
            ):
                raise ValueError(f"invalid build-cost {label} admission contract")
        admission_gate_sha = str(campaign_admission.get("gate_sha256", ""))
        validate_sha(admission_gate_sha, "build-cost admission gate SHA")
        if provenance_admission.get("gate_sha256") != admission_gate_sha:
            raise ValueError("build-cost admission gate provenance mismatch")
        if (
            expected_admission_gate_sha is not None
            and admission_gate_sha != expected_admission_gate_sha
        ):
            raise ValueError("build-cost admission gate SHA does not match expected gate")
        if (
            expected_admission_scope is not None
            and campaign_admission.get("scope") != expected_admission_scope
        ):
            raise ValueError("build-cost admission scope does not match expected scope")
        retained_gate_value = str(campaign_admission.get("retained_gate", ""))
        if provenance_admission.get("retained_gate") != retained_gate_value:
            raise ValueError("build-cost retained admission gate mismatch")
        retained_gate = validate_retained_source(
            directory,
            retained_gate_value,
            admission_gate_sha,
            "build-cost construction gate",
        )
        try:
            retained_gate_payload = json.loads(retained_gate.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid retained build-cost construction gate") from exc
        if (
            not isinstance(retained_gate_payload, dict)
            or retained_gate_payload.get("kind") != campaign_admission["kind"]
            or retained_gate_payload.get("scope") != campaign_admission["scope"]
            or retained_gate_payload.get("construction_ready") is not True
            or retained_gate_payload.get("general_promotion_ready") is not False
            or retained_gate_payload.get("failures") != []
        ):
            raise ValueError("retained build-cost construction gate contract mismatch")
        campaign_inputs = campaign_admission.get("inputs")
        provenance_inputs = provenance_admission.get("inputs")
        gate_inputs = retained_gate_payload.get("inputs")
        if (
            not isinstance(campaign_inputs, dict)
            or not isinstance(provenance_inputs, dict)
            or not isinstance(gate_inputs, dict)
            or set(campaign_inputs) != BUILD_COST_ADMISSION_INPUTS
            or set(provenance_inputs) != BUILD_COST_ADMISSION_INPUTS
            or set(gate_inputs) != BUILD_COST_ADMISSION_INPUTS
        ):
            raise ValueError("build-cost admission input matrix mismatch")
        for name in sorted(BUILD_COST_ADMISSION_INPUTS):
            campaign_input = campaign_inputs[name]
            provenance_input = provenance_inputs[name]
            gate_input = gate_inputs[name]
            if not all(
                isinstance(record, dict)
                for record in (campaign_input, provenance_input, gate_input)
            ):
                raise ValueError(f"invalid retained build-cost admission input: {name}")
            retained_value = str(campaign_input.get("retained", ""))
            input_sha = str(campaign_input.get("sha256", ""))
            validate_sha(input_sha, f"build-cost {name} admission SHA")
            if (
                provenance_input.get("retained") != retained_value
                or provenance_input.get("sha256") != input_sha
                or gate_input.get("sha256") != input_sha
                or str(provenance_input.get("source", ""))
                != str(Path(str(gate_input.get("path", ""))).resolve())
            ):
                raise ValueError(f"build-cost {name} admission provenance mismatch")
            validate_retained_source(
                directory,
                retained_value,
                input_sha,
                f"build-cost {name} admission input",
            )
            admission_inputs_verified += 1

    campaign_sources = campaign.get("source_campaigns", [])
    provenance_sources = provenance.get("source_campaigns", [])
    if not isinstance(campaign_sources, list) or not isinstance(provenance_sources, list):
        raise ValueError("invalid build-cost source-campaign provenance")
    if len(campaign_sources) != len(BUILD_COST_DATASETS) or len(provenance_sources) != len(BUILD_COST_DATASETS):
        raise ValueError("build-cost source-campaign matrix mismatch")
    campaign_sources_by_dataset = {
        str(item.get("dataset", "")): item
        for item in campaign_sources
        if isinstance(item, dict)
    }
    provenance_sources_by_dataset = {
        str(item.get("dataset", "")): item
        for item in provenance_sources
        if isinstance(item, dict)
    }
    if set(campaign_sources_by_dataset) != set(BUILD_COST_DATASETS) or set(
        provenance_sources_by_dataset
    ) != set(BUILD_COST_DATASETS):
        raise ValueError("build-cost source-campaign datasets mismatch")
    for dataset in BUILD_COST_DATASETS:
        source = campaign_sources_by_dataset[dataset]
        provenance_source = provenance_sources_by_dataset[dataset]
        for key in (
            "campaign_id",
            "retained_manifest",
            "retained_manifest_sha256",
            "runner_script_sha256",
        ):
            if str(source.get(key, "")) != str(provenance_source.get(key, "")):
                raise ValueError(f"{dataset}: source campaign provenance mismatch for {key}")
        if admission_gate_sha is not None:
            if (
                source.get("admission_gate_sha256") != admission_gate_sha
                or provenance_source.get("admission_gate_sha256")
                != admission_gate_sha
                or source.get("admission_scope")
                != campaign_admission.get("scope")
                or provenance_source.get("admission_scope")
                != campaign_admission.get("scope")
            ):
                raise ValueError(f"{dataset}: source admission provenance mismatch")
        if source_tree_sha is not None:
            if (
                str(source.get("source_tree_sha256", "")) != source_tree_sha
                or str(provenance_source.get("source_tree_sha256", ""))
                != source_tree_sha
            ):
                raise ValueError(f"{dataset}: source tree provenance mismatch")
        manifest_sha = str(source.get("retained_manifest_sha256", ""))
        runner_sha = str(source.get("runner_script_sha256", ""))
        validate_sha(manifest_sha, f"{dataset} source campaign manifest SHA")
        validate_sha(runner_sha, f"{dataset} source runner SHA")
        retained_manifest = validate_retained_source(
            directory,
            str(source.get("retained_manifest", "")),
            manifest_sha,
            f"{dataset} source campaign",
        )
        try:
            source_manifest = json.loads(retained_manifest.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{dataset}: invalid retained source campaign") from exc
        if (
            str(source_manifest.get("campaign_id", "")) != str(source.get("campaign_id", ""))
            or str(source_manifest.get("binary_sha256", "")) != expected_sha
            or str(source_manifest.get("script_sha256", "")) != runner_sha
            or dataset not in list(source_manifest.get("datasets", []))
            or int(source_manifest.get("repeats", 0)) != 5
            or int(source_manifest.get("builder_threads", 0)) != 20
            or int(source_manifest.get("query_threads", 0)) != 1
            or int(source_manifest.get("query_coroutines", 0)) != 1
            or source_manifest.get("layout") != "packed_fixed"
            or source_manifest.get("measurement") != "derived_build_only"
        ):
            raise ValueError(f"{dataset}: retained source campaign protocol mismatch")
        if source_tree_sha is not None:
            source_identity = source_manifest.get("source")
            if (
                not isinstance(source_identity, dict)
                or source_identity.get("tree_sha256") != source_tree_sha
                or int(source_identity.get("file_count", 0)) <= 0
                or source_identity.get("layout")
                not in {"repository", "graphbeyond_project"}
                or not isinstance(source_identity.get("tree_scope"), list)
                or not source_identity["tree_scope"]
            ):
                raise ValueError(f"{dataset}: retained source tree identity mismatch")
        if admission_gate_sha is not None:
            source_admission = source_manifest.get("admission")
            if (
                not isinstance(source_admission, dict)
                or source_admission.get("kind") != campaign_admission.get("kind")
                or source_admission.get("sha256") != admission_gate_sha
                or source_admission.get("scope") != campaign_admission.get("scope")
                or source_admission.get("construction_ready") is not True
                or source_admission.get("general_promotion_ready") is not False
                or str(Path(str(source_admission.get("path", ""))).resolve())
                != str(provenance_admission.get("source_gate", ""))
            ):
                raise ValueError(f"{dataset}: retained source admission mismatch")

    retained_run_records = provenance.get("retained_runs", [])
    if not isinstance(retained_run_records, list) or len(retained_run_records) != 45:
        raise ValueError("build-cost provenance must retain 45 run files")
    retained_run_keys: set[tuple[str, int, str]] = set()
    expected_kinds = {"json", "err", "mn_err"}
    for item in retained_run_records:
        if not isinstance(item, dict):
            raise ValueError("invalid build-cost retained-run record")
        dataset = str(item.get("dataset", ""))
        repeat = int(item.get("repeat", -1))
        kind = str(item.get("kind", ""))
        key = (dataset, repeat, kind)
        if (
            dataset not in BUILD_COST_DATASETS
            or repeat not in range(5)
            or kind not in expected_kinds
            or key in retained_run_keys
        ):
            raise ValueError(f"invalid build-cost retained-run key: {key}")
        retained_run_keys.add(key)
        retained_sha = str(item.get("sha256", ""))
        validate_sha(retained_sha, f"build-cost retained {dataset}/{repeat}/{kind} SHA")
        validate_retained_source(
            directory,
            str(item.get("retained", "")),
            retained_sha,
            f"build-cost retained {dataset}/{repeat}/{kind}",
        )
    expected_run_keys = {
        (dataset, repeat, kind)
        for dataset in BUILD_COST_DATASETS
        for repeat in range(5)
        for kind in expected_kinds
    }
    if retained_run_keys != expected_run_keys:
        raise ValueError("build-cost retained-run matrix mismatch")

    raw_dir = directory / "raw"
    try:
        parsed = build_cost_summary.collect_runs(raw_dir)
        grouped = build_cost_summary.validate_matrix(
            parsed, list(BUILD_COST_DATASETS), 5
        )
    except ValueError as exc:
        raise ValueError(f"invalid build-cost raw evidence: {exc}") from exc
    expected_codes = {
        "SIFT1M": ("sq8", 8),
        "DEEP1M": ("sq8", 8),
        "GIST1M": ("RaBitQ-2", 2),
    }
    retained: dict[str, str] = {}
    for dataset, runs in grouped.items():
        for run in runs:
            if run.n_vectors != 1_000_000:
                raise ValueError(f"{dataset}: build-cost vector count drift")
            if (run.code_name, run.code_bits_per_dimension) != expected_codes[dataset]:
                raise ValueError(f"{dataset}: build-cost compact-code drift")
            if run.record_mode != "fixed" or not math.isclose(
                run.materialization_fraction, 1.0
            ):
                raise ValueError(f"{dataset}: build-cost materialization drift")
            try:
                raw_obj = json.loads(run.json_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"{run.json_path}: invalid raw JSON") from exc
            if (
                int(raw_obj.get("num_queries", 0)) != 10000
                or int(raw_obj.get("queries", {}).get("processed", 0)) != 10000
            ):
                raise ValueError(f"{run.json_path}: incomplete post-build query check")
            for source in (run.json_path, run.err_path):
                retained[source.relative_to(directory).as_posix()] = file_sha256(source)

    run_rows, summary_rows, stage_rows = build_cost_summary.summarize(grouped)
    validate_recomputed_csv(
        directory / "runs.csv",
        run_rows,
        ("dataset", "repeat"),
        "build-cost runs",
        path_fields=("raw_json", "raw_err"),
        path_root=directory,
    )
    validate_recomputed_csv(
        directory / "summary.csv",
        summary_rows,
        ("dataset",),
        "build-cost summary",
        path_fields=("raw_json_paths", "raw_err_paths"),
        path_root=directory,
    )
    validate_recomputed_csv(
        directory / "stage_breakdown.csv",
        stage_rows,
        ("dataset", "stage"),
        "build-cost stage breakdown",
        path_fields=("raw_json_paths",),
        path_root=directory,
    )
    report = {
        "measured_rows": len(parsed),
        "measured_datasets": len(grouped),
        "repeats_per_dataset": 5,
        "builder_threads": 20,
        "campaign_sha256": file_sha256(campaign_path),
        "runs_sha256": file_sha256(directory / "runs.csv"),
        "summary_sha256": file_sha256(directory / "summary.csv"),
        "stage_breakdown_sha256": file_sha256(directory / "stage_breakdown.csv"),
        "retained_raw_files_verified": len(retained),
        "retained_raw_sha256": retained,
        "source_campaigns_verified": len(campaign_sources_by_dataset),
        "provenance_run_files_verified": len(retained_run_keys),
        "provenance_sha256": provenance_sha,
        "summary_script_sha256": summary_script_sha,
        "excluded_campaigns_retained": len(provenance.get("excluded_campaigns", [])),
    }
    if source_tree_sha is not None:
        report["source_tree_sha256"] = source_tree_sha
    if admission_gate_sha is not None:
        report["admission_gate_sha256"] = admission_gate_sha
        report["admission_scope"] = campaign_admission["scope"]
        report["admission_inputs_verified"] = admission_inputs_verified
    return report


def validate_lifecycle_controls(directory: Path) -> dict[str, object]:
    """Validate retained offline-refresh and compact-code boundary controls."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("missing lifecycle controls manifest")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid lifecycle controls manifest") from exc
    if (
        manifest.get("kind") != "vldb_lifecycle_controls_v1"
        or int(manifest.get("refresh_cells", 0)) != 4
        or int(manifest.get("tti_cells", 0)) != 8
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("invalid lifecycle controls manifest schema")

    inventory: dict[str, dict[str, object]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ValueError("invalid lifecycle controls inventory item")
        rel = str(item.get("path", ""))
        if not rel or rel in inventory:
            raise ValueError("duplicate or empty lifecycle inventory path")
        candidate = validate_retained_source(
            directory,
            rel,
            str(item.get("sha256", "")),
            "lifecycle inventory",
        )
        if candidate.stat().st_size != int(item.get("size_bytes", -1)):
            raise ValueError(f"lifecycle inventory size mismatch for {rel}")
        inventory[rel] = item
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_files != set(inventory):
        raise ValueError("lifecycle inventory file matrix mismatch")

    refresh_path = directory / "refresh.csv"
    refresh_rows = read_csv(refresh_path)
    refresh_by_batch = {integer(row, "batch_inserts", refresh_path): row for row in refresh_rows}
    if (
        set(refresh_by_batch) != set(lifecycle_controls.REFRESH_BATCHES)
        or len(refresh_rows) != 4
    ):
        raise ValueError("lifecycle refresh matrix mismatch")
    retained_sources = 0
    for batch, row in refresh_by_batch.items():
        source = validate_retained_source(
            directory,
            required(row, "source", refresh_path),
            required(row, "source_sha256", refresh_path),
            f"lifecycle refresh {batch}",
        )
        parsed = lifecycle_controls.parse_refresh(source)
        for field in (
            "batch_inserts", "touched_blocks", "write_amp_blocks_per_insert",
            "diff_read_frac", "diff_read_mb", "full_index_mb", "recall",
        ):
            lifecycle_controls.close_float(
                parsed[field], row.get(field, ""), f"refresh/{batch}/{field}"
            )
        if row.get("byte_identical") != "PASS":
            raise ValueError(f"lifecycle refresh {batch} did not pass byte identity")
        retained_sources += 1

    tti_path = directory / "tti.csv"
    tti_rows = read_csv(tti_path)
    tti_by_config = {required(row, "config", tti_path): row for row in tti_rows}
    if (
        set(tti_by_config) != set(lifecycle_controls.TTI_CONFIGS)
        or len(tti_rows) != 8
    ):
        raise ValueError("lifecycle TTI matrix mismatch")
    for config, row in tti_by_config.items():
        source = validate_retained_source(
            directory,
            required(row, "source", tti_path),
            required(row, "source_sha256", tti_path),
            f"lifecycle TTI {config}",
        )
        parsed = lifecycle_controls.parse_tti(source)
        for field in ("threads", "qps", "recall", "posts_per_query", "mb_per_query"):
            lifecycle_controls.close_float(
                parsed[field], row.get(field, ""), f"tti/{config}/{field}"
            )
        if integer(row, "ef", tti_path) != 300:
            raise ValueError(f"lifecycle TTI {config} must use ef=300")
        retained_sources += 1

    return {
        "refresh_cells": len(refresh_rows),
        "tti_cells": len(tti_rows),
        "retained_sources_verified": retained_sources,
        "manifest_sha256": file_sha256(manifest_path),
        "refresh_sha256": file_sha256(refresh_path),
        "tti_sha256": file_sha256(tti_path),
    }


def validate_frontier(
    directory: Path,
    expected_sha: str,
    query_pool_links: dict[str, dict[str, str]],
    expected_repeats: int = 5,
    min_points: int = 5,
    *,
    require_campaign_provenance: bool = False,
) -> dict[str, object]:
    source = directory / "frontier_repeated_raw.csv"
    rows = read_csv(source)
    grouped: dict[tuple[str, str, float], list[dict[str, str]]] = defaultdict(list)
    campaigns: dict[tuple[str, str], set[str]] = defaultdict(set)
    binaries: dict[tuple[str, str], set[str]] = defaultdict(set)
    query_counts: dict[str, set[int]] = defaultdict(set)
    metrics: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        dataset = required(row, "dataset", source)
        method = required(row, "method", source)
        if dataset not in FRONTIER_DATASETS or method not in FRONTIER_METHODS:
            raise ValueError(f"unexpected frontier row: {dataset}/{method}")
        ef = finite(row, "ef", source)
        run_id = required(row, "run_id", source)
        recall = finite(row, "recall", source)
        qps = finite(row, "qps", source)
        if not 0 <= recall <= 1 or qps <= 0:
            raise ValueError(f"invalid frontier result: {dataset}/{method}/ef={ef}")
        if integer(row, "threads", source) != 10:
            raise ValueError(f"{dataset}/{method}/ef={ef}: frontier threads must be 10")
        if method != "d-HNSW" and integer(row, "query_contexts", source) != 10:
            raise ValueError(f"{dataset}/{method}/ef={ef}: query contexts must be 10")
        if integer(row, "top_k", source) != 10:
            raise ValueError(f"{dataset}/{method}/ef={ef}: top-k must be 10")
        if required(row, "measurement_mode", source) != "fixed_query_pool":
            raise ValueError(f"{dataset}/{method}/ef={ef}: not a fixed query pool")
        processed = integer(row, "processed_queries", source)
        expected = integer(row, "expected_queries", source)
        failed = integer(row, "failed_queries", source)
        if expected != 10000:
            raise ValueError(
                f"{dataset}/{method}/ef={ef}: expected exactly 10000 "
                f"fingerprinted queries, found {expected}"
            )
        if processed != expected or failed != 0:
            raise ValueError(
                f"{dataset}/{method}/ef={ef}: incomplete query pool "
                f"processed={processed} expected={expected} failed={failed}"
            )
        if method == "d-HNSW":
            mean_latency = finite(row, "mean_latency_us", source)
            if mean_latency <= 0:
                raise ValueError(f"{dataset}/{method}/ef={ef}: mean_latency_us must be positive")
            for key in ("network_us", "compute_us", "meta_us", "deserialize_us"):
                if finite(row, key, source) < 0:
                    raise ValueError(f"{dataset}/{method}/ef={ef}: invalid {key}")
        else:
            p50 = finite(row, "p50_us", source)
            p95 = finite(row, "p95_us", source)
            p99 = finite(row, "p99_us", source)
            if not 0 <= p50 <= p95 <= p99:
                raise ValueError(f"{dataset}/{method}/ef={ef}: invalid tail latency")
            if finite(row, "posts_per_query", source) < 0:
                raise ValueError(f"{dataset}/{method}/ef={ef}: invalid posts_per_query")
            if finite(row, "bytes_per_query", source) < 0:
                raise ValueError(f"{dataset}/{method}/ef={ef}: invalid bytes_per_query")
        fingerprint = required(row, "protocol_fingerprint", source)
        validate_sha(fingerprint, f"{dataset}/{method} protocol fingerprint")
        campaign = required(row, "campaign_id", source)
        binary = required(row, "binary_sha256", source)
        validate_sha(binary, f"{dataset}/{method} binary SHA")
        if method != "d-HNSW" and binary != expected_sha:
            raise ValueError(
                f"{dataset}/{method}: SlabWalk binary SHA {binary} != {expected_sha}"
            )
        link = query_pool_links.get(f"{dataset}/{method}")
        if link is None:
            raise ValueError(f"missing validated query-pool manifest for {dataset}/{method}")
        for field in aggregate_frontier.QUERY_POOL_LINK_FIELDS:
            actual = required(row, field, source)
            expected_link = str(link[field])
            if field.endswith("sha256"):
                validate_sha(actual, f"{dataset}/{method} {field}")
            if actual != expected_link:
                raise ValueError(
                    f"query-pool link mismatch for {dataset}/{method}: "
                    f"{field}={actual!r} expected {expected_link!r}"
                )
        retained_source = required(row, "source", source)
        source_sha256 = required(row, "source_sha256", source)
        validate_sha(source_sha256, f"{dataset}/{method} source SHA")
        validate_retained_source(
            directory,
            retained_source,
            source_sha256,
            f"{dataset}/{method}/ef={ef}/{run_id}",
        )
        grouped[(dataset, method, ef)].append(row)
        campaigns[(dataset, method)].add(campaign)
        binaries[(dataset, method)].add(binary)
        query_counts[dataset].add(expected)
        metrics[dataset].add(required(row, "metric", source))
        if not fingerprint or not run_id:
            raise AssertionError("required() accepted an empty value")

    point_counts: dict[tuple[str, str], int] = defaultdict(int)
    for (dataset, method, ef), points in grouped.items():
        run_ids = sorted(required(row, "run_id", source) for row in points)
        if (
            len(run_ids) != expected_repeats
            or len(set(run_ids)) != expected_repeats
            or any(re.fullmatch(r"r[0-9]+", run_id) is None for run_id in run_ids)
        ):
            raise ValueError(
                f"frontier repeats for {dataset}/{method}/ef={ef}: "
                f"expected {expected_repeats} unique measured run IDs, found {run_ids}"
            )
        fingerprints = {required(row, "protocol_fingerprint", source) for row in points}
        if len(fingerprints) != 1:
            raise ValueError(f"frontier protocol drift for {dataset}/{method}/ef={ef}")
        point_counts[(dataset, method)] += 1

    missing = [
        (dataset, method, point_counts[(dataset, method)])
        for dataset in FRONTIER_DATASETS
        for method in FRONTIER_METHODS
        if point_counts[(dataset, method)] < min_points
    ]
    if missing:
        raise ValueError(f"incomplete frontier matrix: {missing}")
    for key, values in campaigns.items():
        if len(values) != 1:
            raise ValueError(f"frontier campaign drift for {key}: {sorted(values)}")
    for key, values in binaries.items():
        if len(values) != 1:
            raise ValueError(f"frontier binary drift for {key}: {sorted(values)}")
    for dataset in FRONTIER_DATASETS:
        if campaigns[(dataset, "SHINE")] != campaigns[(dataset, "SlabWalk")]:
            raise ValueError(f"SHINE/SlabWalk campaign mismatch for {dataset}")
        if len(query_counts[dataset]) != 1:
            raise ValueError(f"query-pool drift across systems for {dataset}")
        if len(metrics[dataset]) != 1:
            raise ValueError(f"metric drift across systems for {dataset}")

    campaign_provenance = validate_frontier_campaign_provenance(
        directory,
        campaigns,
        required=require_campaign_provenance,
    )

    summary_path, summary_sha = validate_frontier_summary(
        directory, rows, expected_repeats
    )

    return {
        "measured_rows": len(rows),
        "datasets": list(FRONTIER_DATASETS),
        "methods": list(FRONTIER_METHODS),
        "points_per_curve": {
            f"{dataset}/{method}": point_counts[(dataset, method)]
            for dataset in FRONTIER_DATASETS
            for method in FRONTIER_METHODS
        },
        "expected_repeats": expected_repeats,
        "raw_csv": str(source),
        "raw_sha256": file_sha256(source),
        "summary_csv": str(summary_path),
        "summary_sha256": summary_sha,
        "query_pool_links_verified": len(rows),
        "retained_source_links_verified": len(rows),
        "campaign_provenance": campaign_provenance,
    }


def validate_robustness(
    directory: Path, expected_sha: str, expected_repeats: int = 5
) -> dict[str, object]:
    source = directory / "runs.csv"
    rows = [row for row in read_csv(source) if row.get("run_kind") == "measure"]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    campaigns = set()
    binaries = set()

    for row in rows:
        if required(row, "status", source) != "ok":
            raise ValueError(f"{source}: non-ok robustness run")
        factor = required(row, "factor", source)
        value = required(row, "value", source)
        cell = (factor, value)
        if cell not in ROBUSTNESS_CELLS:
            raise ValueError(f"unexpected robustness cell: {cell}")
        threads = integer(row, "threads", source)
        contexts = integer(row, "query_contexts", source)
        if threads <= 0 or contexts != threads:
            raise ValueError(f"{cell}: query contexts {contexts} do not match workers {threads}")
        if integer(row, "processed", source) <= 0:
            raise ValueError(f"{cell}: no processed queries")
        recall = finite(row, "recall", source)
        qps = finite(row, "qps", source)
        if not 0 <= recall <= 1 or qps <= 0:
            raise ValueError(f"{cell}: invalid recall/QPS")
        if finite(row, "posts_per_query", source) < 0 or finite(row, "bytes_per_query", source) < 0:
            raise ValueError(f"{cell}: invalid access accounting")
        if integer(row, "latency_enabled", source) == 1:
            p50 = finite(row, "p50_us", source)
            p95 = finite(row, "p95_us", source)
            p99 = finite(row, "p99_us", source)
            if not 0 <= p50 <= p95 <= p99:
                raise ValueError(f"{cell}: invalid latency quantiles")
        fingerprint = required(row, "protocol_fingerprint", source)
        campaign = required(row, "campaign_id", source)
        binary = required(row, "binary_sha256", source)
        validate_sha(binary, "robustness binary SHA")
        if binary != expected_sha:
            raise ValueError(f"robustness SlabWalk binary SHA {binary} != {expected_sha}")
        grouped[cell].append(row)
        campaigns.add(campaign)
        binaries.add(binary)
        if not fingerprint:
            raise AssertionError("required() accepted an empty value")

    if set(grouped) != ROBUSTNESS_CELLS:
        missing = sorted(ROBUSTNESS_CELLS - set(grouped))
        extra = sorted(set(grouped) - ROBUSTNESS_CELLS)
        raise ValueError(f"robustness matrix mismatch: missing={missing} extra={extra}")
    for cell, points in grouped.items():
        expect_repeat_set(points, "repeat", expected_repeats, f"robustness repeats for {cell}")
        if len({required(row, "protocol_fingerprint", source) for row in points}) != 1:
            raise ValueError(f"robustness protocol drift for {cell}")
    if len(campaigns) != 1 or len(binaries) != 1:
        raise ValueError("robustness campaign or binary drift")

    return {
        "measured_rows": len(rows),
        "measured_cells": len(grouped),
        "expected_repeats": expected_repeats,
        "campaign_id": next(iter(campaigns)),
        "runs_csv": str(source),
        "runs_sha256": file_sha256(source),
    }


def validate_worker_scaling(
    directory: Path, expected_sha: str, expected_repeats: int = 5
) -> dict[str, object]:
    """Validate a fixed-pool, three-system worker-scaling campaign."""
    source = directory / "runs.csv"
    rows = read_csv(source)
    grouped: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    campaigns = set()
    binaries: dict[str, set[str]] = defaultdict(set)
    query_canonical = set()
    groundtruth_canonical = set()
    query_files: dict[str, set[str]] = defaultdict(set)
    groundtruth_files: dict[str, set[str]] = defaultdict(set)
    query_manifests: dict[str, set[Path]] = defaultdict(set)
    manifest_cache: dict[Path, dict[str, object]] = {}
    retained_sources = set()

    for row in rows:
        method = required(row, "method", source)
        workers = integer(row, "workers", source)
        cell = (method, workers)
        if method not in WORKER_SCALING_METHODS or workers not in WORKER_SCALING_WORKERS:
            raise ValueError(f"unexpected worker-scaling cell: {cell}")
        if required(row, "dataset", source) != "DEEP1M":
            raise ValueError(f"{cell}: worker scaling must use DEEP1M")
        if required(row, "status", source) != "ok":
            raise ValueError(f"{cell}: non-ok worker-scaling run")
        if integer(row, "threads", source) != workers:
            raise ValueError(f"{cell}: thread count does not match workers")
        if method == "d-HNSW":
            if row.get("query_contexts", "").strip() or row.get("coroutines", "").strip():
                raise ValueError(f"{cell}: d-HNSW must not claim CN query contexts/coroutines")
        else:
            if integer(row, "query_contexts", source) != workers:
                raise ValueError(f"{cell}: query contexts do not match workers")
            if integer(row, "coroutines", source) != 2:
                raise ValueError(f"{cell}: graph-preserving paths must use two coroutines")
        if integer(row, "top_k", source) != 10 or integer(row, "ef", source) != 200:
            raise ValueError(f"{cell}: worker scaling must use top-k=10 and ef=200")
        if required(row, "metric", source) != "l2":
            raise ValueError(f"{cell}: worker scaling must use L2")
        if required(row, "measurement_mode", source) != "fixed_query_pool":
            raise ValueError(f"{cell}: worker scaling must use a fixed query pool")
        processed = integer(row, "processed_queries", source)
        expected = integer(row, "expected_queries", source)
        failed = integer(row, "failed_queries", source)
        if processed != 10000 or expected != 10000 or failed != 0:
            raise ValueError(f"{cell}: expected exactly 10000 completed queries")
        recall = finite(row, "recall", source)
        qps = finite(row, "qps", source)
        if not 0 <= recall <= 1 or qps <= 0:
            raise ValueError(f"{cell}: invalid recall/QPS")

        campaign = required(row, "campaign_id", source)
        fingerprint = required(row, "protocol_fingerprint", source)
        binary = required(row, "binary_sha256", source)
        validate_sha(fingerprint, "worker-scaling protocol fingerprint")
        validate_sha(binary, "worker-scaling binary SHA")
        if method in {"SHINE", "SlabWalk"} and binary != expected_sha:
            raise ValueError(f"{cell}: SlabWalk/SHINE binary SHA {binary} != {expected_sha}")

        query_hash = required(row, "query_canonical_sha256", source)
        groundtruth_hash = required(row, "groundtruth_canonical_sha256", source)
        query_file_hash = required(row, "query_file_sha256", source)
        groundtruth_file_hash = required(row, "groundtruth_file_sha256", source)
        for value, label in (
            (query_hash, "worker-scaling query canonical SHA"),
            (groundtruth_hash, "worker-scaling ground-truth canonical SHA"),
            (query_file_hash, "worker-scaling query file SHA"),
            (groundtruth_file_hash, "worker-scaling ground-truth file SHA"),
        ):
            validate_sha(value, label)
        manifest_sha = required(row, "query_pool_manifest_sha256", source)
        validate_sha(manifest_sha, "worker-scaling query manifest SHA")
        manifest_path = validate_retained_source(
            directory,
            required(row, "query_pool_manifest", source),
            manifest_sha,
            f"worker scaling query manifest for {method}",
        )
        if manifest_path not in manifest_cache:
            try:
                manifest_cache[manifest_path] = json.loads(manifest_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid worker-scaling query manifest: {manifest_path}"
                ) from exc
        manifest = manifest_cache[manifest_path]
        manifest_query = manifest.get("query", {})
        manifest_groundtruth = manifest.get("groundtruth", {})
        if (
            manifest.get("kind") != "query_pool_fingerprint"
            or manifest.get("dataset") != "DEEP1M"
            or manifest.get("method") != method
            or manifest.get("metric") != "l2"
            or int(manifest.get("limit", 0)) != 10000
            or int(manifest_query.get("rows", 0)) != 10000
            or int(manifest_query.get("dim", 0)) != 96
            or int(manifest_groundtruth.get("rows", 0)) != 10000
            or int(manifest_groundtruth.get("k", 0)) < 10
            or manifest_query.get("canonical_sha256") != query_hash
            or manifest_groundtruth.get("canonical_ids_sha256") != groundtruth_hash
            or manifest_query.get("file_sha256") != query_file_hash
            or manifest_groundtruth.get("file_sha256") != groundtruth_file_hash
        ):
            raise ValueError(
                f"worker-scaling query-manifest mismatch for {cell}: {manifest_path}"
            )
        retained_source_sha = required(row, "source_sha256", source)
        validate_sha(retained_source_sha, "worker-scaling retained source SHA")
        retained = validate_retained_source(
            directory,
            required(row, "source", source),
            retained_source_sha,
            f"worker scaling {cell} repeat {row.get('repeat', '')}",
        )

        grouped[cell].append(row)
        campaigns.add(campaign)
        binaries[method].add(binary)
        query_canonical.add(query_hash)
        groundtruth_canonical.add(groundtruth_hash)
        query_files[method].add(query_file_hash)
        groundtruth_files[method].add(groundtruth_file_hash)
        query_manifests[method].add(manifest_path)
        retained_sources.add(retained)

    expected_cells = {
        (method, workers)
        for method in WORKER_SCALING_METHODS
        for workers in WORKER_SCALING_WORKERS
    }
    if set(grouped) != expected_cells:
        missing = sorted(expected_cells - set(grouped))
        extra = sorted(set(grouped) - expected_cells)
        raise ValueError(f"worker-scaling matrix mismatch: missing={missing} extra={extra}")
    for cell, points in grouped.items():
        expect_repeat_set(
            points, "repeat", expected_repeats, f"worker-scaling repeats for {cell}"
        )
        if len({required(row, "protocol_fingerprint", source) for row in points}) != 1:
            raise ValueError(f"worker-scaling protocol drift for {cell}")
    if len(campaigns) != 1:
        raise ValueError("worker-scaling campaign drift")
    if any(len(binaries[method]) != 1 for method in WORKER_SCALING_METHODS):
        raise ValueError("worker-scaling binary drift")
    if len(query_canonical) != 1 or len(groundtruth_canonical) != 1:
        raise ValueError("worker-scaling query-pool mismatch across systems")
    if any(len(query_files[method]) != 1 for method in WORKER_SCALING_METHODS):
        raise ValueError("worker-scaling physical query-file drift")
    if any(len(groundtruth_files[method]) != 1 for method in WORKER_SCALING_METHODS):
        raise ValueError("worker-scaling physical ground-truth-file drift")
    if any(len(query_manifests[method]) != 1 for method in WORKER_SCALING_METHODS):
        raise ValueError("worker-scaling query-manifest drift")

    campaign_id = next(iter(campaigns))
    campaign_provenance = validate_worker_campaign_provenance(
        directory, campaign_id, expected_sha
    )
    return {
        "measured_rows": len(rows),
        "measured_cells": len(grouped),
        "methods": list(WORKER_SCALING_METHODS),
        "workers": list(WORKER_SCALING_WORKERS),
        "expected_repeats": expected_repeats,
        "campaign_id": campaign_id,
        "binary_sha256": {
            method: next(iter(binaries[method])) for method in WORKER_SCALING_METHODS
        },
        "query_canonical_sha256": next(iter(query_canonical)),
        "groundtruth_canonical_sha256": next(iter(groundtruth_canonical)),
        "retained_source_links_verified": len(retained_sources),
        "query_pool_manifests_verified": len(manifest_cache),
        "runs_csv": str(source),
        "runs_sha256": file_sha256(source),
        "campaign_provenance": campaign_provenance,
    }


def validate_worker_campaign_provenance(
    directory: Path, campaign_id: str, expected_slabwalk_sha: str
) -> dict[str, object]:
    """Validate the complete parser/runner/assembler amendment chain."""
    provenance_path = directory / "campaign_provenance.json"
    if not provenance_path.is_file():
        raise ValueError("missing worker campaign provenance manifest")
    try:
        provenance = json.loads(provenance_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid worker campaign provenance manifest") from exc
    entries = provenance.get("files")
    if (
        provenance.get("kind") != "worker_scaling_campaign_provenance"
        or provenance.get("campaign_id") != campaign_id
        or not isinstance(entries, list)
        or not entries
    ):
        raise ValueError("invalid worker campaign provenance identity")

    entry_paths = set()
    for item in entries:
        relative = str(item.get("path", ""))
        sha = str(item.get("sha256", ""))
        validate_sha(sha, "worker campaign provenance file SHA")
        retained = validate_retained_source(
            directory, relative, sha, "worker campaign provenance file"
        )
        if retained.stat().st_size != int(item.get("size_bytes", -1)):
            raise ValueError(f"worker campaign provenance size mismatch: {retained}")
        if relative in entry_paths:
            raise ValueError(f"duplicate worker campaign provenance path: {relative}")
        entry_paths.add(relative)
    actual_paths = {
        path.relative_to(directory).as_posix()
        for path in (directory / "campaign").rglob("*")
        if path.is_file()
    }
    if entry_paths != actual_paths:
        raise ValueError("worker campaign provenance inventory is not closed")

    required_names = (
        "campaign.before-parser-amendment.json",
        "parser_amendment.json",
        "campaign.before-assembler-amendment.json",
        "assembler_amendment.json",
        "campaign.before-parser-amendment-v2.json",
        "parser_amendment_v2.json",
        "campaign.before-dhnsw-runner-amendment.json",
        "dhnsw_runner_amendment.json",
        "campaign.before-assembler-amendment-v2.json",
        "assembler_amendment_v2.json",
        "failed_run_archive_w40_r0_before-runner-fix.json",
        "campaign.json",
    )
    for name in required_names:
        if f"campaign/{name}" not in entry_paths:
            raise ValueError(f"missing worker campaign audit file: {name}")

    campaign_dir = directory / "campaign"

    def load(name: str) -> dict[str, object]:
        try:
            value = json.loads((campaign_dir / name).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid worker campaign audit JSON: {name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid worker campaign audit object: {name}")
        return value

    final_manifest = load("campaign.json")
    protocol = final_manifest.get("protocol")
    if (
        final_manifest.get("campaign_id") != campaign_id
        or not isinstance(protocol, dict)
        or protocol.get("slabwalk_binary_sha256") != expected_slabwalk_sha
        or protocol.get("workers") != list(WORKER_SCALING_WORKERS)
        or int(protocol.get("repeats", 0)) != 5
    ):
        raise ValueError("worker campaign final protocol mismatch")
    expected_fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if final_manifest.get("protocol_fingerprint") != expected_fingerprint:
        raise ValueError("worker campaign final protocol fingerprint mismatch")
    for pointer, filename in (
        ("parser_amendment", "parser_amendment.json"),
        ("assembler_amendment", "assembler_amendment.json"),
        ("parser_amendment_v2", "parser_amendment_v2.json"),
        ("dhnsw_runner_amendment", "dhnsw_runner_amendment.json"),
        ("assembler_amendment_v2", "assembler_amendment_v2.json"),
    ):
        if final_manifest.get(pointer) != filename:
            raise ValueError(f"worker campaign missing amendment pointer: {pointer}")

    stages = (
        (
            "campaign.before-parser-amendment.json",
            "parser_amendment.json",
            "campaign.before-assembler-amendment.json",
        ),
        (
            "campaign.before-assembler-amendment.json",
            "assembler_amendment.json",
            "campaign.before-parser-amendment-v2.json",
        ),
        (
            "campaign.before-parser-amendment-v2.json",
            "parser_amendment_v2.json",
            "campaign.before-dhnsw-runner-amendment.json",
        ),
        (
            "campaign.before-dhnsw-runner-amendment.json",
            "dhnsw_runner_amendment.json",
            "campaign.before-assembler-amendment-v2.json",
        ),
        (
            "campaign.before-assembler-amendment-v2.json",
            "assembler_amendment_v2.json",
            "campaign.json",
        ),
    )
    amendment_records = {}
    for before_name, amendment_name, after_name in stages:
        record = load(amendment_name)
        before_sha = file_sha256(campaign_dir / before_name)
        after_sha = file_sha256(campaign_dir / after_name)
        if (
            record.get("campaign_id") != campaign_id
            or record.get("original_manifest_sha256") != before_sha
            or record.get("amended_manifest_sha256") != after_sha
        ):
            raise ValueError(f"worker campaign amendment chain mismatch: {amendment_name}")
        amendment_records[amendment_name] = record

    parser1 = amendment_records["parser_amendment.json"]
    parser2 = amendment_records["parser_amendment_v2.json"]
    assembler1 = amendment_records["assembler_amendment.json"]
    assembler2 = amendment_records["assembler_amendment_v2.json"]
    runner = amendment_records["dhnsw_runner_amendment.json"]
    hash_fields = (
        (parser1, "old_parser_sha256"),
        (parser1, "new_parser_sha256"),
        (parser2, "old_parser_sha256"),
        (parser2, "new_parser_sha256"),
        (assembler1, "old_tool_sha256"),
        (assembler1, "new_tool_sha256"),
        (assembler2, "old_tool_sha256"),
        (assembler2, "new_tool_sha256"),
        (runner, "old_tool_sha256"),
        (runner, "new_tool_sha256"),
    )
    for record, key in hash_fields:
        validate_sha(str(record.get(key, "")), f"worker campaign {key}")
    if (
        parser1.get("new_parser_sha256") != parser2.get("old_parser_sha256")
        or parser2.get("new_parser_sha256") != protocol.get("dhnsw_parser_sha256")
        or assembler1.get("new_tool_sha256") != assembler2.get("old_tool_sha256")
        or assembler2.get("new_tool_sha256") != protocol.get("assembler_sha256")
        or runner.get("new_tool_sha256") != protocol.get("dhnsw_runner_sha256")
    ):
        raise ValueError("worker campaign amendment tool-hash chain mismatch")

    failed = load("failed_run_archive_w40_r0_before-runner-fix.json")
    archive_relative = Path(str(failed.get("archive", "")))
    inventory = failed.get("files")
    parser_v2_before_sha = file_sha256(
        campaign_dir / "campaign.before-parser-amendment-v2.json"
    )
    if (
        failed.get("campaign_id") != campaign_id
        or failed.get("source") != "raw/dhnsw/w40/r0"
        or archive_relative.as_posix()
        != "failed_runs/dhnsw/w40/r0-before-runner-fix"
        or failed.get("campaign_manifest_sha256") != parser_v2_before_sha
        or not isinstance(inventory, list)
        or not inventory
    ):
        raise ValueError("invalid worker failed-run exclusion record")
    archive_root = campaign_dir / archive_relative
    for item in inventory:
        relative = Path(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid path in worker failed-run inventory")
        path = archive_root / relative
        expected_sha = str(item.get("sha256", ""))
        validate_sha(expected_sha, "worker failed-run file SHA")
        if (
            not path.is_file()
            or path.stat().st_size != int(item.get("size_bytes", -1))
            or file_sha256(path) != expected_sha
        ):
            raise ValueError(f"worker failed-run inventory mismatch: {path}")

    return {
        "files_verified": len(entry_paths),
        "amendments_verified": len(stages),
        "failed_run_files_verified": len(inventory),
        "manifest_sha256": file_sha256(provenance_path),
        "final_campaign_sha256": file_sha256(campaign_dir / "campaign.json"),
    }


def validate_topology_control(
    directory: Path, expected_repeats: int = 5
) -> dict[str, object]:
    """Validate d-HNSW loopback-versus-remote topology sensitivity."""
    source = directory / "runs.csv"
    summary_source = directory / "summary.csv"
    query_source = directory / "query_pool.json"
    campaign_source = directory / "campaign.json"
    rows = read_csv(source)
    summary_rows = read_csv(summary_source)
    try:
        query_manifest = json.loads(query_source.read_text())
        campaign = json.loads(campaign_source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid topology manifest under {directory}") from exc

    query_manifest_sha = file_sha256(query_source)
    query = query_manifest.get("query", {})
    groundtruth = query_manifest.get("groundtruth", {})
    if (
        query_manifest.get("kind") != "query_pool_fingerprint"
        or query_manifest.get("dataset") != "DEEP1M"
        or query_manifest.get("method") != "d-HNSW"
        or query_manifest.get("metric") != "l2"
        or int(query_manifest.get("limit", 0)) != 10000
        or int(query.get("rows", 0)) != 10000
        or int(query.get("dim", 0)) != 96
        or int(groundtruth.get("rows", 0)) != 10000
        or int(groundtruth.get("k", 0)) < 10
    ):
        raise ValueError("invalid topology query-pool manifest")
    for value, label in (
        (str(query.get("canonical_sha256", "")), "topology query canonical SHA"),
        (str(query.get("file_sha256", "")), "topology query file SHA"),
        (
            str(groundtruth.get("canonical_ids_sha256", "")),
            "topology ground-truth canonical SHA",
        ),
        (
            str(groundtruth.get("file_sha256", "")),
            "topology ground-truth file SHA",
        ),
    ):
        validate_sha(value, label)

    campaign_id = str(campaign.get("campaign_id", ""))
    protocol = campaign.get("protocol", {})
    if not campaign_id or not isinstance(protocol, dict):
        raise ValueError("invalid topology campaign manifest")
    for key in (
        "client_binary_sha256",
        "server_binary_sha256",
        "remote_server_sha256",
        "base_sha256",
        "remote_base_sha256",
    ):
        validate_sha(str(protocol.get(key, "")), f"topology campaign {key}")
    if protocol["server_binary_sha256"] != protocol["remote_server_sha256"]:
        raise ValueError("topology remote server binary SHA mismatch")
    if protocol["base_sha256"] != protocol["remote_base_sha256"]:
        raise ValueError("topology remote base SHA mismatch")
    if (
        protocol.get("measurement_mode") != "fixed_query_pool"
        or int(protocol.get("queries_per_run", 0)) != 10000
        or protocol.get("topologies") != ["loopback", "remote"]
    ):
        raise ValueError("invalid topology campaign protocol")

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    binaries = set()
    retained_sources = set()
    metric_names = (
        "qps",
        "recall",
        "latency_us",
        "network_us",
        "compute_us",
        "meta_us",
        "deserialize_us",
    )
    for row in rows:
        topology = required(row, "topology", source)
        if topology not in {"loopback", "remote"}:
            raise ValueError(f"unexpected topology-control cell: {topology}")
        if required(row, "campaign_id", source) != campaign_id:
            raise ValueError("topology campaign drift")
        if required(row, "dataset", source) != "DEEP1M":
            raise ValueError("topology control must use DEEP1M")
        if (
            integer(row, "threads", source) != 10
            or integer(row, "ef", source) != 200
            or integer(row, "top_k", source) != 10
            or required(row, "metric", source) != "l2"
            or required(row, "measurement_mode", source) != "fixed_query_pool"
        ):
            raise ValueError(f"invalid topology protocol for {topology}")
        if (
            integer(row, "processed_queries", source) != 10000
            or integer(row, "expected_queries", source) != 10000
            or integer(row, "failed_queries", source) != 0
        ):
            raise ValueError(f"{topology}: expected exactly 10000 completed queries")
        binary = required(row, "binary_sha256", source)
        fingerprint = required(row, "protocol_fingerprint", source)
        validate_sha(binary, "topology client binary SHA")
        validate_sha(fingerprint, "topology protocol fingerprint")
        if binary != protocol["client_binary_sha256"]:
            raise ValueError("topology client binary does not match campaign")
        for metric in metric_names:
            value = finite(row, metric, source)
            if metric == "recall":
                if not 0 <= value <= 1:
                    raise ValueError(f"invalid topology recall: {value}")
            elif value <= 0:
                raise ValueError(f"invalid topology {metric}: {value}")
        expected_links = {
            "query_canonical_sha256": str(query["canonical_sha256"]),
            "groundtruth_canonical_sha256": str(
                groundtruth["canonical_ids_sha256"]
            ),
            "query_file_sha256": str(query["file_sha256"]),
            "groundtruth_file_sha256": str(groundtruth["file_sha256"]),
            "query_manifest_sha256": query_manifest_sha,
        }
        for key, expected in expected_links.items():
            if required(row, key, source) != expected:
                raise ValueError(f"topology query-manifest mismatch: {key}")
        retained_sha = required(row, "source_sha256", source)
        validate_sha(retained_sha, "topology retained source SHA")
        retained = validate_retained_source(
            directory,
            required(row, "source", source),
            retained_sha,
            f"topology {topology} repeat {row.get('repeat', '')}",
        )
        grouped[topology].append(row)
        binaries.add(binary)
        retained_sources.add(retained)

    if set(grouped) != {"loopback", "remote"}:
        raise ValueError("topology-control matrix mismatch")
    for topology, points in grouped.items():
        expect_repeat_set(
            points, "repeat", expected_repeats,
            f"topology-control repeats for {topology}",
        )
        if len({required(row, "protocol_fingerprint", source) for row in points}) != 1:
            raise ValueError(f"topology protocol drift for {topology}")
    if len(binaries) != 1 or len(rows) != 2 * expected_repeats:
        raise ValueError("topology binary drift or incomplete rows")

    summary_by_topology = {
        required(row, "topology", summary_source): row for row in summary_rows
    }
    if set(summary_by_topology) != {"loopback", "remote"} or len(summary_rows) != 2:
        raise ValueError("topology summary matrix mismatch")
    tcrit = 2.776
    for topology, points in grouped.items():
        summary = summary_by_topology[topology]
        if integer(summary, "n", summary_source) != expected_repeats:
            raise ValueError("topology summary repeat count mismatch")
        for metric in metric_names:
            values = [finite(row, metric, source) for row in points]
            expected_mean = statistics.mean(values)
            expected_ci = tcrit * statistics.stdev(values) / math.sqrt(len(values))
            actual_mean = finite(summary, f"{metric}_mean", summary_source)
            actual_ci = finite(summary, f"{metric}_ci95", summary_source)
            if not math.isclose(actual_mean, expected_mean, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"topology summary mismatch: {topology}/{metric}_mean")
            if not math.isclose(actual_ci, expected_ci, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"topology summary mismatch: {topology}/{metric}_ci95")

    return {
        "measured_rows": len(rows),
        "measured_cells": len(grouped),
        "expected_repeats": expected_repeats,
        "campaign_id": campaign_id,
        "binary_sha256": next(iter(binaries)),
        "retained_source_links_verified": len(retained_sources),
        "runs_csv": str(source),
        "runs_sha256": file_sha256(source),
        "summary_csv": str(summary_source),
        "summary_sha256": file_sha256(summary_source),
        "campaign_sha256": file_sha256(campaign_source),
        "query_manifest_sha256": query_manifest_sha,
    }


def validate_resource_ledger(
    directory: Path,
    expected_sha: str,
    expected_repeats: int = 5,
    *,
    require_summary: bool = False,
) -> dict[str, object]:
    source = directory / "runs.csv"
    rows = read_csv(source)
    grouped: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    protocols = set()
    binaries = set()
    datasets = set()

    for row in rows:
        layout = required(row, "layout", source)
        mns = integer(row, "memory_nodes", source)
        cell = (layout, mns)
        if layout not in RESOURCE_LAYOUTS or mns not in RESOURCE_MN_COUNTS:
            raise ValueError(f"unexpected resource-ledger cell: {cell}")
        datasets.add(required(row, "dataset", source).lower())
        if integer(row, "num_vectors", source) <= 0 or integer(row, "num_queries", source) <= 0:
            raise ValueError(f"{cell}: empty resource-ledger workload")
        if integer(row, "threads", source) != 10:
            raise ValueError(f"{cell}: resource-ledger threads must be 10")
        if integer(row, "coroutines_per_thread", source) != 2:
            raise ValueError(f"{cell}: resource-ledger coroutines must be 2")
        binary = required(row, "binary_sha256", source)
        validate_sha(binary, "resource-ledger binary SHA")
        if binary != expected_sha:
            raise ValueError(f"resource-ledger SlabWalk binary SHA {binary} != {expected_sha}")
        manifest = required(row, "manifest_cell_fingerprint", source)
        protocol = required(row, "campaign_protocol_fingerprint", source)
        recall = finite(row, "recall", source)
        qps = finite(row, "qps", source)
        if not 0 <= recall <= 1 or qps <= 0:
            raise ValueError(f"{cell}: invalid recall/QPS")
        authoritative = finite(row, "measured_authoritative_index_bytes", source)
        registered = finite(row, "registered_sidecar_bytes", source)
        materialized = finite(row, "materialized_sidecar_bytes", source)
        written = finite(row, "actual_sidecar_write_bytes", source)
        if authoritative <= 0 or materialized <= 0 or written <= 0 or registered < materialized:
            raise ValueError(f"{cell}: invalid physical byte accounting")
        if finite(row, "storage_amplification", source) < 1:
            raise ValueError(f"{cell}: invalid storage amplification")
        for key in (
            "query_read_bytes_per_query",
            "query_read_wrs_per_query",
            "query_read_submits_per_query",
        ):
            if finite(row, key, source) < 0:
                raise ValueError(f"{cell}: invalid {key}")
        p50 = finite(row, "query_latency_p50_us", source)
        p95 = finite(row, "query_latency_p95_us", source)
        p99 = finite(row, "query_latency_p99_us", source)
        if not 0 <= p50 <= p95 <= p99:
            raise ValueError(f"{cell}: invalid query latency quantiles")
        grouped[cell].append(row)
        protocols.add(protocol)
        binaries.add(binary)
        if not manifest:
            raise AssertionError("required() accepted an empty value")

    expected_cells = {
        (layout, count) for layout in RESOURCE_LAYOUTS for count in RESOURCE_MN_COUNTS
    }
    if set(grouped) != expected_cells:
        missing = sorted(expected_cells - set(grouped))
        extra = sorted(set(grouped) - expected_cells)
        raise ValueError(f"resource-ledger matrix mismatch: missing={missing} extra={extra}")
    for cell, points in grouped.items():
        expect_repeat_set(
            points, "repeat", expected_repeats, f"resource-ledger repeats for {cell}"
        )
        if len({required(row, "manifest_cell_fingerprint", source) for row in points}) != 1:
            raise ValueError(f"resource-ledger manifest drift for {cell}")
    if len(protocols) != 1 or len(binaries) != 1:
        raise ValueError("resource-ledger campaign protocol or binary drift")
    if len(datasets) != 1 or next(iter(datasets)) not in {"gist", "gist1m"}:
        raise ValueError(f"resource ledger must contain only GIST1M, found {sorted(datasets)}")

    raw = directory / "raw"
    summary_source = directory / "summary.csv"
    summary_sha = ""
    if require_summary or raw.exists() or summary_source.exists():
        validate_recomputed_csv(
            summary_source,
            resource_ledger_summary.summarize(rows),
            ("layout", "memory_nodes"),
            "resource-ledger summary",
        )
        summary_sha = file_sha256(summary_source)
    retained_source_cells = 0
    if raw.exists():
        derived_rows, _ = resource_ledger_summary.collect(raw, require_latency=True)

        def key(row: dict[str, object]) -> tuple[str, int, int]:
            return (
                str(row["layout"]),
                int(row["memory_nodes"]),
                int(row["repeat"]),
            )

        def compare_rows(
            actual_rows: list[dict[str, object]],
            expected_rows: list[dict[str, object]],
            label: str,
            row_key,
        ) -> None:
            actual = {row_key(row): row for row in actual_rows}
            expected = {row_key(row): row for row in expected_rows}
            if len(actual) != len(actual_rows) or set(actual) != set(expected):
                raise ValueError(f"{label} matrix differs from retained raw evidence")
            for current_key, wanted in expected.items():
                got = actual[current_key]
                if set(got) != set(wanted):
                    raise ValueError(f"{label} columns differ for {current_key}")
                for field, value in wanted.items():
                    actual_value = got[field]
                    if field == "cell_path":
                        if Path(str(actual_value)).name != Path(str(value)).name:
                            raise ValueError(
                                f"{label} cell_path differs for {current_key}"
                            )
                    elif isinstance(value, (int, float)):
                        try:
                            parsed = float(actual_value)
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                f"{label} invalid {field} for {current_key}"
                            ) from exc
                        if not math.isfinite(parsed) or not math.isclose(
                            parsed, float(value), rel_tol=1e-9, abs_tol=1e-9
                        ):
                            raise ValueError(
                                f"{label} mismatch for {current_key}/{field}: "
                                f"{actual_value} != {value}"
                            )
                    elif str(actual_value) != str(value):
                        raise ValueError(
                            f"{label} mismatch for {current_key}/{field}: "
                            f"{actual_value!r} != {value!r}"
                        )

        compare_rows(rows, derived_rows, "resource-ledger runs", key)
        retained_source_cells = len(derived_rows)

    return {
        "measured_rows": len(rows),
        "measured_cells": len(grouped),
        "expected_repeats": expected_repeats,
        "campaign_protocol_fingerprint": next(iter(protocols)),
        "retained_source_cells_verified": retained_source_cells,
        "runs_csv": str(source),
        "runs_sha256": file_sha256(source),
        "summary_csv": str(summary_source),
        "summary_sha256": summary_sha,
    }


def validate_model_controls(
    directory: Path, expected_repeats: int = 5
) -> dict[str, object]:
    source = directory / "rdma_tau_runs.csv"
    rows = read_csv(source)
    grouped: dict[tuple[object, ...], list[dict[str, str]]] = defaultdict(list)
    endpoints: set[tuple[str, str, str, str, int]] = set()
    ports: set[int] = set()

    for row in rows:
        sweep = required(row, "sweep", source)
        label = required(row, "label", source)
        tool = required(row, "tool", source)
        size = integer(row, "size", source)
        mtu = integer(row, "mtu", source)
        outstanding = integer(row, "outs", source)
        qps = integer(row, "qps", source)
        cq_mod = integer(row, "cq_mod", source)
        client_numa = integer(row, "client_numa", source)
        server_numa = integer(row, "server_numa", source)
        if integer(row, "iters", source) <= 0:
            raise ValueError(f"{source}: iterations must be positive")

        cell = (
            sweep,
            tool,
            size,
            mtu,
            outstanding,
            qps,
            cq_mod,
            client_numa,
            server_numa,
        )
        expected_label = MODEL_CONTROL_CELLS.get(cell)
        if expected_label is None:
            raise ValueError(f"unexpected model-control cell: {cell}")
        if label != expected_label:
            raise ValueError(
                f"model-control label mismatch for {cell}: {label!r} != {expected_label!r}"
            )

        for requested, reported in (
            ("mtu", "reported_mtu"),
            ("outs", "reported_outs"),
            ("qps", "reported_qps"),
        ):
            if integer(row, reported, source) != integer(row, requested, source):
                raise ValueError(
                    f"{source}: {reported} does not match requested {requested}"
                )
        if tool == "ib_read_bw":
            if integer(row, "reported_cq_mod", source) != cq_mod:
                raise ValueError(
                    f"{source}: reported_cq_mod does not match requested cq_mod"
                )
            peak = finite(row, "bw_peak_gbps", source)
            average = finite(row, "bw_avg_gbps", source)
            rate = finite(row, "msg_rate_mpps", source)
            if peak < 0 or average <= 0 or rate <= 0:
                raise ValueError(f"{source}: invalid bandwidth result")
        elif tool == "ib_read_lat":
            average = finite(row, "avg_us", source)
            p99 = finite(row, "p99_us", source)
            p999 = finite(row, "p999_us", source)
            stdev = finite(row, "stdev_us", source)
            if average <= 0 or p99 < average or p999 < p99 or stdev < 0:
                raise ValueError(f"{source}: invalid latency result")
        else:
            raise ValueError(f"{source}: unsupported perftest tool {tool!r}")

        endpoint = (
            required(row, "cn_host", source),
            required(row, "mn_host", source),
            required(row, "mn_ip", source),
            required(row, "device", source),
            integer(row, "gid_index", source),
        )
        endpoints.add(endpoint)
        port = integer(row, "port", source)
        if port <= 0 or port in ports:
            raise ValueError(f"{source}: invalid or reused perftest port {port}")
        ports.add(port)
        grouped[cell].append(row)

    expected_cells = set(MODEL_CONTROL_CELLS)
    if set(grouped) != expected_cells:
        missing = sorted(expected_cells - set(grouped))
        extra = sorted(set(grouped) - expected_cells)
        raise ValueError(
            f"model-control matrix mismatch: missing={missing} extra={extra}"
        )
    for cell, points in grouped.items():
        repeats = sorted(integer(row, "rep", source) for row in points)
        expected = list(range(1, expected_repeats + 1))
        if repeats != expected:
            raise ValueError(
                f"model-control repeats for {cell}: {repeats} != {expected}"
            )
    if len(endpoints) != 1:
        raise ValueError(f"model-control host/device/GID drift: {sorted(endpoints)}")

    cn_host, mn_host, mn_ip, device, gid_index = next(iter(endpoints))
    return {
        "measured_rows": len(rows),
        "measured_cells": len(grouped),
        "expected_repeats": expected_repeats,
        "cn_host": cn_host,
        "mn_host": mn_host,
        "mn_ip": mn_ip,
        "device": device,
        "gid_index": gid_index,
        "runs_csv": str(source),
        "runs_sha256": file_sha256(source),
    }


def validate_query_pools(directory: Path) -> dict[str, object]:
    if not directory.is_dir():
        raise ValueError(f"missing query-pool evidence directory: {directory}")
    grouped: dict[tuple[str, str], tuple[Path, dict[str, object]]] = {}
    manifest_hashes: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid query-pool manifest: {path}") from exc
        if record.get("kind") != "query_pool_fingerprint":
            continue
        dataset = str(record.get("dataset", ""))
        method = str(record.get("method", ""))
        cell = (dataset, method)
        if dataset not in FRONTIER_DATASETS or method not in FRONTIER_METHODS:
            raise ValueError(f"unexpected query-pool cell: {cell}")
        if cell in grouped:
            raise ValueError(f"duplicate query-pool cell: {cell}")
        metric = str(record.get("metric", ""))
        if metric != QUERY_POOL_METRICS[dataset]:
            raise ValueError(f"{path}: metric {metric!r} does not match {dataset}")
        query = record.get("query")
        groundtruth = record.get("groundtruth")
        if not isinstance(query, dict) or not isinstance(groundtruth, dict):
            raise ValueError(f"{path}: missing query/ground-truth records")
        try:
            query_rows = int(query["rows"])
            query_dim = int(query["dim"])
            gt_rows = int(groundtruth["rows"])
            gt_k = int(groundtruth["k"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: invalid query-pool shape") from exc
        if (
            query_rows != 10000
            or gt_rows != 10000
            or query_dim != QUERY_POOL_DIMENSIONS[dataset]
            or gt_k < 10
        ):
            raise ValueError(
                f"{path}: expected 10K rows, dim={QUERY_POOL_DIMENSIONS[dataset]}, "
                f"and GT k>=10; found query=({query_rows},{query_dim}) "
                f"groundtruth=({gt_rows},{gt_k})"
            )
        expected_formats = (
            ("fvecs", "ivecs") if method == "d-HNSW" else ("fbin", "bin")
        )
        if (
            str(query.get("format", "")),
            str(groundtruth.get("format", "")),
        ) != expected_formats:
            raise ValueError(f"{path}: unexpected physical query-pool formats")
        for obj, key in (
            (query, "canonical_sha256"),
            (query, "file_sha256"),
            (groundtruth, "canonical_ids_sha256"),
            (groundtruth, "file_sha256"),
        ):
            validate_sha(str(obj.get(key, "")), f"{path.name} {key}")
        grouped[cell] = (path, record)
        manifest_hashes[path.name] = file_sha256(path)

    expected_cells = {
        (dataset, method)
        for dataset in FRONTIER_DATASETS
        for method in FRONTIER_METHODS
    }
    if set(grouped) != expected_cells:
        missing = sorted(expected_cells - set(grouped))
        extra = sorted(set(grouped) - expected_cells)
        raise ValueError(f"query-pool matrix mismatch: missing={missing} extra={extra}")

    for dataset in FRONTIER_DATASETS:
        query_hashes = {
            str(grouped[(dataset, method)][1]["query"]["canonical_sha256"])
            for method in FRONTIER_METHODS
        }
        gt_hashes = {
            str(
                grouped[(dataset, method)][1]["groundtruth"][
                    "canonical_ids_sha256"
                ]
            )
            for method in FRONTIER_METHODS
        }
        if len(query_hashes) != 1 or len(gt_hashes) != 1:
            raise ValueError(
                f"query-pool content mismatch for {dataset}: "
                f"queries={sorted(query_hashes)} groundtruth={sorted(gt_hashes)}"
            )

    spotcheck_path = directory / "tti_exact_groundtruth_spotcheck.json"
    if not spotcheck_path.is_file():
        raise ValueError("missing TTI exact ground-truth spot check")
    try:
        spotcheck = json.loads(spotcheck_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid TTI exact ground-truth spot check") from exc
    checks = spotcheck.get("checks")
    if (
        spotcheck.get("status") != "ok"
        or spotcheck.get("metric") != "ip"
        or int(spotcheck.get("top_k", 0)) != 10
        or int(spotcheck.get("checked_queries", 0)) < 3
        or list(spotcheck.get("query_indices", [])) != [0, 4999, 9999]
        or int(spotcheck.get("minimum_overlap", -1)) != 10
        or not isinstance(checks, list)
        or len(checks) != 3
        or any(
            int(check.get("overlap", -1)) != 10
            or check.get("exact_set_match") is not True
            for check in checks
        )
    ):
        raise ValueError("TTI exact ground-truth spot check did not pass")

    tti_query_files = {
        str(grouped[("TTI10M", method)][1]["query"]["file_sha256"])
        for method in ("SHINE", "SlabWalk")
    }
    tti_gt_files = {
        str(grouped[("TTI10M", method)][1]["groundtruth"]["file_sha256"])
        for method in ("SHINE", "SlabWalk")
    }
    if (
        str(spotcheck.get("query", {}).get("sha256", "")) not in tti_query_files
        or str(spotcheck.get("groundtruth", {}).get("sha256", "")) not in tti_gt_files
    ):
        raise ValueError(
            "TTI exact ground-truth spot check does not match the measured query pool"
        )
    manifest_hashes[spotcheck_path.name] = file_sha256(spotcheck_path)
    links: dict[str, dict[str, str]] = {}
    for (dataset, method), (path, record) in sorted(grouped.items()):
        query = record["query"]
        groundtruth = record["groundtruth"]
        links[f"{dataset}/{method}"] = {
            "query_pool_manifest": path.name,
            "query_pool_manifest_sha256": manifest_hashes[path.name],
            "query_path": str(query["path"]),
            "groundtruth_path": str(groundtruth["path"]),
            "query_canonical_sha256": str(query["canonical_sha256"]),
            "groundtruth_canonical_sha256": str(
                groundtruth["canonical_ids_sha256"]
            ),
            "query_file_sha256": str(query["file_sha256"]),
            "groundtruth_file_sha256": str(groundtruth["file_sha256"]),
        }
    return {
        "measured_cells": len(grouped),
        "datasets": list(FRONTIER_DATASETS),
        "methods": list(FRONTIER_METHODS),
        "query_rows_per_cell": 10000,
        "tti_exact_queries": 3,
        "directory": str(directory),
        "manifest_sha256": manifest_hashes,
        "links": links,
    }


def validate_cache_control(
    directory: Path, expected_slabwalk_sha: str
) -> dict[str, object]:
    """Recompute the fixed-pool SHINE cache control from retained raw cells."""
    summary_dir = directory / "summary"
    required = ("runs.csv", "summary.csv", "provenance.json", "validation.json")
    for name in required:
        if not (summary_dir / name).is_file():
            raise ValueError(f"missing cache-control summary: {summary_dir / name}")

    with tempfile.TemporaryDirectory(prefix="vldb-cache-gate-") as tmp:
        recomputed = Path(tmp) / "summary"
        cache_control_summary.summarize(
            directory, recomputed, expected_slabwalk_sha
        )
        for name in required:
            actual_sha = file_sha256(summary_dir / name)
            expected_sha = file_sha256(recomputed / name)
            if actual_sha != expected_sha:
                raise ValueError(
                    f"cache-control summary mismatch for {name}: "
                    f"{actual_sha} != {expected_sha}"
                )

    runs = read_csv(summary_dir / "runs.csv")
    summary = read_csv(summary_dir / "summary.csv")
    if len(runs) != 20 or len(summary) != 4:
        raise ValueError(
            "cache-control summary mismatch: expected 20 measured runs and 4 cells"
        )
    conditions = {row.get("condition", "") for row in summary}
    if conditions != set(cache_control_summary.CONDITIONS):
        raise ValueError(
            f"cache-control condition matrix mismatch: {sorted(conditions)}"
        )
    if any(integer(row, "n", summary_dir / "summary.csv") != 5 for row in summary):
        raise ValueError("cache-control summary must retain five repeats per cell")

    provenance = json.loads((summary_dir / "provenance.json").read_text())
    if provenance.get("retained_cells") != 24:
        raise ValueError("cache-control provenance must retain 24 cells")
    retained = provenance.get("retained_source_files")
    if not isinstance(retained, list) or len(retained) != 120:
        raise ValueError("cache-control provenance source inventory mismatch")

    c50 = next(row for row in summary if row["condition"] == "c50")
    return {
        "measured_rows": len(runs),
        "measured_cells": len(summary),
        "retained_cells": 24,
        "retained_source_files": len(retained),
        "runs_sha256": file_sha256(summary_dir / "runs.csv"),
        "summary_sha256": file_sha256(summary_dir / "summary.csv"),
        "post_reduction_c50_pct": finite(
            c50, "post_reduction_vs_off_pct", summary_dir / "summary.csv"
        ),
        "qps_change_c50_pct": finite(
            c50, "qps_change_vs_off_pct", summary_dir / "summary.csv"
        ),
        "directory": str(directory),
    }


def validate_colocation_control(
    directory: Path,
    expected_slabwalk_sha: str,
    expected_campaign_id: str | None = None,
    expected_protocol_fingerprint: str | None = None,
) -> dict[str, object]:
    """Recompute the fixed-code co-location-degree control from raw cells."""
    summary_dir = directory / "summary"
    required = ("runs.csv", "summary.csv", "provenance.json", "validation.json")
    for name in required:
        if not (summary_dir / name).is_file():
            raise ValueError(f"missing co-location summary: {summary_dir / name}")

    with tempfile.TemporaryDirectory(prefix="vldb-colocation-gate-") as tmp:
        recomputed = Path(tmp) / "summary"
        colocation_control_summary.summarize(
            directory, recomputed, expected_slabwalk_sha
        )
        for name in required:
            actual_sha = file_sha256(summary_dir / name)
            expected_sha = file_sha256(recomputed / name)
            if actual_sha != expected_sha:
                raise ValueError(
                    f"co-location summary mismatch for {name}: "
                    f"{actual_sha} != {expected_sha}"
                )

    runs = read_csv(summary_dir / "runs.csv")
    summary = read_csv(summary_dir / "summary.csv")
    if len(runs) != 30 or len(summary) != 6:
        raise ValueError(
            "co-location summary mismatch: expected 30 measured runs and 6 cells"
        )
    degrees = {row.get("degree", "") for row in summary}
    if degrees != set(colocation_control_summary.DEGREES):
        raise ValueError(f"co-location degree matrix mismatch: {sorted(degrees)}")
    if any(integer(row, "n", summary_dir / "summary.csv") != 5 for row in summary):
        raise ValueError("co-location summary must retain five repeats per cell")

    provenance = json.loads((summary_dir / "provenance.json").read_text())
    if provenance.get("retained_cells") != 36:
        raise ValueError("co-location provenance must retain 36 cells")
    retained = provenance.get("retained_source_files")
    if not isinstance(retained, list) or len(retained) != 180:
        raise ValueError("co-location provenance source inventory mismatch")
    campaign_id, protocol_fingerprint = validate_campaign_identity(
        label="co-location",
        observed_campaign_id=provenance.get("campaign_id"),
        observed_protocol_fingerprint=provenance.get("protocol_fingerprint"),
        expected_campaign_id=expected_campaign_id,
        expected_protocol_fingerprint=expected_protocol_fingerprint,
    )

    d1 = next(row for row in summary if row["degree"] == "1")
    validation = json.loads((summary_dir / "validation.json").read_text())
    try:
        recall_span = float(validation["recall_mean_span"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("co-location validation is missing recall_mean_span") from exc
    if not math.isfinite(recall_span) or recall_span < 0:
        raise ValueError("co-location validation has invalid recall_mean_span")
    return {
        "measured_rows": len(runs),
        "measured_cells": len(summary),
        "retained_cells": 36,
        "retained_source_files": len(retained),
        "campaign_id": campaign_id,
        "protocol_fingerprint": protocol_fingerprint,
        "runs_sha256": file_sha256(summary_dir / "runs.csv"),
        "summary_sha256": file_sha256(summary_dir / "summary.csv"),
        "recall_mean_span": recall_span,
        "d1_post_increase_pct": finite(
            d1, "post_increase_vs_full_pct", summary_dir / "summary.csv"
        ),
        "d1_qps_change_pct": finite(
            d1, "qps_change_vs_full_pct", summary_dir / "summary.csv"
        ),
        "directory": str(directory),
    }


def validate_mechanism_controls(
    directory: Path,
    expected_slabwalk_sha: str,
    expected_campaign_id: str | None = None,
    expected_protocol_fingerprint: str | None = None,
) -> dict[str, object]:
    """Recompute budget and resident-upper-graph controls from raw cells."""
    summary_dir = directory / "summary"
    required = (
        "runs.csv",
        "budget_summary.csv",
        "resident_summary.csv",
        "provenance.json",
        "validation.json",
    )
    for name in required:
        if not (summary_dir / name).is_file():
            raise ValueError(f"missing mechanism-control summary: {summary_dir / name}")

    with tempfile.TemporaryDirectory(prefix="vldb-mechanism-gate-") as tmp:
        recomputed = Path(tmp) / "summary"
        mechanism_control_summary.summarize(
            directory, recomputed, expected_slabwalk_sha
        )
        for name in required:
            actual_sha = file_sha256(summary_dir / name)
            expected_sha = file_sha256(recomputed / name)
            if actual_sha != expected_sha:
                raise ValueError(
                    f"mechanism-control summary mismatch for {name}: "
                    f"{actual_sha} != {expected_sha}"
                )

    runs = read_csv(summary_dir / "runs.csv")
    budget = read_csv(summary_dir / "budget_summary.csv")
    resident = read_csv(summary_dir / "resident_summary.csv")
    if len(runs) != 60 or len(budget) != 6 or len(resident) != 6:
        raise ValueError(
            "mechanism-control summary mismatch: expected 60 runs and 12 cells"
        )
    keys = {row.get("key", "") for row in budget}
    if keys != set(mechanism_control_summary.BUDGET_KEYS):
        raise ValueError(
            f"mechanism-control budget matrix mismatch: {sorted(keys)}"
        )
    resident_cells = {
        (row.get("mode", ""), integer(row, "ef", summary_dir / "resident_summary.csv"))
        for row in resident
    }
    expected_resident = {
        (mode, ef)
        for mode in mechanism_control_summary.RESIDENT_MODES
        for ef in mechanism_control_summary.RESIDENT_EFS
    }
    if resident_cells != expected_resident:
        raise ValueError(
            "mechanism-control resident matrix mismatch: "
            f"{sorted(resident_cells)}"
        )
    if any(integer(row, "n", summary_dir / "budget_summary.csv") != 5 for row in budget):
        raise ValueError("mechanism-control budget cells must retain five repeats")
    if any(integer(row, "n", summary_dir / "resident_summary.csv") != 5 for row in resident):
        raise ValueError("mechanism-control resident cells must retain five repeats")

    provenance = json.loads((summary_dir / "provenance.json").read_text())
    if provenance.get("retained_cells") != 72:
        raise ValueError("mechanism-control provenance must retain 72 cells")
    retained = provenance.get("retained_source_files")
    if not isinstance(retained, list) or len(retained) != 360:
        raise ValueError("mechanism-control provenance source inventory mismatch")
    campaign_id, protocol_fingerprint = validate_campaign_identity(
        label="mechanism-control",
        observed_campaign_id=provenance.get("campaign_id"),
        observed_protocol_fingerprint=provenance.get("protocol_fingerprint"),
        expected_campaign_id=expected_campaign_id,
        expected_protocol_fingerprint=expected_protocol_fingerprint,
    )

    f05 = next(row for row in budget if row["key"] == "f05")
    resident100 = next(
        row for row in resident
        if row["mode"] == "resident" and integer(row, "ef", summary_dir / "resident_summary.csv") == 100
    )
    return {
        "measured_rows": len(runs),
        "measured_cells": len(budget) + len(resident),
        "retained_cells": 72,
        "retained_source_files": len(retained),
        "campaign_id": campaign_id,
        "protocol_fingerprint": protocol_fingerprint,
        "runs_sha256": file_sha256(summary_dir / "runs.csv"),
        "budget_summary_sha256": file_sha256(summary_dir / "budget_summary.csv"),
        "resident_summary_sha256": file_sha256(summary_dir / "resident_summary.csv"),
        "f05_materialized_fraction_vs_full": finite(
            f05,
            "materialized_byte_fraction_vs_full",
            summary_dir / "budget_summary.csv",
        ),
        "resident_ef100_upnav_reduction_pct": finite(
            resident100,
            "upnav_reduction_vs_remote_pct",
            summary_dir / "resident_summary.csv",
        ),
        "resident_ef100_qps_change_pct": finite(
            resident100,
            "qps_change_vs_remote_pct",
            summary_dir / "resident_summary.csv",
        ),
        "directory": str(directory),
    }


def validate_query_profile(
    directory: Path,
    expected_slabwalk_sha: str,
    expected_runner_sha: str,
) -> dict[str, object]:
    """Recompute the frozen SIFT1M query profile from retained raw sources."""
    validate_sha(expected_slabwalk_sha, "expected query-profile binary SHA")
    validate_sha(expected_runner_sha, "expected query-profile runner SHA")
    raw = directory / "raw_sources"
    required_paths = (
        directory / "summary" / "summary.csv",
        directory / "summary" / "profile_symbols.csv",
        directory / "PROVENANCE.json",
        directory / "VALIDATION.json",
        directory / "SHA256SUMS",
    )
    for path in required_paths:
        if not path.is_file():
            raise ValueError(f"missing query-profile evidence: {path}")

    with tempfile.TemporaryDirectory(prefix="vldb-query-profile-gate-") as tmp:
        recomputed = Path(tmp) / "profile"
        query_profile_assembler.assemble(
            raw,
            recomputed,
            expected_binary_sha=expected_slabwalk_sha,
            expected_runner_sha=expected_runner_sha,
        )
        for relative in (
            Path("summary/summary.csv"),
            Path("summary/profile_symbols.csv"),
            Path("VALIDATION.json"),
        ):
            if file_sha256(directory / relative) != file_sha256(recomputed / relative):
                raise ValueError(f"profile summary mismatch for {relative}")

    summary_path = directory / "summary" / "summary.csv"
    rows = read_csv(summary_path)
    if len(rows) != 1:
        raise ValueError("query profile must contain exactly one summary row")
    row = rows[0]
    if (
        required(row, "dataset", summary_path) != "SIFT1M"
        or required(row, "method", summary_path) != "SHINE-derived"
        or integer(row, "threads", summary_path) != 1
        or integer(row, "query_contexts", summary_path) != 1
        or integer(row, "coroutines", summary_path) != 8
        or integer(row, "ef", summary_path) != 100
        or integer(row, "top_k", summary_path) != 10
        or integer(row, "query_rows", summary_path) != 200000
        or integer(row, "lost_samples", summary_path) != 0
        or required(row, "event", summary_path) != "cycles:u"
        or required(row, "distance_symbol", summary_path) != "l2"
    ):
        raise ValueError("query-profile protocol or summary matrix drift")
    if required(row, "binary_sha256", summary_path) != expected_slabwalk_sha:
        raise ValueError("query-profile binary SHA mismatch")
    if required(row, "runner_sha256", summary_path) != expected_runner_sha:
        raise ValueError("query-profile runner SHA mismatch")
    distance_percent = finite(row, "distance_self_percent", summary_path)
    if not 0 < distance_percent < 100:
        raise ValueError("query-profile distance share is outside (0, 100)")

    provenance = json.loads((directory / "PROVENANCE.json").read_text())
    retained = provenance.get("retained_sources")
    if not isinstance(retained, list) or len(retained) != 8:
        raise ValueError("query-profile retained-source inventory mismatch")
    for relative in retained:
        path = directory / Path(str(relative))
        if not path.is_file() or directory.resolve() not in path.resolve().parents:
            raise ValueError(f"invalid retained query-profile source: {relative}")

    manifest_path = directory / "SHA256SUMS"
    manifest: dict[str, str] = {}
    for line in manifest_path.read_text().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError("invalid query-profile SHA256SUMS line")
        digest, relative = parts[0], parts[1].lstrip(" *")
        validate_sha(digest, "query-profile inventory SHA")
        if relative in manifest:
            raise ValueError("duplicate path in query-profile SHA256SUMS")
        manifest[relative] = digest
    expected_paths = {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if set(manifest) != expected_paths:
        raise ValueError("query-profile SHA256SUMS inventory mismatch")
    for relative, digest in manifest.items():
        if file_sha256(directory / relative) != digest:
            raise ValueError(f"query-profile SHA mismatch: {relative}")

    return {
        "query_rows": 200000,
        "samples": integer(row, "samples", summary_path),
        "lost_samples": 0,
        "distance_self_percent": distance_percent,
        "summary_sha256": file_sha256(summary_path),
        "perf_data_sha256": required(row, "perf_data_sha256", summary_path),
        "retained_sources": len(retained),
        "directory": str(directory),
    }


def validate_all(
    frontier: Path,
    robustness: Path,
    worker_scaling: Path,
    resource_ledger: Path,
    model_controls: Path,
    query_pools: Path,
    expected_slabwalk_sha: str,
    *,
    topology_control: Path,
    build_cost: Path,
    build_scaling_10m_path: Path,
    index_construction_path: Path,
    lifecycle_control_path: Path,
    cache_control_path: Path,
    query_profile_path: Path,
    expected_profile_runner_sha: str,
    colocation_control_path: Path,
    expected_colocation_campaign_id: str,
    expected_colocation_protocol_fingerprint: str,
    mechanism_control_path: Path,
    expected_mechanism_campaign_id: str,
    expected_mechanism_protocol_fingerprint: str,
) -> dict[str, object]:
    validate_sha(expected_slabwalk_sha, "expected SlabWalk SHA")
    query_pool_report = validate_query_pools(query_pools)
    report = {
        "kind": "vldb_final_evidence_gate",
        "ready_for_plotting": True,
        "validated_utc": publication_timestamp(),
        "expected_slabwalk_sha256": expected_slabwalk_sha,
        "frontier": validate_frontier(
            frontier,
            expected_slabwalk_sha,
            query_pool_report["links"],
            require_campaign_provenance=True,
        ),
        "robustness": validate_robustness(robustness, expected_slabwalk_sha),
        "worker_scaling": validate_worker_scaling(
            worker_scaling, expected_slabwalk_sha
        ),
        "resource_ledger": validate_resource_ledger(
            resource_ledger, expected_slabwalk_sha, require_summary=True
        ),
        "model_controls": validate_model_controls(model_controls),
        "query_pools": query_pool_report,
        "topology_control": validate_topology_control(topology_control),
        "build_cost": validate_build_cost(build_cost, expected_slabwalk_sha),
        "index_construction": validate_index_construction(
            index_construction_path, expected_slabwalk_sha
        ),
        "lifecycle_controls": validate_lifecycle_controls(lifecycle_control_path),
        "cache_control": validate_cache_control(
            cache_control_path, expected_slabwalk_sha
        ),
        "colocation_control": validate_colocation_control(
            colocation_control_path,
            expected_slabwalk_sha,
            expected_colocation_campaign_id,
            expected_colocation_protocol_fingerprint,
        ),
        "mechanism_controls": validate_mechanism_controls(
            mechanism_control_path,
            expected_slabwalk_sha,
            expected_mechanism_campaign_id,
            expected_mechanism_protocol_fingerprint,
        ),
        "query_profile": validate_query_profile(
            query_profile_path,
            expected_slabwalk_sha,
            expected_profile_runner_sha,
        ),
    }
    build_scaling_report = build_scaling_10m_assembler.validate_bundle(
        build_scaling_10m_path, expected_slabwalk_sha
    )
    build_scaling_summary = build_scaling_10m_path / "summary.csv"
    build_scaling_report["summary_sha256"] = file_sha256(build_scaling_summary)
    report["build_scaling_10m"] = build_scaling_report
    report["campaign_identities"] = {
        "colocation_control": {
            "campaign_id": report["colocation_control"]["campaign_id"],
            "protocol_fingerprint": report["colocation_control"][
                "protocol_fingerprint"
            ],
        },
        "mechanism_controls": {
            "campaign_id": report["mechanism_controls"]["campaign_id"],
            "protocol_fingerprint": report["mechanism_controls"][
                "protocol_fingerprint"
            ],
        },
    }
    report["claim_input_sha256"] = {
        "headline_source_summary": report["frontier"]["summary_sha256"],
        "cache_summary": report["cache_control"]["summary_sha256"],
        "colocation_summary": report["colocation_control"]["summary_sha256"],
        "budget_summary": report["mechanism_controls"][
            "budget_summary_sha256"
        ],
        "resident_summary": report["mechanism_controls"][
            "resident_summary_sha256"
        ],
        "profile_summary": report["query_profile"]["summary_sha256"],
        "resource_summary": report["resource_ledger"]["summary_sha256"],
        "resource_runs": report["resource_ledger"]["runs_sha256"],
        "worker_runs": report["worker_scaling"]["runs_sha256"],
        "rdma_runs": report["model_controls"]["runs_sha256"],
        "robustness_runs": report["robustness"]["runs_sha256"],
        "topology_summary": report["topology_control"]["summary_sha256"],
        "lifecycle_refresh": report["lifecycle_controls"]["refresh_sha256"],
        "lifecycle_tti": report["lifecycle_controls"]["tti_sha256"],
        "build_summary": report["build_cost"]["summary_sha256"],
        "build_scaling_10m_summary": report["build_scaling_10m"][
            "summary_sha256"
        ],
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontier", type=Path, required=True)
    parser.add_argument("--robustness", type=Path, required=True)
    parser.add_argument("--worker-scaling", type=Path, required=True)
    parser.add_argument("--resource-ledger", type=Path, required=True)
    parser.add_argument("--model-controls", type=Path, required=True)
    parser.add_argument("--query-pools", type=Path, required=True)
    parser.add_argument("--topology-control", type=Path, required=True)
    parser.add_argument("--build-cost", type=Path, required=True)
    parser.add_argument("--build-scaling-10m", type=Path, required=True)
    parser.add_argument("--index-construction", type=Path, required=True)
    parser.add_argument("--lifecycle-controls", type=Path, required=True)
    parser.add_argument("--cache-control", type=Path, required=True)
    parser.add_argument("--colocation-control", type=Path, required=True)
    parser.add_argument("--mechanism-controls", type=Path, required=True)
    parser.add_argument("--query-profile", type=Path, required=True)
    parser.add_argument("--expected-profile-runner-sha", required=True)
    parser.add_argument("--expected-slabwalk-sha", required=True)
    parser.add_argument("--expected-colocation-campaign-id", required=True)
    parser.add_argument(
        "--expected-colocation-protocol-fingerprint", required=True
    )
    parser.add_argument("--expected-mechanism-campaign-id", required=True)
    parser.add_argument(
        "--expected-mechanism-protocol-fingerprint", required=True
    )
    parser.add_argument("--path-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def validated_output_path(args: argparse.Namespace) -> Path:
    if args.out.is_symlink():
        raise ValueError("evidence-gate output must not be a symbolic link")
    output = args.out.resolve(strict=False)
    protected_fields = (
        "frontier",
        "robustness",
        "worker_scaling",
        "resource_ledger",
        "model_controls",
        "query_pools",
        "topology_control",
        "build_cost",
        "build_scaling_10m",
        "index_construction",
        "lifecycle_controls",
        "cache_control",
        "colocation_control",
        "mechanism_controls",
        "query_profile",
    )
    for field in protected_fields:
        protected = getattr(args, field).resolve(strict=False)
        if (
            output == protected
            or protected in output.parents
            or output in protected.parents
        ):
            raise ValueError(
                f"evidence-gate output overlaps evidence input {field}: {output}"
            )
    return output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.out = validated_output_path(args)
    args.out.unlink(missing_ok=True)
    report = validate_all(
        args.frontier,
        args.robustness,
        args.worker_scaling,
        args.resource_ledger,
        args.model_controls,
        args.query_pools,
        args.expected_slabwalk_sha,
        topology_control=args.topology_control,
        build_cost=args.build_cost,
        build_scaling_10m_path=args.build_scaling_10m,
        index_construction_path=args.index_construction,
        lifecycle_control_path=args.lifecycle_controls,
        cache_control_path=args.cache_control,
        colocation_control_path=args.colocation_control,
        expected_colocation_campaign_id=args.expected_colocation_campaign_id,
        expected_colocation_protocol_fingerprint=(
            args.expected_colocation_protocol_fingerprint
        ),
        mechanism_control_path=args.mechanism_controls,
        expected_mechanism_campaign_id=args.expected_mechanism_campaign_id,
        expected_mechanism_protocol_fingerprint=(
            args.expected_mechanism_protocol_fingerprint
        ),
        query_profile_path=args.query_profile,
        expected_profile_runner_sha=args.expected_profile_runner_sha,
    )
    if args.path_root is not None:
        report = normalize_publication_paths(report, args.path_root)
    _write_gate_atomically(args.out, report)
    print(
        "VLDB final evidence is plot-ready: "
        f"frontier={report['frontier']['measured_rows']} rows, "
        f"robustness={report['robustness']['measured_cells']} cells, "
        f"worker-scaling={report['worker_scaling']['measured_cells']} cells, "
        f"topology={report['topology_control']['measured_cells']} cells, "
        f"build-cost={report['build_cost']['measured_rows']} rows, "
        f"build-scaling-10m={report['build_scaling_10m']['runs']} rows, "
        f"index-construction={report['index_construction']['measured_cells']} cells, "
        f"lifecycle={report['lifecycle_controls']['retained_sources_verified']} sources, "
        f"cache-control={report['cache_control']['measured_rows']} rows, "
        f"colocation-control={report['colocation_control']['measured_rows']} rows, "
        f"mechanism-controls={report['mechanism_controls']['measured_rows']} rows, "
        f"query-profile={report['query_profile']['samples']} samples, "
        f"resource={report['resource_ledger']['measured_cells']} cells, "
        f"model-controls={report['model_controls']['measured_cells']} cells, "
        f"query-pools={report['query_pools']['measured_cells']} cells"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
