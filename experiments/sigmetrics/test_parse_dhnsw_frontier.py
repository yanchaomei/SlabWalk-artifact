#!/usr/bin/env python3
from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import parse_dhnsw_frontier as parser


class DhnswFrontierParserTest(unittest.TestCase):
    def test_fixed_pool_qps_uses_slowest_thread_wall_time(self):
        text = "\n".join(
            [
                "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
                "FRONTIER_QUERY_POOL total_queries=10000 threads=2 top_k=10 fixed=1",
                "FRONTIER_THREAD_RESULT ef=48 thread=0 queries=5000 elapsed_s=2.0 recall=0.90",
                "FRONTIER_THREAD_RESULT ef=48 thread=1 queries=5000 elapsed_s=2.5 recall=0.92",
            ]
        )
        result = parser.parse_fixed_pool_result(text, ef=48, expected_threads=2)
        self.assertEqual(result["processed_queries"], 10000)
        self.assertEqual(result["expected_queries"], 10000)
        self.assertTrue(math.isclose(result["qps"], 4000.0))
        self.assertTrue(math.isclose(result["recall"], 0.91))
        self.assertEqual(result["top_k"], 10)
        self.assertEqual(result["query_rows_per_ground_truth"], 1)

    def test_fixed_pool_parser_rejects_missing_thread(self):
        text = "\n".join(
            [
                "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
                "FRONTIER_QUERY_POOL total_queries=10000 threads=2 top_k=10 fixed=1",
                "FRONTIER_THREAD_RESULT ef=48 thread=0 queries=5000 elapsed_s=2.0 recall=0.90",
            ]
        )
        with self.assertRaisesRegex(ValueError, "thread coverage"):
            parser.parse_fixed_pool_result(text, ef=48, expected_threads=2)

    def test_fixed_pool_parser_recovers_protocol_marker_after_broken_csi(self):
        text = "\n".join(
            [
                "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
                "FRONTIER_QUERY_POOL total_queries=10000 threads=2 top_k=10 fixed=1",
                "FRONTIER_THREAD_RESULT ef=200 thread=0 queries=5000 elapsed_s=2.0 recall=0.90",
                "\x1b[FRONTIER_THREAD_RESULT ef=200 thread=1 queries=5000 elapsed_s=2.5 recall=0.92",
            ]
        )
        result = parser.parse_fixed_pool_result(text, ef=200, expected_threads=2)
        self.assertEqual(result["processed_queries"], 10000)
        self.assertTrue(math.isclose(result["qps"], 4000.0))

    def test_fixed_pool_parser_accepts_complete_sentinel_after_thread_prefix(self):
        text = "\n".join(
            [
                "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
                "FRONTIER_QUERY_POOL total_queries=10000 threads=2 top_k=10 fixed=1",
                "FRONTIER_THREAD_RESULT ef=200 thread=0 queries=5000 elapsed_s=2.0 recall=0.90",
                "Thread FRONTIER_THREAD_RESULT ef=200 thread=1 queries=5000 elapsed_s=2.5 recall=0.92",
                "1 EF 200 benchmark:",
            ]
        )

        result = parser.parse_fixed_pool_result(text, ef=200, expected_threads=2)

        self.assertEqual(result["processed_queries"], 10000)
        self.assertEqual(result["machine_record_prefix_interleavings"], 1)
        self.assertTrue(math.isclose(result["qps"], 4000.0))

    def test_fixed_pool_parser_rejects_unknown_sentinel_prefix(self):
        text = "\n".join(
            [
                "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
                "FRONTIER_QUERY_POOL total_queries=10000 threads=1 top_k=10 fixed=1",
                "quoted FRONTIER_THREAD_RESULT ef=200 thread=0 queries=10000 elapsed_s=2.0 recall=0.90",
            ]
        )

        with self.assertRaisesRegex(ValueError, "thread coverage"):
            parser.parse_fixed_pool_result(text, ef=200, expected_threads=1)

    def test_rss_parser_prefers_peak_high_water_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rss.txt"
            path.write_text("VmRSS:\t1048576 kB\nVmHWM:\t2097152 kB\n")
            self.assertEqual(parser.parse_rss_gb(path), "2.000")

    def test_client_log_is_a_complete_details_fallback(self):
        text = "\n".join(
            [
                "Thread 0 EF 48 benchmark:",
                "  Queries executed: 4000",
                "  Avg total latency: 60 us",
                "  Avg meta search latency: 10 us",
                "  Avg compute time: 40 us",
                "  Avg network latency: 15 us",
                "  Avg deserialize time: 20 us",
                "  Recall: 0.90",
                "  Throughput: 1000 QPS",
                "Thread 1 EF 48 benchmark:",
                "  Queries executed: 6000",
                "  Avg total latency: 80 us",
                "  Avg meta search latency: 20 us",
                "  Avg compute time: 50 us",
                "  Avg network latency: 25 us",
                "  Avg deserialize time: 30 us",
                "  Recall: 0.95",
                "  Throughput: 1200 QPS",
            ]
        )
        detail = parser.parse_client_details(text, ef=48, expected_threads=2)
        self.assertTrue(math.isclose(detail["latency_us"], 72.0))
        self.assertTrue(math.isclose(detail["recall"], 0.93))
        self.assertEqual(detail["raw_qps_buggy"], 2200.0)

    def test_client_detail_parser_tolerates_interleaved_reporter_line(self):
        text = "\n".join(
            [
                "Thread 0 EF 200 benchmark:",
                "  Queries executed: 5000",
                "  Avg total latency: 60 us",
                "  Avg meta search latency: 10 us",
                "  Avg compute time: 40 us",
                "  Avg network latency: 15 us",
                "  Avg deserialize time: 20 us",
                "  Recall: 0.90",
                "  Throughput: 1000 QPS",
                "Thread 1 EF 200 benchmark:",
                "  Queries executed: 5000",
                "  Avg total latency: 80 us",
                "  Avg meta search latency: 20 us",
                "  Avg compute time: 50 us",
                "  Avg network latency: \x1b[39m25.5[reporter.hh:42] "
                "[Batch 0] Compute: 50 ms, Network: 25 ms, Deserialize: 30 ms, "
                "Meta Search: 20 ms, Throughput: 1200 qps, Total Time: 80 ms",
                " us\x1b[0m",
                "  Avg deserialize time: 30 us",
                "  Recall: 0.95",
                "  Throughput: 1200 QPS",
            ]
        )
        detail = parser.parse_client_details(text, ef=200, expected_threads=2)
        self.assertTrue(math.isclose(detail["network_us"], 20.25))
        self.assertTrue(math.isclose(detail["deserialize_us"], 25.0))
        self.assertTrue(math.isclose(detail["recall"], 0.925))

    def test_client_detail_parser_recovers_broken_csi_before_numeric_metric(self):
        text = "\n".join(
            [
                "Thread 0 EF 100 benchmark:",
                "  Queries executed: 1000",
                "  Avg total latency: 1034.35 us",
                "  Avg meta search latency: 14.479 us",
                "  Avg compute time: 782.562 us",
                "  Avg network latency: \x1b[129.7739 us",
                "  Avg deserialize time: 668.471 us",
                "  Recall: 0.8948",
                "  Throughput: 963.822 QPS",
            ]
        )

        detail = parser.parse_client_details(text, ef=100, expected_threads=1)

        self.assertTrue(math.isclose(detail["network_us"], 129.7739))
        self.assertTrue(math.isclose(detail["recall"], 0.8948))


if __name__ == "__main__":
    unittest.main()
