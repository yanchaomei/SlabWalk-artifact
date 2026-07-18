import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from matplotlib import rc_context
from matplotlib.backends.backend_pdf import FigureCanvasPdf
from matplotlib.figure import Figure

import package_vldb_overleaf as package
import publish_vldb_release as release
import render_vldb_claims_tex as renderer
import test_render_vldb_claims_tex as render_test_support


class VldbOverleafPackageTest(unittest.TestCase):
    @staticmethod
    def write_valid_pdf(path: Path) -> None:
        with rc_context({"pdf.fonttype": 42}):
            figure = Figure(figsize=(8.0, 3.0))
            FigureCanvasPdf(figure)
            axis = figure.subplots()
            axis.plot([0, 1, 2], [0, 1, 0])
            axis.set_title("release fixture")
            figure.savefig(path, format="pdf", bbox_inches="tight")

    def make_tree(self, root: Path) -> None:
        (root / "figs").mkdir(parents=True)
        references = []
        pdf_bytes: bytes | None = None
        for index in range(9):
            relative = f"figs/figure_{index}.pdf"
            path = root / relative
            if pdf_bytes is None:
                self.write_valid_pdf(path)
                pdf_bytes = path.read_bytes()
            else:
                path.write_bytes(pdf_bytes)
            references.append(f"\\includegraphics[width=1cm]{{{relative}}}")
        (root / "main.tex").write_text(
            "\\documentclass{article}\n"
            "\\input{generated_claims.tex}\n"
            "\\begin{document}\n"
            + "\n".join(references)
            + "\n\\end{document}\n"
        )
        (root / "refs.bib").write_text("@misc{x, title={X}}\n")
        (root / "acmart.cls").write_text("class\n")
        (root / "pvldb.sty").write_text("style\n")
        (root / "ACM-Reference-Format.bst").write_text("style\n")

    def make_release(self, paper: Path) -> Path:
        repo = paper.parent
        staging = repo / "staging"
        staging.mkdir(exist_ok=True)
        gate = staging / "evidence_gate.json"
        gate.write_text(
            '{"kind":"vldb_final_evidence_gate","ready_for_plotting":true}\n'
        )
        gate_sha = package.sha256(gate)
        claims = staging / "manuscript_claims.json"
        claim_data = render_test_support.fixture()
        claim_data["gate_sha256"] = gate_sha
        claims.write_text(json.dumps(claim_data, sort_keys=True))
        generated = paper / "generated_claims.tex"
        renderer.render(claims, generated)
        members = package.submission_members(paper)
        entries = [
            ("results/evidence_gate.json", gate),
            ("results/manuscript_claims.json", claims),
            *[(f"paper/{member}", paper / member) for member in members],
        ]
        manifest = repo / "results/release_bundle.json"
        release.publish(
            repo_root=repo,
            gate=gate,
            claims=claims,
            generated_claims=generated,
            entries=entries,
            pdf_targets=[
                f"paper/{member}" for member in members if member.endswith(".pdf")
            ],
            manifest_out=manifest,
        )
        return manifest

    def test_package_is_byte_stable_and_has_exact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            first = Path(tmp) / "first.zip"
            second = Path(tmp) / "second.zip"

            first_report = package.build_package(root, first, release_manifest=manifest)
            for path in root.rglob("*"):
                if path.is_file():
                    path.chmod(0o600)
                    os.utime(path, (1_900_000_000, 1_900_000_000))
            second_report = package.build_package(root, second, release_manifest=manifest)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_report["sha256"], second_report["sha256"])
            self.assertEqual(first_report["members"], second_report["members"])
            self.assertEqual(len(first_report["members"]), 15)
            self.assertIn("pvldb.sty", first_report["members"])
            with zipfile.ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                for info in archive.infolist():
                    self.assertEqual(info.date_time, package.FIXED_TIMESTAMP)
                    self.assertEqual(info.create_system, 3)
                    self.assertEqual(
                        info.external_attr >> 16,
                        stat.S_IFREG | 0o644,
                    )
                    self.assertEqual(info.extra, b"")
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_rejects_figure_outside_submission_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    "figs/figure_0.pdf", "../private/figure_0.pdf"
                )
            )
            with self.assertRaisesRegex(ValueError, "portable|figs"):
                package.referenced_figures(root)

    def test_rejects_incomplete_figure_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            lines = main.read_text().splitlines()
            main.write_text(
                "\n".join(
                    line for line in lines if "figs/figure_8.pdf" not in line
                )
                + "\n"
            )
            with self.assertRaisesRegex(ValueError, "nine unique PDF figures"):
                package.referenced_figures(root)

    def test_rejects_generated_claim_input_hidden_by_tex_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\input{generated_claims.tex}",
                    r"\\% \input{generated_claims.tex}",
                )
            )
            with self.assertRaisesRegex(ValueError, "active preamble input"):
                package.submission_members(root)

    def test_rejects_generated_claim_input_inside_inactive_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\input{generated_claims.tex}",
                    "\\iffalse\n\\input{generated_claims.tex}\n\\fi",
                )
            )
            with self.assertRaisesRegex(ValueError, "conditional"):
                package.submission_members(root)

    def test_rejects_generated_claim_input_inside_an_uncalled_macro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\input{generated_claims.tex}",
                    "\\def\\NeverCalled{\n\\input{generated_claims.tex}\n}",
                )
            )
            with self.assertRaisesRegex(ValueError, "top-level|active preamble input"):
                package.submission_members(root)

    def test_rejects_input_primitive_redefinition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\input{generated_claims.tex}",
                    "\\let\\SavedInput\\input\n"
                    "\\def\\input#1{}\n"
                    "\\input{generated_claims.tex}\n"
                    "\\let\\input\\SavedInput",
                )
            )
            with self.assertRaisesRegex(ValueError, "protected.*redefinition"):
                package.submission_members(root)

    def test_rejects_group_scoped_generated_claim_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\input{generated_claims.tex}",
                    "\\begingroup\n"
                    "\\input{generated_claims.tex}\n"
                    "\\endgroup",
                )
            )
            with self.assertRaisesRegex(ValueError, "grouped preamble"):
                package.submission_members(root)

    def test_rejects_generated_claim_macro_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\begin{document}",
                    "\\renewcommand{\\ClaimFrontierQpsMin}{999}\n"
                    "\\begin{document}",
                )
            )
            with self.assertRaisesRegex(ValueError, "claim macro override"):
                package.submission_members(root)

    def test_rejects_starred_generated_claim_macro_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\begin{document}",
                    "\\renewcommand*{\\ClaimFrontierQpsMin}{999}\n"
                    "\\begin{document}",
                )
            )
            with self.assertRaisesRegex(ValueError, "claim macro override"):
                package.submission_members(root)

    def test_rejects_dynamic_generated_claim_macro_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\begin{document}",
                    "\\expandafter\\def\\csname ClaimFrontierQpsMin\\endcsname{999}\n"
                    "\\begin{document}",
                )
            )
            with self.assertRaisesRegex(ValueError, "dynamic control sequence"):
                package.submission_members(root)

    def test_rejects_figure_reference_inside_an_uncalled_macro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\includegraphics[width=1cm]{figs/figure_8.pdf}",
                    r"\def\NeverUsed{\includegraphics[width=1cm]{figs/figure_8.pdf}}",
                )
            )
            with self.assertRaisesRegex(ValueError, "top-level figure"):
                package.referenced_figures(root)

    def test_rejects_figure_environment_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            main = root / "main.tex"
            main.write_text(
                main.read_text().replace(
                    r"\begin{document}",
                    "\\usepackage{comment}\n"
                    "\\excludecomment{figure}\n"
                    "\\begin{document}",
                )
            )
            with self.assertRaisesRegex(ValueError, "environment control"):
                package.member_names(main.read_text())

    def test_rejects_stale_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            figure = root / "figs/figure_0.pdf"
            figure.write_bytes(b"PDF-0\n")
            with self.assertRaisesRegex(
                ValueError, r"release target (?:size|hash) mismatch"
            ):
                package.build_package(
                    root, Path(tmp) / "bad.zip", release_manifest=manifest
                )

    def test_rejects_output_aliasing_release_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            protected = (root / "main.tex", manifest)
            before = {path: path.read_bytes() for path in protected}
            for output in protected:
                with self.subTest(output=output), self.assertRaisesRegex(
                    ValueError, "protected release input"
                ):
                    package.build_package(
                        root,
                        output,
                        release_manifest=manifest,
                        force=True,
                    )
            for path, expected in before.items():
                self.assertEqual(path.read_bytes(), expected)

    def test_revalidates_pdf_bytes_from_release_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            figure = root / "figs/figure_0.pdf"
            figure.write_bytes(b"not a PDF\n")
            record = json.loads(manifest.read_text())
            target = "paper/figs/figure_0.pdf"
            record["entries"][target] = {
                "sha256": package.sha256(figure),
                "size_bytes": figure.stat().st_size,
            }
            manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

            with self.assertRaisesRegex(ValueError, "publication PDF verification"):
                package.build_package(
                    root,
                    Path(tmp) / "invalid-pdf.zip",
                    release_manifest=manifest,
                )

    def test_uses_verified_member_snapshot_when_live_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            victim = root / "figs/figure_0.pdf"
            expected = victim.read_bytes()
            original_capture = release.capture_release_snapshot

            def capture_then_mutate(repo_root: Path, manifest_bytes: bytes):
                report, snapshots = original_capture(repo_root, manifest_bytes)
                victim.write_bytes(b"changed-after-release-verification\n")
                return report, snapshots

            release.capture_release_snapshot = capture_then_mutate
            output = Path(tmp) / "raced.zip"
            try:
                package.build_package(root, output, release_manifest=manifest)
            finally:
                release.capture_release_snapshot = original_capture
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("figs/figure_0.pdf"), expected)

    def test_uses_one_verified_manifest_byte_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            manifest_bytes = manifest.read_bytes()
            original_capture = release.capture_release_snapshot

            def capture_then_replace_manifest(repo_root: Path, snapshot: bytes):
                report, members = original_capture(repo_root, snapshot)
                manifest.write_text('{"kind":"changed-after-snapshot"}\n')
                return report, members

            release.capture_release_snapshot = capture_then_replace_manifest
            output = Path(tmp) / "snapshot.zip"
            try:
                report = package.build_package(
                    root, output, release_manifest=manifest
                )
            finally:
                release.capture_release_snapshot = original_capture
            self.assertEqual(
                report["release_manifest_sha256"],
                package.sha256_bytes(manifest_bytes),
            )
            self.assertEqual(len(report["members"]), 15)
            self.assertIn("pvldb.sty", report["members"])

    def test_packages_from_one_verified_member_byte_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            self.make_tree(root)
            manifest = self.make_release(root)
            victim = root / "figs/figure_0.pdf"
            expected = victim.read_bytes()
            original_capture = release.capture_release_snapshot

            def capture_then_remove(repo_root: Path, manifest_bytes: bytes):
                report, snapshots = original_capture(repo_root, manifest_bytes)
                victim.unlink()
                return report, snapshots

            release.capture_release_snapshot = capture_then_remove
            output = Path(tmp) / "snapshot-members.zip"
            try:
                package.build_package(root, output, release_manifest=manifest)
            finally:
                release.capture_release_snapshot = original_capture
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("figs/figure_0.pdf"), expected)


if __name__ == "__main__":
    unittest.main()
