#!/usr/bin/env python3
"""Validate and summarize materialization-budget and resident-upper-graph controls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import statistics
import uuid
from pathlib import Path
from typing import Any


BUDGET_KEYS = ("f05", "f10", "f25", "f50", "f75", "full")
BUDGET_VALUES = {
    "f05": 0.05,
    "f10": 0.10,
    "f25": 0.25,
    "f50": 0.50,
    "f75": 0.75,
    "full": 1.0,
}
RESIDENT_MODES = ("remote", "resident")
RESIDENT_EFS = (50, 100, 200)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DESCRIPTOR_RE = re.compile(
    r"^\[LAVD\]\[native\] packed addressing restored from descriptor: "
    r"N=(\d+) mns=(\d+) policy=([a-z_]+) layout=([a-z_]+) "
    r"descriptor_version=(\d+) packed_bytes=(\d+) sparse_bytes=(\d+)\s*$",
    re.MULTILINE,
)
BUDGET_BUILD_RE = re.compile(
    r"^\[LAVD\]\[multi\]\[budget\] f=([0-9.eE+-]+) H=(\d+)/(\d+) "
    r"hotset=([a-z_]+) reached=(\d+)\s*$",
    re.MULTILINE,
)
BUDGET_LOAD_RE = re.compile(
    r"^\[LAVD\]\[budget\] CN loaded map: N=(\d+) H=(\d+) "
    r"\(([0-9.eE+-]+)% co-located\)\s*$",
    re.MULTILINE,
)
ACCOUNTING_RE = re.compile(r"^LAVD_PHYSICAL_ACCOUNTING (\{.*\})$", re.MULTILINE)
UPPER_RE = re.compile(
    r"^\[CRANE\]\[multi\] upper cache built: U=(\d+) \((\d+)-d\) "
    r"comp=(\d+)B entry_uid=(\d+) num_mns=(\d+)\s*$",
    re.MULTILINE,
)
T95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_bundle_file(root: Path, path: Path) -> None:
    """Require a regular source file whose full path stays inside the campaign."""
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        relative = path.relative_to(root)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: expected bundle-contained regular file") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{path}: expected bundle-contained regular file")
    if not path.is_file():
        raise ValueError(f"{path}: expected bundle-contained regular file")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def canonical_float(value: float) -> float:
    """Remove runtime-specific noise below 15 significant decimal digits."""
    if not math.isfinite(value):
        raise ValueError(f"refusing non-finite derived statistic: {value!r}")
    return float(format(value, ".15g"))


def ci95(values: list[float]) -> float:
    if len(values) < 2:
        raise ValueError("95% CI requires at least two values")
    return canonical_float(
        T95.get(len(values), 1.96)
        * statistics.stdev(values)
        / math.sqrt(len(values))
    )


def require_number(obj: dict[str, Any], key: str, source: Path) -> float:
    try:
        value = float(obj[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{source}: missing or invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: non-finite {key}")
    return value


def require_integer(obj: dict[str, Any], key: str, source: Path) -> int:
    value = require_number(obj, key, source)
    if not value.is_integer():
        raise ValueError(f"{source}: non-integral {key}")
    return int(value)


def require_sha(value: Any, label: str) -> str:
    text = str(value)
    if not SHA_RE.fullmatch(text):
        raise ValueError(f"invalid {label}: {value!r}")
    return text


def load_campaign(root: Path, expected_sha: str) -> tuple[dict[str, Any], str, str]:
    path = root / "campaign.json"
    require_bundle_file(root, path)
    campaign = json.loads(path.read_text())
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("mechanism-control campaign is missing protocol")
    exact = {
        "binary_sha256": expected_sha,
        "budget": {
            "dataset": "GIST200K",
            "fractions": list(BUDGET_KEYS),
            "fraction_values": BUDGET_VALUES,
            "threads": 16,
            "query_contexts": 16,
            "coroutines": 8,
            "ef_search": 100,
            "ef_construction": 200,
            "m": 32,
            "queries_per_run": 1000,
            "hotset": "indeg",
        },
        "resident": {
            "dataset": "SIFT1M",
            "modes": list(RESIDENT_MODES),
            "ef_values": list(RESIDENT_EFS),
            "threads": 1,
            "query_contexts": 1,
            "coroutines": 8,
            "ef_construction": 100,
            "m": 16,
            "queries_per_run": 10000,
        },
        "repeats": 5,
        "warmups": 1,
        "top_k": 10,
        "query_suffix": "uniform",
        "scoring_code": "sq8",
        "record_layout": "packed_variable",
        "tcp_port": 1316,
        "index_region_bytes": 4294967296,
        "lavd_region_bytes": 17179869184,
    }
    for key, expected in exact.items():
        if protocol.get(key) != expected:
            raise ValueError(
                f"mechanism-control protocol mismatch for {key}: "
                f"{protocol.get(key)!r} != {expected!r}"
            )
    if protocol.get("memory_node") != "skv-node5":
        raise ValueError("mechanism-control protocol memory-node drift")
    for key in (
        "binary_sha256",
        "gist_index_dump_sha256",
        "sift_index_dump_sha256",
        "gist_query_sha256",
        "gist_groundtruth_sha256",
        "sift_query_sha256",
        "sift_groundtruth_sha256",
        "runner_sha256",
        "summarizer_sha256",
        "fingerprint_tool_sha256",
    ):
        require_sha(protocol.get(key), f"mechanism-control protocol {key}")
    encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if campaign.get("protocol_fingerprint") != fingerprint:
        raise ValueError("mechanism-control protocol fingerprint mismatch")
    campaign_id = str(campaign.get("campaign_id", "")).strip()
    if not campaign_id:
        raise ValueError("mechanism-control campaign ID is missing")
    return protocol, campaign_id, fingerprint


def option(command: list[str], flag: str, source: Path) -> str:
    if command.count(flag) != 1:
        raise ValueError(f"{source}: expected exactly one {flag}")
    index = command.index(flag)
    if index + 1 >= len(command):
        raise ValueError(f"{source}: missing value after {flag}")
    return command[index + 1]


def validate_command(
    command: Any, control: str, ef: int, source: Path
) -> None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"{source}: command must be a string list")
    budget = control == "budget"
    exact = {
        "--servers": "skv-node5",
        "--port": "1316",
        "--index-region-bytes": "4294967296",
        "--lavd-region-bytes": "17179869184",
        "--threads": "16" if budget else "1",
        "--query-contexts": "16" if budget else "1",
        "--coroutines": "8",
        "--ef-search": "100" if budget else str(ef),
        "--ef-construction": "200" if budget else "100",
        "--m": "32" if budget else "16",
        "--k": "10",
        "--query-suffix": "uniform",
        "--spec-k": "1",
        "--lavd": "8",
    }
    for flag, expected in exact.items():
        actual = option(command, flag, source)
        if actual != expected:
            raise ValueError(f"{source}: {flag} mismatch: {actual!r}")
    data_path = option(command, "--data-path", source).rstrip("/")
    expected_dataset = "gist200k" if budget else "sift1m"
    if not data_path.endswith("/" + expected_dataset):
        raise ValueError(f"{source}: data-path mismatch")
    if "--initiator" not in command or "--load-index" not in command:
        raise ValueError(f"{source}: mechanism control must load the fixed index")
    if "--store-index" in command or "--cache" in command or "--cache-ratio" in command:
        raise ValueError(f"{source}: disallowed mechanism-control command option")


def validate_environment(
    environment: Any, control: str, key: str, source: Path
) -> None:
    common = {
        "SHINE_LAVD_NATIVE_PACKED_WRITE": "1",
        "SHINE_LAVD_VARBLOCK": "1",
        "GB_BITMAP_DEDUP": "1",
        "GB_QUERY_LATENCY": "1",
    }
    if control == "budget":
        expected = {
            **common,
            "SHINE_LAVD_HOTSET": "indeg",
            "SHINE_CRANE": "1",
        }
        if key != "full":
            expected["SHINE_LAVD_BUDGET"] = str(BUDGET_VALUES[key])
        if environment != expected:
            raise ValueError(f"{source}: budget environment mismatch")
    else:
        expected = {
            **common,
            "SHINE_CRANE": "1" if key == "resident" else "0",
        }
        if environment != expected:
            raise ValueError(f"{source}: resident environment mismatch")


def parse_accounting(text: str, source: Path) -> dict[str, Any]:
    matches = ACCOUNTING_RE.findall(text)
    if len(matches) != 1:
        raise ValueError(f"{source}: expected one LAVD physical-accounting record")
    try:
        account = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: malformed LAVD physical-accounting record") from exc
    if not isinstance(account, dict):
        raise ValueError(f"{source}: malformed LAVD physical-accounting object")
    return account


def validate_accounting(
    account: dict[str, Any], control: str, key: str, vectors: int, source: Path
) -> None:
    exact = {
        "descriptor_version": 2,
        "policy": "block_cyclic",
        "record_layout": "variable",
        "scoring_code": "scalar",
        "scoring_bits": 8,
        "total_slots": vectors,
        "num_mns": 1,
        "mn": 0,
        "local_slots": vectors,
    }
    for field, expected in exact.items():
        if account.get(field) != expected:
            raise ValueError(f"{source}: physical accounting mismatch for {field}")
    registered = require_integer(account, "registered_bytes", source)
    materialized = require_integer(account, "materialized_bytes", source)
    written = require_integer(account, "actual_write_bytes", source)
    offset = require_integer(account, "offset_table_bytes", source)
    budget_map = require_integer(account, "budget_map_bytes", source)
    if materialized <= 0 or registered < materialized or not 0 < written <= materialized:
        raise ValueError(f"{source}: invalid materialized/registered/write byte accounting")
    if offset <= 0:
        raise ValueError(f"{source}: variable layout is missing its offset table")
    if control == "budget" and key != "full" and budget_map <= 0:
        raise ValueError(f"{source}: partial materialization is missing its budget map")
    if (control != "budget" or key == "full") and budget_map != 0:
        raise ValueError(f"{source}: unexpected budget map")


def load_cell(
    root: Path,
    control: str,
    key: str,
    ef: int,
    run_kind: str,
    repeat: int,
    campaign_id: str,
    fingerprint: str,
    expected_sha: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if control == "budget":
        if key not in BUDGET_KEYS or ef != 100:
            raise ValueError(f"unknown budget cell: {key}/ef{ef}")
        cell = root / "raw" / "budget" / key / f"{run_kind}_r{repeat}"
    elif control == "resident":
        if key not in RESIDENT_MODES or ef not in RESIDENT_EFS:
            raise ValueError(f"unknown resident cell: {key}/ef{ef}")
        cell = root / "raw" / "resident" / key / f"ef{ef}" / f"{run_kind}_r{repeat}"
    else:
        raise ValueError(f"unknown mechanism control: {control}")
    required = (
        cell / "manifest.json",
        cell / "cn.json",
        cell / "cn.err",
        cell / "mn" / "mn.err",
        cell / "mn" / "status",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError(f"missing mechanism cell files: {missing}")
    for path in required:
        require_bundle_file(root, path)
    if (cell / "mn" / "status").read_text().strip() != "0":
        raise ValueError(f"{cell}: memory-node status is not zero")

    manifest_path = cell / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    expected_manifest = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "control": control,
        "key": key,
        "ef": ef,
        "run_kind": run_kind,
        "repeat": repeat,
        "binary_sha256": expected_sha,
    }
    for field, expected in expected_manifest.items():
        if manifest.get(field) != expected:
            if field == "binary_sha256":
                raise ValueError(f"{manifest_path}: binary SHA mismatch")
            raise ValueError(f"{manifest_path}: {field} mismatch")
    protocol, loaded_campaign_id, loaded_fingerprint = load_campaign(root, expected_sha)
    if loaded_campaign_id != campaign_id or loaded_fingerprint != fingerprint:
        raise ValueError(f"{manifest_path}: campaign identity mismatch")
    if control == "budget":
        expected_inputs = {
            "cn_binary": expected_sha,
            "mn_binary": expected_sha,
            "index_dump": protocol["gist_index_dump_sha256"],
            "query": protocol["gist_query_sha256"],
            "groundtruth": protocol["gist_groundtruth_sha256"],
        }
    else:
        expected_inputs = {
            "cn_binary": expected_sha,
            "mn_binary": expected_sha,
            "index_dump": protocol["sift_index_dump_sha256"],
            "query": protocol["sift_query_sha256"],
            "groundtruth": protocol["sift_groundtruth_sha256"],
        }
    observed_inputs = manifest.get("observed_inputs")
    if observed_inputs != expected_inputs:
        raise ValueError(f"{manifest_path}: observed input SHA mismatch")
    for label, value in expected_inputs.items():
        require_sha(value, f"{manifest_path} observed input {label}")
    validate_command(manifest.get("command"), control, ef, manifest_path)
    validate_environment(manifest.get("environment"), control, key, manifest_path)

    cn_err_path = cell / "cn.err"
    err_text = cn_err_path.read_text(errors="replace")
    upper = UPPER_RE.findall(err_text)
    upper_required = control == "budget" or key == "resident"
    if upper_required and len(upper) != 1:
        raise ValueError(f"{cn_err_path}: resident upper graph build record is missing")
    if not upper_required and upper:
        raise ValueError(f"{cn_err_path}: remote upper graph unexpectedly became resident")

    cn_path = cell / "cn.json"
    data = json.loads(cn_path.read_text())
    meta = data.get("meta")
    queries = data.get("queries")
    timings = data.get("timings")
    hnsw = data.get("hnsw_parameters")
    if not all(isinstance(value, dict) for value in (meta, queries, timings, hnsw)):
        raise ValueError(f"{cn_path}: missing meta/queries/timings/HNSW object")
    budget = control == "budget"
    dataset = "GIST200K" if budget else "SIFT1M"
    dataset_json = "gist200k" if budget else "sift1m"
    vectors = 200000 if budget else 1000000
    expected_queries = 1000 if budget else 10000
    threads = 16 if budget else 1
    contexts = 16 if budget else 1
    m = 32 if budget else 16
    efc = 200 if budget else 100
    expected_meta = {
        "dataset": dataset_json,
        "compute_threads": threads,
        "coroutines_per_thread": 8,
        "memory_nodes": 1,
        "query_suffix": "uniform",
    }
    for field, expected in expected_meta.items():
        if meta.get(field) != expected:
            raise ValueError(f"{cn_path}: metadata mismatch for {field}")
    expected_hnsw = {"ef_construction": efc, "ef_search": ef, "k": 10, "m": m}
    for field, expected in expected_hnsw.items():
        if hnsw.get(field) != expected:
            raise ValueError(f"{cn_path}: HNSW parameter mismatch for {field}")
    if require_integer(data, "query_contexts", cn_path) != contexts:
        raise ValueError(f"{cn_path}: query-context mismatch")
    if require_integer(data, "num_queries", cn_path) != expected_queries:
        raise ValueError(f"{cn_path}: query-count mismatch")
    if require_integer(data, "num_vectors", cn_path) != vectors:
        raise ValueError(f"{cn_path}: vector-count mismatch")
    if data.get("distance") != "squared_l2":
        raise ValueError(f"{cn_path}: distance mismatch")

    descriptors = DESCRIPTOR_RE.findall(err_text)
    if len(descriptors) != 1:
        raise ValueError(f"{cn_err_path}: packed descriptor readback is missing")
    descriptor = descriptors[0]
    descriptor_values = {
        "N": int(descriptor[0]),
        "mns": int(descriptor[1]),
        "policy": descriptor[2],
        "layout": descriptor[3],
        "version": int(descriptor[4]),
        "packed_bytes": int(descriptor[5]),
        "sparse_bytes": int(descriptor[6]),
    }
    if (
        descriptor_values["N"] != vectors
        or descriptor_values["mns"] != 1
        or descriptor_values["policy"] != "block_cyclic"
        or descriptor_values["layout"] != "variable"
        or descriptor_values["version"] != 2
        or descriptor_values["packed_bytes"] <= 0
        or descriptor_values["sparse_bytes"] != 0
    ):
        raise ValueError(f"{cn_err_path}: packed descriptor readback mismatch")

    budget_build = BUDGET_BUILD_RE.findall(err_text)
    budget_load = BUDGET_LOAD_RE.findall(err_text)
    partial_budget = control == "budget" and key != "full"
    if partial_budget:
        if len(budget_build) != 1 or len(budget_load) != 1:
            raise ValueError(f"{cn_err_path}: budget publication/readback is missing")
        expected_fraction = BUDGET_VALUES[key]
        expected_hot = max(1, int(math.floor(expected_fraction * vectors + 0.5)))
        build_fraction, build_hot, build_n, hotset, reached = budget_build[0]
        load_n, load_hot, load_percent = budget_load[0]
        if (
            not math.isclose(float(build_fraction), expected_fraction, rel_tol=0, abs_tol=1e-6)
            or int(build_hot) != expected_hot
            or int(build_n) != vectors
            or hotset != "indeg"
            or int(reached) != vectors
            or int(load_n) != vectors
            or int(load_hot) != expected_hot
            or not math.isclose(
                float(load_percent), 100.0 * expected_hot / vectors,
                rel_tol=0,
                abs_tol=1e-5,
            )
        ):
            raise ValueError(f"{cn_err_path}: budget publication/readback mismatch")
    elif budget_build or budget_load:
        raise ValueError(f"{cn_err_path}: unexpected budget publication/readback")
    if require_integer(queries, "processed", cn_path) != expected_queries:
        raise ValueError(f"{cn_path}: processed-query mismatch")
    if require_integer(queries, "local_latency_samples", cn_path) != expected_queries:
        raise ValueError(f"{cn_path}: latency-sample mismatch")

    qps = require_number(queries, "queries_per_sec", cn_path)
    recall = require_number(queries, "recall", cn_path)
    posts = require_integer(queries, "rdma_posts", cn_path)
    wrs = require_integer(queries, "rdma_wrs", cn_path)
    bytes_read = require_integer(queries, "rdma_reads_in_bytes", cn_path)
    posts_upnav = require_integer(queries, "posts_upnav", cn_path)
    posts_l0 = require_integer(queries, "posts_l0", cn_path)
    posts_rerank = require_integer(queries, "posts_rerank", cn_path)
    p50 = require_number(queries, "local_latency_p50_us", cn_path)
    p95 = require_number(queries, "local_latency_p95_us", cn_path)
    p99 = require_number(queries, "local_latency_p99_us", cn_path)
    if qps <= 0 or not 0 <= recall <= 1 or posts <= 0 or wrs <= 0 or bytes_read <= 0:
        raise ValueError(f"{cn_path}: invalid query result")
    if posts_upnav + posts_l0 + posts_rerank != wrs:
        raise ValueError(f"{cn_path}: operation-class accounting does not close")
    if not 0 <= posts <= wrs:
        raise ValueError(f"{cn_path}: submitted/logical operation accounting is invalid")
    if p50 < 0 or not p50 <= p95 <= p99:
        raise ValueError(f"{cn_path}: invalid latency quantiles")
    if control == "resident":
        if key == "resident" and posts_upnav != 0:
            raise ValueError(f"{cn_path}: resident upper graph did not eliminate remote descent")
        if key == "remote" and posts_upnav <= 0:
            raise ValueError(f"{cn_path}: remote upper graph has no remote descent operations")

    account = parse_accounting(err_text, cn_err_path)
    validate_accounting(account, control, key, vectors, cn_err_path)
    if descriptor_values["packed_bytes"] != int(account["materialized_bytes"]):
        raise ValueError(
            f"{cn_err_path}: descriptor and physical byte accounting disagree"
        )
    upper_nodes = upper_dim = upper_bytes = entry_uid = upper_mns = 0
    if upper:
        upper_nodes, upper_dim, upper_bytes, entry_uid, upper_mns = (
            int(value) for value in upper[0]
        )
        if upper_nodes <= 0 or upper_dim <= 0 or upper_bytes <= 0:
            raise ValueError(f"{cn_err_path}: invalid resident upper graph accounting")
        if upper_mns != descriptor_values["mns"]:
            raise ValueError(
                f"{cn_err_path}: resident upper graph MN count mismatch: "
                f"{upper_mns} != {descriptor_values['mns']}"
            )
    if upper_required:
        build_ms = require_number(timings, "crane_build_multi", cn_path)
        if build_ms <= 0:
            raise ValueError(f"{cn_path}: invalid resident upper graph build time")
    elif "crane_build_multi" in timings:
        raise ValueError(f"{cn_path}: remote upper graph unexpectedly reports a build time")

    inventory = [
        {"path": str(path.relative_to(root)), "sha256": sha256(path)}
        for path in required
    ]
    row = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "control": control,
        "key": key,
        "ef": ef,
        "materialized_fraction": BUDGET_VALUES[key] if budget else 1.0,
        "upper_graph_state": "resident" if upper_required else "remote",
        "run_kind": run_kind,
        "repeat": repeat,
        "dataset": dataset,
        "binary_sha256": expected_sha,
        "num_vectors": vectors,
        "threads": threads,
        "query_contexts": contexts,
        "coroutines": 8,
        "top_k": 10,
        "processed": expected_queries,
        "recall": recall,
        "qps": qps,
        "posts_per_query": posts / expected_queries,
        "wrs_per_query": wrs / expected_queries,
        "bytes_per_query": bytes_read / expected_queries,
        "posts_upnav_per_query": posts_upnav / expected_queries,
        "posts_l0_per_query": posts_l0 / expected_queries,
        "posts_rerank_per_query": posts_rerank / expected_queries,
        "p50_us": p50,
        "p95_us": p95,
        "p99_us": p99,
        "descriptor_version": int(account["descriptor_version"]),
        "descriptor_packed_bytes": descriptor_values["packed_bytes"],
        "descriptor_sparse_bytes": descriptor_values["sparse_bytes"],
        "record_layout": str(account["record_layout"]),
        "scoring_bits": int(account["scoring_bits"]),
        "registered_bytes": int(account["registered_bytes"]),
        "materialized_bytes": int(account["materialized_bytes"]),
        "actual_write_bytes": int(account["actual_write_bytes"]),
        "budget_map_bytes": int(account["budget_map_bytes"]),
        "offset_table_bytes": int(account["offset_table_bytes"]),
        "upper_nodes": upper_nodes,
        "upper_dim": upper_dim,
        "upper_bytes": upper_bytes,
        "upper_entry_uid": entry_uid,
        "upper_build_ms": require_number(timings, "crane_build_multi", cn_path)
        if upper_required else 0.0,
        "source_json": str(cn_path.relative_to(root)),
        "source_json_sha256": sha256(cn_path),
        "source_manifest": str(manifest_path.relative_to(root)),
        "source_manifest_sha256": sha256(manifest_path),
    }
    return row, inventory


def validate_query_pool(root: Path, filename: str, dataset: str, rows: int) -> Path:
    path = root / "query_pools" / filename
    require_bundle_file(root, path)
    record = json.loads(path.read_text())
    if (
        record.get("kind") != "query_pool_fingerprint"
        or record.get("dataset") != dataset
        or record.get("method") != "SlabWalk"
        or record.get("metric") != "l2"
        or record.get("limit") != rows
        or record.get("query", {}).get("rows") != rows
        or record.get("groundtruth", {}).get("rows") != rows
    ):
        raise ValueError(f"{path}: mechanism-control query-pool manifest mismatch")
    require_sha(record.get("query", {}).get("canonical_sha256"), f"{dataset} query pool")
    require_sha(
        record.get("groundtruth", {}).get("canonical_ids_sha256"),
        f"{dataset} ground-truth pool",
    )
    return path


def load_runs(root: Path, expected_sha: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    protocol, campaign_id, fingerprint = load_campaign(root, expected_sha)
    query_manifests = (
        validate_query_pool(root, "gist200k_slabwalk.json", "GIST200K", 1000),
        validate_query_pool(root, "sift1m_slabwalk.json", "SIFT1M", 10000),
    )
    cells = [
        ("budget", key, 100) for key in BUDGET_KEYS
    ] + [
        ("resident", mode, ef)
        for mode in RESIDENT_MODES
        for ef in RESIDENT_EFS
    ]
    measured: list[dict[str, Any]] = []
    inventory: list[dict[str, str]] = []
    for control, key, ef in cells:
        for run_kind, repeats in (
            ("warmup", int(protocol["warmups"])),
            ("measure", int(protocol["repeats"])),
        ):
            for repeat in range(repeats):
                row, source_files = load_cell(
                    root,
                    control,
                    key,
                    ef,
                    run_kind,
                    repeat,
                    campaign_id,
                    fingerprint,
                    expected_sha,
                )
                inventory.extend(source_files)
                if run_kind == "measure":
                    measured.append(row)
    counts: dict[tuple[str, str, int], int] = {
        (control, key, ef): 0 for control, key, ef in cells
    }
    for row in measured:
        counts[(str(row["control"]), str(row["key"]), int(row["ef"]))] += 1
    expected_counts = {cell: 5 for cell in cells}
    if counts != expected_counts:
        raise ValueError(f"incomplete mechanism-control matrix: {counts}")
    provenance = {
        "campaign_id": campaign_id,
        "protocol_fingerprint": fingerprint,
        "campaign_sha256": sha256(root / "campaign.json"),
        "query_pool_manifests": [
            {"path": str(path.relative_to(root)), "sha256": sha256(path)}
            for path in query_manifests
        ],
        "retained_cells": 72,
        "retained_source_files": inventory,
    }
    return measured, provenance


def stats(rows: list[dict[str, Any]], field: str) -> tuple[float, float]:
    values = [float(row[field]) for row in rows]
    return canonical_float(statistics.mean(values)), ci95(values)


def summarize_budget(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    full = [row for row in runs if row["control"] == "budget" and row["key"] == "full"]
    full_qps = statistics.mean(float(row["qps"]) for row in full)
    full_materialized = statistics.mean(float(row["materialized_bytes"]) for row in full)
    summary: list[dict[str, Any]] = []
    for key in BUDGET_KEYS:
        rows = [row for row in runs if row["control"] == "budget" and row["key"] == key]
        qps_mean, qps_ci = stats(rows, "qps")
        materialized_mean, materialized_ci = stats(rows, "materialized_bytes")
        row: dict[str, Any] = {
            "key": key,
            "materialized_fraction": BUDGET_VALUES[key],
            "n": len(rows),
            "qps_mean": qps_mean,
            "qps_ci95": qps_ci,
            "qps_change_vs_full_pct": 100.0 * (qps_mean / full_qps - 1.0),
            "materialized_bytes_mean": materialized_mean,
            "materialized_bytes_ci95": materialized_ci,
            "materialized_byte_fraction_vs_full": materialized_mean / full_materialized,
        }
        for field in (
            "recall",
            "posts_per_query",
            "wrs_per_query",
            "bytes_per_query",
            "p99_us",
            "registered_bytes",
            "actual_write_bytes",
            "budget_map_bytes",
        ):
            mean, ci = stats(rows, field)
            row[f"{field}_mean"] = mean
            row[f"{field}_ci95"] = ci
        summary.append(
            {
                field: canonical_float(value) if isinstance(value, float) else value
                for field, value in row.items()
            }
        )
    materialized = [float(row["materialized_bytes_mean"]) for row in summary]
    if any(right <= left for left, right in zip(materialized, materialized[1:])):
        raise ValueError("materialization budget did not produce a monotone byte frontier")
    return summary


def summarize_resident(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for ef in RESIDENT_EFS:
        remote_rows = [
            row for row in runs
            if row["control"] == "resident" and row["key"] == "remote" and row["ef"] == ef
        ]
        remote_qps = canonical_float(
            statistics.mean(float(row["qps"]) for row in remote_rows)
        )
        remote_upnav = canonical_float(
            statistics.mean(float(row["posts_upnav_per_query"]) for row in remote_rows)
        )
        for mode in RESIDENT_MODES:
            rows = [
                row for row in runs
                if row["control"] == "resident" and row["key"] == mode and row["ef"] == ef
            ]
            qps_mean, qps_ci = stats(rows, "qps")
            upnav_mean, upnav_ci = stats(rows, "posts_upnav_per_query")
            row = {
                "mode": mode,
                "ef": ef,
                "n": len(rows),
                "qps_mean": qps_mean,
                "qps_ci95": qps_ci,
                "qps_change_vs_remote_pct": 100.0 * (qps_mean / remote_qps - 1.0),
                "posts_upnav_per_query_mean": upnav_mean,
                "posts_upnav_per_query_ci95": upnav_ci,
                "upnav_reduction_vs_remote_pct": 100.0 * (1.0 - upnav_mean / remote_upnav),
            }
            for field in (
                "recall",
                "posts_per_query",
                "wrs_per_query",
                "bytes_per_query",
                "p99_us",
                "upper_nodes",
                "upper_bytes",
                "upper_build_ms",
            ):
                mean, ci = stats(rows, field)
                row[f"{field}_mean"] = mean
                row[f"{field}_ci95"] = ci
            summary.append(
                {
                    field: canonical_float(value) if isinstance(value, float) else value
                    for field, value in row.items()
                }
            )
    return summary


def summarize(root: Path, out: Path, expected_sha: str) -> dict[str, Any]:
    if out.exists():
        raise ValueError(f"refusing existing mechanism-control summary: {out}")
    runs, provenance = load_runs(root, expected_sha)
    budget = summarize_budget(runs)
    resident = summarize_resident(runs)
    staging = out.with_name(f".{out.name}.tmp-{uuid.uuid4().hex}")
    staging.mkdir(parents=True)
    try:
        write_csv(staging / "runs.csv", runs)
        write_csv(staging / "budget_summary.csv", budget)
        write_csv(staging / "resident_summary.csv", resident)
        provenance["summarizer_sha256"] = sha256(Path(__file__).resolve())
        (staging / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        report = {
            "measured_runs": len(runs),
            "measured_cells": len(budget) + len(resident),
            "retained_cells": provenance["retained_cells"],
            "retained_source_files": len(provenance["retained_source_files"]),
            "runs_sha256": sha256(staging / "runs.csv"),
            "budget_summary_sha256": sha256(staging / "budget_summary.csv"),
            "resident_summary_sha256": sha256(staging / "resident_summary.csv"),
            "provenance_sha256": sha256(staging / "provenance.json"),
        }
        (staging / "validation.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        staging.rename(out)
        return report
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-binary-sha", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = summarize(args.campaign, args.out, args.expected_binary_sha)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
