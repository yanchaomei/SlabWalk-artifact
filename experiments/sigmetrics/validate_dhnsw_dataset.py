#!/usr/bin/env python3
"""Validate the fixed-vector files consumed by the d-HNSW harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import numpy as np


def _fixed_rows(path: Path, kind: str) -> tuple[int, int]:
    size = path.stat().st_size
    if size < 8:
        raise ValueError(f"{kind} file is too short: {path}")
    with path.open("rb") as handle:
        dim = struct.unpack("<i", handle.read(4))[0]
    if dim <= 0:
        raise ValueError(f"invalid {kind} dimension {dim}: {path}")
    row_bytes = (dim + 1) * 4
    if size % row_bytes:
        raise ValueError(
            f"{kind} file is not fixed-width: size={size}, row_bytes={row_bytes}"
        )
    rows = size // row_bytes
    if rows <= 0:
        raise ValueError(f"empty {kind} file: {path}")

    # Checking every base header would page through multi-GB vector payloads.
    # Size plus representative first/middle/last headers validates the converter's
    # fixed-width contract without turning this guard into another data scan.
    with path.open("rb") as handle:
        for row in sorted({0, rows // 2, rows - 1}):
            handle.seek(row * row_bytes)
            actual = struct.unpack("<i", handle.read(4))[0]
            if actual != dim:
                raise ValueError(
                    f"{kind} file is not fixed-width at row {row}: "
                    f"expected {dim}, got {actual}"
                )
    return rows, dim


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_dataset(
    base_fvecs: str | Path,
    groundtruth_ivecs: str | Path,
    *,
    query_fvecs: str | Path | None = None,
    expected_queries: int | None = None,
    min_k: int = 1,
) -> dict[str, Any]:
    base = Path(base_fvecs)
    groundtruth = Path(groundtruth_ivecs)
    base_rows, base_dim = _fixed_rows(base, "base")
    groundtruth_rows, groundtruth_k = _fixed_rows(groundtruth, "ground-truth")
    if expected_queries is not None and groundtruth_rows != expected_queries:
        raise ValueError(
            f"ground-truth row count mismatch: expected {expected_queries}, "
            f"got {groundtruth_rows}"
        )
    if groundtruth_k < min_k:
        raise ValueError(
            f"ground-truth width is below top-k requirement: "
            f"required {min_k}, got {groundtruth_k}"
        )

    query_record: dict[str, Any] = {}
    if query_fvecs is not None:
        query = Path(query_fvecs)
        query_rows, query_dim = _fixed_rows(query, "query")
        if query_rows != groundtruth_rows:
            raise ValueError(
                "query/ground-truth row count mismatch: "
                f"{query_rows} != {groundtruth_rows}"
            )
        if query_dim != base_dim:
            raise ValueError(
                f"query/base dimension mismatch: {query_dim} != {base_dim}"
            )
        query_record = {
            "query_path": str(query.resolve()),
            "query_rows": query_rows,
            "query_dim": query_dim,
            "query_bytes": query.stat().st_size,
            "query_sha256": _sha256(query),
        }

    words = np.memmap(
        groundtruth,
        dtype="<i4",
        mode="r",
        shape=(groundtruth_rows, groundtruth_k + 1),
    )
    bad_width = np.flatnonzero(words[:, 0] != groundtruth_k)
    if bad_width.size:
        row = int(bad_width[0])
        raise ValueError(
            f"ground-truth file is not fixed-width at row {row}: "
            f"expected {groundtruth_k}, got {int(words[row, 0])}"
        )
    ids = words[:, 1:]
    min_id = int(ids.min())
    max_id = int(ids.max())
    if min_id < 0 or max_id >= base_rows:
        raise ValueError(
            f"ground-truth ID outside base domain [0,{base_rows}): "
            f"min={min_id}, max={max_id}"
        )

    return {
        "base_path": str(base.resolve()),
        "base_rows": base_rows,
        "base_dim": base_dim,
        "base_bytes": base.stat().st_size,
        "groundtruth_path": str(groundtruth.resolve()),
        "groundtruth_rows": groundtruth_rows,
        "groundtruth_k": groundtruth_k,
        "groundtruth_bytes": groundtruth.stat().st_size,
        "groundtruth_sha256": _sha256(groundtruth),
        "min_groundtruth_id": min_id,
        "max_groundtruth_id": max_id,
        "status": "ok",
        **query_record,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--query", type=Path)
    parser.add_argument("--groundtruth", required=True, type=Path)
    parser.add_argument("--expected-queries", type=int)
    parser.add_argument("--min-k", type=int, default=10)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    record = validate_dataset(
        args.base,
        args.groundtruth,
        query_fvecs=args.query,
        expected_queries=args.expected_queries,
        min_k=args.min_k,
    )
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(args.out)
    print(payload, end="")


if __name__ == "__main__":
    main()
