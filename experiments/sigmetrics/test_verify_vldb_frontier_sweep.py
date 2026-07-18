#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "verify_vldb_frontier_sweep", HERE / "verify_vldb_frontier_sweep.py"
)
assert SPEC and SPEC.loader
frontier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(frontier)


SHA = "a" * 64


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def make_bundle(root: Path) -> None:
    campaign_id = "frontier-test"
    protocol = {
        "binary_sha256": SHA,
        "datasets": ["DEEP1M"],
        "run_id": "r1",
        "run_kind": "measure",
        "trace": False,
        "measurement_mode": "fixed_query_pool",
        "workers": 10,
        "query_contexts": 10,
        "coroutines": 2,
        "top_k": 10,
        "tcp_port": 1234,
        "timeout_s": 900,
        "compute_host": "skv-node3",
        "method_order_offset": 0,
        "minimum_frontier_points": 5,
    }
    harness = root / "harness"
    harness.mkdir(parents=True)
    runner = harness / "runner__run.sh"
    runner.write_text("#!/bin/sh\n")
    harness_payload = {
        "schema_version": 1,
        "entries": {
            "runner": {
                "path": runner.name,
                "source_path": "/source/run.sh",
                "bytes": runner.stat().st_size,
                "sha256": digest(runner),
                "executable": False,
            }
        },
    }
    harness_manifest = harness / "harness.json"
    harness_manifest.write_text(json.dumps(harness_payload, sort_keys=True))
    campaign = {
        "schema_version": 2,
        "campaign_id": campaign_id,
        "campaign_uuid": "00000000-0000-0000-0000-000000000001",
        "protocol_fingerprint": fingerprint(protocol),
        "protocol": protocol,
        "harness": {
            "manifest": "harness/harness.json",
            "manifest_sha256": digest(harness_manifest),
        },
    }
    (root / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))

    input_records = [
        {
            "dataset": "DEEP1M",
            "role": "query",
            "host": "skv-node3",
            "path": "/data/deep1m/queries/query-uniform.fbin",
            "bytes": 128,
            "sha256": "1" * 64,
        },
        {
            "dataset": "DEEP1M",
            "role": "groundtruth",
            "host": "skv-node3",
            "path": "/data/deep1m/queries/groundtruth-uniform.bin",
            "bytes": 256,
            "sha256": "2" * 64,
        },
        {
            "dataset": "DEEP1M",
            "role": "index_dump",
            "host": "skv-node6",
            "path": "/data/deep1m/dump/index_m16_efc100_node1_of1.dat",
            "bytes": 512,
            "sha256": "3" * 64,
        },
    ]
    input_signature = fingerprint(input_records)
    with (root / "input_manifest.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            delimiter="\t",
            fieldnames=["dataset", "role", "host", "path", "bytes", "sha256"],
        )
        writer.writeheader()
        writer.writerows(input_records)

    rows = []
    for ef in (50, 80, 100, 150, 200):
        for method, variant, lavd, region, env_name in (
            ("SHINE", "shine_path", 0, 0, "none"),
            (
                "SlabWalk",
                "slabwalk_expansion",
                8,
                4294967296,
                "SHINE_CRANE=1 GB_BITMAP_DEDUP=1",
            ),
        ):
            tag = f"DEEP1M_{variant}_r1_measure_T10_ef{ef}"
            raw_path = root / f"{tag}.json"
            err_path = root / f"{tag}.err"
            mn_out = root / f"{tag}.mn.out"
            mn_err = root / f"{tag}.mn.err"
            execution = root / f"{tag}.execution.json"
            qps = 1000 + ef + (100 if method == "SlabWalk" else 0)
            raw = {
                "num_queries": 10000,
                "query_contexts": 10,
                "queries": {
                    "processed": 10000,
                    "recall": ef / 250,
                    "queries_per_sec": qps,
                    "local_latency_samples": 10000,
                    "local_latency_p50_us": 10.0,
                    "local_latency_p95_us": 20.0,
                    "local_latency_p99_us": 30.0,
                    "rdma_posts": 2_000_000,
                    "rdma_reads_in_bytes": 4_000_000_000,
                },
            }
            raw_path.write_text(json.dumps(raw, sort_keys=True))
            err_path.write_text("")
            mn_out.write_text("ready\n")
            mn_err.write_text("")
            artifacts = []
            for path in (raw_path, err_path, mn_out, mn_err):
                artifacts.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": digest(path),
                    }
                )
            execution.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "campaign_id": campaign_id,
                        "cell": {
                            "tag": tag,
                            "dataset": "DEEP1M",
                            "method": method,
                            "variant": variant,
                            "input_signature": input_signature,
                        },
                        "compute_process": {
                            "host": "skv-node3",
                            "pid": 101,
                            "executable": "/bin/shine",
                            "binary_sha256": SHA,
                            "proc_starttime": 123,
                            "identity_verified": True,
                        },
                        "memory_process": {
                            "host": "skv-node6",
                            "pid": 202,
                            "executable": "/bin/shine",
                            "binary_sha256": SHA,
                            "proc_starttime": 456,
                            "identity_verified": True,
                        },
                        "exit_code": 0,
                        "artifacts": artifacts,
                    },
                    sort_keys=True,
                )
            )
            row_protocol = {
                "binary_sha256": SHA,
                "input_signature": input_signature,
                "compute_host": "skv-node3",
                "memory_host": "skv-node6",
                "mn_binary_sha256": SHA,
                "dataset": "DEEP1M",
                "method": method,
                "variant": variant,
                "threads": 10,
                "query_contexts": 10,
                "coroutines": 2,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "latency_mode": "thread_local_steady_clock",
                "tcp_port": 1234,
                "expected_queries": 10000,
                "ef": ef,
                "m": 16,
                "efc": 100,
                "query_suffix": "uniform",
                "lavd": lavd,
                "index_region_bytes": 4294967296,
                "lavd_region_bytes": region,
                "env": env_name,
            }
            rows.append(
                {
                    "dataset": "DEEP1M",
                    "method": method,
                    "variant": variant,
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint(row_protocol),
                    "binary_sha256": SHA,
                    "input_signature": input_signature,
                    "compute_host": "skv-node3",
                    "memory_host": "skv-node6",
                    "mn_binary_sha256": SHA,
                    "run_id": "r1",
                    "run_kind": "measure",
                    "trace": "0",
                    "measurement_mode": "fixed_query_pool",
                    "threads": "10",
                    "query_contexts": "10",
                    "coroutines": "2",
                    "top_k": "10",
                    "metric": "l2",
                    "ef": str(ef),
                    "m": "16",
                    "efc": "100",
                    "query_suffix": "uniform",
                    "lavd": str(lavd),
                    "index_region_bytes": "4294967296",
                    "lavd_region_bytes": str(region),
                    "env": env_name,
                    "recall": str(raw["queries"]["recall"]),
                    "qps": str(qps),
                    "p50_us": "10.0",
                    "p95_us": "20.0",
                    "p99_us": "30.0",
                    "posts_per_q": "200.0",
                    "bytes_per_q": "400000.0",
                    "processed": "10000",
                    "expected_queries": "10000",
                    "failed_queries": "0",
                    "trace_csv": "",
                    "json": raw_path.name,
                    "stderr": err_path.name,
                    "execution_manifest": execution.name,
                    "status": "ok",
                }
            )
    csv_path = root / "slabwalk_shine_frontier_raw.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class FrontierVerifierTest(unittest.TestCase):
    def verify(self, root: Path):
        return frontier.verify_frontier_bundle(
            root,
            expected_binary_sha=SHA,
            expected_campaign_id="frontier-test",
            expected_run_id="r1",
            expected_run_kind="measure",
            expected_datasets={"DEEP1M"},
            expected_threads=10,
            expected_query_contexts=10,
            expected_coroutines=2,
            expected_trace=False,
            min_points=5,
        )

    def test_reparses_complete_matched_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_bundle(root)
            result = self.verify(root)
            self.assertEqual(result["rows"], 10)
            self.assertEqual(result["datasets"], ["DEEP1M"])
            self.assertEqual(result["points_per_method"], 5)

    def test_rejects_raw_json_tamper_even_when_cell_hash_is_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_bundle(root)
            execution_path = next(root.glob("*.execution.json"))
            execution = json.loads(execution_path.read_text())
            raw_path = root / execution["artifacts"][0]["path"]
            raw = json.loads(raw_path.read_text())
            raw["queries"]["queries_per_sec"] += 77
            raw_path.write_text(json.dumps(raw, sort_keys=True))
            execution["artifacts"][0]["bytes"] = raw_path.stat().st_size
            execution["artifacts"][0]["sha256"] = digest(raw_path)
            execution_path.write_text(json.dumps(execution, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "qps"):
                self.verify(root)

    def test_rejects_csv_tamper_after_attacker_recomputes_row_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_bundle(root)
            csv_path = root / "slabwalk_shine_frontier_raw.csv"
            with csv_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["recall"] = "0.999"
            with csv_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "recall"):
                self.verify(root)

    def test_rejects_a_recorded_identity_probe_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_bundle(root)
            execution_path = next(root.glob("*.execution.json"))
            execution = json.loads(execution_path.read_text())
            execution["identity_failure_reason"] = "mn_probe_unreachable"
            execution_path.write_text(json.dumps(execution, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "identity probe failure"):
                self.verify(root)


if __name__ == "__main__":
    unittest.main()
