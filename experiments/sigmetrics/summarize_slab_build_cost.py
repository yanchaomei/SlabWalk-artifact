#!/usr/bin/env python3
"""Validate and summarize repeated Slab construction measurements."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


STAGES = [
    "lavd_build_fetch",
    "lavd_build_parse",
    "lavd_build_rank",
    "lavd_build_encode",
    "lavd_build_metadata",
    "lavd_build_materialize",
]

DATASET_NAMES = {
    "sift1m": "SIFT1M",
    "deep1m": "DEEP1M",
    "gist1m": "GIST1M",
}

# Two-sided Student-t 95% critical values.  The final experiment uses n=5;
# the wider table keeps the summarizer useful for sensitivity reruns.
T95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}

MULTI_START_RE = re.compile(r"\[LAVD\]\[multi\] start build,.*?bits=(\d+)")
ACCOUNTING_RE = re.compile(r"^LAVD_PHYSICAL_ACCOUNTING (\{.*\})$", re.MULTILINE)
BUILD_RSS_RE = re.compile(r"\[LAVD\]\[build-profile\] peak_rss_kb=(\d+)")
PROCESS_RSS_RE = re.compile(r"Maximum resident set size \(kbytes\):\s*(\d+)")
PUBLICATION_RE = re.compile(r"^LAVD_BUILD_PUBLICATION (\{.*\})$", re.MULTILINE)
SINGLE_BUILD_DONE_RE = re.compile(
    r"^\[LAVD\] build done: N=(\d+) m_max0=\d+ bits=(\d+) "
    r"stride=(\d+) budget_f=([0-9.]+) blocks=(\d+)/(\d+) "
    r"region=(\d+)B\b",
    re.MULTILINE,
)
RABITQ_RUNTIME_RE = re.compile(
    r"^\[LAVD\]\[rabitq\].*?B=(\d+).*?rotation_reused=(true|false)\b",
    re.MULTILINE,
)
PARAMS_RESERVE_BYTES = 16384
REPEAT_RE = re.compile(r"_r(\d+)$")


@dataclass(frozen=True)
class Run:
    dataset: str
    repeat: int
    json_path: Path
    err_path: Path
    n_vectors: int
    authoritative_index_bytes: int
    lavd_cli_bits: int
    code_name: str
    code_bits_per_dimension: int
    record_mode: str
    materialization_fraction: float
    region_bytes: int
    build_rss_kb: int
    process_rss_kb: int
    total_ms: float
    stages_ms: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True, help="Directory containing JSON/err pairs")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument(
        "--expected-datasets",
        default="SIFT1M,DEEP1M,GIST1M",
        help="Comma-separated canonical dataset names",
    )
    parser.add_argument("--expected-repeats", type=int, default=5)
    return parser.parse_args()


def canonical_dataset(data: dict[str, object], path: Path) -> str:
    meta = data.get("meta")
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: missing object field meta")
    raw = str(meta.get("dataset", "")).lower()
    if raw not in DATASET_NAMES:
        raise ValueError(f"{path}: unsupported dataset {raw!r}")
    return DATASET_NAMES[raw]


def parse_repeat(path: Path, data: dict[str, object]) -> int:
    match = REPEAT_RE.search(path.stem)
    if match:
        return int(match.group(1))
    meta = data.get("meta")
    label = str(meta.get("label", "")) if isinstance(meta, dict) else ""
    match = REPEAT_RE.search(label)
    if match:
        return int(match.group(1))
    return 0


def required_float(mapping: dict[str, object], key: str, path: Path) -> float:
    if key not in mapping:
        raise ValueError(f"{path}: missing timing field {key}")
    value = mapping[key]
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{path}: timing field {key} is not finite")
    if float(value) < 0:
        raise ValueError(f"{path}: timing field {key} is negative")
    return float(value)


def single_match(pattern: re.Pattern[str], text: str, field: str, path: Path) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one {field}, found {len(matches)}")
    return matches[0]


def parse_run(json_path: Path) -> Run:
    with json_path.open() as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{json_path}: root must be a JSON object")

    timings = data.get("timings")
    if not isinstance(timings, dict):
        raise ValueError(f"{json_path}: missing timings object")
    total_key = "lavd_build_multi" if "lavd_build_multi" in timings else "lavd_build"
    if total_key not in timings:
        raise ValueError(f"{json_path}: missing Slab build timing")
    total_ms = required_float(timings, total_key, json_path)
    if total_ms <= 0:
        raise ValueError(f"{json_path}: lavd_build must be positive")
    stages_ms = {stage: required_float(timings, stage, json_path) for stage in STAGES}
    stage_sum = sum(stages_ms.values())
    closure = abs(stage_sum - total_ms) / total_ms
    if closure > 0.02:
        raise ValueError(
            f"{json_path}: stage timings do not close within 2% of lavd_build "
            f"(relative error {closure:.4%})"
        )

    err_path = json_path.with_suffix(".err")
    if not err_path.is_file():
        raise ValueError(f"{json_path}: missing paired stderr file {err_path}")
    err_text = err_path.read_text(errors="replace")
    process_rss = single_match(PROCESS_RSS_RE, err_text, "process peak RSS", err_path)
    process_rss_kb = int(process_rss.group(1))
    build_rss_matches = BUILD_RSS_RE.findall(err_text)
    build_rss_kb = int(build_rss_matches[0]) if len(build_rss_matches) == 1 else process_rss_kb
    if process_rss_kb < build_rss_kb:
        raise ValueError(
            f"{err_path}: process peak RSS {process_rss_kb} KiB is below "
            f"build-end peak {build_rss_kb} KiB"
        )

    n_vectors = int(data.get("num_vectors", 0))
    authoritative_index_bytes = int(data.get("estimated_total_index_size", 0))
    if n_vectors <= 0 or authoritative_index_bytes <= 0:
        raise ValueError(f"{json_path}: invalid vector count or authoritative index size")

    accounts = [json.loads(payload) for payload in ACCOUNTING_RE.findall(err_text)]
    if accounts:
        start = single_match(
            MULTI_START_RE, err_text, "multi-MN build start", err_path
        )
        lavd_cli_bits = int(start.group(1))
        scoring_codes = {str(account["scoring_code"]) for account in accounts}
        scoring_bits = {int(account["scoring_bits"]) for account in accounts}
        record_modes = {str(account["record_layout"]) for account in accounts}
        if (
            len(scoring_codes) != 1
            or len(scoring_bits) != 1
            or len(record_modes) != 1
        ):
            raise ValueError(
                f"{err_path}: inconsistent physical accounting across MNs"
            )
        scoring_code = next(iter(scoring_codes))
        code_bits_per_dimension = next(iter(scoring_bits))
        code_name = (
            f"RaBitQ-{code_bits_per_dimension}"
            if scoring_code == "rabitq"
            else f"sq{code_bits_per_dimension}"
        )
        record_mode = next(iter(record_modes))
        materialization_fraction = 1.0
        region_bytes = sum(
            int(account["materialized_bytes"]) for account in accounts
        )
    else:
        publication_match = single_match(
            PUBLICATION_RE, err_text, "single-MN publication record", err_path
        )
        publication = json.loads(publication_match.group(1))
        if (
            publication.get("mode") != "staged_fixed"
            or int(publication.get("workers", 0)) != 20
            or int(publication.get("records", 0)) != n_vectors
        ):
            raise ValueError(f"{err_path}: invalid staged fixed publication")
        done = single_match(
            SINGLE_BUILD_DONE_RE, err_text, "single-MN build completion", err_path
        )
        done_n = int(done.group(1))
        lavd_cli_bits = int(done.group(2))
        stride = int(done.group(3))
        materialization_fraction = float(done.group(4))
        blocks = int(done.group(5))
        block_denominator = int(done.group(6))
        record_bytes = int(done.group(7))
        if (
            done_n != n_vectors
            or blocks != n_vectors
            or block_denominator != n_vectors
            or not math.isclose(materialization_fraction, 1.0)
            or record_bytes != n_vectors * stride
        ):
            raise ValueError(f"{err_path}: invalid single-MN physical accounting")
        if not re.search(
            r"\[LAVD\]\[selftest\] checked=64 fails=0 .* PASS", err_text
        ):
            raise ValueError(f"{err_path}: missing successful 64-record self-test")
        if (
            "retained authoritative snapshot for resident upper graph" not in err_text
            or "reused authoritative build snapshot" not in err_text
        ):
            raise ValueError(f"{err_path}: authoritative snapshot was not reused")
        rabitq = list(RABITQ_RUNTIME_RE.finditer(err_text))
        if len(rabitq) > 1:
            raise ValueError(f"{err_path}: duplicate RaBitQ reconstruction records")
        if rabitq:
            if rabitq[0].group(2) != "true":
                raise ValueError(f"{err_path}: RaBitQ rotation was rebuilt")
            code_bits_per_dimension = int(rabitq[0].group(1))
            code_name = f"RaBitQ-{code_bits_per_dimension}"
        else:
            code_bits_per_dimension = lavd_cli_bits
            code_name = f"sq{code_bits_per_dimension}"
        record_mode = "fixed"
        region_bytes = PARAMS_RESERVE_BYTES + record_bytes

    return Run(
        dataset=canonical_dataset(data, json_path),
        repeat=parse_repeat(json_path, data),
        json_path=json_path,
        err_path=err_path,
        n_vectors=n_vectors,
        authoritative_index_bytes=authoritative_index_bytes,
        lavd_cli_bits=lavd_cli_bits,
        code_name=code_name,
        code_bits_per_dimension=code_bits_per_dimension,
        record_mode=record_mode,
        materialization_fraction=materialization_fraction,
        region_bytes=region_bytes,
        build_rss_kb=build_rss_kb,
        process_rss_kb=process_rss_kb,
        total_ms=total_ms,
        stages_ms=stages_ms,
    )


def collect_runs(raw: Path) -> list[Run]:
    if not raw.is_dir():
        raise ValueError(f"raw directory does not exist: {raw}")
    candidates: list[Path] = []
    for path in sorted(raw.rglob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            timings = data.get("timings")
            if isinstance(timings, dict) and (
                "lavd_build_multi" in timings or "lavd_build" in timings
            ):
                candidates.append(path)
    if not candidates:
        raise ValueError(f"{raw}: no JSON file contains timing field lavd_build")
    return [parse_run(path) for path in candidates]


def t_ci_half(values: Iterable[float]) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    critical = T95.get(len(vals) - 1, 1.960)
    return critical * statistics.stdev(vals) / math.sqrt(len(vals))


def describe(values: Iterable[float]) -> tuple[float, float, float, float]:
    vals = list(values)
    return (
        statistics.mean(vals),
        statistics.median(vals),
        statistics.stdev(vals) if len(vals) > 1 else 0.0,
        t_ci_half(vals),
    )


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def validate_matrix(runs: list[Run], expected: list[str], repeats: int) -> dict[str, list[Run]]:
    grouped: dict[str, list[Run]] = {name: [] for name in expected}
    unexpected = sorted({run.dataset for run in runs} - set(expected))
    if unexpected:
        raise ValueError(f"unexpected datasets in raw directory: {', '.join(unexpected)}")
    for run in runs:
        grouped[run.dataset].append(run)

    for dataset, items in grouped.items():
        if len(items) != repeats:
            raise ValueError(f"{dataset}: expected {repeats} runs, found {len(items)}")
        observed = sorted(run.repeat for run in items)
        wanted = list(range(repeats))
        if observed != wanted:
            raise ValueError(f"{dataset}: repeat ids {observed}, expected {wanted}")
        invariant_fields = {
            (
                run.n_vectors,
                run.authoritative_index_bytes,
                run.lavd_cli_bits,
                run.code_name,
                run.code_bits_per_dimension,
                run.record_mode,
                run.materialization_fraction,
                run.region_bytes,
            )
            for run in items
        }
        if len(invariant_fields) != 1:
            raise ValueError(f"{dataset}: index-size or code configuration changed across repeats")
        items.sort(key=lambda run: run.repeat)
    return grouped


def summarize(grouped: dict[str, list[Run]]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    run_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    stage_rows: list[dict[str, object]] = []

    for dataset, runs in grouped.items():
        for run in runs:
            stage_sum_ms = sum(run.stages_ms.values())
            run_rows.append(
                {
                    "dataset": dataset,
                    "repeat": run.repeat,
                    "n_vectors": run.n_vectors,
                    "lavd_cli_bits": run.lavd_cli_bits,
                    "code_name": run.code_name,
                    "code_bits_per_dimension": run.code_bits_per_dimension,
                    "record_mode": run.record_mode,
                    "materialization_fraction": run.materialization_fraction,
                    "build_s": run.total_ms / 1000.0,
                    "stage_sum_s": stage_sum_ms / 1000.0,
                    "closure_relative": abs(stage_sum_ms - run.total_ms) / run.total_ms,
                    "build_peak_rss_gib": run.build_rss_kb / 1024.0 / 1024.0,
                    "process_peak_rss_gib": run.process_rss_kb / 1024.0 / 1024.0,
                    "region_gb": run.region_bytes / 1e9,
                    "authoritative_index_gb": run.authoritative_index_bytes / 1e9,
                    "storage_amplification": run.region_bytes / run.authoritative_index_bytes,
                    "raw_json": str(run.json_path),
                    "raw_err": str(run.err_path),
                }
            )

        build_s = [run.total_ms / 1000.0 for run in runs]
        build_rss = [run.build_rss_kb / 1024.0 / 1024.0 for run in runs]
        process_rss = [run.process_rss_kb / 1024.0 / 1024.0 for run in runs]
        build_mean, build_median, build_std, build_ci = describe(build_s)
        rss_mean, rss_median, rss_std, rss_ci = describe(build_rss)
        process_mean, process_median, process_std, process_ci = describe(process_rss)
        exemplar = runs[0]
        summary_rows.append(
            {
                "dataset": dataset,
                "repeats": len(runs),
                "n_vectors": exemplar.n_vectors,
                "lavd_cli_bits": exemplar.lavd_cli_bits,
                "code_name": exemplar.code_name,
                "code_bits_per_dimension": exemplar.code_bits_per_dimension,
                "record_mode": exemplar.record_mode,
                "materialization_fraction": exemplar.materialization_fraction,
                "build_mean_s": build_mean,
                "build_median_s": build_median,
                "build_stdev_s": build_std,
                "build_ci95_half_s": build_ci,
                "build_peak_rss_mean_gib": rss_mean,
                "build_peak_rss_median_gib": rss_median,
                "build_peak_rss_stdev_gib": rss_std,
                "build_peak_rss_ci95_half_gib": rss_ci,
                "process_peak_rss_mean_gib": process_mean,
                "process_peak_rss_median_gib": process_median,
                "process_peak_rss_stdev_gib": process_std,
                "process_peak_rss_ci95_half_gib": process_ci,
                "region_gb": exemplar.region_bytes / 1e9,
                "authoritative_index_gb": exemplar.authoritative_index_bytes / 1e9,
                "storage_amplification": exemplar.region_bytes / exemplar.authoritative_index_bytes,
                "raw_json_paths": ";".join(str(run.json_path) for run in runs),
                "raw_err_paths": ";".join(str(run.err_path) for run in runs),
            }
        )

        for stage in STAGES:
            stage_s = [run.stages_ms[stage] / 1000.0 for run in runs]
            shares = [run.stages_ms[stage] / run.total_ms * 100.0 for run in runs]
            mean_s, median_s, std_s, ci_s = describe(stage_s)
            stage_rows.append(
                {
                    "dataset": dataset,
                    "stage": stage[len("lavd_build_") :] if stage.startswith("lavd_build_") else stage,
                    "mean_s": mean_s,
                    "median_s": median_s,
                    "stdev_s": std_s,
                    "ci95_half_s": ci_s,
                    "median_share_pct": statistics.median(shares),
                    "raw_json_paths": ";".join(str(run.json_path) for run in runs),
                }
            )

    return run_rows, summary_rows, stage_rows


def write_readme(path: Path, summaries: list[dict[str, object]]) -> None:
    repeat_counts = {int(row["repeats"]) for row in summaries}
    repeat_text = str(next(iter(repeat_counts))) if len(repeat_counts) == 1 else "the recorded"
    lines = [
        "# Slab construction cost",
        "",
        f"Each row below summarizes {repeat_text} independent construction runs.",
        "The campaign manifest records query and builder parallelism separately.",
        "Intervals are reported with a two-sided 95% Student-t confidence interval.",
        "Build-end RSS is sampled before query state allocation; process peak RSS from",
        "`/usr/bin/time -v` is retained as a cross-check.",
        "",
        "| Dataset | Code | Records | f | Build mean (s) | 95% CI half-width (s) | Build RSS mean (GiB) | Slab region (GB) | Amp. vs. estimated HNSW |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['dataset']} | {row['code_name']} | {row['record_mode']} | "
            f"{float(row['materialization_fraction']):.2f} | "
            f"{float(row['build_mean_s']):.2f} | "
            f"{float(row['build_ci95_half_s']):.2f} | "
            f"{float(row['build_peak_rss_mean_gib']):.2f} | "
            f"{float(row['region_gb']):.2f} | {float(row['storage_amplification']):.2f}x |"
        )
    lines.extend(
        [
            "",
            "Generated with:",
            "",
            "```bash",
            "python3 experiments/sigmetrics/summarize_slab_build_cost.py \\",
            "  --raw results/vldb_build_cost/raw --out results/vldb_build_cost",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> None:
    args = parse_args()
    if args.expected_repeats < 1:
        raise ValueError("--expected-repeats must be positive")
    expected = [item.strip() for item in args.expected_datasets.split(",") if item.strip()]
    if not expected:
        raise ValueError("--expected-datasets is empty")

    runs = collect_runs(args.raw)
    grouped = validate_matrix(runs, expected, args.expected_repeats)
    run_rows, summary_rows, stage_rows = summarize(grouped)

    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(args.out / "runs.csv", run_rows)
    write_csv(args.out / "summary.csv", summary_rows)
    write_csv(args.out / "stage_breakdown.csv", stage_rows)
    write_readme(args.out / "README.md", summary_rows)
    print(f"Validated {len(runs)} runs across {len(grouped)} datasets; wrote {args.out}")


if __name__ == "__main__":
    main()
