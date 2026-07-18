#!/usr/bin/env python3
"""Assemble a self-contained three-system worker-scaling evidence bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

try:
    from . import validate_vldb_final_evidence as evidence
except ImportError:
    import validate_vldb_final_evidence as evidence


def method_slug(method: str) -> str:
    return method.lower().replace("-", "").replace(" ", "")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protocol_sha(record: dict[str, object]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing input CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty input CSV: {path}")
    return rows


def integer(row: dict[str, str], key: str, source: Path) -> int:
    try:
        value = float(row.get(key, ""))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {key}") from exc
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError(f"{source}: invalid {key}")
    return int(value)


def number(row: dict[str, str], key: str, source: Path) -> float:
    try:
        value = float(row.get(key, ""))
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {key}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{source}: invalid {key}")
    return value


def write_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


CAMPAIGN_AUDIT_FILES = (
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


def copy_campaign_provenance(
    campaign_root: Path, destination: Path, campaign_id: str
) -> dict[str, object]:
    campaign_root = campaign_root.resolve()
    final_manifest = campaign_root / "campaign.json"
    if not final_manifest.is_file():
        raise ValueError(f"missing worker campaign manifest: {final_manifest}")
    campaign = json.loads(final_manifest.read_text())
    if campaign.get("campaign_id") != campaign_id:
        raise ValueError("worker campaign ID does not match assembler request")

    required = [campaign_root / name for name in CAMPAIGN_AUDIT_FILES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"missing worker campaign audit files: {missing}")

    failed_record_path = (
        campaign_root / "failed_run_archive_w40_r0_before-runner-fix.json"
    )
    failed_record = json.loads(failed_record_path.read_text())
    archive_relative = Path(str(failed_record.get("archive", "")))
    if (
        archive_relative.is_absolute()
        or ".." in archive_relative.parts
        or archive_relative.parts[:2] != ("failed_runs", "dhnsw")
    ):
        raise ValueError("invalid failed-run archive path")
    archive_root = campaign_root / archive_relative
    if not archive_root.is_dir():
        raise ValueError(f"missing failed-run archive tree: {archive_root}")
    archived_files = failed_record.get("files")
    if not isinstance(archived_files, list) or not archived_files:
        raise ValueError("failed-run archive has no file inventory")
    for item in archived_files:
        relative = Path(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid file path in failed-run archive")
        source = archive_root / relative
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size != int(item.get("size_bytes", -1))
            or file_sha256(source) != str(item.get("sha256", ""))
        ):
            raise ValueError(f"failed-run archive inventory mismatch: {source}")

    sources = required + sorted(path for path in archive_root.rglob("*") if path.is_file())
    records = []
    for source in sources:
        if source.is_symlink():
            raise ValueError(f"campaign provenance contains a symlink: {source}")
        relative = source.relative_to(campaign_root)
        target_relative = Path("campaign") / relative
        target = destination / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append(
            {
                "path": target_relative.as_posix(),
                "size_bytes": target.stat().st_size,
                "sha256": file_sha256(target),
            }
        )
    record = {
        "kind": "worker_scaling_campaign_provenance",
        "campaign_id": campaign_id,
        "files": records,
    }
    write_json(destination / "campaign_provenance.json", record)
    return record


def load_query_pools(directory: Path) -> dict[str, tuple[Path, dict[str, object]]]:
    pools: dict[str, tuple[Path, dict[str, object]]] = {}
    query_canonical = set()
    groundtruth_canonical = set()
    for method in evidence.WORKER_SCALING_METHODS:
        path = directory / f"deep1m_{method_slug(method)}.json"
        if not path.is_file():
            raise ValueError(f"missing worker-scaling query manifest: {path}")
        record = json.loads(path.read_text())
        query = record.get("query", {})
        groundtruth = record.get("groundtruth", {})
        if (
            record.get("kind") != "query_pool_fingerprint"
            or record.get("dataset") != "DEEP1M"
            or record.get("method") != method
            or record.get("metric") != "l2"
            or int(record.get("limit", 0)) != 10000
            or int(query.get("rows", 0)) != 10000
            or int(query.get("dim", 0)) != 96
            or int(groundtruth.get("rows", 0)) != 10000
            or int(groundtruth.get("k", 0)) < 10
        ):
            raise ValueError(f"invalid worker-scaling query manifest: {path}")
        for value, label in (
            (str(query.get("canonical_sha256", "")), "query canonical SHA"),
            (str(groundtruth.get("canonical_ids_sha256", "")), "GT canonical SHA"),
            (str(query.get("file_sha256", "")), "query file SHA"),
            (str(groundtruth.get("file_sha256", "")), "GT file SHA"),
        ):
            evidence.validate_sha(value, label)
        query_canonical.add(str(query["canonical_sha256"]))
        groundtruth_canonical.add(str(groundtruth["canonical_ids_sha256"]))
        pools[method] = path, record
    if len(query_canonical) != 1 or len(groundtruth_canonical) != 1:
        raise ValueError("worker-scaling query manifests do not describe one logical pool")
    return pools


def checked_sw_row(
    source: Path, method: str, workers: int, repeat: int
) -> tuple[dict[str, str], dict[str, object]]:
    matches = [
        row
        for row in read_csv(source)
        if row.get("dataset") == "DEEP1M"
        and row.get("method") == method
        and row.get("run_kind") == "measure"
        and row.get("run_id") == f"r{repeat}"
        and row.get("status") == "ok"
        and integer(row, "threads", source) == workers
        and integer(row, "ef", source) == 200
    ]
    if len(matches) != 1:
        raise ValueError(
            f"missing {method} input for workers={workers} repeat={repeat}: {source}"
        )
    row = matches[0]
    measurement_path = Path(row.get("json", ""))
    if not measurement_path.is_file():
        raise ValueError(f"missing {method} measurement JSON: {measurement_path}")
    measurement = json.loads(measurement_path.read_text())
    queries = measurement.get("queries", {})
    if (
        int(measurement.get("num_queries", 0)) != 10000
        or int(measurement.get("query_contexts", 0)) != workers
        or int(queries.get("processed", 0)) != 10000
    ):
        raise ValueError(f"invalid {method} fixed-pool measurement: {measurement_path}")
    for key, measured_key in (("qps", "queries_per_sec"), ("recall", "recall")):
        if not math.isclose(
            number(row, key, source),
            float(queries.get(measured_key, math.nan)),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError(f"{method} CSV/JSON {key} mismatch: {source}")
    stderr_path = Path(row.get("stderr", "")) if row.get("stderr", "") else None
    raw = {
        "kind": "worker_scaling_raw_source",
        "method": method,
        "workers": workers,
        "repeat": repeat,
        "input_csv": str(source),
        "input_csv_sha256": file_sha256(source),
        "parsed_row": row,
        "measurement_json": measurement,
        "stderr": (
            stderr_path.read_text(errors="replace")
            if stderr_path is not None and stderr_path.is_file()
            else ""
        ),
    }
    return row, raw


def checked_dhnsw_row(
    source: Path, workers: int, repeat: int
) -> tuple[dict[str, str], dict[str, object]]:
    rows = [
        row
        for row in read_csv(source)
        if row.get("dataset", "").lower() == "deep1m"
        and integer(row, "ef", source) == 200
        and row.get("status") == "ok"
        and integer(row, "threads", source) == workers
    ]
    if len(rows) != 1:
        raise ValueError(
            f"missing d-HNSW input for workers={workers} repeat={repeat}: {source}"
        )
    row = rows[0]
    client_log = source.parent / "deep1M_ef200_client.log"
    details = source.parent / "deep1M_ef200_benchmark_details.txt"
    if not client_log.is_file():
        raise ValueError(f"missing retained d-HNSW client log beside {source}")
    details_text = details.read_text(errors="replace") if details.is_file() else None
    raw = {
        "kind": "worker_scaling_raw_source",
        "method": "d-HNSW",
        "workers": workers,
        "repeat": repeat,
        "input_csv": str(source),
        "input_csv_sha256": file_sha256(source),
        "parsed_row": row,
        "client_log": client_log.read_text(errors="replace"),
        "detail_source": (
            "benchmark_details_and_client_log" if details_text is not None else "client_log"
        ),
        "benchmark_details": details_text,
    }
    return row, raw


def build_output_row(
    *,
    method: str,
    workers: int,
    repeat: int,
    input_row: dict[str, str],
    raw_source: dict[str, object],
    pool_record: dict[str, object],
    pool_relative: Path,
    pool_sha: str,
    destination: Path,
    campaign_id: str,
) -> dict[str, object]:
    query = pool_record["query"]
    groundtruth = pool_record["groundtruth"]
    is_dhnsw = method == "d-HNSW"
    binary = input_row["binary_sha256"]
    qps_key = "qps_recomputed" if is_dhnsw else "qps"
    processed_key = "processed_queries" if is_dhnsw else "processed"
    protocol = {
        "campaign_id": campaign_id,
        "binary_sha256": binary,
        "dataset": "DEEP1M",
        "method": method,
        "workers": workers,
        "query_contexts": None if is_dhnsw else workers,
        "coroutines": None if is_dhnsw else 2,
        "top_k": 10,
        "ef": 200,
        "metric": "l2",
        "measurement_mode": "fixed_query_pool",
        "expected_queries": 10000,
        "query_canonical_sha256": query["canonical_sha256"],
        "groundtruth_canonical_sha256": groundtruth["canonical_ids_sha256"],
    }
    retained_relative = (
        Path("raw_sources") / f"{method_slug(method)}_w{workers}_r{repeat}.json"
    )
    retained_path = destination / retained_relative
    write_json(retained_path, raw_source)
    return {
        "campaign_id": campaign_id,
        "protocol_fingerprint": protocol_sha(protocol),
        "binary_sha256": binary,
        "dataset": "DEEP1M",
        "method": method,
        "workers": workers,
        "repeat": repeat,
        "threads": workers,
        "query_contexts": "" if is_dhnsw else workers,
        "coroutines": "" if is_dhnsw else 2,
        "top_k": 10,
        "ef": 200,
        "metric": "l2",
        "measurement_mode": "fixed_query_pool",
        "status": "ok",
        "processed_queries": integer(input_row, processed_key, Path("input row")),
        "expected_queries": integer(input_row, "expected_queries", Path("input row")),
        "failed_queries": integer(input_row, "failed_queries", Path("input row")),
        "recall": number(input_row, "recall", Path("input row")),
        "qps": number(input_row, qps_key, Path("input row")),
        "query_canonical_sha256": query["canonical_sha256"],
        "groundtruth_canonical_sha256": groundtruth["canonical_ids_sha256"],
        "query_file_sha256": query["file_sha256"],
        "groundtruth_file_sha256": groundtruth["file_sha256"],
        "query_pool_manifest": pool_relative.as_posix(),
        "query_pool_manifest_sha256": pool_sha,
        "source": retained_relative.as_posix(),
        "source_sha256": file_sha256(retained_path),
    }


def assemble(
    raw_root: Path,
    query_pool_dir: Path,
    campaign_root: Path,
    out_dir: Path,
    *,
    campaign_id: str,
    expected_slabwalk_sha: str,
    repeats: int = 5,
) -> dict[str, object]:
    if repeats != 5:
        raise ValueError("the final worker-scaling gate requires five repeats")
    evidence.validate_sha(expected_slabwalk_sha, "expected SlabWalk SHA")
    if not campaign_id.strip():
        raise ValueError("campaign_id must be non-empty")
    if out_dir.exists():
        raise ValueError(f"refusing existing output directory: {out_dir}")
    pools = load_query_pools(query_pool_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{out_dir.name}.staging.", dir=out_dir.parent
    ) as temporary:
        staging = Path(temporary)
        copy_campaign_provenance(campaign_root, staging, campaign_id)
        copied_pools: dict[str, tuple[Path, str, dict[str, object]]] = {}
        for method, (source, record) in pools.items():
            relative = Path("query_pools") / source.name
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_pools[method] = relative, file_sha256(destination), record

        output_rows: list[dict[str, object]] = []
        for workers in evidence.WORKER_SCALING_WORKERS:
            for repeat in range(repeats):
                sw_csv = (
                    raw_root
                    / "sw"
                    / f"w{workers}"
                    / f"r{repeat}"
                    / "slabwalk_shine_frontier_raw.csv"
                )
                for method in ("SHINE", "SlabWalk"):
                    input_row, raw_source = checked_sw_row(
                        sw_csv, method, workers, repeat
                    )
                    relative, pool_sha, pool = copied_pools[method]
                    output_rows.append(
                        build_output_row(
                            method=method,
                            workers=workers,
                            repeat=repeat,
                            input_row=input_row,
                            raw_source=raw_source,
                            pool_record=pool,
                            pool_relative=relative,
                            pool_sha=pool_sha,
                            destination=staging,
                            campaign_id=campaign_id,
                        )
                    )

                dh_csv = (
                    raw_root
                    / "dhnsw"
                    / f"w{workers}"
                    / f"r{repeat}"
                    / "frontier.csv"
                )
                if not dh_csv.is_file():
                    raise ValueError(
                        f"missing d-HNSW input for workers={workers} repeat={repeat}: {dh_csv}"
                    )
                input_row, raw_source = checked_dhnsw_row(
                    dh_csv, workers, repeat
                )
                relative, pool_sha, pool = copied_pools["d-HNSW"]
                output_rows.append(
                    build_output_row(
                        method="d-HNSW",
                        workers=workers,
                        repeat=repeat,
                        input_row=input_row,
                        raw_source=raw_source,
                        pool_record=pool,
                        pool_relative=relative,
                        pool_sha=pool_sha,
                        destination=staging,
                        campaign_id=campaign_id,
                    )
                )

        runs = staging / "runs.csv"
        with runs.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
            writer.writeheader()
            writer.writerows(output_rows)
        evidence.validate_worker_scaling(staging, expected_slabwalk_sha)
        os.rename(staging, out_dir)

    return evidence.validate_worker_scaling(out_dir, expected_slabwalk_sha)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--query-pools", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--expected-slabwalk-sha", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = assemble(
        args.raw_root,
        args.query_pools,
        args.campaign_root,
        args.out_dir,
        campaign_id=args.campaign_id,
        expected_slabwalk_sha=args.expected_slabwalk_sha,
        repeats=args.repeats,
    )
    print(
        "worker-scaling bundle ready: "
        f"rows={report['measured_rows']} cells={report['measured_cells']}"
    )


if __name__ == "__main__":
    main()
