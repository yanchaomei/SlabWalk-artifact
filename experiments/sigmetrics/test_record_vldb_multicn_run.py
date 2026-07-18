from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path

import record_vldb_multicn_run as record


class MultiCnRecordTest(unittest.TestCase):
    @staticmethod
    def dhnsw_log(*, complete_details: bool, detail_recall: float = 0.93) -> str:
        lines = [
            "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 "
            "query_rows_per_ground_truth=1",
            "FRONTIER_QUERY_POOL total_queries=10000 threads=2 top_k=10 fixed=1",
            "FRONTIER_THREAD_RESULT ef=100 thread=0 queries=4000 "
            "elapsed_s=2.0 recall=0.90",
            "FRONTIER_THREAD_RESULT ef=100 thread=1 queries=6000 "
            "elapsed_s=2.5 recall=0.95",
            "Thread 0 EF 100 benchmark:",
            "  Queries executed: 4000",
            "  Avg total latency: 60 us",
            "  Avg meta search latency: 10 us",
            "  Avg compute time: 40 us",
            "  Avg network latency: 15 us",
            "  Avg deserialize time: 20 us",
            "  Recall: 0.90",
            "  Throughput: 1000 QPS",
        ]
        if complete_details:
            lines.extend(
                [
                    "Thread 1 EF 100 benchmark:",
                    "  Queries executed: 6000",
                    "  Avg total latency: 80 us",
                    "  Avg meta search latency: 20 us",
                    "  Avg compute time: 50 us",
                    "  Avg network latency: 25 us",
                    "  Avg deserialize time: 30 us",
                    f"  Recall: {(detail_recall - 0.4 * 0.90) / 0.6}",
                    "  Throughput: 1200 QPS",
                ]
            )
        else:
            # Matches the 0718b failure mode: another writer breaks the
            # human-readable block, while atomic protocol records stay intact.
            lines.extend(
                [
                    "Thread 1 EF 100 benchmark:",
                    "  Queries executed: 6000",
                    "  Avg total latency: Thread 17 EF 100 benchmark:",
                    "  Avg meta search latency: 20 us",
                ]
            )
        return "\n".join(lines) + "\n"

    def graph_payload(
        self,
        *,
        cn_count: int,
        local_id: int,
        processed: int,
        query_ms: float,
        aggregate: bool,
    ) -> dict[str, object]:
        queries: dict[str, object] = {
            "processed": processed if not aggregate else 10,
            "local_latency_samples": processed,
            "local_latency_p50_us": 100.0 + local_id * 5,
            "local_latency_p99_us": 200.0 + local_id * 10,
        }
        timings = {"query_c0": 1000.0}
        if aggregate:
            local_counts = {
                1: [10],
                2: [5, 5],
                3: [4, 3, 3],
            }[cn_count]
            queries.update(
                {
                    "processed_local": {
                        f"c{index}": count
                        for index, count in enumerate(local_counts)
                    },
                    "queries_per_sec": 10.0,
                    "recall": 0.97,
                    "rdma_posts": 1200,
                    "rdma_reads_in_bytes": 600000,
                }
            )
            timings = {
                f"query_c{index}": count * 250.0
                for index, count in enumerate(local_counts)
            }
        else:
            timings = {f"query_c{local_id}": query_ms}
        return {
            "num_queries": 10,
            "meta": {"compute_nodes": cn_count},
            "queries": queries,
            "timings": timings,
        }

    def test_graph_record_binds_aggregate_and_per_cn_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initiator = root / "c0.json"
            initiator.write_text(
                json.dumps(
                    self.graph_payload(
                        cn_count=3,
                        local_id=0,
                        processed=4,
                        query_ms=1000,
                        aggregate=True,
                    )
                )
            )
            client_logs = []
            for index in (1, 2):
                path = root / f"c{index}.stderr"
                path.write_text(f"[STATUS]: processed queries: {3 if index else 4}\n")
                client_logs.append(path)
            source = root / "source.json"
            csv_path = root / "runs.csv"
            result = record.record_graph(
                campaign_id="campaign",
                protocol_fingerprint="f" * 64,
                dataset="SIFT1M",
                system="SlabWalk",
                cn_count=3,
                repeat=0,
                binary_sha256="a" * 64,
                query_sha256="b" * 64,
                groundtruth_sha256="c" * 64,
                expected_queries=10,
                initiator_json=initiator,
                client_logs=client_logs,
                source_path=source,
                csv_path=csv_path,
            )
            self.assertEqual(result["metrics"]["qps"], 10.0)
            self.assertEqual(result["metrics"]["posts_per_query"], 120.0)
            self.assertEqual(result["metrics"]["bytes_per_query"], 60000.0)
            self.assertIsNone(result["metrics"]["p50_us"])
            self.assertIsNone(result["metrics"]["p99_us"])
            self.assertEqual(
                result["latency_scope"],
                "not_reported_cross_cn_frozen_binary_boundary",
            )
            self.assertAlmostEqual(result["metrics"]["fairness"], 1.0)
            with csv_path.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "source.json")

    def test_graph_single_cn_retains_complete_latency_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initiator = root / "c0.json"
            initiator.write_text(
                json.dumps(
                    self.graph_payload(
                        cn_count=1,
                        local_id=0,
                        processed=10,
                        query_ms=2500,
                        aggregate=True,
                    )
                )
            )
            result = record.record_graph(
                campaign_id="campaign",
                protocol_fingerprint="f" * 64,
                dataset="SIFT1M",
                system="SHINE",
                cn_count=1,
                repeat=0,
                binary_sha256="a" * 64,
                query_sha256="b" * 64,
                groundtruth_sha256="c" * 64,
                expected_queries=10,
                initiator_json=initiator,
                client_logs=[],
                source_path=root / "source.json",
                csv_path=root / "runs.csv",
            )
            self.assertEqual(result["metrics"]["p50_us"], 100.0)
            self.assertEqual(result["metrics"]["p99_us"], 200.0)
            self.assertEqual(result["latency_scope"], "all_queries_single_cn")

    def test_dhnsw_record_aggregates_disjoint_client_shards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.json"
            csv_path = root / "runs.csv"
            result = record.record_dhnsw_parsed(
                campaign_id="campaign",
                protocol_fingerprint="f" * 64,
                dataset="GIST1M",
                cn_count=2,
                repeat=1,
                binary_sha256="d" * 64,
                query_sha256="b" * 64,
                groundtruth_sha256="c" * 64,
                expected_queries=1000,
                clients=[
                    {"processed_queries": 500, "expected_queries": 500, "qps": 1000.0, "recall": 0.90},
                    {"processed_queries": 500, "expected_queries": 500, "qps": 900.0, "recall": 0.92},
                ],
                source_path=source,
                csv_path=csv_path,
            )
            self.assertEqual(result["metrics"]["processed_queries"], 1000)
            self.assertEqual(result["metrics"]["qps"], 1900.0)
            self.assertAlmostEqual(result["metrics"]["recall"], 0.91)
            self.assertIsNone(result["metrics"]["p50_us"])
            self.assertGreater(result["metrics"]["fairness"], 0.99)

    def test_dhnsw_client_keeps_atomic_metrics_when_detail_stdout_interleaves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.log"
            path.write_text(self.dhnsw_log(complete_details=False))

            parsed = record.parsed_dhnsw_client(path, ef=100, threads=2)

            self.assertEqual(parsed["processed_queries"], 10000)
            self.assertTrue(math.isclose(float(parsed["qps"]), 4000.0))
            self.assertTrue(math.isclose(float(parsed["recall"]), 0.93))
            self.assertEqual(
                parsed["detail_scope"], "unavailable_interleaved_stdout"
            )
            self.assertIn("thread coverage mismatch", parsed["detail_error"])
            for key in (
                "average_latency_us",
                "network_us",
                "compute_us",
                "meta_us",
                "deserialize_us",
            ):
                self.assertIsNone(parsed[key])

    def test_dhnsw_client_retains_complete_optional_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.log"
            path.write_text(self.dhnsw_log(complete_details=True))

            parsed = record.parsed_dhnsw_client(path, ef=100, threads=2)

            self.assertEqual(parsed["detail_scope"], "complete_per_thread_text")
            self.assertIsNone(parsed["detail_error"])
            self.assertTrue(math.isclose(float(parsed["recall"]), 0.93))
            self.assertTrue(math.isclose(float(parsed["average_latency_us"]), 72.0))

    def test_dhnsw_client_records_machine_sentinel_prefix_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.log"
            log = self.dhnsw_log(complete_details=False).replace(
                "FRONTIER_THREAD_RESULT ef=100 thread=1",
                "Thread FRONTIER_THREAD_RESULT ef=100 thread=1",
            )
            path.write_text(log)

            parsed = record.parsed_dhnsw_client(path, ef=100, threads=2)

            self.assertEqual(parsed["processed_queries"], 10000)
            self.assertEqual(parsed["machine_record_prefix_interleavings"], 1)

    def test_dhnsw_client_rejects_material_detail_recall_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.log"
            path.write_text(
                self.dhnsw_log(complete_details=True, detail_recall=0.80)
            )

            with self.assertRaisesRegex(ValueError, "recall disagrees"):
                record.parsed_dhnsw_client(path, ef=100, threads=2)

    def test_graph_record_rejects_incomplete_client_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initiator = root / "c0.json"
            initiator.write_text(
                json.dumps(
                    self.graph_payload(
                        cn_count=3,
                        local_id=0,
                        processed=4,
                        query_ms=1000,
                        aggregate=True,
                    )
                )
            )
            with self.assertRaisesRegex(ValueError, "coverage"):
                record.record_graph(
                    campaign_id="campaign",
                    protocol_fingerprint="f" * 64,
                    dataset="SIFT1M",
                    system="SHINE",
                    cn_count=3,
                    repeat=0,
                    binary_sha256="a" * 64,
                    query_sha256="b" * 64,
                    groundtruth_sha256="c" * 64,
                    expected_queries=10,
                    initiator_json=initiator,
                    client_logs=[],
                    source_path=root / "source.json",
                    csv_path=root / "runs.csv",
                )


if __name__ == "__main__":
    unittest.main()
