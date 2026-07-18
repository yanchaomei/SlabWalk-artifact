#!/usr/bin/env python3
"""Validate and summarize staged Slab builder worker scaling."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

try:
    from . import vldb_evidence_bundle as evidence_bundle
except ImportError:
    import vldb_evidence_bundle as evidence_bundle


INT_FIELDS = {
    "repeat",
    "position",
    "requested_bytes",
    "fixed_bytes",
    "record_bytes",
    "admitted_bytes",
    "physical_bytes",
    "unused_bytes",
    "selected_records",
    "total_records",
    "selection_hash",
    "result_hash_version",
    "result_hash",
    "physical_hash_version",
    "budget_map_owner_mn",
    "total_benefit",
    "build_workers",
    "rank_workers",
    "rank_workers_recorded",
    "staging_bytes",
    "record_write_posts",
    "processed",
}

FLOAT_FIELDS = {
    "qps",
    "recall",
    "p50_us",
    "p95_us",
    "p99_us",
    "posts_per_query",
    "bytes_per_query",
    "build_total_ms",
    "build_rank_ms",
    "build_materialize_ms",
    "build_record_assemble_ms",
    "build_record_publish_ms",
}

SUMMARY_METRICS = (
    "build_total_ms",
    "build_rank_ms",
    "build_materialize_ms",
    "build_record_assemble_ms",
    "build_record_publish_ms",
    "record_write_posts",
    "qps",
    "recall",
    "posts_per_query",
    "bytes_per_query",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _finite(value: Any, name: str, *, positive: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"expected finite {name}")
    if positive and number <= 0.0:
        raise ValueError(f"expected positive {name}")
    if not positive and number < 0.0:
        raise ValueError(f"expected non-negative {name}")
    return number


def _convert_row(row: dict[str, str]) -> dict[str, Any]:
    converted: dict[str, Any] = dict(row)
    for field in INT_FIELDS:
        if field in converted:
            converted[field] = int(converted[field])
    for field in FLOAT_FIELDS:
        if field in converted:
            converted[field] = _finite(
                converted[field], field, positive=(field == "qps")
            )
    return converted


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


def _single(values: Iterable[Any], label: str) -> Any:
    distinct = set(values)
    if len(distinct) != 1:
        raise ValueError(f"{label} drift across builder cells")
    return distinct.pop()


def _resolve_child(index_path: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        raise ValueError("child evidence path escapes campaign root")
    campaign_root = index_path.parent.resolve()
    child = (campaign_root / path).resolve()
    try:
        child.relative_to(campaign_root)
    except ValueError as error:
        raise ValueError("child evidence path escapes campaign root") from error
    return child


def _verify_sha256_manifest(manifest: Path) -> None:
    root = manifest.parent.resolve()
    seal = (root / "SEALED.json").resolve()
    if not manifest.is_file():
        raise ValueError(f"missing child SHA256SUMS under {root}")
    expected: dict[Path, str] = {}
    for line_number, line in enumerate(manifest.read_text().splitlines(), start=1):
        if "  " not in line:
            raise ValueError(f"malformed child SHA256SUMS line {line_number}")
        digest, raw_path = line.split("  ", 1)
        if not SHA256_RE.fullmatch(digest) or not raw_path:
            raise ValueError(f"malformed child SHA256SUMS line {line_number}")
        relative = Path(raw_path)
        if relative.is_absolute():
            raise ValueError("child SHA256SUMS path escapes child root")
        artifact = (root / relative).resolve()
        try:
            artifact.relative_to(root)
        except ValueError as error:
            raise ValueError("child SHA256SUMS path escapes child root") from error
        if artifact in expected:
            raise ValueError("duplicate child SHA256SUMS entry")
        expected[artifact] = digest
    actual = {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and path.resolve() != manifest.resolve()
        and path.resolve() != seal
    }
    if set(expected) != actual:
        raise ValueError("child SHA256SUMS does not cover the complete child tree")
    for artifact, digest in expected.items():
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError(f"child SHA256SUMS mismatch: {artifact}")


def _path_within(root: Path, raw: str, label: str) -> Path:
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label} path escapes child evidence") from error
    return resolved


def _relocated_row_artifact(
    child: Path, raw: str, artifact: Path, label: str
) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        return _path_within(child, raw, label)
    try:
        return _path_within(child, raw, label)
    except ValueError:
        relative = artifact.resolve().relative_to(child.resolve())
        if len(path.parts) >= len(relative.parts) and tuple(
            path.parts[-len(relative.parts) :]
        ) == relative.parts:
            return artifact.resolve()
        raise ValueError(f"{label} path does not identify its sealed artifact")


def _verify_child_evidence(
    child: Path,
    row: dict[str, Any],
    expected_sha: str,
    expected_compute_host: str,
) -> None:
    cells = [
        path
        for path in child.glob("raw/**/campaign.json")
        if path.parent != child
    ]
    measured = []
    for campaign_path in cells:
        campaign = json.loads(campaign_path.read_text())
        if campaign.get("kind") == "r":
            measured.append((campaign_path, campaign))
    if len(measured) != 1:
        raise ValueError(f"child must contain one measured provenance cell: {child}")
    campaign_path, campaign = measured[0]
    if int(campaign.get("schema_version", -1)) != 2:
        raise ValueError("unsupported child provenance schema")
    executables = campaign.get("executables", {})
    compute_node = executables.get("compute_node", {})
    if (
        compute_node.get("sha256") != expected_sha
        or executables.get("memory_node", {}).get("sha256") != expected_sha
    ):
        raise ValueError("child executable provenance mismatch")
    if str(compute_node.get("host", "")).strip() != expected_compute_host:
        raise ValueError("build-pipeline compute host drift in child provenance")
    if (
        str(campaign.get("dataset")) != str(row["dataset"])
        or int(campaign.get("budget_bytes", -1)) != int(row["requested_bytes"])
        or str(campaign.get("policy")) != str(row["policy"])
        or str(campaign.get("input_signature")) != str(row["input_signature"])
    ):
        raise ValueError("child cell provenance does not match runs row")
    artifacts = campaign.get("artifacts", {})
    expected_artifacts = {
        "compute_stdout": "result_json",
        "compute_stderr": "stderr",
        "memory_node_stdout": None,
        "memory_node_stderr": None,
    }
    cell_root = campaign_path.parent
    for artifact_name, row_field in expected_artifacts.items():
        record = artifacts.get(artifact_name, {})
        artifact = _path_within(cell_root, str(record.get("path", "")), artifact_name)
        digest = str(record.get("sha256", ""))
        if not SHA256_RE.fullmatch(digest) or not artifact.is_file():
            raise ValueError(f"missing child artifact provenance: {artifact_name}")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError(f"child artifact hash mismatch: {artifact_name}")
        if row_field is not None:
            row_path = _relocated_row_artifact(
                child, str(row[row_field]), artifact, row_field
            )
            if row_path != artifact:
                raise ValueError(f"runs row {row_field} does not reference its sealed artifact")


def _load_index(index_path: Path) -> list[dict[str, Any]]:
    with index_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("empty builder cell index")
    converted = []
    for row in rows:
        repeat_key = "outer_repeat" if "outer_repeat" in row else "repeat"
        position_key = "outer_position" if "outer_position" in row else "position"
        converted.append(
            {
                "outer_repeat": int(row[repeat_key]),
                "outer_position": int(row[position_key]),
                "build_threads": int(row["build_threads"]),
                "child_dir": _resolve_child(index_path, row["child_dir"]),
                "status": row["status"],
            }
        )
    return converted


def summarize_campaign(
    index_path: Path,
    *,
    expected_threads: list[int],
    expected_repeats: int,
    expected_sha: str,
    expected_compute_host: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    expected_compute_host = expected_compute_host.strip()
    if not expected_compute_host:
        raise ValueError("build-pipeline compute host drift: missing expected host")
    if expected_threads != sorted(set(expected_threads)):
        raise ValueError("expected builder thread list must be unique and sorted")
    if not expected_threads or expected_threads[0] != 1:
        raise ValueError("builder scaling requires a one-worker reference")
    index_rows = _load_index(index_path)
    expected_cells = set()
    for repeat in range(expected_repeats):
        rotation = repeat % len(expected_threads)
        for position in range(len(expected_threads)):
            workers = expected_threads[(position + rotation) % len(expected_threads)]
            expected_cells.add((repeat, position, workers))
    actual_cells = {
        (row["outer_repeat"], row["outer_position"], row["build_threads"])
        for row in index_rows
    }
    if actual_cells != expected_cells or len(index_rows) != len(expected_cells):
        raise ValueError("incomplete, duplicate, or invalid rotated worker schedule")
    if any(row["status"] != "ok" for row in index_rows):
        raise ValueError("builder worker matrix contains a failed cell")

    runs: list[dict[str, Any]] = []
    for index_row in index_rows:
        child = index_row["child_dir"]
        _verify_sha256_manifest(child / "SHA256SUMS")
        campaign_path = child / "campaign.json"
        runs_path = child / "runs.csv"
        if not campaign_path.is_file() or not runs_path.is_file():
            raise ValueError(f"missing child evidence under {child}")
        campaign = json.loads(campaign_path.read_text())
        protocol = campaign.get("protocol", {})
        if (
            protocol.get("binary_sha256") != expected_sha
            or protocol.get("datasets") != ["DEEP1M"]
            or protocol.get("policies") != ["indeg"]
            or protocol.get("budget_bytes") != [536870912]
            or int(protocol.get("repeats", 0)) != 1
            or int(protocol.get("build_threads", 0))
            != index_row["build_threads"]
            or protocol.get("staged_build") is not True
        ):
            raise ValueError(f"child campaign protocol mismatch under {child}")
        if str(protocol.get("compute_host", "")).strip() != expected_compute_host:
            raise ValueError("build-pipeline compute host drift in child protocol")
        with runs_path.open(newline="") as handle:
            child_rows = list(csv.DictReader(handle))
        if len(child_rows) != 1:
            raise ValueError(f"child campaign must expose one measured row: {child}")
        row = _convert_row(child_rows[0])
        if (
            row.get("binary_sha256") != expected_sha
            or row.get("build_mode") != "staged"
            or row.get("build_workers") != index_row["build_threads"]
            or row.get("rank_workers_recorded") != 1
            or row.get("rank_workers") != index_row["build_threads"]
        ):
            raise ValueError(f"child run identity mismatch under {child}")
        if str(row.get("compute_host", "")).strip() != expected_compute_host:
            raise ValueError("build-pipeline compute host drift in measured row")
        _verify_child_evidence(
            child, row, expected_sha, expected_compute_host
        )
        row["outer_repeat"] = index_row["outer_repeat"]
        row["outer_position"] = index_row["outer_position"]
        row["child_dir"] = str(child)
        runs.append(row)

    runs.sort(
        key=lambda row: (
            int(row["outer_repeat"]),
            int(row["outer_position"]),
        )
    )
    identity = {
        "binary_sha256": _single(
            (row["binary_sha256"] for row in runs), "binary SHA"
        ),
        "input_signature": _single(
            (row["input_signature"] for row in runs), "input signature"
        ),
        "source_tree_sha256": _single(
            (row["source_tree_sha256"] for row in runs), "source-tree SHA"
        ),
        "compute_host": _single(
            (row["compute_host"] for row in runs), "compute host"
        ),
        "selection_hash": _single(
            (int(row["selection_hash"]) for row in runs), "selection hash"
        ),
        "result_hash_version": _single(
            (int(row["result_hash_version"]) for row in runs),
            "query-result hash version",
        ),
        "result_hash": _single(
            (int(row["result_hash"]) for row in runs), "query-result hash"
        ),
        "physical_hash_version": _single(
            (int(row["physical_hash_version"]) for row in runs),
            "physical hash version",
        ),
        "physical_hash_algorithm": _single(
            (str(row["physical_hash_algorithm"]) for row in runs),
            "physical hash algorithm",
        ),
        "physical_hash_scope": _single(
            (str(row["physical_hash_scope"]) for row in runs),
            "physical hash scope",
        ),
        "header_hash_scope": _single(
            (str(row["header_hash_scope"]) for row in runs),
            "header hash scope",
        ),
        "descriptor_hash_scope": _single(
            (str(row["descriptor_hash_scope"]) for row in runs),
            "descriptor hash scope",
        ),
        "map_hash_scope": _single(
            (str(row["map_hash_scope"]) for row in runs),
            "map hash scope",
        ),
        "offset_table_hash_scope": _single(
            (str(row["offset_table_hash_scope"]) for row in runs),
            "offset-table hash scope",
        ),
        "record_payload_hash_scope": _single(
            (str(row["record_payload_hash_scope"]) for row in runs),
            "record-payload hash scope",
        ),
        "selected_uid_hash_scope": _single(
            (str(row["selected_uid_hash_scope"]) for row in runs),
            "selected-UID hash scope",
        ),
        "budget_map_owner_mn": _single(
            (int(row["budget_map_owner_mn"]) for row in runs),
            "budget-map owner MN",
        ),
        "header_hash": _single(
            (str(row["header_hash"]) for row in runs), "header hash"
        ),
        "descriptor_hash": _single(
            (str(row["descriptor_hash"]) for row in runs), "descriptor hash"
        ),
        "map_hash": _single((str(row["map_hash"]) for row in runs), "map hash"),
        "offset_table_hashes": _single(
            (str(row["offset_table_hashes"]) for row in runs),
            "offset-table hashes",
        ),
        "record_payload_hashes": _single(
            (str(row["record_payload_hashes"]) for row in runs),
            "record-payload hashes",
        ),
        "selected_uid_hash": _single(
            (str(row["selected_uid_hash"]) for row in runs),
            "selected-UID hash",
        ),
        "physical_signature": _single(
            (str(row["physical_signature"]) for row in runs),
            "physical signature",
        ),
        "physical_bytes": _single(
            (int(row["physical_bytes"]) for row in runs), "physical bytes"
        ),
        "selected_records": _single(
            (int(row["selected_records"]) for row in runs), "selected records"
        ),
        "staging_bytes": _single(
            (int(row["staging_bytes"]) for row in runs), "staging bytes"
        ),
        "record_write_posts": _single(
            (int(row["record_write_posts"]) for row in runs),
            "record write posts",
        ),
        "recall": _single((float(row["recall"]) for row in runs), "recall"),
        "posts_per_query": _single(
            (float(row["posts_per_query"]) for row in runs), "posts/query"
        ),
        "bytes_per_query": _single(
            (float(row["bytes_per_query"]) for row in runs), "bytes/query"
        ),
    }

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in runs:
        grouped[int(row["build_workers"])].append(row)
    summaries: list[dict[str, Any]] = []
    for workers in expected_threads:
        cells = grouped[workers]
        if len(cells) != expected_repeats:
            raise ValueError(f"builder worker {workers} has incomplete repeats")
        record: dict[str, Any] = {
            "build_workers": workers,
            "rank_workers": _single(
                (int(cell["rank_workers"]) for cell in cells),
                f"rank workers at builder worker {workers}",
            ),
            "n": len(cells),
            **identity,
        }
        for metric in SUMMARY_METRICS:
            values = [
                _finite(cell[metric], metric, positive=(metric == "qps"))
                for cell in cells
            ]
            record[f"{metric}_mean"] = statistics.mean(values)
            record[f"{metric}_ci95"] = _ci95(values)
        summaries.append(record)

    baseline = next(row for row in summaries if row["build_workers"] == 1)
    for row in summaries:
        row["build_total_speedup_vs_t1"] = (
            baseline["build_total_ms_mean"] / row["build_total_ms_mean"]
        )
        row["build_materialize_speedup_vs_t1"] = (
            baseline["build_materialize_ms_mean"]
            / row["build_materialize_ms_mean"]
        )
        row["materialize_parallel_efficiency"] = (
            row["build_materialize_speedup_vs_t1"] / row["build_workers"]
        )

    best_materialize = min(
        summaries, key=lambda row: row["build_materialize_ms_mean"]
    )
    best_total = min(summaries, key=lambda row: row["build_total_ms_mean"])
    comparison = {
        "kind": "vldb_staged_build_worker_scaling_v1",
        "identity": identity,
        "expected_repeats": expected_repeats,
        "build_workers": expected_threads,
        "best_materialize": {
            "build_workers": best_materialize["build_workers"],
            "mean_ms": best_materialize["build_materialize_ms_mean"],
            "speedup_vs_t1": best_materialize[
                "build_materialize_speedup_vs_t1"
            ],
        },
        "best_total": {
            "build_workers": best_total["build_workers"],
            "mean_ms": best_total["build_total_ms_mean"],
            "speedup_vs_t1": best_total["build_total_speedup_vs_t1"],
        },
    }
    return runs, summaries, comparison


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing empty output: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not rows:
        raise ValueError("refusing empty semantic comparison")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return list(csv.DictReader(buffer))


def _read_csv_rows(path: Path, label: str) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing sealed {label}: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty sealed {label}: {path}")
    return rows


def _without_child_paths(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {key: value for key, value in row.items() if key != "child_dir"}
        for row in rows
    ]


def _derived_float_field(field: str) -> bool:
    return field.endswith(
        ("_mean", "_ci95", "_speedup_vs_t1", "_parallel_efficiency")
    )


def _semantic_summary_equal(
    stored: list[dict[str, str]], recomputed: list[dict[str, str]]
) -> bool:
    if len(stored) != len(recomputed):
        return False
    for stored_row, recomputed_row in zip(stored, recomputed):
        if stored_row.keys() != recomputed_row.keys():
            return False
        for field, stored_value in stored_row.items():
            recomputed_value = recomputed_row[field]
            if stored_value == recomputed_value:
                continue
            if not _derived_float_field(field):
                return False
            try:
                stored_number = float(stored_value)
                recomputed_number = float(recomputed_value)
            except ValueError:
                return False
            if not math.isfinite(stored_number) or not math.isfinite(
                recomputed_number
            ):
                return False
            tolerance = 4.0 * max(
                math.ulp(stored_number), math.ulp(recomputed_number)
            )
            if abs(stored_number - recomputed_number) > tolerance:
                return False
    return True


def validate_bundle(
    root: Path,
    *,
    expected_sha: str,
    expected_compute_host: str,
) -> dict[str, Any]:
    """Recompute a sealed builder campaign after it has been relocated."""

    root = root.resolve()
    evidence_bundle.verify_bundle(root)
    campaign_path = root / "campaign.json"
    campaign = json.loads(campaign_path.read_text())
    protocol = campaign.get("protocol", {})
    worker_counts = protocol.get("build_threads")
    if (
        not isinstance(worker_counts, list)
        or not worker_counts
        or any(not isinstance(value, int) for value in worker_counts)
    ):
        raise ValueError("sealed builder protocol has invalid worker counts")
    repeats = int(protocol.get("repeats", 0))
    campaign_kind = str(protocol.get("campaign_kind", ""))
    compute_host = str(protocol.get("compute_host", "")).strip()
    if protocol.get("binary_sha256") != expected_sha:
        raise ValueError("sealed builder protocol binary SHA drift")
    if compute_host != expected_compute_host.strip():
        raise ValueError("sealed builder protocol compute host drift")
    if (
        protocol.get("dataset") != "DEEP1M"
        or protocol.get("policy") != "indeg"
        or int(protocol.get("budget_bytes", -1)) != 536870912
        or protocol.get("staged_build") is not True
        or campaign_kind not in {"formal", "smoke"}
        or repeats <= 0
    ):
        raise ValueError("sealed builder protocol is unsupported")
    if campaign_kind == "formal" and repeats % len(worker_counts) != 0:
        raise ValueError("sealed formal builder protocol is not position-balanced")

    runs, summaries, comparison = summarize_campaign(
        root / "cell_index.csv",
        expected_threads=worker_counts,
        expected_repeats=repeats,
        expected_sha=expected_sha,
        expected_compute_host=expected_compute_host,
    )
    stored_runs = _read_csv_rows(root / "runs.csv", "builder runs")
    recomputed_runs = _csv_rows(runs)
    if _without_child_paths(stored_runs) != _without_child_paths(recomputed_runs):
        raise ValueError("semantic runs mismatch in sealed builder bundle")
    stored_summary = _read_csv_rows(root / "summary.csv", "builder summary")
    if not _semantic_summary_equal(stored_summary, _csv_rows(summaries)):
        raise ValueError("semantic summary mismatch in sealed builder bundle")
    stored_comparison = json.loads((root / "comparison.json").read_text())
    if stored_comparison != comparison:
        raise ValueError("semantic comparison mismatch in sealed builder bundle")

    return {
        "kind": "vldb_staged_build_worker_scaling_validation_v1",
        "campaign_id": str(campaign.get("campaign_id", "")),
        "campaign_kind": campaign_kind,
        "binary_sha256": expected_sha,
        "source_tree_sha256": comparison["identity"]["source_tree_sha256"],
        "compute_host": compute_host,
        "worker_counts": worker_counts,
        "repeats": repeats,
        "measured_cells": len(runs),
        "summary_sha256": hashlib.sha256(
            (root / "summary.csv").read_bytes()
        ).hexdigest(),
        "comparison_sha256": hashlib.sha256(
            (root / "comparison.json").read_bytes()
        ).hexdigest(),
    }


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        parser = argparse.ArgumentParser(
            description="Semantically revalidate a relocated sealed builder bundle."
        )
        parser.add_argument("--bundle", type=Path, required=True)
        parser.add_argument("--expected-sha", required=True)
        parser.add_argument("--expected-compute-host", required=True)
        args = parser.parse_args(sys.argv[2:])
        report = validate_bundle(
            args.bundle,
            expected_sha=args.expected_sha,
            expected_compute_host=args.expected_compute_host,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--threads", nargs="+", type=int, required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-compute-host", required=True)
    parser.add_argument("--out-runs", type=Path, required=True)
    parser.add_argument("--out-summary", type=Path, required=True)
    parser.add_argument("--out-comparison", type=Path, required=True)
    args = parser.parse_args()
    runs, summaries, comparison = summarize_campaign(
        args.index,
        expected_threads=args.threads,
        expected_repeats=args.repeats,
        expected_sha=args.expected_sha,
        expected_compute_host=args.expected_compute_host,
    )
    _write_csv(args.out_runs, runs)
    _write_csv(args.out_summary, summaries)
    args.out_comparison.write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
