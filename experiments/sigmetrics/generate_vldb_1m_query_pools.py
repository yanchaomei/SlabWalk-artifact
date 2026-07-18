#!/usr/bin/env python3
"""Generate the audited seven-dataset, three-system 1M query-pool matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from fingerprint_query_pool import fingerprint_query_pool


DATASETS = (
    "SIFT1M",
    "GIST1M",
    "DEEP1M",
    "BIGANN1M",
    "SPACEV1M",
    "TURING1M",
    "TTI1M",
)
METHODS = ("SHINE", "SlabWalk", "d-HNSW")
DIMENSIONS = {
    "SIFT1M": 128,
    "GIST1M": 960,
    "DEEP1M": 96,
    "BIGANN1M": 128,
    "SPACEV1M": 100,
    "TURING1M": 100,
    "TTI1M": 200,
}
GRAPH_PATHS = {
    "SIFT1M": ("sift1m/queries/query-uniform.fbin", "sift1m/queries/groundtruth-uniform.bin"),
    "GIST1M": ("gist1m/queries/query-u10k.fbin", "gist1m/queries/groundtruth-u10k.bin"),
    "DEEP1M": ("deep1m/queries/query-uniform.fbin", "deep1m/queries/groundtruth-uniform.bin"),
    "BIGANN1M": ("bigann1m/queries/query-uniform.u8bin", "bigann1m/queries/groundtruth-uniform.bin"),
    "SPACEV1M": ("spacev1m/queries/query-uniform.i8bin", "spacev1m/queries/groundtruth-uniform.bin"),
    "TURING1M": ("turing1m/queries/query-uniform.fbin", "turing1m/queries/groundtruth-uniform.bin"),
    "TTI1M": ("tti1m/queries/query-uniform.fbin", "tti1m/queries/groundtruth-uniform.bin"),
}
DHNSW_PATHS = {
    "SIFT1M": ("sift/sift_query.fvecs", "sift/sift_groundtruth.ivecs"),
    "GIST1M": ("gist/gist_query.fvecs", "gist/gist_groundtruth.ivecs"),
    "DEEP1M": ("deep1M/deep1M_query.fvecs", "deep1M/deep1M_groundtruth.ivecs"),
    "BIGANN1M": ("bigann1M/bigann1M_query.fvecs", "bigann1M/bigann1M_groundtruth.ivecs"),
    "SPACEV1M": ("spacev1M/spacev1M_query.fvecs", "spacev1M/spacev1M_groundtruth.ivecs"),
    "TURING1M": ("turing1M/turing1M_query.fvecs", "turing1M/turing1M_groundtruth.ivecs"),
    "TTI1M": ("text1M/text1M_query.fvecs", "text1M/text1M_groundtruth.ivecs"),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matrix_paths(
    graph_root: Path, dhnsw_datasets: Path
) -> dict[tuple[str, str], tuple[Path, Path]]:
    matrix: dict[tuple[str, str], tuple[Path, Path]] = {}
    for dataset in DATASETS:
        graph_query, graph_gt = GRAPH_PATHS[dataset]
        for method in ("SHINE", "SlabWalk"):
            matrix[(dataset, method)] = (
                graph_root / graph_query,
                graph_root / graph_gt,
            )
        dh_query, dh_gt = DHNSW_PATHS[dataset]
        matrix[(dataset, "d-HNSW")] = (
            dhnsw_datasets / dh_query,
            dhnsw_datasets / dh_gt,
        )
    return matrix


def validate_records(records: dict[tuple[str, str], dict[str, object]]) -> None:
    expected = {(dataset, method) for dataset in DATASETS for method in METHODS}
    if set(records) != expected:
        raise ValueError("query-pool record matrix is incomplete")
    for dataset in DATASETS:
        query_hashes: set[str] = set()
        groundtruth_hashes: set[str] = set()
        for method in METHODS:
            record = records[(dataset, method)]
            query = record.get("query")
            groundtruth = record.get("groundtruth")
            if not isinstance(query, dict) or not isinstance(groundtruth, dict):
                raise ValueError(f"{dataset}/{method}: malformed fingerprint")
            if (
                int(query.get("rows", 0)) != 10000
                or int(query.get("dim", 0)) != DIMENSIONS[dataset]
                or int(groundtruth.get("rows", 0)) != 10000
                or int(groundtruth.get("k", 0)) < 10
            ):
                raise ValueError(f"{dataset}/{method}: query-pool shape mismatch")
            query_hashes.add(str(query.get("canonical_sha256", "")))
            groundtruth_hashes.add(
                str(groundtruth.get("canonical_ids_sha256", ""))
            )
        if len(query_hashes) != 1 or len(groundtruth_hashes) != 1:
            raise ValueError(f"{dataset}: logical query-pool content differs by system")


def generate(graph_root: Path, dhnsw_datasets: Path, out: Path) -> None:
    if out.exists():
        raise ValueError(f"output already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.parent / f".{out.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        records: dict[tuple[str, str], dict[str, object]] = {}
        for (dataset, method), (query, groundtruth) in matrix_paths(
            graph_root, dhnsw_datasets
        ).items():
            slug = method.lower().replace("-", "")
            target = staging / f"{dataset.lower()}_{slug}.json"
            record = fingerprint_query_pool(
                query,
                groundtruth,
                dataset=dataset,
                method=method,
                metric="ip" if dataset == "TTI1M" else "l2",
                limit=10000,
                out=target,
            )
            records[(dataset, method)] = record
        validate_records(records)
        provenance = {
            "kind": "vldb_1m_query_pool_matrix",
            "datasets": list(DATASETS),
            "methods": list(METHODS),
            "rows_per_dataset": 10000,
            "cells": len(records),
            "graph_root": str(graph_root.resolve()),
            "dhnsw_datasets": str(dhnsw_datasets.resolve()),
        }
        (staging / "PROVENANCE.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )
        paths = sorted(path for path in staging.iterdir() if path.is_file())
        (staging / "SHA256SUMS").write_text(
            "".join(f"{file_sha256(path)}  {path.name}\n" for path in paths)
        )
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-root", type=Path, required=True)
    parser.add_argument("--dhnsw-datasets", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    generate(args.graph_root, args.dhnsw_datasets, args.out)
    print(f"wrote audited 1M query-pool matrix: {args.out}")


if __name__ == "__main__":
    main()
