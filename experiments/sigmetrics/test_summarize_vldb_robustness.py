#!/usr/bin/env python3
import math
import unittest
from pathlib import Path

import summarize_vldb_robustness as robustness


class RobustnessSummaryTest(unittest.TestCase):
    def test_parses_complete_latency_and_resource_row(self):
        obj = {
            "num_queries": 10000,
            "queries": {
                "processed": 10000,
                "recall": 0.96,
                "queries_per_sec": 2500,
                "local_latency_samples": 10000,
                "local_latency_p50_us": 400.0,
                "local_latency_p95_us": 800.0,
                "local_latency_p99_us": 1200.0,
                "rdma_posts": 500000,
                "rdma_reads_in_bytes": 320000000,
            },
        }
        row = robustness.parse_query_metrics(obj, Path("run.json"))
        self.assertEqual(row["p99_us"], 1200.0)
        self.assertEqual(row["posts_per_query"], 50.0)
        self.assertEqual(row["bytes_per_query"], 32000.0)

    def test_rejects_incomplete_latency_samples(self):
        obj = {
            "num_queries": 10000,
            "queries": {
                "processed": 10000,
                "recall": 0.96,
                "queries_per_sec": 2500,
                "local_latency_samples": 9999,
                "local_latency_p50_us": 400.0,
                "local_latency_p95_us": 800.0,
                "local_latency_p99_us": 1200.0,
                "rdma_posts": 500000,
                "rdma_reads_in_bytes": 320000000,
            },
        }
        with self.assertRaisesRegex(ValueError, "latency sample count"):
            robustness.parse_query_metrics(obj, Path("run.json"))

    def test_rejects_non_monotonic_quantiles(self):
        obj = {
            "num_queries": 1,
            "queries": {
                "processed": 1,
                "recall": 1.0,
                "queries_per_sec": 1,
                "local_latency_samples": 1,
                "local_latency_p50_us": 100.0,
                "local_latency_p95_us": 90.0,
                "local_latency_p99_us": 120.0,
                "rdma_posts": 1,
                "rdma_reads_in_bytes": 1,
            },
        }
        with self.assertRaisesRegex(ValueError, "monotonic"):
            robustness.parse_query_metrics(obj, Path("run.json"))

    def test_accepts_latency_disabled_overhead_control(self):
        obj = {
            "num_queries": 10000,
            "queries": {
                "processed": 10000,
                "recall": 0.96,
                "queries_per_sec": 2520,
                "rdma_posts": 500000,
                "rdma_reads_in_bytes": 320000000,
            },
        }
        row = robustness.parse_query_metrics(
            obj, Path("run.json"), require_latency=False
        )
        self.assertIsNone(row["p99_us"])
        self.assertEqual(row["qps"], 2520.0)

    def test_student_t_interval(self):
        half = robustness.t_ci_half([10.0, 12.0, 14.0, 16.0, 18.0])
        self.assertTrue(math.isclose(half, 3.926, rel_tol=0.01))

    def test_matrix_rejects_missing_or_duplicate_repeats(self):
        rows = [
            {"factor": "workers", "value": "1", "run_kind": "measure", "repeat": 0,
             "protocol_fingerprint": "a", "campaign_id": "c"},
            {"factor": "workers", "value": "1", "run_kind": "measure", "repeat": 0,
             "protocol_fingerprint": "a", "campaign_id": "c"},
        ]
        with self.assertRaisesRegex(ValueError, "repeat set"):
            robustness.validate_matrix(rows, [("workers", "1")], repeats=2)

    def test_matrix_rejects_protocol_drift(self):
        rows = [
            {"factor": "workers", "value": "1", "run_kind": "measure", "repeat": 0,
             "protocol_fingerprint": "a", "campaign_id": "c"},
            {"factor": "workers", "value": "1", "run_kind": "measure", "repeat": 1,
             "protocol_fingerprint": "b", "campaign_id": "c"},
        ]
        with self.assertRaisesRegex(ValueError, "protocol drift"):
            robustness.validate_matrix(rows, [("workers", "1")], repeats=2)

    def test_matrix_accepts_complete_cells(self):
        rows = [
            {"factor": "workers", "value": "1", "run_kind": "measure", "repeat": rep,
             "protocol_fingerprint": "a", "campaign_id": "c"}
            for rep in range(2)
        ]
        robustness.validate_matrix(rows, [("workers", "1")], repeats=2)

    def test_summary_preserves_query_context_count(self):
        rows = [
            {
                "factor": "workers",
                "value": "8",
                "run_kind": "measure",
                "threads": 8,
                "query_contexts": 8,
                "coroutines": 2,
                "top_k": 10,
                "ef": 200,
                "query_suffix": "uniform",
                "recall": 0.98,
                "qps": 1000.0 + rep,
                "p50_us": 100.0,
                "p95_us": 200.0,
                "p99_us": 300.0,
                "posts_per_query": 20.0,
                "bytes_per_query": 1000.0,
            }
            for rep in range(2)
        ]
        summary = robustness.summarize(rows)
        self.assertEqual(summary[0]["query_contexts"], 8)


if __name__ == "__main__":
    unittest.main()
