#!/usr/bin/env python3
"""Parse checked d-HNSW worker-scaling logs.

The released client prints a latency-derived throughput field.  We instead sum
the per-thread completed-query counters and divide by the configured duration.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path


def parse_run(path: Path, workers: int, duration: float) -> tuple[float, float, str]:
    if not path.exists():
        return 0.0, 0.0, "missing"
    text = path.read_text(errors="replace")
    counts = [int(value) for value in re.findall(r"Queries executed:\s+(\d+)", text)]
    recalls = [
        float(value)
        for value in re.findall(r"Thread\s+\d+\s+recall:\s*([0-9.eE+-]+)", text)
    ]
    if len(counts) != workers or len(recalls) != workers:
        return 0.0, 0.0, f"incomplete counts={len(counts)} recalls={len(recalls)}"
    return sum(counts) / duration, statistics.fmean(recalls), "ok"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--workers", default="1 8 16 40")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--ef", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    worker_values = [int(value) for value in args.workers.replace(",", " ").split()]
    rows: list[dict[str, object]] = []
    for workers in worker_values:
        for rep in range(1, args.repeats + 1):
            source = args.result_dir / f"deep1M_w{workers}_r{rep}_client.log"
            qps, recall, status = parse_run(source, workers, args.duration)
            rows.append(
                {
                    "dataset": "DEEP",
                    "system": "d-HNSW",
                    "threads": workers,
                    "run": rep,
                    "ef": args.ef,
                    "duration_s": f"{args.duration:g}",
                    "qps": f"{qps:.3f}" if status == "ok" else "",
                    "recall": f"{recall:.6f}" if status == "ok" else "",
                    "status": status,
                    "source": str(source),
                }
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
