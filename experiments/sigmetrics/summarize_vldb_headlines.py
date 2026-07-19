#!/usr/bin/env python3
"""Derive recall-guarded manuscript headline candidates from gated 10M frontiers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path

import plot_vldb_frontier_10m as frontier_plot
from publication_metadata import normalize_publication_paths, publication_timestamp


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def optional_metric(row: dict[str, str], name: str, source: Path) -> float | None:
    raw_n = row.get(f"{name}_n", "").strip()
    raw_value = row.get(f"{name}_mean", "").strip()
    if not raw_n and not raw_value:
        return None
    try:
        count = int(raw_n or "0")
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {name}_n") from exc
    if count == 0 and not raw_value:
        return None
    if count != 5:
        raise ValueError(f"{source}: {name} must have five repeats, found {count}")
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{source}: invalid {name}_mean") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{source}: non-positive {name}_mean")
    return value


def summary_optional_metrics(summary: Path) -> dict[tuple[str, str, float], dict[str, float | None]]:
    with summary.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    metrics: dict[tuple[str, str, float], dict[str, float | None]] = {}
    for row in rows:
        try:
            key = (row["dataset"], row["method"], float(row["ef"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{summary}: invalid frontier summary key") from exc
        if key in metrics:
            raise ValueError(f"{summary}: duplicate frontier summary key {key}")
        metrics[key] = {
            "posts_per_query": optional_metric(row, "posts_per_query", summary),
            "bytes_per_query": optional_metric(row, "bytes_per_query", summary),
        }
    return metrics


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    return numerator / denominator


def derive(
    summary: Path,
    gate: Path,
    *,
    recall_tolerance: float = 0.002,
    recall_floor: float = 0.90,
) -> dict[str, object]:
    if not math.isfinite(recall_tolerance) or recall_tolerance < 0:
        raise ValueError("recall tolerance must be finite and non-negative")
    if not math.isfinite(recall_floor) or not 0 <= recall_floor <= 1:
        raise ValueError("recall floor must be finite and in [0, 1]")
    rows = frontier_plot.load_validated(summary, gate)
    optional = summary_optional_metrics(summary)
    datasets: dict[str, object] = {}
    high_recall_pairs: list[tuple[str, dict[str, object]]] = []

    for dataset in frontier_plot.DATASETS:
        current = [row for row in rows if row["dataset"] == dataset]
        shine = {float(row["ef"]): row for row in current if row["method"] == "SHINE"}
        slab = {float(row["ef"]): row for row in current if row["method"] == "SlabWalk"}
        if set(shine) != set(slab):
            raise ValueError(f"{dataset}: SHINE/SlabWalk ef grids differ")
        pairs: list[dict[str, object]] = []
        for ef in sorted(shine):
            base = shine[ef]
            design = slab[ef]
            recall_delta = float(design["recall"]) - float(base["recall"])
            base_metrics = optional[(dataset, "SHINE", ef)]
            slab_metrics = optional[(dataset, "SlabWalk", ef)]
            pair: dict[str, object] = {
                "ef": ef,
                "shine_recall": float(base["recall"]),
                "slabwalk_recall": float(design["recall"]),
                "recall_delta": recall_delta,
                "recall_matched": abs(recall_delta) <= recall_tolerance,
                "shine_qps": float(base["qps"]),
                "slabwalk_qps": float(design["qps"]),
                "qps_speedup": float(design["qps"]) / float(base["qps"]),
                "shine_posts_per_query": base_metrics["posts_per_query"],
                "slabwalk_posts_per_query": slab_metrics["posts_per_query"],
                "post_reduction": ratio(
                    base_metrics["posts_per_query"], slab_metrics["posts_per_query"]
                ),
                "shine_bytes_per_query": base_metrics["bytes_per_query"],
                "slabwalk_bytes_per_query": slab_metrics["bytes_per_query"],
                "byte_reduction": ratio(
                    base_metrics["bytes_per_query"], slab_metrics["bytes_per_query"]
                ),
            }
            pairs.append(pair)

        matched = [pair for pair in pairs if pair["recall_matched"]]
        headline_eligible = [
            pair
            for pair in matched
            if min(
                float(pair["shine_recall"]),
                float(pair["slabwalk_recall"]),
            )
            + 1e-12
            >= recall_floor
            and float(pair["qps_speedup"]) > 1.0 + 1e-12
            and pair["post_reduction"] is not None
            and float(pair["post_reduction"]) > 1.0 + 1e-12
        ]
        high = (
            max(
                headline_eligible,
                key=lambda pair: min(
                    float(pair["shine_recall"]), float(pair["slabwalk_recall"])
                ),
            )
            if headline_eligible
            else None
        )
        if high is not None:
            high_recall_pairs.append((dataset, high))

        dhnsw = [row for row in current if row["method"] == "d-HNSW"]
        max_recall = max(dhnsw, key=lambda row: (float(row["recall"]), float(row["qps"])))
        datasets[dataset] = {
            "same_ef_graph_pairs": pairs,
            "matched_pair_count": len(matched),
            "high_recall_matched_pair": high,
            "dhnsw_max_recall": {
                "ef": float(max_recall["ef"]),
                "recall": float(max_recall["recall"]),
                "qps": float(max_recall["qps"]),
            },
        }

    speedups = [float(pair["qps_speedup"]) for _, pair in high_recall_pairs]
    post_reductions = [
        float(pair["post_reduction"])
        for _, pair in high_recall_pairs
        if pair["post_reduction"] is not None
    ]
    return {
        "kind": "vldb_headline_candidates",
        "created_utc": publication_timestamp(),
        "summary": str(summary.resolve()),
        "summary_sha256": file_sha256(summary),
        # The gate is generated in a fresh staging directory before atomic
        # publication.  Persist its logical name, not that ephemeral path.
        "gate": gate.name,
        "gate_sha256": file_sha256(gate),
        "recall_tolerance": recall_tolerance,
        "recall_floor": recall_floor,
        "datasets": datasets,
        "headline_ranges": {
            "matched_datasets": [dataset for dataset, _ in high_recall_pairs],
            "high_recall_qps_speedup_min": min(speedups) if speedups else None,
            "high_recall_qps_speedup_max": max(speedups) if speedups else None,
            "high_recall_post_reduction_min": min(post_reductions) if post_reductions else None,
            "high_recall_post_reduction_max": max(post_reductions) if post_reductions else None,
        },
    }


def summarize(
    summary: Path,
    gate: Path,
    out: Path,
    *,
    recall_tolerance: float = 0.002,
    recall_floor: float = 0.90,
    path_root: Path | None = None,
) -> None:
    report = derive(
        summary,
        gate,
        recall_tolerance=recall_tolerance,
        recall_floor=recall_floor,
    )
    if path_root is not None:
        report = normalize_publication_paths(report, path_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--recall-tolerance", type=float, default=0.002)
    parser.add_argument("--recall-floor", type=float, default=0.90)
    parser.add_argument("--path-root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    summarize(
        args.summary,
        args.gate,
        args.out,
        recall_tolerance=args.recall_tolerance,
        recall_floor=args.recall_floor,
        path_root=args.path_root,
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
