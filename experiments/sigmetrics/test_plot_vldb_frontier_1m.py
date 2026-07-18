#!/usr/bin/env python3
import csv
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_frontier_1m as assembler
import plot_vldb_frontier_1m as plotter
import validate_vldb_frontier_1m as validator
from test_validate_vldb_frontier_1m import FINAL_SHA, write_bundle, write_csv


class PlotVldbFrontier1MTest(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path]:
        bundle = write_bundle(root)
        gate = root / "frontier_1m_gate.json"
        gate.write_text(json.dumps(validator.validate(bundle, FINAL_SHA)))
        return bundle / "frontier_summary.csv", gate

    def test_loads_all_seven_datasets_and_three_methods(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            summary, gate = self.make_inputs(Path(tmp_s))
            rows = plotter.load_validated(summary, gate)
            self.assertEqual({row["dataset"] for row in rows}, set(assembler.DATASETS))
            self.assertEqual({row["method"] for row in rows}, set(assembler.METHODS))
            self.assertEqual(len(rows), 105)

    def test_missing_method_curve_is_rejected_even_with_a_rehashed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = self.make_inputs(root)
            with summary.open(newline="") as handle:
                rows = [
                    row
                    for row in csv.DictReader(handle)
                    if not (row["dataset"] == "SPACEV1M" and row["method"] == "d-HNSW")
                ]
            write_csv(summary, rows)
            gate_obj = json.loads(gate.read_text())
            gate_obj["summary_sha256"] = validator.file_sha256(summary)
            gate.write_text(json.dumps(gate_obj))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                plotter.load_validated(summary, gate)

    def test_generates_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            summary, gate = self.make_inputs(root)
            output = root / "frontier_1m.pdf"
            plotter.generate(summary, gate, output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
