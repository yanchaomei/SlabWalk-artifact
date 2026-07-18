#!/usr/bin/env python3
"""Validate the matched three-system recall-QPS frontier matrix."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd


EXPECTED_DATASETS = [
    "SIFT1M",
    "GIST1M",
    "DEEP1M",
    "BIGANN1M",
    "SPACEV1M",
    "TURING1M",
    "TTI1M",
]
EXPECTED_METHODS = ["SHINE", "d-HNSW", "SlabWalk"]


def validate(path: Path, min_points: int, expected_datasets: Optional[list[str]] = None) -> pd.DataFrame:
    expected_datasets = expected_datasets or EXPECTED_DATASETS
    df = pd.read_csv(path)
    required = {"dataset", "method", "ef", "recall", "qps", "threads"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    for column in ("ef", "recall", "qps", "threads"):
        df[column] = pd.to_numeric(df[column], errors="raise")

    selected = df[df["dataset"].isin(expected_datasets)].copy()
    unknown_methods = sorted(set(selected["method"]).difference(EXPECTED_METHODS))
    if unknown_methods:
        raise ValueError(f"unexpected methods: {unknown_methods}")
    if not (selected["threads"] == 10).all():
        bad = selected.loc[selected["threads"] != 10, ["dataset", "method", "threads"]]
        raise ValueError(f"non-matched thread rows:\n{bad.to_string(index=False)}")
    if not selected["recall"].between(0.0, 1.0).all():
        raise ValueError("recall must be in [0, 1]")
    if not (selected["qps"] > 0).all():
        raise ValueError("QPS must be positive")

    duplicates = selected.duplicated(["dataset", "method", "ef"], keep=False)
    if duplicates.any():
        bad = selected.loc[duplicates, ["dataset", "method", "ef"]]
        raise ValueError(f"duplicate frontier points:\n{bad.to_string(index=False)}")

    counts = selected.groupby(["dataset", "method"]).size().unstack(fill_value=0)
    counts = counts.reindex(index=expected_datasets, columns=EXPECTED_METHODS, fill_value=0)
    if (counts < min_points).any().any():
        raise ValueError(
            f"each curve needs at least {min_points} points:\n{counts.to_string()}"
        )

    for (dataset, method), curve in selected.groupby(["dataset", "method"]):
        recalls = curve.sort_values("ef")["recall"].to_numpy()
        if (recalls[1:] + 1e-3 < recalls[:-1]).any():
            raise ValueError(f"recall is not monotone for {dataset}/{method}")

    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--min-points", type=int, default=5)
    parser.add_argument(
        "--expected-datasets",
        default=",".join(EXPECTED_DATASETS),
        help="Comma-separated datasets that must each contain every method",
    )
    args = parser.parse_args()
    expected = [value.strip() for value in args.expected_datasets.split(",") if value.strip()]
    counts = validate(args.csv, args.min_points, expected)
    print("frontier matrix: PASS")
    print(counts.to_string())


if __name__ == "__main__":
    main()
