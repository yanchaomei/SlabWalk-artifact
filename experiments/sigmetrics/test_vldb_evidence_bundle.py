import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import vldb_evidence_bundle as evidence


def write_manifest(root: Path) -> None:
    output = root / "SHA256SUMS"
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS", "SEALED.json"}
    )
    output.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(root).as_posix()}\n"
            for path in files
        )
    )


class VldbEvidenceBundleTest(unittest.TestCase):
    def test_snapshot_harness_is_content_addressed_and_detects_mutation(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            runner = root / "runner.sh"
            summarizer = root / "summary.py"
            runner.write_text("#!/bin/sh\nexit 0\n")
            summarizer.write_text("print('ok')\n")

            manifest = evidence.snapshot_harness(
                root / "out" / "harness",
                {"runner": runner, "summarizer": summarizer},
            )

            self.assertEqual(set(manifest["entries"]), {"runner", "summarizer"})
            evidence.verify_harness(root / "out" / "harness" / "harness.json")
            frozen = root / "out" / "harness" / manifest["entries"]["runner"]["path"]
            frozen.chmod(0o755)
            frozen.write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "harness SHA"):
                evidence.verify_harness(root / "out" / "harness" / "harness.json")

    def test_seal_rejects_stale_nested_manifest(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            child = root / "raw" / "child"
            child.mkdir(parents=True)
            artifact = child / "result.json"
            artifact.write_text("{}\n")
            write_manifest(child)
            artifact.write_text('{"tampered":true}\n')
            campaign = root / "campaign.json"
            campaign.write_text(
                json.dumps(
                    {
                        "campaign_id": "test-campaign",
                        "campaign_uuid": "00000000-0000-0000-0000-000000000001",
                        "protocol_fingerprint": "a" * 64,
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "nested SHA256SUMS"):
                evidence.seal_bundle(root, campaign)

    def test_sealed_bundle_binds_campaign_and_complete_tree(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            campaign = root / "campaign.json"
            campaign.write_text(
                json.dumps(
                    {
                        "campaign_id": "test-campaign",
                        "campaign_uuid": "00000000-0000-0000-0000-000000000001",
                        "protocol_fingerprint": "b" * 64,
                    }
                )
            )
            (root / "runs.csv").write_text("status\nok\n")

            seal = evidence.seal_bundle(root, campaign)

            self.assertEqual(seal["protocol_fingerprint"], "b" * 64)
            self.assertEqual(seal["campaign_id"], "test-campaign")
            evidence.verify_bundle(root)
            (root / "unsealed.txt").write_text("late file\n")
            with self.assertRaisesRegex(ValueError, "complete bundle tree"):
                evidence.verify_bundle(root)

    def test_manifest_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            (root / "SHA256SUMS").write_text(f"{'0' * 64}  ../outside\n")

            with self.assertRaisesRegex(ValueError, "escapes"):
                evidence.verify_manifest(root / "SHA256SUMS")


if __name__ == "__main__":
    unittest.main()
