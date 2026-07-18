#!/usr/bin/env python3
"""Recompute a small exact top-k sample against a DiskANN-style base file."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def _header(path: Path, label: str) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size < 8:
        raise ValueError(f"missing or truncated {label}: {path}")
    with path.open("rb") as handle:
        rows, dim = struct.unpack("<II", handle.read(8))
    if rows <= 0 or dim <= 0:
        raise ValueError(f"invalid {label} shape ({rows}, {dim}): {path}")
    return rows, dim


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, record: dict[str, Any]) -> None:
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


def _select_topk(
    scores: np.ndarray, ids: np.ndarray, top_k: int, maximize: bool
) -> tuple[np.ndarray, np.ndarray]:
    if scores.size <= top_k:
        selected = np.arange(scores.size)
    elif maximize:
        selected = np.argpartition(scores, -top_k)[-top_k:]
    else:
        selected = np.argpartition(scores, top_k - 1)[:top_k]
    if maximize:
        order = np.lexsort((ids[selected], -scores[selected]))
    else:
        order = np.lexsort((ids[selected], scores[selected]))
    selected = selected[order]
    return scores[selected], ids[selected]


def spotcheck_groundtruth(
    base_fbin: str | Path,
    query_fbin: str | Path,
    groundtruth_bin: str | Path,
    *,
    metric: str,
    query_indices: Iterable[int],
    top_k: int = 10,
    block_rows: int = 250_000,
    require_exact: bool = False,
    out: str | Path | None = None,
) -> dict[str, Any]:
    base_path = Path(base_fbin)
    query_path = Path(query_fbin)
    groundtruth_path = Path(groundtruth_bin)
    base_rows, base_dim = _header(base_path, "base")
    query_rows, query_dim = _header(query_path, "query")
    gt_rows, gt_k = _header(groundtruth_path, "ground-truth")
    if base_path.stat().st_size != 8 + base_rows * base_dim * 4:
        raise ValueError("base payload size does not match its header")
    if query_path.stat().st_size != 8 + query_rows * query_dim * 4:
        raise ValueError("query payload size does not match its header")
    if base_dim != query_dim:
        raise ValueError(f"base/query dimension mismatch: {base_dim} != {query_dim}")
    if query_rows != gt_rows:
        raise ValueError(f"query/ground-truth row mismatch: {query_rows} != {gt_rows}")
    if top_k <= 0 or top_k > gt_k or top_k > base_rows:
        raise ValueError(f"invalid top_k={top_k} for gt_k={gt_k}, base_rows={base_rows}")
    if block_rows <= 0:
        raise ValueError("block_rows must be positive")
    metric = metric.lower()
    if metric not in {"ip", "l2"}:
        raise ValueError("metric must be ip or l2")

    ids_bytes = gt_rows * gt_k * 4
    payload_bytes = groundtruth_path.stat().st_size - 8
    if payload_bytes == ids_bytes:
        gt_layout = "ids_only"
    elif payload_bytes == 2 * ids_bytes:
        gt_layout = "ids_then_float_distances"
    else:
        raise ValueError("ground-truth payload size is not a supported layout")

    indices = list(dict.fromkeys(int(value) for value in query_indices))
    if not indices or any(value < 0 or value >= query_rows for value in indices):
        raise ValueError(f"query indices must be in [0,{query_rows})")

    base = np.memmap(
        base_path, dtype="<f4", mode="r", offset=8, shape=(base_rows, base_dim)
    )
    queries = np.memmap(
        query_path,
        dtype="<f4",
        mode="r",
        offset=8,
        shape=(query_rows, query_dim),
    )
    groundtruth = np.memmap(
        groundtruth_path,
        dtype="<i4",
        mode="r",
        offset=8,
        shape=(gt_rows, gt_k),
    )
    selected_queries = np.asarray(queries[indices], dtype=np.float32)
    query_norms = np.einsum("ij,ij->i", selected_queries, selected_queries)
    best_scores = [np.empty(0, dtype=np.float32) for _ in indices]
    best_ids = [np.empty(0, dtype=np.int64) for _ in indices]
    maximize = metric == "ip"

    for start in range(0, base_rows, block_rows):
        stop = min(base_rows, start + block_rows)
        block = np.asarray(base[start:stop], dtype=np.float32)
        products = block @ selected_queries.T
        if metric == "l2":
            block_norms = np.einsum("ij,ij->i", block, block)
            values = block_norms[:, None] + query_norms[None, :] - 2.0 * products
        else:
            values = products
        block_ids = np.arange(start, stop, dtype=np.int64)
        for column in range(len(indices)):
            scores = np.concatenate((best_scores[column], values[:, column]))
            ids = np.concatenate((best_ids[column], block_ids))
            best_scores[column], best_ids[column] = _select_topk(
                scores, ids, top_k, maximize
            )

    checks = []
    for position, query_index in enumerate(indices):
        expected = [int(value) for value in groundtruth[query_index, :top_k]]
        computed = [int(value) for value in best_ids[position]]
        overlap = len(set(expected) & set(computed))
        checks.append(
            {
                "query_index": query_index,
                "expected_ids": expected,
                "computed_ids": computed,
                "computed_scores": [float(value) for value in best_scores[position]],
                "overlap": overlap,
                "exact_set_match": overlap == top_k,
                "query_l2_norm": math.sqrt(float(query_norms[position])),
            }
        )

    minimum_overlap = min(check["overlap"] for check in checks)
    record: dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "metric": metric,
        "top_k": top_k,
        "checked_queries": len(indices),
        "query_indices": indices,
        "minimum_overlap": minimum_overlap,
        "status": "ok" if minimum_overlap == top_k else "mismatch",
        "groundtruth_layout": gt_layout,
        "base": {
            "path": str(base_path.resolve()),
            "rows": base_rows,
            "dim": base_dim,
            "bytes": base_path.stat().st_size,
        },
        "query": {
            "path": str(query_path.resolve()),
            "rows": query_rows,
            "dim": query_dim,
            "bytes": query_path.stat().st_size,
            "sha256": _sha256(query_path),
        },
        "groundtruth": {
            "path": str(groundtruth_path.resolve()),
            "rows": gt_rows,
            "k": gt_k,
            "bytes": groundtruth_path.stat().st_size,
            "sha256": _sha256(groundtruth_path),
        },
        "checks": checks,
    }
    if out is not None:
        _atomic_json(Path(out), record)
    if require_exact and minimum_overlap != top_k:
        raise ValueError(
            f"exact top-{top_k} spot check failed: minimum overlap={minimum_overlap}"
        )
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--query", required=True, type=Path)
    parser.add_argument("--groundtruth", required=True, type=Path)
    parser.add_argument("--metric", choices=("ip", "l2"), required=True)
    parser.add_argument("--query-indices", default="0,4999,9999")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--block-rows", type=int, default=250_000)
    parser.add_argument("--require-exact", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    record = spotcheck_groundtruth(
        args.base,
        args.query,
        args.groundtruth,
        metric=args.metric,
        query_indices=(int(value) for value in args.query_indices.split(",")),
        top_k=args.top_k,
        block_rows=args.block_rows,
        require_exact=args.require_exact,
        out=args.out,
    )
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
