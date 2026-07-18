#!/usr/bin/env python3
"""Summarize repeated-run and tail-measurement evidence for the paper."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path


CONFIG_NAMES = {
    "lavd0": "Baseline",
    "lavd8": "SlabWalk-int8",
    "lavd4": "SlabWalk-int4",
}

CACHE_NAMES = {
    "shine": "SHINE-cache-off",
    "shine_c5": "SHINE-cache-5pct",
    "shine_c20": "SHINE-cache-20pct",
    "shine_c50": "SHINE-cache-50pct",
    "lavd": "SlabWalk-int8",
    "crane": "SlabWalk+upper graph",
}

T_CRIT_95 = {
    2: 12.706,
    3: 4.303,
    4: 3.182,
    5: 2.776,
    6: 2.571,
    7: 2.447,
    8: 2.365,
    9: 2.306,
    10: 2.262,
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sample_sd(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return math.sqrt(sum((value - mu) ** 2 for value in values) / (len(values) - 1))


def ci95_half_width(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    tcrit = T_CRIT_95.get(len(values), 1.96)
    return tcrit * sample_sd(values) / math.sqrt(len(values))


def format_float(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def read_lavd_repeats(root: Path) -> list[dict[str, object]]:
    rows_by_config: dict[str, list[dict[str, str]]] = defaultdict(list)
    with (root / "results" / "errbar" / "hero.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            rows_by_config[row["config"]].append(row)

    summaries: list[dict[str, object]] = []
    for config in ("lavd0", "lavd8", "lavd4"):
        valid = [row for row in rows_by_config[config] if row["qps"] != "FAIL"]
        qps = [float(row["qps"]) for row in valid]
        recalls = sorted({row["recall"] for row in valid})
        summaries.append(
            {
                "config": CONFIG_NAMES[config],
                "valid_n": len(qps),
                "failed_n": len(rows_by_config[config]) - len(valid),
                "mean_qps": mean(qps),
                "sample_sd": sample_sd(qps),
                "ci95_half_width": ci95_half_width(qps),
                "min_qps": min(qps),
                "max_qps": max(qps),
                "recall_values": ",".join(recalls),
            }
        )
    return summaries


def read_shine_cache_repeats(root: Path) -> list[dict[str, object]]:
    rows_by_cond: dict[str, list[dict[str, str]]] = defaultdict(list)
    with (root / "results" / "shine_cache_baseline" / "sb.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            rows_by_cond[row["cond"]].append(row)

    baseline_qps = [float(row["qps"]) for row in rows_by_cond["shine"]]
    baseline_mean = mean(baseline_qps)
    summaries: list[dict[str, object]] = []
    for cond in ("shine", "shine_c5", "shine_c20", "shine_c50", "lavd", "crane"):
        rows = rows_by_cond[cond]
        qps = [float(row["qps"]) for row in rows]
        posts_per_query = [float(row["posts"]) / float(row["processed"]) for row in rows]
        cache_hits = [float(row["chit"]) for row in rows]
        below_cache_off = ""
        if cond.startswith("shine_c"):
            below_cache_off = str(all(value < min(baseline_qps) for value in qps))
        summaries.append(
            {
                "condition": CACHE_NAMES[cond],
                "n": len(qps),
                "mean_qps": mean(qps),
                "sample_sd": sample_sd(qps),
                "ci95_half_width": ci95_half_width(qps),
                "relative_to_cache_off": mean(qps) / baseline_mean,
                "posts_per_query": mean(posts_per_query),
                "cache_hits_mean": mean(cache_hits),
                "all_reps_below_cache_off": below_cache_off,
            }
        )
    return summaries


def read_tail_reductions(root: Path) -> list[dict[str, object]]:
    rows_by_dataset: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    with (root / "results" / "sigmetrics_trace_tail_cdf" / "tail_summary.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            rows_by_dataset[row["dataset"]][row["system"]] = row

    reductions: list[dict[str, object]] = []
    for dataset in ("SIFT", "DEEP", "GIST"):
        base = rows_by_dataset[dataset]["Baseline"]
        slab = rows_by_dataset[dataset]["SlabWalk"]
        reductions.append(
            {
                "dataset": dataset,
                "queries": int(base["queries"]),
                "latency_p50_reduction": float(base["latency_p50_ms"]) / float(slab["latency_p50_ms"]),
                "latency_p99_reduction": float(base["latency_p99_ms"]) / float(slab["latency_p99_ms"]),
                "latency_p99_9_reduction": float(base["latency_p99_9_ms"]) / float(slab["latency_p99_9_ms"]),
                "posts_mean_reduction": float(base["posts_mean"]) / float(slab["posts_mean"]),
                "posts_p99_reduction": float(base["posts_p99"]) / float(slab["posts_p99"]),
                "posts_p99_9_reduction": float(base["posts_p99_9"]) / float(slab["posts_p99_9"]),
            }
        )
    return reductions


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path}")
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    path: Path,
    lavd: list[dict[str, object]],
    cache: list[dict[str, object]],
    tails: list[dict[str, object]],
) -> None:
    base = next(row for row in lavd if row["config"] == "Baseline")
    int8 = next(row for row in lavd if row["config"] == "SlabWalk-int8")
    int4 = next(row for row in lavd if row["config"] == "SlabWalk-int4")
    speedup_int8 = float(int8["mean_qps"]) / float(base["mean_qps"])
    speedup_int4 = float(int4["mean_qps"]) / float(base["mean_qps"])

    cache_off = next(row for row in cache if row["condition"] == "SHINE-cache-off")
    cache_50 = next(row for row in cache if row["condition"] == "SHINE-cache-50pct")
    cache_50_post_drop = 1.0 - float(cache_50["posts_per_query"]) / float(cache_off["posts_per_query"])

    p99_tail_min = min(float(row["latency_p99_reduction"]) for row in tails)
    p99_tail_max = max(float(row["latency_p99_reduction"]) for row in tails)
    p999_tail_min = min(float(row["latency_p99_9_reduction"]) for row in tails)
    p999_tail_max = max(float(row["latency_p99_9_reduction"]) for row in tails)

    text = f"""# SIGMETRICS Measurement-Rigor Summary

Generated by:

```bash
python3 experiments/sigmetrics/measurement_rigor_summary.py
```

## Repeated SIFT single-CN mechanism point

Source: `results/errbar/hero.csv`.  Each valid run uses SIFT-1M, M=16,
efC=100, ef=100, k=10, c=8, one CN and one MN.  One baseline repeat hit an
MN-startup failure and is excluded before statistics.

| config | valid n | failed n | mean QPS | sample sd | 95% t CI half-width | range | recall values |
|---|---:|---:|---:|---:|---:|---:|---|
"""
    for row in lavd:
        text += (
            f"| {row['config']} | {row['valid_n']} | {row['failed_n']} | "
            f"{format_float(float(row['mean_qps']), 1)} | "
            f"{format_float(float(row['sample_sd']), 1)} | "
            f"+/-{format_float(float(row['ci95_half_width']), 1)} | "
            f"{format_float(float(row['min_qps']), 0)}-{format_float(float(row['max_qps']), 0)} | "
            f"{row['recall_values']} |\n"
        )

    text += f"""
Same-campaign ratio of means: SlabWalk-int8 is {format_float(speedup_int8, 2)}x
over baseline; SlabWalk-int4 is {format_float(speedup_int4, 2)}x.  Recall is
deterministic across valid repeats for each configuration.

## SHINE cache fairness control

Source: `results/shine_cache_baseline/sb.csv`.  These n=3 runs use one session
and test SHINE's own cache rather than SlabWalk with a cache wrapper.

| condition | n | mean QPS | sample sd | 95% t CI half-width | relative to cache-off | posts/query | all reps below cache-off |
|---|---:|---:|---:|---:|---:|---:|---|
"""
    for row in cache:
        text += (
            f"| {row['condition']} | {row['n']} | "
            f"{format_float(float(row['mean_qps']), 1)} | "
            f"{format_float(float(row['sample_sd']), 1)} | "
            f"+/-{format_float(float(row['ci95_half_width']), 1)} | "
            f"{format_float(float(row['relative_to_cache_off']), 2)}x | "
            f"{format_float(float(row['posts_per_query']), 1)} | "
            f"{row['all_reps_below_cache_off']} |\n"
        )

    text += f"""
At the 50% cache budget, SHINE removes {format_float(cache_50_post_drop * 100, 1)}%
of posted reads but every replicate remains below cache-off throughput.

## Tail trace reductions

Source: `results/sigmetrics_trace_tail_cdf/tail_summary.csv`.  These are
10K-query no-batching traces; throughput figures in the paper use the separate
8-coroutine denominator.

| dataset | queries | P50 latency reduction | P99 latency reduction | P99.9 latency reduction | mean post reduction | P99 post reduction | P99.9 post reduction |
|---|---:|---:|---:|---:|---:|---:|---:|
"""
    for row in tails:
        text += (
            f"| {row['dataset']} | {row['queries']} | "
            f"{format_float(float(row['latency_p50_reduction']), 2)}x | "
            f"{format_float(float(row['latency_p99_reduction']), 2)}x | "
            f"{format_float(float(row['latency_p99_9_reduction']), 2)}x | "
            f"{format_float(float(row['posts_mean_reduction']), 2)}x | "
            f"{format_float(float(row['posts_p99_reduction']), 2)}x | "
            f"{format_float(float(row['posts_p99_9_reduction']), 2)}x |\n"
        )

    text += f"""
Across SIFT/DEEP/GIST, P99 latency falls {format_float(p99_tail_min, 1)}-{format_float(p99_tail_max, 1)}x
and P99.9 latency falls {format_float(p999_tail_min, 1)}-{format_float(p999_tail_max, 1)}x.

## Paper use

The main body uses the repeated-run numbers only in the experiment setup
paragraph, because a separate error-bar figure would duplicate the Q1 mechanism
figure and consume page budget.  The CSV summaries in this directory are meant
for camera-ready audit, rebuttal, and slides.
"""
    path.write_text(text)


def main() -> None:
    root = repo_root()
    out_dir = root / "results" / "sigmetrics_measurement_rigor"
    out_dir.mkdir(parents=True, exist_ok=True)

    lavd = read_lavd_repeats(root)
    cache = read_shine_cache_repeats(root)
    tails = read_tail_reductions(root)

    write_csv(out_dir / "sift_repeat_ci.csv", lavd)
    write_csv(out_dir / "shine_cache_ci.csv", cache)
    write_csv(out_dir / "tail_reduction_summary.csv", tails)
    write_markdown(out_dir / "README.md", lavd, cache, tails)

    print(f"wrote {out_dir}")


if __name__ == "__main__":
    main()
