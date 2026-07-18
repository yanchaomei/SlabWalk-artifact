#!/usr/bin/env python3
"""Collect three-system recall-QPS frontier curves for the SIGMETRICS paper.

Inputs are intentionally plain CSVs so that long remote sweeps can be resumed
and audited before the plotting script consumes the final table.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


DHNSW_LABELS = {
    "SIFT": "SIFT1M",
    "sift1M": "SIFT1M",
    "sift1m": "SIFT1M",
    "BIGANN": "BIGANN1M",
    "BIGANN1M": "BIGANN1M",
    "bigann1M": "BIGANN1M",
    "bigann1m": "BIGANN1M",
    "SPACEV": "SPACEV1M",
    "SPACEV1M": "SPACEV1M",
    "spacev1M": "SPACEV1M",
    "spacev1m": "SPACEV1M",
    "TURING": "TURING1M",
    "TURING1M": "TURING1M",
    "turing1M": "TURING1M",
    "turing1m": "TURING1M",
    "GIST": "GIST1M",
    "gist1M": "GIST1M",
    "gist1m": "GIST1M",
    "DEEP": "DEEP10M",
    "DEEP1M": "DEEP1M",
    "deep1M": "DEEP1M",
    "deep1m": "DEEP1M",
    "DEEP10M": "DEEP10M",
    "deep10M": "DEEP10M",
    "deep10m": "DEEP10M",
    "TEXT": "TTI10M",
    "TEXT1M": "TTI1M",
    "text1M": "TTI1M",
    "text1m": "TTI1M",
    "TTI1M": "TTI1M",
    "tti1M": "TTI1M",
    "tti1m": "TTI1M",
    "TEXT10M": "TTI10M",
    "text10M": "TTI10M",
    "text10m": "TTI10M",
    "TTI10M": "TTI10M",
    "tti10M": "TTI10M",
    "tti10m": "TTI10M",
    "SIFT10M": "SIFT10M",
    "sift10M": "SIFT10M",
    "sift10m": "SIFT10M",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def require_valid_campaign(source: Path) -> None:
    for directory in (source.parent, source.parent.parent):
        marker = directory / "campaign_validity.json"
        if not marker.exists():
            continue
        record = json.loads(marker.read_text())
        if record.get("status") not in {"valid", "ok"}:
            reason = record.get("reason", "campaign is not valid for plotting")
            raise ValueError(f"refusing frontier source {source}: {reason}")
        return


def add_sw_rows(rows: list[dict[str, str]], source: Path) -> None:
    require_valid_campaign(source)
    for row in read_csv(source):
        if row.get("status") != "ok":
            continue
        recall = row.get("recall", "")
        qps = row.get("qps", "")
        if not recall or not qps:
            continue
        rows.append(
            {
                "dataset": DHNSW_LABELS.get(row["dataset"], row["dataset"]),
                "method": row["method"],
                "unit": "node/vector" if row["method"] == "SHINE" else "one expansion",
                "kind": "sweep",
                "ef": row["ef"],
                "recall": recall,
                "qps": qps,
                "threads": row.get("threads", "10"),
                "source": str(source),
                "note": row.get("variant", ""),
            }
        )


def add_dhnsw_rows(rows: list[dict[str, str]], source: Path) -> None:
    require_valid_campaign(source)
    for row in read_csv(source):
        if row.get("status", "ok") != "ok":
            continue
        raw_dataset = row.get("dataset", "")
        dataset = DHNSW_LABELS.get(raw_dataset, raw_dataset)
        recall = row.get("recall", "")
        qps = row.get("qps_recomputed", row.get("qps", ""))
        if not dataset or not recall or not qps:
            continue
        rows.append(
            {
                "dataset": dataset,
                "method": "d-HNSW",
                "unit": "routed partition",
                "kind": "sweep",
                "ef": row.get("ef", ""),
                "recall": recall,
                "qps": qps,
                "threads": "10",
                "source": str(source),
                "note": "recomputed sustained QPS over 20s",
            }
        )


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["dataset", "method", "unit", "kind", "ef", "recall", "qps", "threads", "source", "note"]
    deduplicated: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["dataset"], row["method"], row["ef"])
        if key in deduplicated:
            print(
                "replace resumed frontier point "
                f"{key}: {deduplicated[key]['source']} -> {row['source']}"
            )
        deduplicated[key] = row
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(
            deduplicated.values(),
            key=lambda r: (r["dataset"], r["method"], float(r["ef"] or 0)),
        ):
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sw", type=Path, action="append", default=[], help="SlabWalk/SHINE raw CSV from run_frontier_sweeps.sh")
    parser.add_argument("--dhnsw", type=Path, action="append", default=[], help="d-HNSW parsed ef-sweep CSV")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/sigmetrics_main_figures/q0_frontier_curves.csv"),
    )
    args = parser.parse_args()

    rows: list[dict[str, str]] = []
    for source in args.sw:
        add_sw_rows(rows, source)
    for source in args.dhnsw:
        add_dhnsw_rows(rows, source)
    if not rows:
        raise SystemExit("no frontier rows collected")
    write_csv(args.out, rows)
    print(f"wrote {args.out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
