import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import amend_worker_scaling_parser as amendment
import parse_dhnsw_frontier as parser


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def client_log(threads: int, *, interleave_last: bool = False) -> str:
    lines = [
        "QUERY_GT_SHAPE query_rows=10000 ground_truth_rows=10000 query_rows_per_ground_truth=1",
        f"FRONTIER_QUERY_POOL total_queries=10000 threads={threads} top_k=10 fixed=1",
    ]
    base = 10000 // threads
    remainder = 10000 % threads
    for thread in range(threads):
        queries = base + (1 if thread < remainder else 0)
        lines.extend(
            [
                f"Thread {thread} EF 200 benchmark:",
                f"  Queries executed: {queries}",
                f"  Avg total latency: {60 + thread} us",
                f"  Avg meta search latency: {10 + thread} us",
                f"  Avg compute time: {40 + thread} us",
            ]
        )
        if interleave_last and thread == threads - 1:
            lines.extend(
                [
                    "  Avg network latency: \x1b[39m25.5[reporter.hh:42] "
                    "[Batch 0] Compute: 50 ms, Network: 25 ms, Deserialize: 30 ms, "
                    "Meta Search: 20 ms, Throughput: 1200 qps, Total Time: 80 ms",
                    " us\x1b[0m",
                ]
            )
        else:
            lines.append(f"  Avg network latency: {15 + thread} us")
        lines.extend(
            [
                f"  Avg deserialize time: {20 + thread} us",
                "  Recall: 0.90",
                f"  Throughput: {1000 + thread} QPS",
                f"FRONTIER_THREAD_RESULT ef=200 thread={thread} queries={queries} "
                f"elapsed_s={2 + thread / 10} recall=0.90",
            ]
        )
    return "\n".join(lines) + "\n"


def write_run(root: Path, workers: int, run_id: str, *, interleaved: bool) -> Path:
    run = root / "raw" / "dhnsw" / f"w{workers}" / run_id
    run.mkdir(parents=True)
    (run / "deep1M_ef200_client.log").write_text(
        client_log(workers, interleave_last=interleaved)
    )
    (run / "deep1M_server_rss_before.txt").write_text("VmRSS:\t1048576 kB\n")
    (run / "deep1M_server_rss_after.txt").write_text("VmHWM:\t2097152 kB\n")
    return run


def write_frontier(run: Path, workers: int, campaign_id: str, binary: str) -> None:
    text = (run / "deep1M_ef200_client.log").read_text()
    fixed = parser.parse_fixed_pool_result(text, ef=200, expected_threads=workers)
    detail = parser.parse_client_details(text, ef=200, expected_threads=workers)
    fingerprint = parser.protocol_fingerprint(
        {
            "binary_sha256": binary,
            "dataset": "deep1M",
            "ef": 200,
            "threads": workers,
            "top_k": 10,
            "metric": "l2",
            "measurement_mode": "fixed_query_pool",
            "expected_queries": 10000,
            "query_rows": 10000,
            "ground_truth_rows": 10000,
            "query_rows_per_ground_truth": 1,
        }
    )
    parser.write_csv(
        run / "frontier.csv",
        [
            {
                "dataset": "deep1M",
                "ef": 200,
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
                "binary_sha256": binary,
                "threads": workers,
                "duration_s": "20",
                "measurement_mode": "fixed_query_pool",
                "processed_queries": fixed["processed_queries"],
                "expected_queries": fixed["expected_queries"],
                "failed_queries": 0,
                "wall_seconds": f"{fixed['wall_seconds']:.6f}",
                "top_k": 10,
                "metric": "l2",
                "query_rows": 10000,
                "ground_truth_rows": 10000,
                "query_rows_per_ground_truth": 1,
                "qps_recomputed": f"{fixed['qps']:.3f}",
                "recall": f"{detail['recall']:.6f}",
                "latency_us": f"{detail['latency_us']:.3f}",
                "network_us": f"{detail['network_us']:.3f}",
                "compute_us": f"{detail['compute_us']:.3f}",
                "meta_us": f"{detail['meta_us']:.3f}",
                "deserialize_us": f"{detail['deserialize_us']:.3f}",
                "raw_qps_buggy": f"{detail['raw_qps_buggy']:.6g}",
                "server_rss_before_gb": "1.000",
                "server_rss_after_gb": "2.000",
                "status": "ok",
            }
        ],
    )


class ParserAmendmentTest(unittest.TestCase):
    def make_campaign(self, root: Path) -> tuple[str, str]:
        campaign_id = "worker-test"
        binary = "1" * 64
        protocol = {
            "dhnsw_parser_sha256": "0" * 64,
            "dhnsw_client_binary_sha256": binary,
            "workers": [1, 2],
            "ef": 200,
            "repeats": 5,
        }
        (root / "campaign.json").write_text(
            json.dumps(
                {
                    "campaign_id": campaign_id,
                    "protocol": protocol,
                    "protocol_fingerprint": amendment.protocol_fingerprint(protocol),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return campaign_id, binary

    def test_reparses_all_logs_and_preserves_originals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign_id, binary = self.make_campaign(root)
            valid = write_run(root, 1, "r0", interleaved=False)
            write_frontier(valid, 1, campaign_id, binary)
            valid_before = (valid / "frontier.csv").read_bytes()
            broken = write_run(root, 2, "warmup0", interleaved=True)
            (broken / "frontier.csv").write_text("status\nincomplete\n")

            amendment.amend(
                root,
                Path(parser.__file__),
                expected_old_parser_sha="0" * 64,
            )

            campaign = json.loads((root / "campaign.json").read_text())
            self.assertEqual(
                campaign["protocol"]["dhnsw_parser_sha256"],
                sha256(Path(parser.__file__)),
            )
            self.assertTrue((root / "campaign.before-parser-amendment.json").is_file())
            self.assertTrue((root / "parser_amendment.json").is_file())
            self.assertEqual((valid / "frontier.csv").read_bytes(), valid_before)
            self.assertTrue((valid / "frontier.before-parser-amendment.csv").is_file())
            with (broken / "frontier.csv").open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["processed_queries"], "10000")

    def test_refuses_measurement_drift_in_previously_valid_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign_id, binary = self.make_campaign(root)
            run = write_run(root, 1, "r0", interleaved=False)
            write_frontier(run, 1, campaign_id, binary)
            with (run / "frontier.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["qps_recomputed"] = "999999"
            parser.write_csv(run / "frontier.csv", rows)
            original = (root / "campaign.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "measurement drift"):
                amendment.amend(
                    root,
                    Path(parser.__file__),
                    expected_old_parser_sha="0" * 64,
                )
            self.assertEqual((root / "campaign.json").read_bytes(), original)
            self.assertFalse((root / "parser_amendment.json").exists())


if __name__ == "__main__":
    unittest.main()
