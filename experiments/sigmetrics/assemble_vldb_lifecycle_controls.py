#!/usr/bin/env python3
"""Assemble retained offline-refresh and TTI boundary controls."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path


REFRESH_BATCHES = (1000, 10000, 50000, 100000)
TTI_CONFIGS = (
    "fp32 baseline",
    "sq8 Slabs",
    "sq8 Slabs+upper graph",
    "RaBitQ-2 Slabs",
    "RaBitQ-4 Slabs",
    "fp32 baseline 16T",
    "sq8 Slabs 16T",
    "sq8 Slabs+upper graph 16T",
)
SELFTEST_RE = re.compile(r"\[selftest\].*?mismatches=(\d+).*?PASS")
RECALL_RE = re.compile(r"local recall:\s*([0-9.eE+-]+)")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"missing lifecycle source CSV: {path}")
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty lifecycle source CSV: {path}")
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty lifecycle CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def close_float(actual: object, expected: object, label: str) -> None:
    if actual in (None, "") and expected in (None, ""):
        return
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric lifecycle field {label}") from exc
    if not math.isfinite(left) or not math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-8):
        raise ValueError(f"lifecycle field mismatch for {label}: {left} != {right}")


def resolve_source(root: Path, source: str) -> Path:
    path = Path(source)
    candidate = path if path.is_absolute() else root / path
    if not candidate.is_file():
        raise ValueError(f"missing lifecycle retained source: {candidate}")
    return candidate


def parse_refresh(path: Path) -> dict[str, object]:
    text = path.read_text(errors="replace")
    candidates: list[dict[str, str]] = []
    for line in text.splitlines():
        if "[LAVD][maintain]" not in line or "[selftest]" in line:
            continue
        tokens: dict[str, str] = {}
        for token in line.split():
            if "=" in token:
                key, value = token.strip("(),").split("=", 1)
                tokens[key] = value.strip("(),")
        if {"inserts", "touched", "write_amp"} <= set(tokens):
            candidates.append(tokens)
    if not candidates:
        raise ValueError(f"{path}: missing refresh maintenance record")
    selected = next((record for record in candidates if record.get("mode") == "diff"), candidates[-1])
    selftests = [int(value) for value in SELFTEST_RE.findall(text)]
    if not selftests or any(value != 0 for value in selftests):
        raise ValueError(f"{path}: offline refresh self-test did not pass")
    recalls = RECALL_RE.findall(text)
    if len(recalls) != 1:
        raise ValueError(f"{path}: expected one post-refresh recall")
    return {
        "batch_inserts": int(selected["inserts"]),
        "touched_blocks": int(selected["touched"]),
        "write_amp_blocks_per_insert": float(selected["write_amp"]),
        "diff_read_frac": float(selected["read_frac"]) if "read_frac" in selected else "",
        "diff_read_mb": float(selected["read_MB"]) if "read_MB" in selected else "",
        "full_index_mb": float(selected["full_idx_MB"]) if "full_idx_MB" in selected else "",
        "byte_identical": "PASS",
        "recall": float(recalls[0]),
    }


def parse_tti(path: Path) -> dict[str, object]:
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid TTI raw JSON") from exc
    queries = record.get("queries")
    meta = record.get("meta")
    if not isinstance(queries, dict) or not isinstance(meta, dict):
        raise ValueError(f"{path}: missing TTI metadata or query record")
    processed = int(queries.get("processed", 0))
    if processed != 10000 or int(record.get("num_queries", 0)) != 10000:
        raise ValueError(f"{path}: incomplete TTI query run")
    return {
        "threads": int(meta.get("compute_threads", 0)),
        "qps": float(queries["queries_per_sec"]),
        "recall": float(queries["recall"]),
        "posts_per_query": float(queries["rdma_posts"]) / processed,
        "mb_per_query": float(queries["rdma_reads_in_bytes"]) / processed / 1e6,
    }


def derive_refresh(
    summary: Path, source_root: Path, destination: Path
) -> list[dict[str, object]]:
    source_rows = read_csv(summary)
    by_batch = {int(row["batch_inserts"]): row for row in source_rows}
    if set(by_batch) != set(REFRESH_BATCHES) or len(by_batch) != len(source_rows):
        raise ValueError("offline-refresh source matrix mismatch")
    rows: list[dict[str, object]] = []
    for batch in REFRESH_BATCHES:
        source_row = by_batch[batch]
        source = resolve_source(source_root, source_row["source"])
        parsed = parse_refresh(source)
        for field in (
            "batch_inserts", "touched_blocks", "write_amp_blocks_per_insert",
            "diff_read_frac", "diff_read_mb", "full_index_mb", "recall",
        ):
            close_float(parsed[field], source_row.get(field, ""), f"refresh/{batch}/{field}")
        if source_row.get("byte_identical") != "PASS":
            raise ValueError(f"refresh/{batch}: source summary is not byte-identical")
        retained = destination / "raw_sources" / "refresh" / source.name
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, retained)
        rows.append({
            **parsed,
            "source": retained.relative_to(destination).as_posix(),
            "source_sha256": file_sha256(retained),
        })
    return rows


def derive_tti(
    summary: Path, source_root: Path, destination: Path
) -> list[dict[str, object]]:
    source_rows = read_csv(summary)
    by_config = {row["config"]: row for row in source_rows}
    if set(by_config) != set(TTI_CONFIGS) or len(by_config) != len(source_rows):
        raise ValueError("TTI boundary source matrix mismatch")
    rows: list[dict[str, object]] = []
    for config in TTI_CONFIGS:
        source_row = by_config[config]
        source = resolve_source(source_root, source_row["source"])
        parsed = parse_tti(source)
        for field in ("threads", "qps", "recall", "posts_per_query", "mb_per_query"):
            close_float(parsed[field], source_row[field], f"tti/{config}/{field}")
        if int(source_row["ef"]) != 300:
            raise ValueError(f"tti/{config}: expected ef=300")
        retained = destination / "raw_sources" / "tti" / source.name
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, retained)
        rows.append({
            "config": config,
            "threads": parsed["threads"],
            "ef": 300,
            "qps": parsed["qps"],
            "recall": parsed["recall"],
            "posts_per_query": parsed["posts_per_query"],
            "mb_per_query": parsed["mb_per_query"],
            "note": source_row.get("note", ""),
            "source": retained.relative_to(destination).as_posix(),
            "source_sha256": file_sha256(retained),
        })
    return rows


def assemble(
    refresh_summary: Path,
    refresh_source_root: Path,
    tti_summary: Path,
    tti_source_root: Path,
    out: Path,
) -> None:
    out = out.resolve()
    if out.exists():
        raise ValueError(f"lifecycle output already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        source_summaries = staging / "source_summaries"
        source_summaries.mkdir(parents=True)
        shutil.copy2(refresh_summary, source_summaries / "refresh.csv")
        shutil.copy2(tti_summary, source_summaries / "tti.csv")
        refresh_rows = derive_refresh(refresh_summary, refresh_source_root, staging)
        tti_rows = derive_tti(tti_summary, tti_source_root, staging)
        write_csv(staging / "refresh.csv", refresh_rows)
        write_csv(staging / "tti.csv", tti_rows)
        inventory = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                inventory.append({
                    "path": path.relative_to(staging).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                })
        manifest = {
            "kind": "vldb_lifecycle_controls_v1",
            "refresh_cells": len(refresh_rows),
            "tti_cells": len(tti_rows),
            "files": inventory,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        os.replace(staging, out)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-summary", type=Path, required=True)
    parser.add_argument("--refresh-source-root", type=Path, required=True)
    parser.add_argument("--tti-summary", type=Path, required=True)
    parser.add_argument("--tti-source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assemble(
        args.refresh_summary,
        args.refresh_source_root,
        args.tti_summary,
        args.tti_source_root,
        args.out,
    )
    print(f"assembled lifecycle controls in {args.out}")


if __name__ == "__main__":
    main()
