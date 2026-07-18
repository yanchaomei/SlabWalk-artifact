from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import package_public_artifact as artifact
import vldb_evidence_bundle as evidence_bundle


def write(path: Path, data: bytes | str = b"fixture\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)


class PackagePublicArtifactTest(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        write(root / ".gitignore", "__pycache__/\n*.py[cod]\n")
        write(root / "PUBLIC_ARTIFACT_README.md", "# Public artifact\n")
        write(root / "ARTIFACT.md", "# Workflow\n")
        write(root / "requirements.txt", "matplotlib\n")
        write(root / "graphbeyond/LICENSE", "Apache-2.0\n")
        write(root / "graphbeyond/src/index.cc", "int main() { return 0; }\n")
        write(root / "graphbeyond/tests/index_test.cc", "// test\n")
        write(root / "graphbeyond/build/shine", b"ELF build output")
        write(root / "experiments/README.md", "# Experiments\n")
        write(root / "experiments/sigmetrics/run.py", "print('run')\n")
        write(root / "experiments/sigmetrics/__pycache__/run.pyc", b"cache")
        write(root / "experiments/tools/groundtruth.py", "print('gt')\n")
        write(root / "results/vldb_final_evidence/evidence_gate.json", "{}\n")
        write(root / "results/vldb_final_evidence/raw/run.stderr", "ok\n")
        write(root / "paper_vldb/main.tex", "paper\n")
        write(root / "paper_vldb/refs.bib", "refs\n")
        write(root / "paper_vldb/acmart.cls", "class\n")
        write(root / "paper_vldb/pvldb.sty", "style\n")
        write(root / "paper_vldb/ACM-Reference-Format.bst", "bst\n")
        write(root / "paper_vldb/generated_claims.tex", "claims\n")
        write(root / "paper_vldb/CLAIM_EVIDENCE_LEDGER.md", "ledger\n")
        write(
            root / "paper_vldb/figs/gen_vldb_design_figures.py",
            "def build_all():\n    return None\n",
        )
        write(root / "paper_vldb/figs/figure.pdf", b"%PDF-1.4\nfixture")
        release = {
            "kind": "vldb_release_bundle",
            "publication_pdf_targets": ["paper_vldb/figs/figure.pdf"],
        }
        write(
            root / "results/vldb_final_evidence/release_bundle.json",
            json.dumps(release),
        )

    def test_builds_allowlisted_hash_bound_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            out = Path(tmp) / "public"
            self.make_repo(root)

            report = artifact.build_public_artifact(root, out)

            self.assertEqual((out / "README.md").read_text(), "# Public artifact\n")
            self.assertEqual(
                (out / ".gitignore").read_text(), "__pycache__/\n*.py[cod]\n"
            )
            self.assertTrue((out / "graphbeyond/src/index.cc").is_file())
            self.assertTrue((out / "results/vldb_final_evidence/raw/run.stderr").is_file())
            self.assertTrue((out / "paper_vldb/figs/figure.pdf").is_file())
            self.assertTrue(
                (out / "paper_vldb/figs/gen_vldb_design_figures.py").is_file()
            )
            self.assertTrue((out / "paper_vldb/acmart.cls").is_file())
            self.assertTrue((out / "paper_vldb/pvldb.sty").is_file())
            self.assertTrue(
                (out / "paper_vldb/ACM-Reference-Format.bst").is_file()
            )
            self.assertFalse((out / "graphbeyond/build/shine").exists())
            self.assertFalse(
                (out / "experiments/sigmetrics/__pycache__/run.pyc").exists()
            )

            manifest = json.loads((out / "artifact_manifest.json").read_text())
            self.assertEqual(manifest["kind"], "slabwalk_public_artifact")
            self.assertEqual(manifest["file_count"], len(manifest["files"]))
            self.assertEqual(report["manifest_sha256"], hashlib.sha256(
                (out / "artifact_manifest.json").read_bytes()
            ).hexdigest())
            for relative, record in manifest["files"].items():
                payload = (out / relative).read_bytes()
                self.assertEqual(record["bytes"], len(payload))
                self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())

            checksums = (out / "SHA256SUMS").read_text().splitlines()
            self.assertEqual(len(checksums), manifest["file_count"])
            self.assertEqual(checksums, sorted(checksums, key=lambda line: line[66:]))

    def test_preserves_complete_sealed_evidence_despite_generic_ignores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            out = Path(tmp) / "public"
            self.make_repo(root)
            bundle = root / "results/vldb_final_evidence/sealed_policy"
            campaign = {
                "campaign_id": "sealed-policy-fixture",
                "campaign_uuid": "5d51d1d3-b27b-40ab-a30b-0cf48841279b",
                "protocol_fingerprint": "1" * 64,
            }
            write(bundle / "campaign.json", json.dumps(campaign) + "\n")
            write(bundle / "harness/__pycache__/tool.cpython-38.pyc", b"sealed-pyc")
            evidence_bundle.seal_bundle(bundle, bundle / "campaign.json")

            artifact.build_public_artifact(root, out)

            copied = out / "results/vldb_final_evidence/sealed_policy"
            self.assertTrue(
                (copied / "harness/__pycache__/tool.cpython-38.pyc").is_file()
            )
            evidence_bundle.verify_bundle(copied)

    def test_rejects_public_ip_or_private_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            self.make_repo(root)
            public_ip = ".".join(["8", "8", "8", "8"])
            write(
                root / "experiments/sigmetrics/leak.sh",
                f"ssh user@{public_ip}\n"
                + "-----BEGIN "
                + "OPENSSH PRIVATE KEY-----\n",
            )

            with self.assertRaisesRegex(ValueError, "sensitive material"):
                artifact.build_public_artifact(root, Path(tmp) / "public")

    def test_allows_shared_library_versions_that_look_like_ipv4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            self.make_repo(root)
            write(
                root / "results/vldb_final_evidence/profile.txt",
                "/usr/lib/x86_64-linux-gnu/libmlx5.so.1.19.35.0\n",
            )

            artifact.build_public_artifact(root, Path(tmp) / "public")

    def test_rejects_symlinks_inside_public_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            self.make_repo(root)
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside\n")
            (root / "graphbeyond/src/link.txt").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symbolic link"):
                artifact.build_public_artifact(root, Path(tmp) / "public")


if __name__ == "__main__":
    unittest.main()
