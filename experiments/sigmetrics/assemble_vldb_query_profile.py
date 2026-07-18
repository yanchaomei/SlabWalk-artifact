#!/usr/bin/env python3
"""Validate and atomically assemble the frozen SIFT1M CPU-profile evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid


TAG = "SIFT1M_shine_T1_C8_ef100"
REQUIRED_PROTOCOL = {
    "datasets": ["SIFT1M"],
    "methods": ["shine"],
    "threads": 1,
    "query_contexts_requested": 1,
    "coroutines": 8,
    "ef": 100,
    "top_k": 10,
    "query_tile": 20,
    "profile_seconds": 20,
    "capture_perf": True,
    "compute_recall": False,
}
PERF_ROW = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)%\s+(\S+)\s+(\S+)\s+\[\.\]\s+(.+?)\s+-\s+-\s*$"
)
LOST_SAMPLES = re.compile(r"^# Total Lost Samples:\s*([0-9]+)\s*$")
SAMPLES = re.compile(r"^# Samples:\s*([0-9]+(?:\.[0-9]+)?)([KMG]?)\s+of event '([^']+)'\s*$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_fingerprint(protocol: dict) -> str:
    payload = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def read_hash_manifest(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise ValueError(f"invalid hash manifest line {path}:{lineno}")
        entries.append((parts[0], parts[1].lstrip(" *")))
    if not entries:
        raise ValueError(f"empty hash manifest: {path}")
    return entries


def parse_perf_report(path: Path) -> tuple[int, int, str, list[dict[str, str]]]:
    lost: int | None = None
    samples: int | None = None
    event = ""
    rows: list[dict[str, str]] = []
    multipliers = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    for line in path.read_text(errors="replace").splitlines():
        if match := LOST_SAMPLES.match(line):
            lost = int(match.group(1))
            continue
        if match := SAMPLES.match(line):
            samples = int(float(match.group(1)) * multipliers[match.group(2)])
            event = match.group(3)
            continue
        if match := PERF_ROW.match(line):
            rows.append(
                {
                    "percent": match.group(1),
                    "command": match.group(2),
                    "shared_object": match.group(3),
                    "symbol": match.group(4).strip(),
                }
            )
    if lost is None:
        raise ValueError("perf report does not disclose lost samples")
    if lost != 0:
        raise ValueError(f"perf report has {lost} lost samples")
    if samples is None or samples <= 0 or event != "cycles:u":
        raise ValueError("perf report has no valid cycles:u sample count")
    if not rows:
        raise ValueError("perf report contains no self-time rows")
    return lost, samples, event, rows


def validate_protocol(campaign: dict, expected_binary_sha: str) -> dict:
    protocol = campaign.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("campaign is missing its protocol")
    if protocol.get("binary_sha256") != expected_binary_sha:
        raise ValueError("campaign binary SHA does not match the frozen binary")
    for key, expected in REQUIRED_PROTOCOL.items():
        if protocol.get(key) != expected:
            raise ValueError(f"profile protocol drift for {key}: {protocol.get(key)!r}")
    if protocol.get("memory_nodes_by_dataset", {}).get("SIFT1M") != "skv-node4":
        raise ValueError("profile memory-node placement drift")
    recorded = campaign.get("protocol_fingerprint")
    if recorded != canonical_fingerprint(protocol):
        raise ValueError("campaign protocol fingerprint mismatch")
    return protocol


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_sha_inventory(root: Path) -> None:
    paths = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    with (root / "SHA256SUMS").open("w") as handle:
        for path in paths:
            handle.write(f"{sha256_file(path)}  {path.relative_to(root)}\n")


def assemble(
    source_dir: Path,
    out_dir: Path,
    *,
    expected_binary_sha: str,
    expected_runner_sha: str,
) -> None:
    source_dir = source_dir.resolve()
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise FileExistsError(f"output exists: {out_dir}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_binary_sha):
        raise ValueError("invalid expected binary SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_runner_sha):
        raise ValueError("invalid expected runner SHA")

    required = [
        "campaign.json",
        "runner_snapshot.sh",
        "profile_sources.sha256",
        f"{TAG}.json",
        f"{TAG}.perf.data",
        f"{TAG}.perf.data.sha256",
        f"{TAG}.perf.record.status",
        f"{TAG}.perf.txt",
    ]
    for name in required:
        if not (source_dir / name).is_file():
            raise ValueError(f"missing profile source: {name}")

    campaign = json.loads((source_dir / "campaign.json").read_text())
    protocol = validate_protocol(campaign, expected_binary_sha)
    runner_sha = sha256_file(source_dir / "runner_snapshot.sh")
    if runner_sha != expected_runner_sha:
        raise ValueError("runner SHA does not match the frozen profile runner")
    source_hashes = {digest for digest, _ in read_hash_manifest(source_dir / "profile_sources.sha256")}
    if expected_binary_sha not in source_hashes or expected_runner_sha not in source_hashes:
        raise ValueError("profile source manifest does not close binary and runner hashes")

    perf_data = source_dir / f"{TAG}.perf.data"
    perf_sha = sha256_file(perf_data)
    perf_manifest = read_hash_manifest(source_dir / f"{TAG}.perf.data.sha256")
    if len(perf_manifest) != 1 or perf_manifest[0][0] != perf_sha:
        raise ValueError("perf.data SHA mismatch")
    if (source_dir / f"{TAG}.perf.record.status").read_text().strip() != "0":
        raise ValueError("perf record did not exit successfully")

    run = json.loads((source_dir / f"{TAG}.json").read_text())
    expected_queries = 10_000 * int(protocol["query_tile"])
    processed = int(run.get("queries", {}).get("processed", -1))
    if int(run.get("num_queries", -1)) != expected_queries or processed != expected_queries:
        raise ValueError("profile did not complete the 200K fixed query stream")
    if int(run.get("query_contexts", -1)) != 1:
        raise ValueError("profile query-context count drift")
    if run.get("meta", {}).get("dataset") != "sift1m":
        raise ValueError("profile dataset drift")
    if int(run.get("meta", {}).get("compute_threads", -1)) != 1:
        raise ValueError("profile worker count drift")
    if int(run.get("meta", {}).get("coroutines_per_thread", -1)) != 8:
        raise ValueError("profile coroutine count drift")
    if run.get("meta", {}).get("query_suffix") != "profile20x":
        raise ValueError("profile query stream drift")
    if int(run.get("hnsw_parameters", {}).get("ef_search", -1)) != 100:
        raise ValueError("profile ef drift")
    if int(run.get("hnsw_parameters", {}).get("k", -1)) != 10:
        raise ValueError("profile top-k drift")

    lost, samples, event, perf_rows = parse_perf_report(source_dir / f"{TAG}.perf.txt")
    distance_rows = [row for row in perf_rows if row["symbol"] == "l2"]
    if len(distance_rows) != 1:
        raise ValueError(f"expected one exact l2 self-time row, found {len(distance_rows)}")
    distance_percent = float(distance_rows[0]["percent"])
    queries = run["queries"]
    posts_per_query = float(queries["rdma_posts"]) / processed
    bytes_per_query = float(queries["rdma_reads_in_bytes"]) / processed

    staging = out_dir.parent / f".{out_dir.name}.staging-{uuid.uuid4().hex}"
    try:
        raw = staging / "raw_sources"
        raw.mkdir(parents=True)
        for name in required:
            shutil.copy2(source_dir / name, raw / name)

        summary_row = {
            "dataset": "SIFT1M",
            "method": "SHINE-derived",
            "threads": 1,
            "query_contexts": 1,
            "coroutines": 8,
            "ef": 100,
            "top_k": 10,
            "query_rows": processed,
            "profile_seconds": int(protocol["profile_seconds"]),
            "samples": samples,
            "lost_samples": lost,
            "event": event,
            "distance_symbol": "l2",
            "distance_self_percent": distance_percent,
            "qps": float(queries["queries_per_sec"]),
            "posts_per_query": posts_per_query,
            "bytes_per_query": bytes_per_query,
            "binary_sha256": expected_binary_sha,
            "runner_sha256": expected_runner_sha,
            "perf_data_sha256": perf_sha,
            "protocol_fingerprint": campaign["protocol_fingerprint"],
        }
        write_csv(staging / "summary" / "summary.csv", list(summary_row), [summary_row])
        segment_rows = [
            {
                **row,
                "role": "useful" if row["symbol"] == "l2" else "observed_other",
            }
            for row in perf_rows
        ]
        write_csv(
            staging / "summary" / "profile_symbols.csv",
            ["percent", "command", "shared_object", "symbol", "role"],
            segment_rows,
        )
        provenance = {
            "campaign_id": campaign.get("campaign_id"),
            "source_directory": str(source_dir),
            "protocol_fingerprint": campaign["protocol_fingerprint"],
            "binary_sha256": expected_binary_sha,
            "runner_sha256": expected_runner_sha,
            "perf_data_sha256": perf_sha,
            "retained_sources": [f"raw_sources/{name}" for name in required],
        }
        (staging / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        validation = {
            "passed": True,
            "dataset": "SIFT1M",
            "query_rows": processed,
            "samples": samples,
            "lost_samples": lost,
            "distance_self_percent": distance_percent,
        }
        (staging / "VALIDATION.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n")
        write_sha_inventory(staging)
        staging.rename(out_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--expected-binary-sha", required=True)
    parser.add_argument("--expected-runner-sha", required=True)
    args = parser.parse_args()
    assemble(
        args.campaign,
        args.out_dir,
        expected_binary_sha=args.expected_binary_sha,
        expected_runner_sha=args.expected_runner_sha,
    )
    print(f"Assembled frozen query profile: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
