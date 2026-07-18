from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import summarize_vldb_colocation_control as coloc_summary


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
DEGREES = ("full", "24", "16", "8", "4", "1")
INLINE_CODES = {"full": 32, "24": 24, "16": 16, "8": 8, "4": 4, "1": 1}


def write_fixture(
    root: Path,
    *,
    omit: tuple[str, int] | None = None,
    bad_selftest: tuple[str, int] | None = None,
) -> None:
    protocol = {
        "binary_sha256": FINAL_SHA,
        "dataset": "DEEP1M",
        "degrees": list(DEGREES),
        "inline_codes": INLINE_CODES,
        "m_max0": 32,
        "code": "sq8",
        "repeats": 5,
        "warmups": 1,
        "threads": 10,
        "query_contexts": 10,
        "coroutines": 2,
        "ef_search": 200,
        "top_k": 10,
        "query_suffix": "uniform",
        "queries_per_run": 10000,
        "memory_node": "skv-node5",
        "tcp_port": 1314,
        "index_region_bytes": 4294967296,
        "lavd_region_bytes": 6442450944,
        "index_dump_sha256": "c" * 64,
        "query_sha256": "1" * 64,
        "groundtruth_sha256": "2" * 64,
        "runner_sha256": "d" * 64,
        "summarizer_sha256": "e" * 64,
        "fingerprint_tool_sha256": "f" * 64,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (root / "campaign.json").write_text(json.dumps({
        "campaign_id": "coloc-fixture",
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }))
    query_pool = root / "query_pools"
    query_pool.mkdir()
    (query_pool / "deep1m_slabwalk.json").write_text(json.dumps({
        "kind": "query_pool_fingerprint",
        "dataset": "DEEP1M",
        "method": "SlabWalk",
        "metric": "l2",
        "limit": 10000,
        "query": {"rows": 10000, "canonical_sha256": "a" * 64},
        "groundtruth": {"rows": 10000, "canonical_ids_sha256": "b" * 64},
    }))

    for degree in DEGREES:
        inline = INLINE_CODES[degree]
        for run_kind, repeats in (("warmup", 1), ("measure", 5)):
            for repeat in range(repeats):
                if run_kind == "measure" and omit == (degree, repeat):
                    continue
                cell = root / "raw" / degree / f"{run_kind}_r{repeat}"
                cell.mkdir(parents=True)
                environment = {
                    "SHINE_CRANE": "1",
                    "GB_BITMAP_DEDUP": "1",
                    "SHINE_LAVD_HOT_COLD_BATCH": "1",
                    "SHINE_LAVD_SELFTEST": "1",
                    "SHINE_LAVD_COLOC_SELFTEST": "1",
                    "GB_QUERY_LATENCY": "1",
                }
                if degree != "full":
                    environment["SHINE_LAVD_COLOC_DEGREE"] = degree
                command = [
                    "shine", "--lavd", "8", "--threads", "10",
                    "--query-contexts", "10", "--coroutines", "2",
                    "--ef-search", "200", "--ef-construction", "100",
                    "--m", "16", "--k", "10",
                    "--query-suffix", "uniform", "--load-index",
                    "--lavd-region-bytes", "6442450944",
                ]
                (cell / "manifest.json").write_text(json.dumps({
                    "campaign_id": "coloc-fixture",
                    "protocol_fingerprint": fingerprint,
                    "degree": degree,
                    "run_kind": run_kind,
                    "repeat": repeat,
                    "binary_sha256": FINAL_SHA,
                    "observed_inputs": {
                        "cn_binary": FINAL_SHA,
                        "mn_binary": FINAL_SHA,
                        "index_dump": "c" * 64,
                        "query": "1" * 64,
                        "groundtruth": "2" * 64,
                    },
                    "environment": environment,
                    "command": command,
                }))
                posts_per_query = 190.0 + (32 - inline) * 20.0
                bytes_per_query = 500000.0 + (32 - inline) * 10000.0
                qps = 16000.0 - (32 - inline) * 280.0 + repeat
                (cell / "cn.json").write_text(json.dumps({
                    "meta": {
                        "dataset": "deep1m",
                        "compute_threads": 10,
                        "coroutines_per_thread": 2,
                        "memory_nodes": 1,
                        "query_suffix": "uniform",
                    },
                    "query_contexts": 10,
                    "num_queries": 10000,
                    "distance": "squared_l2",
                    "queries": {
                        "processed": 10000,
                        "queries_per_sec": qps,
                        "recall": 0.98907 + repeat * 1e-6,
                        "rdma_posts": posts_per_query * 10000,
                        "rdma_reads_in_bytes": bytes_per_query * 10000,
                        "local_latency_p50_us": 900.0,
                        "local_latency_p95_us": 1200.0,
                        "local_latency_p99_us": 1500.0,
                        "local_latency_samples": 10000,
                    },
                }))
                fails = 1 if run_kind == "measure" and bad_selftest == (degree, repeat) else 0
                (cell / "cn.err").write_text(
                    f"[LAVD][selftest] checked=64 fails={fails} coloc_d={inline}  "
                    f"{'PASS' if fails == 0 else 'FAIL'}\n"
                )
                mn = cell / "mn"
                mn.mkdir()
                (mn / "mn.err").write_text("clean server\n")
                (mn / "status").write_text("0\n")


class VldbColocationControlSummaryTest(unittest.TestCase):
    def test_recomputes_complete_six_degree_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            out = root / "summary"
            report = coloc_summary.summarize(root, out, FINAL_SHA)
            self.assertEqual(report["measured_runs"], 30)
            rows = coloc_summary.read_csv(out / "summary.csv")
            self.assertEqual({row["degree"] for row in rows}, set(DEGREES))
            self.assertTrue(all(int(row["n"]) == 5 for row in rows))
            full = next(row for row in rows if row["degree"] == "full")
            d1 = next(row for row in rows if row["degree"] == "1")
            self.assertEqual(int(full["inline_codes"]), 32)
            self.assertGreater(float(d1["posts_per_query_mean"]), float(full["posts_per_query_mean"]))
            self.assertLess(float(d1["qps_mean"]), float(full["qps_mean"]))

    def test_rejects_a_missing_measured_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root, omit=("8", 4))
            with self.assertRaisesRegex(
                ValueError,
                "missing co-location cell files|incomplete co-location matrix",
            ):
                coloc_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_layout_selftest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root, bad_selftest=("16", 2))
            with self.assertRaisesRegex(ValueError, "layout selftest"):
                coloc_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_degree_environment_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            manifest = root / "raw" / "8" / "measure_r0" / "manifest.json"
            obj = json.loads(manifest.read_text())
            obj["environment"]["SHINE_LAVD_COLOC_DEGREE"] = "4"
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "co-location degree environment mismatch"):
                coloc_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_per_cell_input_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            manifest = root / "raw" / "8" / "measure_r2" / "manifest.json"
            obj = json.loads(manifest.read_text())
            obj["observed_inputs"]["query"] = "9" * 64
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "observed input SHA mismatch"):
                coloc_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_source_symlink_outside_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn = root / "raw" / "24" / "measure_r3" / "cn.json"
            outside = Path(tmp) / "outside.json"
            outside.write_bytes(cn.read_bytes())
            cn.unlink()
            cn.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "bundle-contained regular file"):
                coloc_summary.summarize(root, root / "summary", FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
