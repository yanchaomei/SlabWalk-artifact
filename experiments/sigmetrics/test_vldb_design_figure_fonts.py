import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = REPO_ROOT / "paper_vldb" / "figs" / "gen_vldb_design_figures.py"
SPEC = importlib.util.spec_from_file_location("gen_vldb_design_figures", GENERATOR_PATH)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GENERATOR)


class VldbDesignFigureFontTest(unittest.TestCase):
    def test_rsvg_renderer_can_be_selected_for_memory_bounded_release(self) -> None:
        with mock.patch.dict(os.environ, {"SLABWALK_SVG_RENDERER": "rsvg"}):
            self.assertEqual(GENERATOR.svg_renderer_mode(), "rsvg")

        with mock.patch.dict(os.environ, {"SLABWALK_SVG_RENDERER": "invalid"}):
            with self.assertRaisesRegex(ValueError, "SLABWALK_SVG_RENDERER"):
                GENERATOR.svg_renderer_mode()

    def test_svg_only_mode_never_invokes_a_pdf_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    "python3",
                    str(GENERATOR_PATH),
                    "--only",
                    "fig_physical_units",
                    "--svg-only",
                    "--output-dir",
                    tmp,
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "fig_physical_units.svg").is_file())
            self.assertFalse((Path(tmp) / "fig_physical_units.pdf").exists())

    def test_overview_separates_client_cn_and_mn_ownership(self) -> None:
        svg = GENERATOR.overview()
        root = ET.fromstring(svg)
        zones = [
            element
            for element in root.iter()
            if element.attrib.get("data-role") == "ownership-zone"
        ]
        self.assertEqual(
            {zone.attrib.get("data-owner") for zone in zones},
            {"clients", "cn-local", "mn-resident"},
        )
        self.assertEqual(len(zones), 3)
        self.assertIn("CN-local state", svg)
        self.assertIn("CN: prefix tables", svg)
        self.assertIn("MN: authoritative HNSW", svg)
        self.assertIn("MN: Slab regions", svg)

    def test_slab_layout_row_labels_have_readable_vertical_separation(self) -> None:
        root = ET.fromstring(GENERATOR.slab_layout())
        labels = {
            "".join(element.itertext()): float(element.attrib["y"])
            for element in root.iter()
            if element.tag.endswith("text") and element.attrib.get("x") == "48"
        }
        self.assertIn("IDs", labels)
        self.assertIn("hot prefix", labels)
        self.assertGreaterEqual(labels["hot prefix"] - labels["IDs"], 26.0)

    def test_slab_layout_records_policy_guards_in_descriptor(self) -> None:
        svg = GENERATOR.slab_layout()
        self.assertIn("static budget map", svg)
        self.assertIn("recorded in descriptor", svg)
        self.assertIn("metric + dimension guard", svg)
        self.assertNotIn("no auto-optimizer", svg)
        self.assertNotIn("no online optimizer", svg)

    def test_figures_distinguish_topology_from_beam_order_and_measured_balance(self) -> None:
        with mock.patch.object(
            GENERATOR,
            "measured_cache_control",
            return_value=(
                (0, 5, 20, 50),
                (100.0, 80.0, 50.0, 25.0),
                (1.0, 1.0, 1.0, 1.0),
                (1000.0, 950.0, 900.0, 850.0),
                (10.0, 10.0, 10.0, 10.0),
                20.0,
            ),
        ):
            svg = "\n".join((
                GENERATOR.physical_units(),
                GENERATOR.overview(),
                GENERATOR.search_placement(),
            ))
        self.assertIn("authoritative topology", svg)
        self.assertIn("owners striped", svg)
        self.assertNotIn("same global beam", svg)
        self.assertNotIn("same beam update", svg)
        self.assertNotIn("links balanced", svg)

    def test_construction_figure_names_builder_parallelism_unambiguously(self) -> None:
        svg = GENERATOR.construction_refresh()
        self.assertIn("pack B[u] (20T)", svg)
        self.assertNotIn("pack B[u] x20", svg)

    def test_generated_design_figures_retain_embedded_text_fonts(self) -> None:
        if shutil.which("pdffonts") is None:
            self.skipTest("pdffonts is unavailable")

        figure_dir = REPO_ROOT / "paper_vldb" / "figs"
        for name in (
            "overview",
            "fig_slab_layout",
            "fig_search_placement",
            "fig_construction_refresh",
        ):
            with self.subTest(name=name):
                GENERATOR.verify_publication_fonts(figure_dir / f"{name}.pdf")

    def test_font_gate_rejects_outlined_fontless_pdf(self) -> None:
        fontless_report = """name type encoding emb sub uni object ID
---- ---- -------- --- --- --- ---------
"""
        completed = subprocess.CompletedProcess(
            args=["pdffonts", "figure.pdf"],
            returncode=0,
            stdout=fontless_report,
            stderr="",
        )
        with mock.patch.object(GENERATOR.shutil, "which", return_value="pdffonts"):
            with mock.patch.object(GENERATOR.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "searchable text"):
                    GENERATOR.verify_publication_fonts(Path("figure.pdf"))


if __name__ == "__main__":
    unittest.main()
