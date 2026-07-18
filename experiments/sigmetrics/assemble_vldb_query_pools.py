#!/usr/bin/env python3
"""Atomically assemble and validate the final 10M query-pool evidence matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from validate_vldb_final_evidence import validate_query_pools


METHOD_SLUG = {"SHINE": "shine", "SlabWalk": "slabwalk", "d-HNSW": "dhnsw"}
BASE_CELLS = {
    (dataset, method)
    for dataset in ("DEEP10M", "SIFT10M")
    for method in METHOD_SLUG
}
GRAPH_TTI_CELLS = {("TTI10M", "SHINE"), ("TTI10M", "SlabWalk")}
DHNSW_TTI_CELL = ("TTI10M", "d-HNSW")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_fingerprint(path: Path) -> tuple[tuple[str, str], dict[str, object]]:
    if not path.is_file():
        raise ValueError(f"missing query-pool manifest: {path}")
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid query-pool manifest: {path}") from exc
    if record.get("kind") != "query_pool_fingerprint":
        raise ValueError(f"not a query-pool fingerprint: {path}")
    cell = (str(record.get("dataset", "")), str(record.get("method", "")))
    return cell, record


def discover_directory(
    directory: Path, expected: set[tuple[str, str]], label: str
) -> dict[tuple[str, str], Path]:
    if not directory.is_dir():
        raise ValueError(f"missing {label} directory: {directory}")
    found: dict[tuple[str, str], Path] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON source: {path}") from exc
        if record.get("kind") != "query_pool_fingerprint":
            continue
        cell = (str(record.get("dataset", "")), str(record.get("method", "")))
        if cell in found:
            raise ValueError(f"duplicate {label} query-pool cell: {cell}")
        found[cell] = path
    if set(found) != expected:
        missing = sorted(expected - set(found))
        extra = sorted(set(found) - expected)
        raise ValueError(f"{label} query-pool matrix mismatch: missing={missing} extra={extra}")
    return found


def destination_name(cell: tuple[str, str]) -> str:
    dataset, method = cell
    return f"{dataset.lower()}_{METHOD_SLUG[method]}.json"


def write_sha256s(root: Path) -> None:
    output = root / "SHA256SUMS"
    files = sorted(path for path in root.iterdir() if path.is_file() and path != output)
    output.write_text(
        "".join(f"{file_sha256(path)}  {path.name}\n" for path in files)
    )


def assemble(
    base_dir: Path,
    tti_graph_dir: Path,
    tti_dhnsw_manifest: Path,
    out_dir: Path,
) -> None:
    if out_dir.exists():
        raise ValueError(f"output already exists: {out_dir}")

    base = discover_directory(base_dir, BASE_CELLS, "DEEP/SIFT base")
    graph = discover_directory(tti_graph_dir, GRAPH_TTI_CELLS, "TTI graph")
    dhnsw_cell, _ = load_fingerprint(tti_dhnsw_manifest)
    if dhnsw_cell != DHNSW_TTI_CELL:
        raise ValueError(
            f"TTI d-HNSW query-pool cell mismatch: {dhnsw_cell} != {DHNSW_TTI_CELL}"
        )
    spotcheck = tti_graph_dir / "tti_exact_groundtruth_spotcheck.json"
    if not spotcheck.is_file():
        raise ValueError(f"missing TTI exact ground-truth spot check: {spotcheck}")

    sources = {**base, **graph, DHNSW_TTI_CELL: tti_dhnsw_manifest}
    if len(sources) != 9:
        raise ValueError(f"query-pool source matrix must contain nine cells, found {len(sources)}")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.parent / f".{out_dir.name}.staging.{os.getpid()}"
    if staging.exists():
        raise ValueError(f"staging path already exists: {staging}")
    staging.mkdir()
    try:
        retained: list[dict[str, str]] = []
        for cell, source in sorted(sources.items()):
            destination = staging / destination_name(cell)
            shutil.copy2(source, destination)
            retained.append(
                {
                    "cell": f"{cell[0]}/{cell[1]}",
                    "source": str(source.resolve()),
                    "retained": destination.name,
                    "sha256": file_sha256(destination),
                }
            )
        spotcheck_destination = staging / spotcheck.name
        shutil.copy2(spotcheck, spotcheck_destination)
        retained.append(
            {
                "cell": "TTI10M/exact-groundtruth-spotcheck",
                "source": str(spotcheck.resolve()),
                "retained": spotcheck_destination.name,
                "sha256": file_sha256(spotcheck_destination),
            }
        )

        readme = base_dir / "README.md"
        if readme.is_file():
            shutil.copy2(readme, staging / "README.md")
        else:
            (staging / "README.md").write_text(
                "# Query-Pool Evidence\n\n"
                "Nine manifests identify the same 10,000 logical queries and exact "
                "ground-truth rows across three datasets and three systems.\n"
            )

        provenance = {
            "assembled_utc": datetime.now(timezone.utc).isoformat(),
            "expected_datasets": ["DEEP10M", "TTI10M", "SIFT10M"],
            "expected_methods": list(METHOD_SLUG),
            "input_roots": {
                "base_dir": str(base_dir.resolve()),
                "tti_graph_dir": str(tti_graph_dir.resolve()),
                "tti_dhnsw_manifest": str(tti_dhnsw_manifest.resolve()),
            },
            "retained_sources": retained,
        }
        (staging / "PROVENANCE.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        )

        validation = validate_query_pools(staging)
        validation["directory"] = "."
        (staging / "VALIDATION.json").write_text(
            json.dumps(validation, indent=2, sort_keys=True) + "\n"
        )
        write_sha256s(staging)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--tti-graph-dir", type=Path, required=True)
    parser.add_argument("--tti-dhnsw-manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    assemble(
        args.base_dir,
        args.tti_graph_dir,
        args.tti_dhnsw_manifest,
        args.out_dir,
    )
    print(f"assembled final VLDB query-pool evidence: {args.out_dir}")


if __name__ == "__main__":
    main()
