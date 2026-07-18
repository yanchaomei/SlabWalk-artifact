#!/usr/bin/env python3
"""Parse the historical d-HNSW top-1 supplement logs.

These July 7 logs predate the corrected top-10 evaluator and must not be used
as Recall@10 inputs.  Main-paper frontiers use parse_dhnsw_frontier.py on the
July 9/10 runs instead.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path


EF_VALUES = [48, 64, 96, 128, 200]
BENCHMARK_SECONDS = 20.0
DATASETS = {
    "sift1M": "SIFT",
    "gist1M": "GIST",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_details(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for line in path.read_text().splitlines():
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)
        if len(nums) < 7:
            continue
        latency, recall, network, compute, meta, deserialize, raw_qps = (float(x) for x in nums[:7])
        rows.append(
            {
                "latency_us": latency,
                "recall": recall,
                "network_us": network,
                "compute_us": compute,
                "meta_us": meta,
                "deserialize_us": deserialize,
                "raw_qps_buggy": raw_qps,
            }
        )
    if len(rows) != len(EF_VALUES):
        raise ValueError(f"{path} has {len(rows)} rows, expected {len(EF_VALUES)}")
    return rows


def parse_query_counts(path: Path) -> list[int]:
    counts = [int(match.group(1)) for match in re.finditer(r"Queries executed:\s+(\d+)", path.read_text())]
    expected = len(EF_VALUES) * 10
    if len(counts) != expected:
        raise ValueError(f"{path} has {len(counts)} query-count lines, expected {expected}")
    return [sum(counts[i : i + 10]) for i in range(0, len(counts), 10)]


def parse_rss_gb(path: Path) -> float:
    match = re.search(r"VmRSS:\s+(\d+)\s+kB", path.read_text())
    if not match:
        raise ValueError(f"missing VmRSS in {path}")
    return int(match.group(1)) / (1024.0 * 1024.0)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path}")
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    root = repo_root()
    result_dir = root / "results" / "dhnsw_supplement_20260707"

    sweep_rows: list[dict[str, object]] = []
    endpoint_rows: list[dict[str, object]] = []
    for raw_name, label in DATASETS.items():
        details = parse_details(result_dir / f"{raw_name}_benchmark_details.txt")
        queries = parse_query_counts(result_dir / f"{raw_name}_client.log")
        rss_before = parse_rss_gb(result_dir / f"{raw_name}_server_rss_before.txt")
        rss_after = parse_rss_gb(result_dir / f"{raw_name}_server_rss_after.txt")
        for ef, detail, query_count in zip(EF_VALUES, details, queries):
            qps = query_count / BENCHMARK_SECONDS
            row = {
                "dataset": label,
                "ef": ef,
                "queries_10threads": query_count,
                "duration_s": f"{BENCHMARK_SECONDS:.0f}",
                "qps_recomputed": f"{qps:.1f}",
                "recall": f"{detail['recall']:.6f}",
                "latency_us": f"{detail['latency_us']:.3f}",
                "network_us": f"{detail['network_us']:.3f}",
                "compute_us": f"{detail['compute_us']:.3f}",
                "meta_us": f"{detail['meta_us']:.3f}",
                "deserialize_us": f"{detail['deserialize_us']:.3f}",
                "raw_qps_buggy": f"{detail['raw_qps_buggy']:.6g}",
                "server_rss_before_gb": f"{rss_before:.3f}",
                "server_rss_after_gb": f"{rss_after:.3f}",
            }
            sweep_rows.append(row)
            if ef == 48:
                endpoint_rows.append(row)

    write_csv(result_dir / "dhnsw_fresh_ef_sweep.csv", sweep_rows)
    write_csv(result_dir / "dhnsw_fresh_endpoint_ef48.csv", endpoint_rows)
    print(f"wrote {result_dir / 'dhnsw_fresh_ef_sweep.csv'}")
    print(f"wrote {result_dir / 'dhnsw_fresh_endpoint_ef48.csv'}")


if __name__ == "__main__":
    main()
