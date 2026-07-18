#!/usr/bin/env python3
"""Materialize one auditable query/ground-truth prefix for all frontiers.

DiskANN ground-truth files occur in two layouts in this project:

* ``header + n*k int32 ids``; and
* ``header + n*k int32 ids + n*k float32 distances``.

The header width is authoritative.  In particular, the second layout must not
be interpreted as a ground-truth matrix with width ``2*k``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_header(path: Path, label: str) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size < 8:
        raise ValueError(f"{label} file is missing or truncated: {path}")
    with path.open("rb") as handle:
        rows, dim = struct.unpack("<II", handle.read(8))
    if rows <= 0 or dim <= 0:
        raise ValueError(f"invalid {label} shape ({rows}, {dim}): {path}")
    return rows, dim


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp.{os.getpid()}")


def _atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path)
    try:
        with temporary.open("wb") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bin(path: Path, values: np.ndarray) -> None:
    rows, dim = values.shape

    def writer(handle) -> None:
        handle.write(struct.pack("<II", rows, dim))
        values.tofile(handle)

    _atomic_write(path, writer)


def _write_fixed_vectors(path: Path, values: np.ndarray) -> None:
    rows, dim = values.shape

    def writer(handle) -> None:
        width = struct.pack("<i", dim)
        for row in values:
            handle.write(width)
            row.tofile(handle)

    _atomic_write(path, writer)


def _output_record(path: Path, rows: int, dim: int) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "rows": rows,
        "dim": dim,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def prepare_fixed_query_pool(
    query: str | Path,
    groundtruth: str | Path,
    *,
    limit: int,
    query_fbin: str | Path | None = None,
    groundtruth_bin: str | Path | None = None,
    query_fvecs: str | Path | None = None,
    groundtruth_ivecs: str | Path | None = None,
    manifest: str | Path | None = None,
) -> dict[str, Any]:
    query = Path(query)
    groundtruth = Path(groundtruth)
    outputs = {
        "query_fbin": Path(query_fbin) if query_fbin is not None else None,
        "groundtruth_bin": (
            Path(groundtruth_bin) if groundtruth_bin is not None else None
        ),
        "query_fvecs": Path(query_fvecs) if query_fvecs is not None else None,
        "groundtruth_ivecs": (
            Path(groundtruth_ivecs) if groundtruth_ivecs is not None else None
        ),
    }
    if not any(outputs.values()):
        raise ValueError("at least one materialized output is required")

    query_rows, query_dim = _read_header(query, "query")
    gt_rows, gt_k = _read_header(groundtruth, "ground-truth")
    if query_rows != gt_rows:
        raise ValueError(
            f"query/ground-truth row mismatch: {query_rows} != {gt_rows}"
        )
    if limit <= 0 or limit > query_rows:
        raise ValueError(f"limit must be in [1,{query_rows}], got {limit}")

    query_expected_bytes = 8 + query_rows * query_dim * 4
    if query.stat().st_size != query_expected_bytes:
        raise ValueError(
            "query payload size does not match its header: "
            f"expected {query_expected_bytes}, got {query.stat().st_size}"
        )

    ids_bytes = gt_rows * gt_k * 4
    gt_payload_bytes = groundtruth.stat().st_size - 8
    if gt_payload_bytes == ids_bytes:
        gt_layout = "ids_only"
    elif gt_payload_bytes == 2 * ids_bytes:
        gt_layout = "ids_then_float_distances"
    else:
        raise ValueError(
            "ground-truth payload size matches neither IDs-only nor "
            f"IDs+distances: rows={gt_rows}, k={gt_k}, bytes={gt_payload_bytes}"
        )

    queries = np.memmap(
        query,
        dtype="<f4",
        mode="r",
        offset=8,
        shape=(query_rows, query_dim),
    )[:limit]
    ids = np.memmap(
        groundtruth,
        dtype="<i4",
        mode="r",
        offset=8,
        shape=(gt_rows, gt_k),
    )[:limit]

    if outputs["query_fbin"] is not None:
        _write_bin(outputs["query_fbin"], queries)
    if outputs["groundtruth_bin"] is not None:
        _write_bin(outputs["groundtruth_bin"], ids)
    if outputs["query_fvecs"] is not None:
        _write_fixed_vectors(outputs["query_fvecs"], queries)
    if outputs["groundtruth_ivecs"] is not None:
        _write_fixed_vectors(outputs["groundtruth_ivecs"], ids)

    record: dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "selection": "first_rows",
        "selected_rows": limit,
        "query_source": {
            "path": str(query.resolve()),
            "rows": query_rows,
            "dim": query_dim,
            "bytes": query.stat().st_size,
            "sha256": _sha256(query),
        },
        "groundtruth_source": {
            "path": str(groundtruth.resolve()),
            "rows": gt_rows,
            "k": gt_k,
            "bytes": groundtruth.stat().st_size,
            "sha256": _sha256(groundtruth),
        },
        "groundtruth_layout": gt_layout,
        "outputs": {},
    }
    for name, path in outputs.items():
        if path is not None:
            dim = query_dim if name.startswith("query_") else gt_k
            record["outputs"][name] = _output_record(path, limit, dim)

    if manifest is not None:
        manifest_path = Path(manifest)

        def write_manifest(handle) -> None:
            handle.write(
                (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
            )

        _atomic_write(manifest_path, write_manifest)
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True, type=Path)
    parser.add_argument("--groundtruth", required=True, type=Path)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--query-fbin", type=Path)
    parser.add_argument("--groundtruth-bin", type=Path)
    parser.add_argument("--query-fvecs", type=Path)
    parser.add_argument("--groundtruth-ivecs", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    record = prepare_fixed_query_pool(
        args.query,
        args.groundtruth,
        limit=args.limit,
        query_fbin=args.query_fbin,
        groundtruth_bin=args.groundtruth_bin,
        query_fvecs=args.query_fvecs,
        groundtruth_ivecs=args.groundtruth_ivecs,
        manifest=args.manifest,
    )
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
