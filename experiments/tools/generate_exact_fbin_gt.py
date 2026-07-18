#!/usr/bin/env python3
"""Generate exact L2 ground truth for headered float32 matrices with FAISS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import time
from pathlib import Path

import numpy as np


def read_fbin(path: Path) -> np.memmap:
    with path.open("rb") as handle:
        header = handle.read(8)
    if len(header) != 8:
        raise ValueError(f"{path}: truncated fbin header")
    rows, dim = struct.unpack("<II", header)
    expected = 8 + rows * dim * np.dtype("<f4").itemsize
    actual = path.stat().st_size
    if rows == 0 or dim == 0 or actual != expected:
        raise ValueError(
            f"{path}: invalid fbin shape {rows}x{dim}; "
            f"size={actual}, expected={expected}"
        )
    return np.memmap(path, dtype="<f4", mode="r", offset=8, shape=(rows, dim))


def write_ibin_atomic(path: Path, ids: np.ndarray) -> None:
    if ids.ndim != 2 or ids.shape[0] == 0 or ids.shape[1] == 0:
        raise ValueError("ground-truth IDs must be a nonempty matrix")
    if np.any(ids < 0) or np.any(ids > np.iinfo(np.uint32).max):
        raise ValueError("ground-truth IDs do not fit uint32")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(struct.pack("<II", *ids.shape))
        np.asarray(ids, dtype="<u4", order="C").tofile(handle)
    os.replace(tmp, path)


def sha256_file(path: Path, chunk_bytes: int = 8 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--query", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--k", type=int, default=100)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--add-batch", type=int, default=500_000)
    parser.add_argument("--query-batch", type=int, default=100)
    parser.add_argument("--verify-queries", type=int, default=3)
    parser.add_argument("--cpu-threads", type=int, default=40)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for name in ("k", "add_batch", "query_batch", "cpu_threads"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    if args.verify_queries < 0:
        raise SystemExit("--verify-queries must be non-negative")

    import faiss  # Imported lazily so the file-format helpers remain testable.

    base = read_fbin(args.base)
    query = read_fbin(args.query)
    if base.shape[1] != query.shape[1]:
        raise SystemExit(f"dimension mismatch: base={base.shape}, query={query.shape}")
    if args.k > base.shape[0]:
        raise SystemExit(f"k={args.k} exceeds base rows={base.shape[0]}")
    if not 0 <= args.gpu < faiss.get_num_gpus():
        raise SystemExit(f"GPU {args.gpu} unavailable; count={faiss.get_num_gpus()}")

    started = time.time()
    resources = faiss.StandardGpuResources()
    config = faiss.GpuIndexFlatConfig()
    config.device = args.gpu
    config.useFloat16 = False
    index = faiss.GpuIndexFlatL2(resources, base.shape[1], config)

    add_started = time.time()
    for lo in range(0, base.shape[0], args.add_batch):
        hi = min(base.shape[0], lo + args.add_batch)
        index.add(np.ascontiguousarray(base[lo:hi], dtype=np.float32))
        print(f"ADD {hi}/{base.shape[0]}", flush=True)
    if index.ntotal != base.shape[0]:
        raise RuntimeError(f"FAISS added {index.ntotal}, expected {base.shape[0]}")
    add_seconds = time.time() - add_started

    distances = np.empty((query.shape[0], args.k), dtype=np.float32)
    ids = np.empty((query.shape[0], args.k), dtype=np.int64)
    search_started = time.time()
    for lo in range(0, query.shape[0], args.query_batch):
        hi = min(query.shape[0], lo + args.query_batch)
        dists, labels = index.search(
            np.ascontiguousarray(query[lo:hi], dtype=np.float32), args.k
        )
        distances[lo:hi] = dists
        ids[lo:hi] = labels
        print(f"SEARCH {hi}/{query.shape[0]}", flush=True)
    search_seconds = time.time() - search_started

    if np.any(ids < 0) or np.any(ids >= base.shape[0]):
        raise RuntimeError("FAISS returned an out-of-range label")
    if np.any(distances[:, 1:] < distances[:, :-1]):
        raise RuntimeError("FAISS distances are not nondecreasing")

    verify_count = min(args.verify_queries, query.shape[0])
    verify_started = time.time()
    if verify_count:
        faiss.omp_set_num_threads(args.cpu_threads)
        cpu_index = faiss.IndexFlatL2(base.shape[1])
        for lo in range(0, base.shape[0], args.add_batch):
            hi = min(base.shape[0], lo + args.add_batch)
            cpu_index.add(np.ascontiguousarray(base[lo:hi], dtype=np.float32))
        cpu_distances, cpu_ids = cpu_index.search(
            np.ascontiguousarray(query[:verify_count], dtype=np.float32), args.k
        )
        if not np.array_equal(ids[:verify_count], cpu_ids):
            raise RuntimeError("GPU and CPU exact top-k labels differ")
        if not np.allclose(
            distances[:verify_count], cpu_distances, rtol=2e-5, atol=2e-4
        ):
            raise RuntimeError("GPU and CPU exact top-k distances differ")
    verify_seconds = time.time() - verify_started

    write_ibin_atomic(args.out, ids)
    manifest = {
        "method": "FAISS GpuIndexFlatL2 float32",
        "faiss_version": faiss.__version__,
        "gpu": args.gpu,
        "gpu_count": faiss.get_num_gpus(),
        "base": str(args.base.resolve()),
        "base_shape": list(base.shape),
        "base_sha256": sha256_file(args.base),
        "query": str(args.query.resolve()),
        "query_shape": list(query.shape),
        "query_sha256": sha256_file(args.query),
        "output": str(args.out.resolve()),
        "output_shape": list(ids.shape),
        "output_sha256": sha256_file(args.out),
        "k": args.k,
        "add_batch": args.add_batch,
        "query_batch": args.query_batch,
        "verify_queries": verify_count,
        "cpu_threads": args.cpu_threads,
        "add_seconds": add_seconds,
        "search_seconds": search_seconds,
        "verify_seconds": verify_seconds,
        "total_seconds": time.time() - started,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_manifest, args.manifest)
    print(json.dumps(manifest, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
