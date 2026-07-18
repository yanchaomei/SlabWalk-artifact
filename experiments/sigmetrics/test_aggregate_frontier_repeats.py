#!/usr/bin/env python3
import math
import unittest

import aggregate_frontier_repeats as aggregate


class FrontierRepeatAggregatorTest(unittest.TestCase):
    def test_canonicalizes_all_one_million_dataset_aliases(self):
        aliases = {
            "sift1M": "SIFT1M",
            "gist1m": "GIST1M",
            "deep1M": "DEEP1M",
            "bigann1m": "BIGANN1M",
            "spacev1M": "SPACEV1M",
            "turing1m": "TURING1M",
            "text1M": "TTI1M",
            "TEXT1M": "TTI1M",
            "tti1m": "TTI1M",
        }
        for raw, expected in aliases.items():
            with self.subTest(raw=raw):
                self.assertEqual(aggregate.canonical_dataset(raw), expected)

    def test_infers_repeat_without_overwriting(self):
        self.assertEqual(aggregate.infer_run_id("/tmp/frontier_r3/frontier.csv"), "r3")
        self.assertEqual(aggregate.infer_run_id("/tmp/frontier_warmup/frontier.csv"), "warmup")

    def test_summary_keeps_all_repeats(self):
        rows = [
            {"dataset": "DEEP10M", "method": "SlabWalk", "ef": 50.0, "run_id": f"r{i}", "recall": value, "qps": 1000.0 + i}
            for i, value in enumerate([0.90, 0.91, 0.92, 0.93, 0.94], start=1)
        ]
        summary = aggregate.summarize(rows, expected_repeats=5)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["n"], 5)
        self.assertTrue(math.isclose(summary[0]["recall_median"], 0.92))
        self.assertGreater(summary[0]["qps_ci95"], 0)

    def test_summary_preserves_protocol_provenance(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "run_id": f"r{i}",
                "recall": 0.9,
                "qps": 1000.0 + i,
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "threads": 10,
                "query_contexts": 10,
                "top_k": 10,
                "metric": "l2",
                "expected_queries": 10000,
            }
            for i in range(1, 3)
        ]
        row = aggregate.summarize(rows, expected_repeats=2)[0]
        self.assertEqual(row["campaign_ids"], "campaign-a")
        self.assertEqual(row["binary_sha256s"], "sha-a")
        self.assertEqual(row["threads"], 10)
        self.assertEqual(row["query_contexts"], 10)
        self.assertEqual(row["top_k"], 10)
        self.assertEqual(row["metric"], "l2")
        self.assertEqual(row["expected_queries"], 10000)

    def test_summary_preserves_latency_and_access_metrics(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "run_id": f"r{i}",
                "recall": 0.9,
                "qps": 1000.0 + i,
                "p50_us": 100.0 + i,
                "p95_us": 200.0 + i,
                "p99_us": 300.0 + i,
                "posts_per_query": 40.0 + i,
                "bytes_per_query": 4096.0 + i,
            }
            for i in range(2)
        ]
        row = aggregate.summarize(rows, expected_repeats=2)[0]
        self.assertEqual(row["p50_us_n"], 2)
        self.assertTrue(math.isclose(row["p50_us_mean"], 100.5))
        self.assertEqual(row["posts_per_query_n"], 2)
        self.assertTrue(math.isclose(row["bytes_per_query_median"], 4096.5))
        self.assertEqual(row["mean_latency_us_n"], 0)
        self.assertEqual(row["mean_latency_us_mean"], "")

    def test_measurement_validation_accepts_native_method_metrics(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "p50_us": 100.0,
                "p95_us": 200.0,
                "p99_us": 300.0,
                "posts_per_query": 40.0,
                "bytes_per_query": 4096.0,
            },
            {
                "dataset": "DEEP10M",
                "method": "d-HNSW",
                "ef": 50.0,
                "mean_latency_us": 500.0,
                "network_us": 100.0,
                "compute_us": 200.0,
                "meta_us": 50.0,
                "deserialize_us": 150.0,
            },
        ]
        aggregate.validate_measurement_metrics(rows)

    def test_measurement_validation_rejects_missing_tail_sample(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "p50_us": 100.0,
                "p95_us": 200.0,
                "p99_us": None,
                "posts_per_query": 40.0,
                "bytes_per_query": 4096.0,
            }
        ]
        with self.assertRaisesRegex(ValueError, "p99_us"):
            aggregate.validate_measurement_metrics(rows)

    def test_rejects_nonfinite_frontier_values(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "run_id": "r1",
                "recall": float("nan"),
                "qps": 1000.0,
            }
        ]
        with self.assertRaisesRegex(ValueError, "non-finite"):
            aggregate.summarize(rows, expected_repeats=1)

    def test_protocol_validation_rejects_mixed_threads(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "run_id": "r1",
                "threads": 10,
                "query_contexts": 10,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": "same",
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            },
            {
                "dataset": "DEEP10M",
                "method": "SlabWalk",
                "ef": 50.0,
                "run_id": "r2",
                "threads": 40,
                "query_contexts": 10,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": "same",
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            },
        ]
        with self.assertRaisesRegex(ValueError, "threads"):
            aggregate.validate_protocol(
                rows,
                expected_threads=10,
                expected_top_k=10,
                expected_query_contexts=10,
            )

    def test_protocol_validation_rejects_repeat_drift(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "d-HNSW",
                "ef": 48.0,
                "run_id": f"r{rep}",
                "threads": 10,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": fingerprint,
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for rep, fingerprint in ((1, "protocol-a"), (2, "protocol-b"))
        ]
        with self.assertRaisesRegex(ValueError, "protocol drift"):
            aggregate.validate_protocol(
                rows,
                expected_threads=10,
                expected_top_k=10,
                expected_query_contexts=10,
            )

    def test_protocol_validation_requires_expected_sw_query_contexts(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": method,
                "ef": 50.0,
                "run_id": "r1",
                "threads": 10,
                "query_contexts": query_contexts,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": f"protocol-{method}",
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for method, query_contexts in (
                ("SHINE", 10),
                ("SlabWalk", 4),
                ("d-HNSW", ""),
            )
        ]
        with self.assertRaisesRegex(ValueError, "query_contexts"):
            aggregate.validate_protocol(
                rows,
                expected_threads=10,
                expected_top_k=10,
                expected_query_contexts=10,
            )

    def test_protocol_validation_accepts_no_context_field_for_dhnsw(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": method,
                "ef": 50.0,
                "run_id": "r1",
                "threads": 10,
                "query_contexts": query_contexts,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": f"protocol-{method}",
                "campaign_id": "campaign-a",
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for method, query_contexts in (
                ("SHINE", 10),
                ("SlabWalk", 10),
                ("d-HNSW", ""),
            )
        ]
        aggregate.validate_protocol(
            rows,
            expected_threads=10,
            expected_top_k=10,
            expected_query_contexts=10,
        )

    def test_protocol_validation_accepts_independent_baseline_campaign(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": method,
                "ef": 50.0,
                "run_id": "r1",
                "threads": 10,
                "query_contexts": query_contexts,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": f"protocol-{method}",
                "campaign_id": campaign_id,
                "binary_sha256": f"sha-{method}",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for method, query_contexts, campaign_id in (
                ("SHINE", 10, "sw-campaign"),
                ("SlabWalk", 10, "sw-campaign"),
                ("d-HNSW", "", "dhnsw-campaign"),
            )
        ]
        aggregate.validate_protocol(
            rows,
            expected_threads=10,
            expected_top_k=10,
            expected_query_contexts=10,
        )

    def test_protocol_validation_rejects_campaign_drift_within_method(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": "d-HNSW",
                "ef": 48.0,
                "run_id": f"r{rep}",
                "threads": 10,
                "query_contexts": "",
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": "protocol-a",
                "campaign_id": campaign_id,
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for rep, campaign_id in ((1, "campaign-a"), (2, "campaign-b"))
        ]
        with self.assertRaisesRegex(ValueError, "campaign drift"):
            aggregate.validate_protocol(
                rows,
                expected_threads=10,
                expected_top_k=10,
                expected_query_contexts=10,
            )

    def test_protocol_validation_rejects_shine_slabwalk_campaign_mismatch(self):
        rows = [
            {
                "dataset": "DEEP10M",
                "method": method,
                "ef": 50.0,
                "run_id": "r1",
                "threads": 10,
                "query_contexts": 10,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "protocol_fingerprint": f"protocol-{method}",
                "campaign_id": campaign_id,
                "binary_sha256": "sha-a",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
            }
            for method, campaign_id in (
                ("SHINE", "campaign-a"),
                ("SlabWalk", "campaign-b"),
            )
        ]
        with self.assertRaisesRegex(ValueError, "SHINE/SlabWalk campaign mismatch"):
            aggregate.validate_protocol(
                rows,
                expected_threads=10,
                expected_top_k=10,
                expected_query_contexts=10,
            )


if __name__ == "__main__":
    unittest.main()
