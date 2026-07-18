from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from matplotlib import rc_context
from matplotlib.backends.backend_pdf import FigureCanvasPdf
from matplotlib.figure import Figure

import publish_vldb_release as release
import render_vldb_claims_tex as renderer
import test_render_vldb_claims_tex as render_test_support


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublishVldbReleaseTest(unittest.TestCase):
    @staticmethod
    def write_valid_pdf(path: Path, label: str = "release fixture") -> None:
        with rc_context({"pdf.fonttype": 42}):
            figure = Figure(figsize=(8.0, 3.0))
            FigureCanvasPdf(figure)
            axis = figure.subplots()
            axis.plot([0, 1, 2], [0, 1, 0])
            axis.set_title(label)
            figure.savefig(path, format="pdf", bbox_inches="tight")

    def make_inputs(self, root: Path) -> tuple[Path, Path, Path, list[Path]]:
        staging = root / "staging"
        staging.mkdir()
        gate = staging / "evidence_gate.json"
        gate.write_text(json.dumps({
            "kind": "vldb_final_evidence_gate",
            "ready_for_plotting": True,
            "frontier": {},
        }))
        gate_sha = sha(gate)
        claims = staging / "manuscript_claims.json"
        claim_data = render_test_support.fixture()
        claim_data["gate_sha256"] = gate_sha
        claims.write_text(json.dumps(claim_data, sort_keys=True))
        generated = staging / "generated_claims.tex"
        renderer.render(claims, generated)
        figures = [staging / f"figure_{index}.pdf" for index in range(9)]
        self.write_valid_pdf(figures[0])
        pdf_bytes = figures[0].read_bytes()
        for figure in figures[1:]:
            figure.write_bytes(pdf_bytes)
        return gate, claims, generated, figures

    @staticmethod
    def entries(
        gate: Path, claims: Path, generated: Path, figures: list[Path]
    ) -> list[tuple[str, Path]]:
        return [
            ("results/evidence_gate.json", gate),
            ("results/manuscript_claims.json", claims),
            ("paper/generated_claims.tex", generated),
            *[
                (f"paper/figure_{index}.pdf", figure)
                for index, figure in enumerate(figures)
            ],
        ]

    @staticmethod
    def pdf_targets(figures: list[Path]) -> list[str]:
        return [f"paper/figure_{index}.pdf" for index, _ in enumerate(figures)]

    def test_publishes_marker_last_and_verifies_every_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            manifest = root / "results" / "release_bundle.json"
            report = release.publish(
                repo_root=root,
                gate=gate,
                claims=claims,
                generated_claims=generated,
                entries=self.entries(gate, claims, generated, figures),
                pdf_targets=self.pdf_targets(figures),
                manifest_out=manifest,
            )
            self.assertEqual(report["kind"], "vldb_release_bundle")
            self.assertEqual(report["gate_sha256"], sha(root / "results/evidence_gate.json"))
            self.assertEqual(release.verify_release(root, manifest)["entries_verified"], 12)
            (root / "paper/figure_0.pdf").write_bytes(b"publication-PDF\n")
            with self.assertRaisesRegex(
                ValueError, r"release target (?:size|hash) mismatch"
            ):
                release.verify_release(root, manifest)

    def test_rejects_a_release_without_exactly_nine_pdf_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            manifest = root / "results" / "release_bundle.json"

            with self.assertRaisesRegex(ValueError, "exactly nine"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures)[:-1],
                    manifest_out=manifest,
                )
            self.assertFalse(manifest.exists())

    def test_rejects_noncanonical_release_target_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            entries = self.entries(gate, claims, generated, figures)
            entries[3] = ("paper//figure_0.pdf", figures[0])
            targets = self.pdf_targets(figures)
            targets[0] = "paper//figure_0.pdf"
            manifest = root / "results/release_bundle.json"

            with self.assertRaisesRegex(ValueError, "canonical"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=entries,
                    pdf_targets=targets,
                    manifest_out=manifest,
                )
            self.assertFalse(manifest.exists())

    def test_failed_replacement_removes_the_ready_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            manifest = root / "results" / "release_bundle.json"
            manifest.parent.mkdir()
            manifest.write_text('{"kind":"vldb_release_bundle"}\n')
            calls = 0

            def fail_after_first(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publication failure")
                os.replace(source, target)

            with self.assertRaisesRegex(OSError, "injected publication failure"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=manifest,
                    replace_fn=fail_after_first,
                )
            self.assertFalse(manifest.exists())

    def test_rejects_generated_claim_mutation_even_with_provenance_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            generated.write_text(
                generated.read_text().replace(
                    r"\newcommand{\ClaimFrontierQpsMin}{9.99}",
                    r"\newcommand{\ClaimFrontierQpsMin}{9999.99}",
                )
            )
            manifest = root / "results/release_bundle.json"
            manifest.parent.mkdir()
            manifest.write_text('{"kind":"vldb_release_bundle"}\n')

            with self.assertRaisesRegex(ValueError, "deterministic rendering"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=manifest,
                )
            self.assertFalse(manifest.exists())

    def test_rejects_ready_payload_with_wrong_gate_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            gate_data = json.loads(gate.read_text())
            gate_data["kind"] = "not_a_final_gate"
            gate.write_text(json.dumps(gate_data))
            claim_data = json.loads(claims.read_text())
            claim_data["gate_sha256"] = sha(gate)
            claims.write_text(json.dumps(claim_data, sort_keys=True))
            renderer.render(claims, generated)

            with self.assertRaisesRegex(ValueError, "gate kind"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=root / "results/release_bundle.json",
                )

    def test_hashes_the_staged_copy_when_source_changes_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "repo").resolve()
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            manifest = root / "results/release_bundle.json"
            original_copy = release.shutil.copyfileobj
            mutated = False
            alternate = root / "alternate.pdf"
            self.write_valid_pdf(alternate, "changed during copy")
            alternate_bytes = alternate.read_bytes()

            def mutate_then_copy(src, dst, length=0):
                nonlocal mutated
                if Path(src.name).resolve() == figures[0].resolve() and not mutated:
                    figures[0].write_bytes(alternate_bytes)
                    mutated = True
                return original_copy(src, dst, length=length)

            release.shutil.copyfileobj = mutate_then_copy
            try:
                report = release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=manifest,
                )
            finally:
                release.shutil.copyfileobj = original_copy

            self.assertTrue(mutated)
            self.assertEqual(report["entries_verified"], 12)
            self.assertEqual(
                json.loads(manifest.read_text())["entries"]["paper/figure_0.pdf"]["sha256"],
                sha(root / "paper/figure_0.pdf"),
            )

    def test_target_corruption_before_manifest_install_leaves_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            manifest = root / "results/release_bundle.json"

            def replace_then_corrupt(source: Path, target: Path) -> None:
                os.replace(source, target)
                if target.name == "figure_0.pdf":
                    target.write_bytes(b"corrupt-installed-target\n")

            with self.assertRaisesRegex(ValueError, "release target .* mismatch"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=manifest,
                    replace_fn=replace_then_corrupt,
                )
            self.assertFalse(manifest.exists())

    def test_rejects_invalid_pdf_from_the_publisher_staging_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            figures[0].write_bytes(b"publication-pdf\n")
            manifest = root / "results" / "release_bundle.json"
            with self.assertRaisesRegex(ValueError, "publication PDF verification"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=manifest,
                )
            self.assertFalse(manifest.exists())

    def test_directory_fsync_failure_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                release.os, "fsync", side_effect=OSError("durability failure")
            ):
                with self.assertRaisesRegex(OSError, "durability failure"):
                    release.fsync_directory(Path(tmp))

    def test_rejects_claims_not_bound_to_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            gate, claims, generated, figures = self.make_inputs(root)
            data = json.loads(claims.read_text())
            data["gate_sha256"] = "b" * 64
            claims.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "claims are not bound"):
                release.publish(
                    repo_root=root,
                    gate=gate,
                    claims=claims,
                    generated_claims=generated,
                    entries=self.entries(gate, claims, generated, figures),
                    pdf_targets=self.pdf_targets(figures),
                    manifest_out=root / "results/release_bundle.json",
                )


if __name__ == "__main__":
    unittest.main()
