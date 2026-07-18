import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import plot_vldb_frontier_10m as plotter


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


def summary_rows() -> list[dict[str, object]]:
    rows = []
    for dataset in plotter.DATASETS:
        for method in plotter.METHODS:
            for index, ef in enumerate((48, 64, 96, 128, 200)):
                rows.append({
                    "dataset": dataset,
                    "method": method,
                    "ef": ef,
                    "n": 5,
                    "campaign_ids": f"campaign-{dataset}-{method}",
                    "binary_sha256s": "d" * 64 if method == "d-HNSW" else FINAL_SHA,
                    "threads": 10,
                    "query_contexts": "" if method == "d-HNSW" else 10,
                    "top_k": 10,
                    "metric": "ip" if dataset == "TTI10M" else "l2",
                    "expected_queries": 10000,
                    "recall_mean": 0.70 + index * 0.05,
                    "recall_median": 0.70 + index * 0.05,
                    "recall_ci95": 0.002,
                    "qps_mean": 20000 / (index + 1),
                    "qps_median": 20000 / (index + 1),
                    "qps_ci95": 100 / (index + 1),
                    "posts_per_query_n": 0 if method == "d-HNSW" else 5,
                    "posts_per_query_mean": "" if method == "d-HNSW" else (
                        100.0 if method == "SHINE" else 10.0
                    ),
                    "posts_per_query_ci95": "" if method == "d-HNSW" else 0.5,
                    "bytes_per_query_n": 0 if method == "d-HNSW" else 5,
                    "bytes_per_query_mean": "" if method == "d-HNSW" else (
                        1000.0 if method == "SHINE" else 600.0
                    ),
                    "bytes_per_query_ci95": "" if method == "d-HNSW" else 5.0,
                    "run_ids": "r1;r2;r3;r4;r5",
                })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class VldbFrontierPlotTest(unittest.TestCase):
    def create_inputs(self, root: Path) -> tuple[Path, Path]:
        summary = root / "frontier_summary.csv"
        gate = root / "evidence_gate.json"
        write_csv(summary, summary_rows())
        gate.write_text(json.dumps({
            "ready_for_plotting": True,
            "expected_slabwalk_sha256": FINAL_SHA,
            "frontier": {"summary_sha256": hashlib.sha256(summary.read_bytes()).hexdigest()},
        }))
        return summary, gate

    def test_loads_complete_three_system_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary, gate = self.create_inputs(Path(tmp))
            rows = plotter.load_validated(summary, gate)
            self.assertEqual(len(rows), 45)

    def test_selects_recall_guarded_post_reduction_per_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary, gate = self.create_inputs(Path(tmp))
            rows = plotter.load_validated(summary, gate)
            pairs = plotter.select_high_recall_post_pairs(rows)
            self.assertEqual({pair["dataset"] for pair in pairs}, set(plotter.DATASETS))
            self.assertTrue(all(pair["post_reduction"] == 10.0 for pair in pairs))

    def test_rejects_summary_not_bound_to_final_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, gate = self.create_inputs(root)
            rows = summary_rows()
            rows[0]["binary_sha256s"] = "a" * 64
            write_csv(summary, rows)
            gate_obj = json.loads(gate.read_text())
            gate_obj["frontier"]["summary_sha256"] = hashlib.sha256(
                summary.read_bytes()
            ).hexdigest()
            gate.write_text(json.dumps(gate_obj))
            with self.assertRaisesRegex(ValueError, "binary SHA"):
                plotter.load_validated(summary, gate)

    def test_generates_nonempty_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, gate = self.create_inputs(root)
            out = root / "frontier.pdf"
            plotter.generate(summary, gate, out)
            self.assertGreater(out.stat().st_size, 5000)
            self.assertEqual(out.read_bytes()[:4], b"%PDF")

    def test_rejects_summary_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary, gate = self.create_inputs(root)
            summary.write_text(summary.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "summary SHA"):
                plotter.load_validated(summary, gate)


if __name__ == "__main__":
    unittest.main()
