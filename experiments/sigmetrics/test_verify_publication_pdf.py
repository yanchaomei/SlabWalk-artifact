import tempfile
import unittest
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pypdf import PdfReader, PdfWriter

import verify_publication_pdf as verifier


VALID_FONT_REPORT = """name                                 type              encoding         emb sub uni object ID
------------------------------------ ----------------- ---------------- --- --- --- ---------
ABCDEF+TimesNewRomanPSMT             CID TrueType      Identity-H       yes yes yes     24  0
"""


class PublicationPdfTest(unittest.TestCase):
    def write_figure(self, path: Path, *, landscape: bool = True) -> None:
        size = (4.0, 2.0) if landscape else (2.0, 4.0)
        fig, ax = plt.subplots(figsize=size)
        ax.plot([0, 1], [0, 1])
        ax.set_xlabel("Recall")
        fig.savefig(path)
        plt.close(fig)

    def test_accepts_single_page_landscape_pdf_with_embedded_fonts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "figure.pdf"
            self.write_figure(path)
            report = verifier.verify_pdf_structure(path)
            self.assertEqual(report.pages, 1)
            self.assertGreater(report.width_points, report.height_points)
            fonts = verifier.parse_pdffonts(VALID_FONT_REPORT)
            self.assertEqual(len(fonts), 1)

    def test_rejects_multiple_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "figure.pdf"
            source = Path(tmp) / "source.pdf"
            self.write_figure(source)
            reader = PdfReader(source)
            writer = PdfWriter()
            writer.add_page(reader.pages[0])
            writer.add_page(reader.pages[0])
            with path.open("wb") as handle:
                writer.write(handle)
            with self.assertRaisesRegex(ValueError, "exactly one page"):
                verifier.verify_pdf_structure(path)

    def test_rejects_portrait_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "figure.pdf"
            self.write_figure(path, landscape=False)
            with self.assertRaisesRegex(ValueError, "landscape"):
                verifier.verify_pdf_structure(path)

    def test_rejects_type3_font(self) -> None:
        report = """name type encoding emb sub uni object ID
---- ---- -------- --- --- --- ---------
ABCDEF+CMR10 Type 3 Custom yes yes no 7 0
"""
        with self.assertRaisesRegex(ValueError, "Type 3"):
            verifier.parse_pdffonts(report)

    def test_rejects_unembedded_font(self) -> None:
        report = """name type encoding emb sub uni object ID
---- ---- -------- --- --- --- ---------
Times-Roman Type 1 WinAnsi no no yes 7 0
"""
        with self.assertRaisesRegex(ValueError, "not embedded"):
            verifier.parse_pdffonts(report)

    def test_rejects_fontless_report(self) -> None:
        report = """name type encoding emb sub uni object ID
---- ---- -------- --- --- --- ---------
"""
        with self.assertRaisesRegex(ValueError, "no fonts"):
            verifier.parse_pdffonts(report)


if __name__ == "__main__":
    unittest.main()
