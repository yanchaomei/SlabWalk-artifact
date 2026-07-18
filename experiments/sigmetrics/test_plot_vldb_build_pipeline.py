import tempfile
import unittest
from pathlib import Path
from unittest import mock

from experiments.sigmetrics import plot_vldb_build_pipeline as plotter
from experiments.sigmetrics.test_summarize_vldb_build_pipeline import (
    HOST,
    SHA,
    write_bundle,
)


class BuildPipelinePlotTest(unittest.TestCase):
    def test_rejects_smoke_campaign_by_default(self) -> None:
        with mock.patch.object(
            plotter.build_summary,
            "validate_bundle",
            return_value={"campaign_kind": "smoke"},
        ):
            with self.assertRaisesRegex(ValueError, "non-formal"):
                plotter.generate(
                    Path("unused"),
                    Path("unused.pdf"),
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_generates_vector_figure_from_semantically_valid_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = write_bundle(root / "bundle")
            output = root / "builder_scaling.pdf"

            report = plotter.generate(
                bundle,
                output,
                expected_sha=SHA,
                expected_compute_host=HOST,
            )

            self.assertEqual(report["measured_cells"], 4)
            self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
            self.assertGreater(output.stat().st_size, 4_000)


if __name__ == "__main__":
    unittest.main()
