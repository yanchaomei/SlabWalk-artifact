import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import plot_vldb_resource_ledger as plotter


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


def measured_rows() -> list[dict[str, object]]:
    rows = []
    for layout_index, layout in enumerate(plotter.LAYOUTS):
        for mns in plotter.MN_COUNTS:
            for repeat in range(5):
                materialized = (5.0 - layout_index) * 2**30
                rows.append({
                    "dataset": "gist1m",
                    "layout": layout,
                    "memory_nodes": mns,
                    "repeat": repeat,
                    "num_vectors": 1000000,
                    "num_queries": 1000,
                    "threads": 10,
                    "coroutines_per_thread": 2,
                    "binary_sha256": FINAL_SHA,
                    "manifest_cell_fingerprint": f"manifest-{layout}-{mns}",
                    "campaign_protocol_fingerprint": "resource-final",
                    "recall": 0.9,
                    "qps": 8000 + layout_index * 1000 + mns * 100 + repeat,
                    "query_read_bytes_per_query": 2**20 * (6 - layout_index),
                    "query_read_wrs_per_query": 60 - layout_index * 10,
                    "query_read_submits_per_query": 30 - layout_index * 8,
                    "read_bytes_gini": 0.006 - layout_index * 0.001,
                    "measured_authoritative_index_bytes": 12 * 2**30,
                    "registered_sidecar_bytes": materialized * 1.25,
                    "materialized_sidecar_bytes": materialized,
                    "actual_sidecar_write_bytes": materialized * 0.98,
                    "registered_utilization": 0.8,
                    "storage_amplification": 1 + materialized / (12 * 2**30),
                    "query_latency_p50_us": 80 + layout_index * 5,
                    "query_latency_p95_us": 100 + layout_index * 10,
                    "query_latency_p99_us": 120 + layout_index * 15 + repeat,
                    "lavd_build_ms": 20000 - layout_index * 2000 + repeat * 10,
                    "cn_peak_rss_kib": (6 - layout_index) * 2**20,
                    "mn_peak_rss_sum_kib": mns * (4 - layout_index * 0.5) * 2**20,
                    "mn_peak_rss_max_kib": (4 - layout_index * 0.5) * 2**20,
                    "lavd_build_fetch_ms": 1000 - layout_index * 100,
                    "lavd_build_encode_ms": 2000 - layout_index * 200,
                    "lavd_build_materialize_ms": 15000 - layout_index * 1500,
                    "resident_upper_build_ms": 250 + repeat,
                })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def mechanism_summaries(root: Path) -> tuple[Path, Path]:
    budget = root / "mechanism_controls" / "budget_summary.csv"
    resident = root / "mechanism_controls" / "resident_summary.csv"
    budget_rows = []
    for key, fraction in (
        ("f05", 0.05), ("f10", 0.10), ("f25", 0.25),
        ("f50", 0.50), ("f75", 0.75), ("full", 1.0),
    ):
        budget_rows.append({
            "key": key,
            "n": 5,
            "materialized_fraction": fraction,
            "materialized_bytes_mean": fraction * 8 * 2**30,
            "materialized_bytes_ci95": 1024,
            "qps_mean": 8000 + fraction * 2000,
            "qps_ci95": 50,
            "recall_mean": 0.90 + fraction * 0.05,
            "recall_ci95": 0.001,
        })
    resident_rows = []
    for mode in ("remote", "resident"):
        for ef in (50, 100, 200):
            resident_rows.append({
                "mode": mode,
                "ef": ef,
                "n": 5,
                "posts_upnav_per_query_mean": 25 if mode == "remote" else 0,
                "qps_mean": (4000 if mode == "remote" else 5200) - ef,
                "qps_ci95": 30,
            })
    write_csv(budget, budget_rows)
    write_csv(resident, resident_rows)
    return budget, resident


class VldbResourceLedgerPlotTest(unittest.TestCase):
    def create_inputs(self, root: Path) -> tuple[Path, Path, Path, Path]:
        runs = root / "resource_ledger" / "runs.csv"
        gate = root / "evidence_gate.json"
        write_csv(runs, measured_rows())
        budget, resident = mechanism_summaries(root)
        gate.write_text(json.dumps({
            "ready_for_plotting": True,
            "expected_slabwalk_sha256": FINAL_SHA,
            "resource_ledger": {
                "runs_sha256": hashlib.sha256(runs.read_bytes()).hexdigest()
            },
            "mechanism_controls": {
                "budget_summary_sha256": hashlib.sha256(budget.read_bytes()).hexdigest(),
                "resident_summary_sha256": hashlib.sha256(resident.read_bytes()).hexdigest(),
            },
        }))
        return runs, budget, resident, gate

    def test_loads_complete_layout_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, _, _, gate = self.create_inputs(Path(tmp))
            rows = plotter.load_validated(runs, gate)
            self.assertEqual(len(rows), 45)

    def test_rejects_runs_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, _, _, gate = self.create_inputs(Path(tmp))
            runs.write_text(runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "runs SHA"):
                plotter.load_validated(runs, gate)

    def test_generates_nonempty_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, budget, resident, gate = self.create_inputs(root)
            out = root / "resource.pdf"
            plotter.generate(runs, budget, resident, gate, out)
            self.assertGreater(out.stat().st_size, 10000)
            self.assertEqual(out.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
