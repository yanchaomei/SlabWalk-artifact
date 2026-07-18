#!/usr/bin/env python3
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import plot_vldb_frontier_10m as plotter
import summarize_vldb_headlines as headlines
from test_plot_vldb_frontier_10m import FINAL_SHA, summary_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_inputs(root: Path) -> tuple[Path, Path]:
    rows = summary_rows()
    for row in rows:
        method = row["method"]
        if method == "SHINE":
            row["qps_mean"] = float(row["qps_mean"])
        elif method == "SlabWalk":
            row["qps_mean"] = 2.0 * float(row["qps_mean"])
            if row["dataset"] == "TTI10M":
                row["recall_mean"] = float(row["recall_mean"]) - 0.05
            else:
                row["recall_mean"] = float(row["recall_mean"]) + 0.0001
        else:
            row["recall_mean"] = float(row["recall_mean"]) - 0.10
            row["qps_mean"] = 500.0
        row["posts_per_query_n"] = 0 if method == "d-HNSW" else 5
        row["posts_per_query_mean"] = "" if method == "d-HNSW" else (
            100.0 if method == "SHINE" else 10.0
        )
        row["bytes_per_query_n"] = 0 if method == "d-HNSW" else 5
        row["bytes_per_query_mean"] = "" if method == "d-HNSW" else (
            1000.0 if method == "SHINE" else 600.0
        )
    summary = root / "frontier_summary.csv"
    write_csv(summary, rows)
    gate = root / "evidence_gate.json"
    gate.write_text(json.dumps({
        "ready_for_plotting": True,
        "expected_slabwalk_sha256": FINAL_SHA,
        "frontier": {"summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()},
    }))
    return summary, gate


class SummarizeVldbHeadlinesTest(unittest.TestCase):
    def test_derives_recall_guarded_same_ef_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            summary, gate = write_inputs(Path(tmp_s))
            report = headlines.derive(summary, gate, recall_tolerance=0.002)

            deep = report["datasets"]["DEEP10M"]
            self.assertEqual(len(deep["same_ef_graph_pairs"]), 5)
            self.assertEqual(deep["matched_pair_count"], 5)
            self.assertAlmostEqual(deep["high_recall_matched_pair"]["qps_speedup"], 2.0)
            self.assertAlmostEqual(
                deep["high_recall_matched_pair"]["post_reduction"], 10.0
            )
            self.assertAlmostEqual(
                deep["high_recall_matched_pair"]["byte_reduction"], 1000 / 600
            )

            tti = report["datasets"]["TTI10M"]
            self.assertEqual(tti["matched_pair_count"], 0)
            self.assertIsNone(tti["high_recall_matched_pair"])
            self.assertAlmostEqual(tti["dhnsw_max_recall"]["recall"], 0.80)

            ranges = report["headline_ranges"]
            self.assertEqual(ranges["matched_datasets"], ["DEEP10M", "SIFT10M"])
            self.assertEqual(ranges["high_recall_qps_speedup_min"], 2.0)
            self.assertEqual(ranges["high_recall_qps_speedup_max"], 2.0)
            self.assertEqual(report["recall_floor"], 0.90)

    def test_excludes_recall_matched_points_below_the_absolute_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = write_inputs(root)
            with summary.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                if row["dataset"] != "TTI10M":
                    continue
                if row["method"] == "SHINE":
                    row["recall_mean"] = 0.8500
                elif row["method"] == "SlabWalk":
                    row["recall_mean"] = 0.8501
            write_csv(summary, rows)
            gate.write_text(json.dumps({
                "ready_for_plotting": True,
                "expected_slabwalk_sha256": FINAL_SHA,
                "frontier": {
                    "summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()
                },
            }))

            report = headlines.derive(
                summary,
                gate,
                recall_tolerance=0.002,
                recall_floor=0.90,
            )
            tti = report["datasets"]["TTI10M"]
            self.assertEqual(tti["matched_pair_count"], 5)
            self.assertIsNone(tti["high_recall_matched_pair"])
            self.assertEqual(
                report["headline_ranges"]["matched_datasets"],
                ["DEEP10M", "SIFT10M"],
            )

    def test_excludes_nonimproving_point_from_positive_headline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = write_inputs(root)
            with summary.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            shine_qps = next(
                float(row["qps_mean"])
                for row in rows
                if row["dataset"] == "DEEP10M"
                and row["method"] == "SHINE"
                and row["ef"] == "200"
            )
            for row in rows:
                if (
                    row["dataset"] == "DEEP10M"
                    and row["method"] == "SlabWalk"
                    and row["ef"] == "200"
                ):
                    row["qps_mean"] = 0.9 * shine_qps
                    row["posts_per_query_mean"] = 110.0
            write_csv(summary, rows)
            gate.write_text(json.dumps({
                "ready_for_plotting": True,
                "expected_slabwalk_sha256": FINAL_SHA,
                "frontier": {
                    "summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()
                },
            }))

            report = headlines.derive(summary, gate)
            deep = report["datasets"]["DEEP10M"]
            self.assertEqual(deep["matched_pair_count"], 5)
            self.assertIsNone(deep["high_recall_matched_pair"])
            self.assertEqual(
                report["headline_ranges"]["matched_datasets"],
                ["SIFT10M"],
            )

    def test_rejects_summary_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = write_inputs(root)
            summary.write_text(summary.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "summary SHA"):
                headlines.derive(summary, gate)

    def test_writes_atomic_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = write_inputs(root)
            out = root / "headline.json"
            headlines.summarize(summary, gate, out, recall_tolerance=0.002)
            record = json.loads(out.read_text())
            self.assertEqual(record["kind"], "vldb_headline_candidates")
            self.assertFalse(any(root.glob(".headline.json.tmp.*")))


if __name__ == "__main__":
    unittest.main()
