#!/usr/bin/env python3
"""Assemble one canonical five-repeat Slab derivation sample per 10M dataset."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_REPEATS = tuple(range(1, 6))
T_CRITICAL_95_DF4 = 2.7764451051977987
STAGE_FIELDS = (
    "lavd_build_fetch",
    "lavd_build_parse",
    "lavd_build_rank",
    "lavd_build_encode",
    "lavd_build_metadata",
    "lavd_build_materialize",
)
DATASET_SPECS = {
    "DEEP10M": {
        "campaign": "deep",
        "source_dataset": "DEEP10M",
        "meta_dataset": "deep10m",
        "ef": 48,
    },
    "SIFT10M": {
        "campaign": "text_sift",
        "source_dataset": "SIFT10M",
        "meta_dataset": "sift10m",
        "ef": 64,
    },
    "TTI10M": {
        "campaign": "text_sift",
        "source_dataset": "TEXT10M",
        "meta_dataset": "tti-10m",
        "ef": 100,
    },
}
RUN_FIELDS = (
    "dataset",
    "repeat",
    "canonical_ef",
    "build_ms",
    "resident_build_ms",
    *STAGE_FIELDS,
    "registered_bytes",
    "materialized_bytes",
    "actual_write_bytes",
    "offset_table_bytes",
    "record_bytes",
    "source_json",
    "source_json_sha256",
    "source_stderr",
    "source_stderr_sha256",
    "source_frontier_csv",
    "source_frontier_csv_sha256",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha(value: str, label: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{label} is not a lowercase SHA-256")


def read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing frontier CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty frontier CSV: {path}")
    return rows


def number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field {label}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric field {label}")
    return result


def integer(value: Any, label: str) -> int:
    parsed = number(value, label)
    if not parsed.is_integer():
        raise ValueError(f"non-integral field {label}: {value!r}")
    return int(parsed)


def load_campaign(root: Path, expected_sha: str, datasets: set[str]) -> dict[str, Any]:
    campaign = read_json(root / "campaign.json", "campaign manifest")
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError(f"{root}: campaign protocol is missing")
    if protocol.get("gb_binary_sha256") != expected_sha:
        raise ValueError(f"{root}: campaign binary SHA mismatch")
    expected_protocol = {
        "measurement_mode": "fixed_query_pool",
        "threads": 10,
        "query_contexts": 10,
        "coroutines": 2,
        "top_k": 10,
        "repeats": 5,
    }
    for field, expected in expected_protocol.items():
        actual = protocol.get(field)
        if isinstance(expected, int):
            actual = integer(actual, f"campaign {field}")
        if actual != expected:
            raise ValueError(f"{root}: campaign protocol mismatch for {field}")
    advertised = set(protocol.get("datasets_sw", []))
    if not datasets <= advertised:
        raise ValueError(f"{root}: campaign dataset matrix mismatch")
    fingerprint = str(campaign.get("protocol_fingerprint", ""))
    require_sha(fingerprint, f"{root} protocol fingerprint")
    return campaign


def parse_accounting(path: Path, dataset: str, repeat: int) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{dataset}: missing stderr for repeat {repeat}")
    records = []
    build_done = False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("LAVD_PHYSICAL_ACCOUNTING "):
            try:
                value = json.loads(line.split(" ", 1)[1])
            except json.JSONDecodeError as exc:
                raise ValueError(f"{dataset}: malformed physical accounting") from exc
            records.append(value)
        if line.startswith("[LAVD][multi] build done: N=10000000"):
            build_done = True
    if len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError(f"{dataset}: repeat {repeat} needs one accounting record")
    if not build_done:
        raise ValueError(f"{dataset}: repeat {repeat} lacks build completion marker")
    record = records[0]
    expected = {
        "descriptor_version": 2,
        "policy": "block_cyclic",
        "record_layout": "variable",
        "scoring_code": "scalar",
        "scoring_bits": 8,
        "total_slots": 10_000_000,
        "num_mns": 1,
        "mn": 0,
    }
    for field, wanted in expected.items():
        actual = record.get(field)
        if isinstance(wanted, int):
            actual = integer(actual, f"accounting {field}")
        if actual != wanted:
            raise ValueError(f"{dataset}: accounting mismatch for {field}")
    for field in (
        "registered_bytes",
        "materialized_bytes",
        "actual_write_bytes",
        "offset_table_bytes",
        "record_bytes",
    ):
        record[field] = integer(record.get(field), f"accounting {field}")
    if (
        record["record_bytes"] <= 0
        or record["offset_table_bytes"] <= 0
        or record["materialized_bytes"] <= 0
        or record["materialized_bytes"] > record["registered_bytes"]
        or record["actual_write_bytes"] != record["materialized_bytes"]
    ):
        raise ValueError(f"{dataset}: invalid physical byte accounting")
    return record


def select_frontier_row(
    csv_path: Path,
    dataset: str,
    source_dataset: str,
    repeat: int,
    ef: int,
    expected_sha: str,
    json_path: Path,
    err_path: Path,
    expected_json_name: str | None = None,
    expected_err_name: str | None = None,
) -> dict[str, str]:
    matches = []
    for row in read_csv(csv_path):
        if (
            row.get("dataset") == source_dataset
            and row.get("method") == "SlabWalk"
            and row.get("variant") == "slabwalk_expansion"
            and row.get("run_id") == f"r{repeat}"
            and integer(row.get("ef"), "frontier ef") == ef
        ):
            matches.append(row)
    if len(matches) != 1:
        raise ValueError(f"{dataset}: repeat {repeat} needs one matching frontier row")
    row = matches[0]
    expected = {
        "binary_sha256": expected_sha,
        "run_kind": "measure",
        "measurement_mode": "fixed_query_pool",
        "threads": "10",
        "query_contexts": "10",
        "coroutines": "2",
        "top_k": "10",
        "processed": "10000",
        "expected_queries": "10000",
        "failed_queries": "0",
        "status": "ok",
    }
    for field, wanted in expected.items():
        if row.get(field) != wanted:
            raise ValueError(f"{dataset}: frontier protocol mismatch for {field}")
    if Path(row.get("json", "")).name != (expected_json_name or json_path.name):
        raise ValueError(f"{dataset}: frontier JSON link mismatch")
    if Path(row.get("stderr", "")).name != (expected_err_name or err_path.name):
        raise ValueError(f"{dataset}: frontier stderr link mismatch")
    return row


def parse_measurement(
    json_path: Path,
    err_path: Path,
    dataset: str,
    repeat: int,
) -> dict[str, Any]:
    spec = DATASET_SPECS[dataset]
    source_dataset = str(spec["source_dataset"])
    ef = int(spec["ef"])
    expected_label = (
        f"{source_dataset}_slabwalk_expansion_r{repeat}_measure_T10_ef{ef}"
    )
    if not json_path.is_file():
        raise ValueError(f"{dataset}: repeat {repeat} selected JSON is missing")
    if not err_path.is_file():
        raise ValueError(f"{dataset}: repeat {repeat} selected stderr is missing")
    payload = read_json(json_path, f"{dataset} repeat {repeat} JSON")
    meta = payload.get("meta")
    timings = payload.get("timings")
    queries = payload.get("queries")
    hnsw = payload.get("hnsw_parameters")
    if not all(isinstance(value, dict) for value in (meta, timings, queries, hnsw)):
        raise ValueError(f"{dataset}: repeat {repeat} JSON structure mismatch")
    assert isinstance(meta, dict)
    assert isinstance(timings, dict)
    assert isinstance(queries, dict)
    assert isinstance(hnsw, dict)
    protocol_fields = {
        "meta.dataset": (meta.get("dataset"), spec["meta_dataset"]),
        "meta.label": (meta.get("label"), expected_label),
        "meta.compute_threads": (meta.get("compute_threads"), 10),
        "meta.coroutines_per_thread": (meta.get("coroutines_per_thread"), 2),
        "meta.memory_nodes": (meta.get("memory_nodes"), 1),
        "num_vectors": (payload.get("num_vectors"), 10_000_000),
        "num_queries": (payload.get("num_queries"), 10_000),
        "query_contexts": (payload.get("query_contexts"), 10),
        "hnsw_parameters.ef_search": (hnsw.get("ef_search"), ef),
        "hnsw_parameters.k": (hnsw.get("k"), 10),
        "queries.processed": (queries.get("processed"), 10_000),
    }
    mismatches = []
    for field, (actual, expected) in protocol_fields.items():
        if isinstance(expected, int):
            actual = integer(actual, f"{dataset} repeat {repeat} {field}")
        if actual != expected:
            mismatches.append(f"{field}={actual!r}, expected {expected!r}")
    if mismatches:
        raise ValueError(
            f"{dataset}: repeat {repeat} JSON protocol mismatch: "
            + "; ".join(mismatches)
        )
    stages = {field: number(timings.get(field), field) for field in STAGE_FIELDS}
    if any(value < 0 for value in stages.values()):
        raise ValueError(f"{dataset}: negative build stage")
    build_ms = number(timings.get("lavd_build_multi"), "lavd build time")
    resident_ms = number(timings.get("crane_build_multi"), "resident build time")
    stage_sum = sum(stages.values())
    if (
        build_ms <= 0
        or resident_ms <= 0
        or build_ms + 1e-6 < stage_sum
        or build_ms - stage_sum > max(5_000.0, 0.05 * build_ms)
    ):
        raise ValueError(f"{dataset}: inconsistent build timing")
    accounting = parse_accounting(err_path, dataset, repeat)
    registered = integer(
        payload.get("lavd_region_registered_bytes_total"), "registered bytes"
    )
    if registered != accounting["registered_bytes"]:
        raise ValueError(f"{dataset}: JSON/stderr registered-byte mismatch")
    return {
        "dataset": dataset,
        "repeat": repeat,
        "canonical_ef": ef,
        "build_ms": build_ms,
        "resident_build_ms": resident_ms,
        **stages,
        "registered_bytes": registered,
        "materialized_bytes": accounting["materialized_bytes"],
        "actual_write_bytes": accounting["actual_write_bytes"],
        "offset_table_bytes": accounting["offset_table_bytes"],
        "record_bytes": accounting["record_bytes"],
    }


def parse_run(
    root: Path,
    dataset: str,
    repeat: int,
    expected_sha: str,
) -> dict[str, Any]:
    spec = DATASET_SPECS[dataset]
    source_dataset = str(spec["source_dataset"])
    ef = int(spec["ef"])
    stem = f"{source_dataset}_slabwalk_expansion_r{repeat}_measure_T10_ef{ef}"
    run_dir = root / f"sw_r{repeat}"
    json_path = run_dir / f"{stem}.json"
    err_path = run_dir / f"{stem}.err"
    csv_path = run_dir / "slabwalk_shine_frontier_raw.csv"
    if not json_path.is_file():
        raise ValueError(f"{dataset}: repeat {repeat} selected JSON is missing")
    if not err_path.is_file():
        raise ValueError(f"{dataset}: repeat {repeat} selected stderr is missing")
    select_frontier_row(
        csv_path,
        dataset,
        source_dataset,
        repeat,
        ef,
        expected_sha,
        json_path,
        err_path,
    )
    parsed = parse_measurement(json_path, err_path, dataset, repeat)
    return {
        **parsed,
        "_json": json_path,
        "_stderr": err_path,
        "_csv": csv_path,
    }


def ci95(values: list[float]) -> float:
    if len(values) != 5:
        raise ValueError("95% interval requires five repeats")
    return T_CRITICAL_95_DF4 * statistics.stdev(values) / math.sqrt(len(values))


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for dataset in DATASET_SPECS:
        current = [row for row in rows if row["dataset"] == dataset]
        if [row["repeat"] for row in current] != list(EXPECTED_REPEATS):
            raise ValueError(f"{dataset}: repeat matrix mismatch")
        builds = [float(row["build_ms"]) for row in current]
        resident = [float(row["resident_build_ms"]) for row in current]
        summary = {
            "dataset": dataset,
            "n": 5,
            "canonical_ef": DATASET_SPECS[dataset]["ef"],
            "build_mean_s": statistics.mean(builds) / 1000.0,
            "build_ci95_half_s": ci95(builds) / 1000.0,
            "resident_build_mean_s": statistics.mean(resident) / 1000.0,
            "registered_mean_gib": statistics.mean(
                float(row["registered_bytes"]) for row in current
            )
            / 1024**3,
            "materialized_mean_gib": statistics.mean(
                float(row["materialized_bytes"]) for row in current
            )
            / 1024**3,
        }
        for field in STAGE_FIELDS:
            summary[f"{field}_share_pct"] = statistics.mean(
                100.0 * float(row[field]) / float(row["build_ms"])
                for row in current
            )
        output.append(summary)
    return output


def write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...] | None = None) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    fieldnames = list(fields or tuple(rows[0]))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_sha256s(root: Path) -> None:
    output = root / "SHA256SUMS"
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != output)
    output.write_text(
        "".join(f"{sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in paths)
    )


def assemble(
    *,
    deep_campaign: Path,
    text_sift_campaign: Path,
    out_dir: Path,
    expected_binary_sha: str,
) -> None:
    require_sha(expected_binary_sha, "expected binary SHA")
    if out_dir.exists():
        raise ValueError(f"output already exists: {out_dir}")
    roots = {"deep": deep_campaign, "text_sift": text_sift_campaign}
    campaigns = {
        "deep": load_campaign(deep_campaign, expected_binary_sha, {"DEEP10M"}),
        "text_sift": load_campaign(
            text_sift_campaign, expected_binary_sha, {"TEXT10M", "SIFT10M"}
        ),
    }
    parsed = []
    for dataset, spec in DATASET_SPECS.items():
        root = roots[str(spec["campaign"])]
        for repeat in EXPECTED_REPEATS:
            parsed.append(parse_run(root, dataset, repeat, expected_binary_sha))
    parsed.sort(key=lambda row: (str(row["dataset"]), int(row["repeat"])))

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        source_records = []
        run_rows = []
        for row in parsed:
            dataset = str(row["dataset"])
            repeat = int(row["repeat"])
            retained_dir = staging / "raw" / dataset / f"r{repeat}"
            retained_dir.mkdir(parents=True)
            retained = {}
            for key, name in (
                ("_json", "measurement.json"),
                ("_stderr", "measurement.err"),
                ("_csv", "frontier.csv"),
            ):
                source = Path(row[key])
                destination = retained_dir / name
                shutil.copy2(source, destination)
                retained[key] = destination.relative_to(staging).as_posix()
            output_row = {key: row[key] for key in RUN_FIELDS if not key.startswith("source_")}
            output_row.update(
                {
                    "source_json": retained["_json"],
                    "source_json_sha256": sha256(staging / retained["_json"]),
                    "source_stderr": retained["_stderr"],
                    "source_stderr_sha256": sha256(staging / retained["_stderr"]),
                    "source_frontier_csv": retained["_csv"],
                    "source_frontier_csv_sha256": sha256(staging / retained["_csv"]),
                }
            )
            run_rows.append(output_row)
            for kind, key in (
                ("json", "_json"),
                ("stderr", "_stderr"),
                ("frontier_csv", "_csv"),
            ):
                source_records.append(
                    {
                        "dataset": dataset,
                        "repeat": repeat,
                        "kind": kind,
                        "source": str(Path(row[key]).resolve()),
                        "retained": retained[key],
                        "sha256": sha256(staging / retained[key]),
                    }
                )

        manifest_records = []
        for name, root in roots.items():
            destination = staging / "provenance" / name / "campaign.json"
            destination.parent.mkdir(parents=True)
            shutil.copy2(root / "campaign.json", destination)
            manifest_records.append(
                {
                    "name": name,
                    "source_root": str(root.resolve()),
                    "campaign_id": campaigns[name].get("campaign_id", ""),
                    "retained_manifest": destination.relative_to(staging).as_posix(),
                    "retained_manifest_sha256": sha256(destination),
                }
            )

        summary_rows = summarize(parsed)
        write_csv(staging / "runs.csv", run_rows, RUN_FIELDS)
        write_csv(staging / "summary.csv", summary_rows)
        assembler_path = Path(__file__).resolve()
        provenance = {
            "kind": "vldb_10m_build_scaling_provenance_v1",
            "assembled_utc": datetime.now(timezone.utc).isoformat(),
            "assembler_path": str(assembler_path),
            "assembler_sha256": sha256(assembler_path),
            "source_campaigns": manifest_records,
            "retained_sources": source_records,
            "selection_rule": {
                dataset: {"ef": spec["ef"], "repeats": list(EXPECTED_REPEATS)}
                for dataset, spec in DATASET_SPECS.items()
            },
        }
        (staging / "PROVENANCE.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        bundle = {
            "kind": "vldb_10m_build_scaling_bundle_v1",
            "binary_sha256": expected_binary_sha,
            "datasets": list(DATASET_SPECS),
            "repeats": 5,
            "measurement": "one_canonical_frontier_startup_per_dataset_repeat",
            "assembler_sha256": sha256(assembler_path),
            "provenance_sha256": sha256(staging / "PROVENANCE.json"),
        }
        (staging / "campaign.json").write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n"
        )
        (staging / "README.md").write_text(
            "# SlabWalk 10M frontier-startup derivation cost\n\n"
            "Each dataset contributes one fixed-ef Slab startup from each of five "
            "formal frontier repeats.  Selecting one process per repeat avoids "
            "pseudo-replicating the identical derivation across search widths.  "
            "These packed-variable frontier startups are reported separately "
            "from the 1M fixed-layout build-cost experiment.\n"
        )
        write_sha256s(staging)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_bundle(root: Path, expected_binary_sha: str) -> dict[str, int]:
    require_sha(expected_binary_sha, "expected binary SHA")
    campaign = read_json(root / "campaign.json", "10M build bundle manifest")
    assembler_sha = sha256(Path(__file__).resolve())
    if (
        campaign.get("kind") != "vldb_10m_build_scaling_bundle_v1"
        or campaign.get("binary_sha256") != expected_binary_sha
        or campaign.get("datasets") != list(DATASET_SPECS)
        or integer(campaign.get("repeats"), "bundle repeats") != 5
        or campaign.get("measurement")
        != "one_canonical_frontier_startup_per_dataset_repeat"
    ):
        raise ValueError("10M build bundle manifest mismatch")
    if campaign.get("assembler_sha256") != assembler_sha:
        raise ValueError("10M build bundle assembler SHA mismatch")
    provenance_path = root / "PROVENANCE.json"
    provenance_sha = str(campaign.get("provenance_sha256", ""))
    require_sha(provenance_sha, "bundle provenance SHA")
    if not provenance_path.is_file() or sha256(provenance_path) != provenance_sha:
        raise ValueError("10M build bundle provenance SHA mismatch")
    runs = read_csv(root / "runs.csv")
    if len(runs) != 15:
        raise ValueError("10M build bundle requires 15 runs")
    by_dataset: dict[str, list[dict[str, str]]] = {}
    retained = 0
    for row in runs:
        dataset = row.get("dataset", "")
        if dataset not in DATASET_SPECS:
            raise ValueError("unexpected 10M build dataset")
        by_dataset.setdefault(dataset, []).append(row)
        for path_field, sha_field in (
            ("source_json", "source_json_sha256"),
            ("source_stderr", "source_stderr_sha256"),
            ("source_frontier_csv", "source_frontier_csv_sha256"),
        ):
            relative = Path(row[path_field])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("non-portable retained source path")
            path = root / relative
            require_sha(row[sha_field], sha_field)
            if not path.is_file() or sha256(path) != row[sha_field]:
                raise ValueError(f"retained source mismatch: {path}")
            retained += 1

        repeat = integer(row.get("repeat"), "repeat")
        ef = integer(row.get("canonical_ef"), "canonical ef")
        spec = DATASET_SPECS[dataset]
        source_dataset = str(spec["source_dataset"])
        stem = f"{source_dataset}_slabwalk_expansion_r{repeat}_measure_T10_ef{ef}"
        json_path = root / row["source_json"]
        err_path = root / row["source_stderr"]
        csv_path = root / row["source_frontier_csv"]
        select_frontier_row(
            csv_path,
            dataset,
            source_dataset,
            repeat,
            ef,
            expected_binary_sha,
            json_path,
            err_path,
            expected_json_name=f"{stem}.json",
            expected_err_name=f"{stem}.err",
        )
        measured = parse_measurement(json_path, err_path, dataset, repeat)
        for field, expected in measured.items():
            actual = row.get(field)
            if isinstance(expected, str):
                matches = actual == expected
            elif isinstance(expected, int):
                matches = integer(actual, f"retained {field}") == expected
            else:
                matches = math.isclose(
                    number(actual, f"retained {field}"),
                    float(expected),
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                )
            if not matches:
                raise ValueError(
                    f"{dataset}: retained measurement mismatch for {field}"
                )
    for dataset, rows in by_dataset.items():
        rows.sort(key=lambda row: integer(row["repeat"], "repeat"))
        if [integer(row["repeat"], "repeat") for row in rows] != list(EXPECTED_REPEATS):
            raise ValueError(f"{dataset}: retained repeat matrix mismatch")
        if any(integer(row["canonical_ef"], "canonical ef") != DATASET_SPECS[dataset]["ef"] for row in rows):
            raise ValueError(f"{dataset}: canonical ef mismatch")
        if any(number(row["build_ms"], "build time") <= 0 for row in rows):
            raise ValueError(f"{dataset}: invalid retained build time")
    if set(by_dataset) != set(DATASET_SPECS):
        raise ValueError("10M build dataset matrix mismatch")

    summaries = read_csv(root / "summary.csv")
    if len(summaries) != 3 or {row.get("dataset") for row in summaries} != set(DATASET_SPECS):
        raise ValueError("10M build summary matrix mismatch")
    expected_summary = {row["dataset"]: row for row in summarize([
        {
            key: (
                source[key]
                if key == "dataset"
                else integer(source[key], key)
                if key in {"repeat", "canonical_ef"}
                else number(source[key], key)
            )
            for key in (
                "dataset",
                "repeat",
                "canonical_ef",
                "build_ms",
                "resident_build_ms",
                *STAGE_FIELDS,
                "registered_bytes",
                "materialized_bytes",
            )
        }
        for source in runs
    ])}
    for row in summaries:
        dataset = row["dataset"]
        if integer(row["n"], "summary n") != 5:
            raise ValueError(f"{dataset}: summary repeat mismatch")
        for field, expected in expected_summary[dataset].items():
            if field == "dataset":
                continue
            actual = number(row[field], f"summary {field}")
            if not math.isclose(actual, float(expected), rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"{dataset}: summary mismatch for {field}")

    provenance = read_json(provenance_path, "10M build provenance")
    if (
        provenance.get("kind") != "vldb_10m_build_scaling_provenance_v1"
        or len(provenance.get("retained_sources", [])) != 45
        or len(provenance.get("source_campaigns", [])) != 2
    ):
        raise ValueError("10M build provenance mismatch")
    if provenance.get("assembler_sha256") != assembler_sha:
        raise ValueError("10M build provenance assembler SHA mismatch")
    expected_selection = {
        dataset: {"ef": spec["ef"], "repeats": list(EXPECTED_REPEATS)}
        for dataset, spec in DATASET_SPECS.items()
    }
    if provenance.get("selection_rule") != expected_selection:
        raise ValueError("10M build provenance selection rule mismatch")

    expected_sources = {
        (
            row["dataset"],
            integer(row["repeat"], "repeat"),
            kind,
        ): (row[path_field], row[sha_field])
        for row in runs
        for kind, path_field, sha_field in (
            ("json", "source_json", "source_json_sha256"),
            ("stderr", "source_stderr", "source_stderr_sha256"),
            (
                "frontier_csv",
                "source_frontier_csv",
                "source_frontier_csv_sha256",
            ),
        )
    }
    provenance_sources = {}
    for record in provenance["retained_sources"]:
        if not isinstance(record, dict):
            raise ValueError("10M build retained-source provenance mismatch")
        key = (
            str(record.get("dataset", "")),
            integer(record.get("repeat"), "provenance repeat"),
            str(record.get("kind", "")),
        )
        value = (str(record.get("retained", "")), str(record.get("sha256", "")))
        if key in provenance_sources:
            raise ValueError("duplicate retained-source provenance record")
        provenance_sources[key] = value
    if provenance_sources != expected_sources:
        raise ValueError("10M build retained-source provenance mismatch")

    source_campaigns = provenance["source_campaigns"]
    expected_campaign_names = {"deep", "text_sift"}
    if {
        str(record.get("name", ""))
        for record in source_campaigns
        if isinstance(record, dict)
    } != expected_campaign_names:
        raise ValueError("10M build source-campaign provenance mismatch")
    for record in source_campaigns:
        if not isinstance(record, dict):
            raise ValueError("10M build source-campaign provenance mismatch")
        relative = Path(str(record.get("retained_manifest", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("non-portable retained campaign path")
        retained_campaign = root / relative
        retained_sha = str(record.get("retained_manifest_sha256", ""))
        require_sha(retained_sha, "retained campaign SHA")
        if not retained_campaign.is_file() or sha256(retained_campaign) != retained_sha:
            raise ValueError("retained campaign provenance mismatch")
        retained_payload = read_json(retained_campaign, "retained campaign manifest")
        if retained_payload.get("campaign_id", "") != record.get("campaign_id", ""):
            raise ValueError("retained campaign ID mismatch")
        expected_datasets = (
            {"DEEP10M"}
            if record.get("name") == "deep"
            else {"TEXT10M", "SIFT10M"}
        )
        load_campaign(retained_campaign.parent, expected_binary_sha, expected_datasets)

    retained_source_paths = {path for path, _ in expected_sources.values()}
    retained_campaign_paths = {
        str(record["retained_manifest"]) for record in source_campaigns
    }
    if len(retained_source_paths) != 45 or len(retained_campaign_paths) != 2:
        raise ValueError("10M build retained inventory has duplicate paths")
    allowed_paths = {
        "README.md",
        "PROVENANCE.json",
        "campaign.json",
        "runs.csv",
        "summary.csv",
        *retained_source_paths,
        *retained_campaign_paths,
    }
    manifest = root / "SHA256SUMS"
    expected_paths = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest
    )
    if set(expected_paths) != allowed_paths:
        raise ValueError("10M build signed-file inventory mismatch")
    recorded = {}
    for line in manifest.read_text().splitlines():
        digest, relative = line.split("  ", 1)
        require_sha(digest, "SHA256SUMS digest")
        recorded[relative] = digest
    if sorted(recorded) != expected_paths:
        raise ValueError("10M build SHA256SUMS inventory mismatch")
    for relative, digest in recorded.items():
        if sha256(root / relative) != digest:
            raise ValueError(f"10M build checksum mismatch: {relative}")
    return {"runs": 15, "datasets": 3, "retained_sources": retained}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep-campaign", type=Path, required=True)
    parser.add_argument("--text-sift-campaign", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--expected-binary-sha", required=True)
    args = parser.parse_args()
    assemble(
        deep_campaign=args.deep_campaign,
        text_sift_campaign=args.text_sift_campaign,
        out_dir=args.out_dir,
        expected_binary_sha=args.expected_binary_sha,
    )
    report = validate_bundle(args.out_dir, args.expected_binary_sha)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
