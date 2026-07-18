from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import summarize_vldb_cache_control as cache_summary


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


def write_fixture(root: Path, *, omit: tuple[str, int] | None = None) -> None:
    protocol = {
        "binary_sha256": FINAL_SHA,
        "dataset": "SIFT1M",
        "conditions": ["off", "c5", "c20", "c50"],
        "repeats": 5,
        "warmups": 1,
        "threads": 1,
        "query_contexts": 1,
        "coroutines": 8,
        "ef_search": 100,
        "top_k": 10,
        "query_suffix": "uniform",
        "queries_per_run": 10000,
        "memory_node": "skv-node2",
        "tcp_port": 1310,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (root / "campaign.json").write_text(json.dumps({
        "campaign_id": "cache-fixture",
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }))
    qp = root / "query_pools"
    qp.mkdir()
    (qp / "sift1m_shine.json").write_text(json.dumps({
        "dataset": "SIFT1M",
        "method": "SHINE",
        "metric": "l2",
        "query": {"rows": 10000, "canonical_sha256": "a" * 64},
        "groundtruth": {"rows": 10000, "canonical_ids_sha256": "b" * 64},
    }))

    qps_base = {"off": 1000, "c5": 900, "c20": 800, "c50": 750}
    posts_base = {"off": 17_600_000, "c5": 14_800_000, "c20": 9_800_000, "c50": 2_960_000}
    ratios = {"off": 0, "c5": 5, "c20": 20, "c50": 50}
    for condition in protocol["conditions"]:
        for run_kind, repeats in (("warmup", 1), ("measure", 5)):
            for repeat in range(repeats):
                if run_kind == "measure" and omit == (condition, repeat):
                    continue
                cell = root / "raw" / condition / f"{run_kind}_r{repeat}"
                cell.mkdir(parents=True)
                cache_on = condition != "off"
                command = ["shine", "--lavd", "0"]
                if cache_on:
                    command += ["--cache", "--cache-ratio", str(ratios[condition])]
                manifest = {
                    "campaign_id": "cache-fixture",
                    "protocol_fingerprint": fingerprint,
                    "condition": condition,
                    "run_kind": run_kind,
                    "repeat": repeat,
                    "binary_sha256": FINAL_SHA,
                    "command": command,
                }
                (cell / "manifest.json").write_text(json.dumps(manifest))
                qps = qps_base[condition] + repeat
                posts = posts_base[condition] + repeat * 1000
                hits = 0 if not cache_on else int(20_000_000 - posts)
                cn = {
                    "meta": {
                        "dataset": "sift1m",
                        "compute_threads": 1,
                        "coroutines_per_thread": 8,
                        "memory_nodes": 1,
                        "query_suffix": "uniform",
                    },
                    "query_contexts": 1,
                    "num_queries": 10000,
                    "distance": "squared_l2",
                    "cache": {
                        "cache_size_ratio": ratios[condition],
                        "hits_total": hits,
                        "misses_total": posts if cache_on else 0,
                    },
                    "queries": {
                        "processed": 10000,
                        "queries_per_sec": qps,
                        "recall": 0.9766,
                        "rdma_posts": posts,
                        "rdma_reads_in_bytes": posts * 64,
                        "local_latency_p50_us": 1000.0,
                        "local_latency_p95_us": 1200.0,
                        "local_latency_p99_us": 1400.0,
                        "local_latency_samples": 10000,
                    },
                }
                (cell / "cn.json").write_text(json.dumps(cn))
                (cell / "cn.err").write_text("clean run\n")
                mn = cell / "mn"
                mn.mkdir()
                (mn / "mn.err").write_text("clean server\n")
                (mn / "status").write_text("0\n")


class VldbCacheControlSummaryTest(unittest.TestCase):
    def test_recomputes_complete_four_condition_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            out = root / "summary"
            report = cache_summary.summarize(root, out, FINAL_SHA)
            self.assertEqual(report["measured_runs"], 20)
            rows = cache_summary.read_csv(out / "summary.csv")
            self.assertEqual({row["condition"] for row in rows}, {"off", "c5", "c20", "c50"})
            self.assertTrue(all(int(row["n"]) == 5 for row in rows))
            c50 = next(row for row in rows if row["condition"] == "c50")
            self.assertGreater(float(c50["post_reduction_vs_off_pct"]), 80.0)
            self.assertLess(float(c50["qps_change_vs_off_pct"]), 0.0)

    def test_rejects_a_missing_measured_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root, omit=("c20", 4))
            with self.assertRaisesRegex(
                ValueError,
                "missing cache-control cell files|incomplete cache-control matrix",
            ):
                cache_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            manifest = root / "raw" / "off" / "measure_r0" / "manifest.json"
            obj = json.loads(manifest.read_text())
            obj["binary_sha256"] = "c" * 64
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "binary SHA"):
                cache_summary.summarize(root, root / "summary", FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
