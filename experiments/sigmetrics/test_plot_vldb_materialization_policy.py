import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import plot_vldb_materialization_policy as plot
from experiments.sigmetrics.test_summarize_vldb_materialization_policy import (
    BUNDLE_HOST,
    BUNDLE_SHA,
    write_policy_bundle,
)


class MaterializationPolicyPlotTest(unittest.TestCase):
    def test_generates_vector_pdf_from_validated_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_policy_bundle(Path(tmp) / "bundle")
            output = Path(tmp) / "policy.pdf"

            report = plot.generate(
                root,
                output,
                expected_sha=BUNDLE_SHA,
                expected_compute_host=BUNDLE_HOST,
                allow_incomplete=True,
            )

            self.assertEqual(report["measured_cells"], 9)
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))
            self.assertGreater(output.stat().st_size, 1_000)

    def test_paper_mode_rejects_incomplete_dataset_budget_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_policy_bundle(Path(tmp) / "bundle")

            with self.assertRaisesRegex(ValueError, "requires DEEP1M"):
                plot.generate(
                    root,
                    Path(tmp) / "policy.pdf",
                    expected_sha=BUNDLE_SHA,
                    expected_compute_host=BUNDLE_HOST,
                )


if __name__ == "__main__":
    unittest.main()
