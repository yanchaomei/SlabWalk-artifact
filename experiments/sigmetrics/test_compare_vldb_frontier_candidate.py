#!/usr/bin/env python3
import csv
import tempfile
import unittest
from pathlib import Path

import compare_vldb_frontier_candidate as comparison


class CompareVldbFrontierCandidateTest(unittest.TestCase):
    def write_matrix(self, path: Path, *, qps_scale: float = 1.0) -> None:
        rows = []
        for dataset in comparison.DATASETS:
            raw_dataset = "TEXT1M" if dataset == "TTI1M" else dataset
            for method in comparison.METHODS:
                for ef in (1, 2, 3, 4, 5):
                    for repeat in range(1, 6):
                        rows.append(
                            {
                                "dataset": raw_dataset,
                                "method": method,
                                "ef": ef,
                                "run_id": f"r{repeat}",
                                "recall": 0.8 + ef / 100.0,
                                "qps": (1000 + ef + repeat) * qps_scale,
                                "p99_us": 500 + ef + repeat,
                                "posts_per_query": 40 + ef,
                                "bytes_per_query": 4096 + ef,
                            }
                        )
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_accepts_identical_seven_dataset_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            baseline = root / "baseline.csv"
            candidate = root / "candidate.csv"
            self.write_matrix(baseline)
            self.write_matrix(candidate, qps_scale=1.02)
            report, rows = comparison.compare_frontiers(baseline, candidate)
            self.assertTrue(report["promotion_ready"])
            self.assertEqual(report["compared_cells"], 70)
            self.assertEqual(len(rows), 70)
            self.assertEqual(report["invariant_failures"], 0)

    def test_rejects_query_path_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            baseline = root / "baseline.csv"
            candidate = root / "candidate.csv"
            self.write_matrix(baseline)
            self.write_matrix(candidate)
            with candidate.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["posts_per_query"] = "41.5"
            with candidate.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            report, _ = comparison.compare_frontiers(baseline, candidate)
            self.assertFalse(report["promotion_ready"])
            self.assertEqual(report["invariant_failures"], 1)

    def test_rejects_material_qps_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            baseline = root / "baseline.csv"
            candidate = root / "candidate.csv"
            self.write_matrix(baseline)
            self.write_matrix(candidate, qps_scale=0.80)
            report, _ = comparison.compare_frontiers(baseline, candidate)
            self.assertFalse(report["promotion_ready"])
            self.assertGreater(report["performance_failures"], 0)


if __name__ == "__main__":
    unittest.main()
