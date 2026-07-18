import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_lifecycle_controls as lifecycle_assembler
import plot_vldb_lifecycle as plotter
import validate_vldb_final_evidence as evidence
from test_assemble_vldb_lifecycle_controls import write_lifecycle_sources
from test_validate_vldb_build_cost import FINAL_SHA, write_build_cost_evidence


class VldbLifecyclePlotTest(unittest.TestCase):
    def create_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        build_cost = root / "build_cost"
        write_build_cost_evidence(build_cost)
        refresh_summary, refresh_root, tti_summary, tti_root = (
            write_lifecycle_sources(root / "lifecycle_sources")
        )
        lifecycle = root / "lifecycle"
        lifecycle_assembler.assemble(
            refresh_summary, refresh_root, tti_summary, tti_root, lifecycle
        )
        build_report = evidence.validate_build_cost(build_cost, FINAL_SHA)
        lifecycle_report = evidence.validate_lifecycle_controls(lifecycle)
        gate = root / "gate.json"
        gate.write_text(json.dumps({
            "ready_for_plotting": True,
            "expected_slabwalk_sha256": FINAL_SHA,
            "build_cost": build_report,
            "lifecycle_controls": lifecycle_report,
        }, sort_keys=True))
        return build_cost, lifecycle, gate

    def test_generates_nonempty_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_cost, lifecycle, gate = self.create_inputs(root)
            out = root / "lifecycle.pdf"
            plotter.generate(build_cost, lifecycle, gate, out)
            self.assertGreater(out.stat().st_size, 10000)
            self.assertEqual(out.read_bytes()[:4], b"%PDF")

    def test_rejects_build_summary_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_cost, lifecycle, gate = self.create_inputs(root)
            path = build_cost / "summary.csv"
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "build-cost summary SHA"):
                plotter.load_validated(build_cost, lifecycle, gate)

    def test_rejects_lifecycle_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_cost, lifecycle, gate = self.create_inputs(root)
            path = lifecycle / "refresh.csv"
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "lifecycle refresh SHA"):
                plotter.load_validated(build_cost, lifecycle, gate)


if __name__ == "__main__":
    unittest.main()
