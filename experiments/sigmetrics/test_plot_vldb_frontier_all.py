#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import plot_vldb_frontier_all as plotter
import validate_vldb_frontier_1m as validator_1m
from test_plot_vldb_frontier_10m import FINAL_SHA, summary_rows, write_csv
from test_validate_vldb_frontier_1m import write_bundle


class PlotVldbFrontierAllTest(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path, Path, Path]:
        bundle = write_bundle(root / "one")
        gate_1m = root / "gate_1m.json"
        gate_1m.write_text(json.dumps(validator_1m.validate(bundle, FINAL_SHA)))
        summary_10m = root / "summary_10m.csv"
        write_csv(summary_10m, summary_rows())
        gate_10m = root / "gate_10m.json"
        gate_10m.write_text(
            json.dumps(
                {
                    "ready_for_plotting": True,
                    "expected_slabwalk_sha256": FINAL_SHA,
                    "frontier": {
                        "summary_sha256": hashlib.sha256(
                            summary_10m.read_bytes()
                        ).hexdigest()
                    },
                }
            )
        )
        return bundle / "frontier_summary.csv", gate_1m, summary_10m, gate_10m

    def test_contract_has_seven_breadth_and_three_scale_datasets(self) -> None:
        self.assertEqual(len(plotter.DATASETS), 10)
        self.assertEqual(plotter.DATASETS[:7], plotter.frontier_1m.DATASETS)
        self.assertEqual(plotter.DATASETS[7:], plotter.frontier_10m.DATASETS)

    def test_generates_ten_panel_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            inputs = self.make_inputs(root)
            output = root / "frontier_all.pdf"
            plotter.generate(*inputs, output)
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes()[:4], b"%PDF")
            self.assertGreater(output.stat().st_size, 12_000)


if __name__ == "__main__":
    unittest.main()
