#!/usr/bin/env python3
"""Validate and summarize the SlabWalk robustness-control matrix."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


T_975 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def finite_number(value: Any, name: str, source: Path) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: missing or invalid {name}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{source}: non-finite {name}")
    return result


def parse_query_metrics(
    obj: dict[str, Any], source: Path, require_latency: bool = True
) -> dict[str, float | int | None]:
    queries = obj.get("queries")
    if not isinstance(queries, dict):
        raise ValueError(f"{source}: missing queries object")
    processed = int(queries.get("processed", -1))
    expected = int(obj.get("num_queries", -1))
    if processed <= 0 or processed != expected:
        raise ValueError(
            f"{source}: processed query count {processed} does not match {expected}"
        )
    p50 = p95 = p99 = None
    if require_latency:
        samples = int(queries.get("local_latency_samples", -1))
        if samples != processed:
            raise ValueError(
                f"{source}: latency sample count {samples} does not match {processed}"
            )
        p50 = finite_number(queries.get("local_latency_p50_us"), "p50", source)
        p95 = finite_number(queries.get("local_latency_p95_us"), "p95", source)
        p99 = finite_number(queries.get("local_latency_p99_us"), "p99", source)
        if not (0 <= p50 <= p95 <= p99):
            raise ValueError(f"{source}: latency quantiles are not monotonic")
    posts = finite_number(queries.get("rdma_posts"), "rdma_posts", source)
    read_bytes = finite_number(
        queries.get("rdma_reads_in_bytes"), "rdma_reads_in_bytes", source
    )
    qps = finite_number(queries.get("queries_per_sec"), "queries_per_sec", source)
    recall = finite_number(queries.get("recall"), "recall", source)
    if qps <= 0 or not 0 <= recall <= 1:
        raise ValueError(f"{source}: invalid QPS or recall")
    return {
        "processed": processed,
        "recall": recall,
        "qps": qps,
        "p50_us": p50,
        "p95_us": p95,
        "p99_us": p99,
        "posts_per_query": posts / processed,
        "bytes_per_query": read_bytes / processed,
    }


def t_ci_half(values: Iterable[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return 0.0
    df = len(values) - 1
    critical = T_975.get(df, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def validate_matrix(
    rows: list[dict[str, Any]], expected_cells: list[tuple[str, str]], repeats: int
) -> None:
    measured = [row for row in rows if row["run_kind"] == "measure"]
    campaigns = {row["campaign_id"] for row in measured}
    if len(campaigns) != 1:
        raise ValueError(f"campaign drift: {sorted(campaigns)}")
    seen_cells = {(row["factor"], row["value"]) for row in measured}
    if seen_cells != set(expected_cells):
        raise ValueError(
            f"cell mismatch: expected {sorted(expected_cells)}, got {sorted(seen_cells)}"
        )
    for factor, value in expected_cells:
        cell = [
            row for row in measured
            if row["factor"] == factor and row["value"] == value
        ]
        repeat_set = sorted(int(row["repeat"]) for row in cell)
        if repeat_set != list(range(repeats)):
            raise ValueError(
                f"{factor}={value}: repeat set {repeat_set} != {list(range(repeats))}"
            )
        fingerprints = {row["protocol_fingerprint"] for row in cell}
        if len(fingerprints) != 1:
            raise ValueError(f"{factor}={value}: protocol drift across repeats")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_runs(raw_csv: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in read_csv(raw_csv):
        if raw["status"] != "ok":
            raise ValueError(
                f"failed run {raw['factor']}={raw['value']} "
                f"{raw['run_kind']} r{raw['repeat']}: {raw['status']}"
            )
        source = Path(raw["json"])
        if not source.is_absolute():
            source = raw_csv.parent / source
        latency_enabled = raw.get("latency_enabled", "1") == "1"
        metrics = parse_query_metrics(
            json.loads(source.read_text()), source, require_latency=latency_enabled
        )
        row: dict[str, Any] = dict(raw)
        row["repeat"] = int(raw["repeat"])
        for field in ("threads", "query_contexts", "coroutines", "top_k", "ef"):
            row[field] = int(raw[field])
        row.update(metrics)
        rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    measured = [row for row in rows if row["run_kind"] == "measure"]
    keys = sorted({(row["factor"], row["value"]) for row in measured})
    output: list[dict[str, Any]] = []
    metrics = (
        "recall", "qps", "p50_us", "p95_us", "p99_us",
        "posts_per_query", "bytes_per_query",
    )
    for factor, value in keys:
        cell = [
            row for row in measured
            if row["factor"] == factor and row["value"] == value
        ]
        record: dict[str, Any] = {
            "factor": factor,
            "value": value,
            "n": len(cell),
            "threads": cell[0]["threads"],
            "query_contexts": cell[0]["query_contexts"],
            "coroutines": cell[0]["coroutines"],
            "top_k": cell[0]["top_k"],
            "ef": cell[0]["ef"],
            "query_suffix": cell[0]["query_suffix"],
        }
        for metric in metrics:
            values = [float(row[metric]) for row in cell if row[metric] is not None]
            record[f"{metric}_mean"] = statistics.mean(values) if values else ""
            record[f"{metric}_ci95"] = t_ci_half(values) if values else ""
        output.append(record)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    if args.repeats < 2:
        raise ValueError("at least two measured repeats are required")
    expected = [(row["factor"], row["value"]) for row in read_csv(args.matrix)]
    if len(expected) != len(set(expected)):
        raise ValueError("matrix contains duplicate cells")
    runs = load_runs(args.raw)
    validate_matrix(runs, expected, args.repeats)
    summary = summarize(runs)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "runs.csv", runs)
    write_csv(args.out_dir / "summary.csv", summary)
    campaign_id = next(row["campaign_id"] for row in runs if row["run_kind"] == "measure")
    (args.out_dir / "README.md").write_text(
        "# SlabWalk robustness controls\n\n"
        f"Campaign: `{campaign_id}`. Each cell uses one warmup followed by "
        f"{args.repeats} measured fixed-query-pool runs. Error bars are "
        "two-sided 95% Student-t confidence intervals. Latency is local "
        "per-query service time measured with a thread-local steady clock; "
        "sample counts must equal processed queries.\n"
    )


if __name__ == "__main__":
    main()
