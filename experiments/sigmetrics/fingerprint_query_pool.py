#!/usr/bin/env python3
"""Fingerprint logical query/ground-truth content across ANN file formats."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHUNK_BYTES = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_exact_bytes(handle: Any, count: int) -> str:
    digest = hashlib.sha256()
    remaining = count
    while remaining:
        chunk = handle.read(min(CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError("truncated vector payload")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def query_fbin(path: Path, limit: int | None) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError(f"truncated fbin header: {path}")
        source_rows, dim = struct.unpack("<II", header)
        expected = 8 + source_rows * dim * 4
        if source_rows <= 0 or dim <= 0 or path.stat().st_size != expected:
            raise ValueError(f"invalid fbin shape or payload: {path}")
        rows = source_rows if limit is None else min(limit, source_rows)
        if rows <= 0 or (limit is not None and limit > source_rows):
            raise ValueError(f"invalid query limit {limit} for {source_rows} rows")
        canonical = hash_exact_bytes(handle, rows * dim * 4)
    return {
        "format": "fbin",
        "source_rows": source_rows,
        "rows": rows,
        "dim": dim,
        "canonical_sha256": canonical,
    }


def query_fvecs(path: Path, limit: int | None) -> dict[str, object]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        raw_dim = handle.read(4)
        if len(raw_dim) != 4:
            raise ValueError(f"truncated fvecs header: {path}")
        dim = struct.unpack("<i", raw_dim)[0]
        row_bytes = 4 + dim * 4
        if dim <= 0 or size % row_bytes != 0:
            raise ValueError(f"invalid fvecs shape or payload: {path}")
        source_rows = size // row_bytes
        rows = source_rows if limit is None else min(limit, source_rows)
        if rows <= 0 or (limit is not None and limit > source_rows):
            raise ValueError(f"invalid query limit {limit} for {source_rows} rows")
        digest = hashlib.sha256()
        handle.seek(0)
        for row_index in range(source_rows):
            raw = handle.read(4)
            if len(raw) != 4 or struct.unpack("<i", raw)[0] != dim:
                raise ValueError(f"inconsistent fvecs row dimension at row {row_index}")
            payload = handle.read(dim * 4)
            if len(payload) != dim * 4:
                raise ValueError(f"truncated fvecs row {row_index}")
            if row_index < rows:
                digest.update(payload)
    return {
        "format": "fvecs",
        "source_rows": source_rows,
        "rows": rows,
        "dim": dim,
        "canonical_sha256": digest.hexdigest(),
    }


def groundtruth_bin(path: Path, limit: int | None) -> dict[str, object]:
    with path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError(f"truncated ground-truth header: {path}")
        source_rows, width = struct.unpack("<II", header)
        ids_bytes = source_rows * width * 4
        payload = path.stat().st_size - 8
        if source_rows <= 0 or width <= 0:
            raise ValueError(f"invalid ground-truth shape: {path}")
        if payload == ids_bytes:
            layout = "ids_only"
        elif payload == 2 * ids_bytes:
            layout = "ids_then_distances"
        else:
            raise ValueError(f"unsupported ground-truth payload: {path}")
        rows = source_rows if limit is None else min(limit, source_rows)
        if rows <= 0 or (limit is not None and limit > source_rows):
            raise ValueError(f"invalid ground-truth limit {limit} for {source_rows} rows")
        canonical = hash_exact_bytes(handle, rows * width * 4)
    return {
        "format": "bin",
        "layout": layout,
        "source_rows": source_rows,
        "rows": rows,
        "k": width,
        "canonical_ids_sha256": canonical,
    }


def groundtruth_ivecs(path: Path, limit: int | None) -> dict[str, object]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        raw_width = handle.read(4)
        if len(raw_width) != 4:
            raise ValueError(f"truncated ivecs header: {path}")
        width = struct.unpack("<i", raw_width)[0]
        row_bytes = 4 + width * 4
        if width <= 0 or size % row_bytes != 0:
            raise ValueError(f"invalid ivecs shape or payload: {path}")
        source_rows = size // row_bytes
        rows = source_rows if limit is None else min(limit, source_rows)
        if rows <= 0 or (limit is not None and limit > source_rows):
            raise ValueError(f"invalid ground-truth limit {limit} for {source_rows} rows")
        digest = hashlib.sha256()
        handle.seek(0)
        for row_index in range(source_rows):
            raw = handle.read(4)
            if len(raw) != 4 or struct.unpack("<i", raw)[0] != width:
                raise ValueError(f"inconsistent ivecs row dimension at row {row_index}")
            payload = handle.read(width * 4)
            if len(payload) != width * 4:
                raise ValueError(f"truncated ivecs row {row_index}")
            if row_index < rows:
                digest.update(payload)
    return {
        "format": "ivecs",
        "layout": "ids_only",
        "source_rows": source_rows,
        "rows": rows,
        "k": width,
        "canonical_ids_sha256": digest.hexdigest(),
    }


def infer_query_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".fbin":
        return "fbin"
    if suffix == ".fvecs":
        return "fvecs"
    raise ValueError(f"cannot infer query format from {path}")


def infer_groundtruth_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".bin":
        return "bin"
    if suffix == ".ivecs":
        return "ivecs"
    raise ValueError(f"cannot infer ground-truth format from {path}")


def atomic_json(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint_query_pool(
    query: str | Path,
    groundtruth: str | Path,
    *,
    dataset: str,
    method: str,
    metric: str,
    query_format: str = "auto",
    groundtruth_format: str = "auto",
    limit: int | None = None,
    out: str | Path | None = None,
) -> dict[str, object]:
    query_path = Path(query)
    groundtruth_path = Path(groundtruth)
    if not query_path.is_file() or not groundtruth_path.is_file():
        raise ValueError("query and ground-truth files must exist")
    if metric not in {"l2", "ip"}:
        raise ValueError("metric must be l2 or ip")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")

    query_format = infer_query_format(query_path) if query_format == "auto" else query_format
    groundtruth_format = (
        infer_groundtruth_format(groundtruth_path)
        if groundtruth_format == "auto"
        else groundtruth_format
    )
    query_record = {
        "fbin": query_fbin,
        "fvecs": query_fvecs,
    }.get(query_format)
    groundtruth_record = {
        "bin": groundtruth_bin,
        "ivecs": groundtruth_ivecs,
    }.get(groundtruth_format)
    if query_record is None or groundtruth_record is None:
        raise ValueError("unsupported query or ground-truth format")

    query_info = query_record(query_path, limit)
    groundtruth_info = groundtruth_record(groundtruth_path, limit)
    if query_info["rows"] != groundtruth_info["rows"]:
        raise ValueError(
            "query/ground-truth row mismatch: "
            f"{query_info['rows']} != {groundtruth_info['rows']}"
        )
    query_info.update(
        {
            "path": str(query_path.resolve()),
            "bytes": query_path.stat().st_size,
            "file_sha256": file_sha256(query_path),
        }
    )
    groundtruth_info.update(
        {
            "path": str(groundtruth_path.resolve()),
            "bytes": groundtruth_path.stat().st_size,
            "file_sha256": file_sha256(groundtruth_path),
        }
    )
    record: dict[str, object] = {
        "kind": "query_pool_fingerprint",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": dataset,
        "method": method,
        "metric": metric,
        "limit": limit,
        "query": query_info,
        "groundtruth": groundtruth_info,
    }
    if out is not None:
        atomic_json(Path(out), record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", type=Path, required=True)
    parser.add_argument("--groundtruth", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--metric", choices=("l2", "ip"), required=True)
    parser.add_argument("--query-format", choices=("auto", "fbin", "fvecs"), default="auto")
    parser.add_argument(
        "--groundtruth-format", choices=("auto", "bin", "ivecs"), default="auto"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    record = fingerprint_query_pool(
        args.query,
        args.groundtruth,
        dataset=args.dataset,
        method=args.method,
        metric=args.metric,
        query_format=args.query_format,
        groundtruth_format=args.groundtruth_format,
        limit=args.limit,
        out=args.out,
    )
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
