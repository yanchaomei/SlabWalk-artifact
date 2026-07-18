import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import record_excluded_campaign as recorder


class RecordExcludedCampaignTest(unittest.TestCase):
    def test_records_closed_inventory_without_self_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text('{"campaign_id":"failed-v1"}\n')
            (root / "raw").mkdir()
            (root / "raw/client.log").write_text("missing query path\n")

            record = recorder.record_exclusion(
                root,
                status="excluded_protocol_failure",
                stage="warmup",
                reason="client launched from the wrong working directory",
            )

            saved = json.loads((root / "campaign_failure.json").read_text())
            self.assertEqual(saved, record)
            self.assertEqual(saved["campaign_id"], "failed-v1")
            self.assertEqual(saved["status"], "excluded_protocol_failure")
            self.assertEqual(saved["measured_rows_admitted"], 0)
            self.assertEqual(
                [item["path"] for item in saved["files"]],
                ["campaign.json", "raw/client.log"],
            )
            self.assertEqual(
                saved["files"][1]["sha256"],
                hashlib.sha256(b"missing query path\n").hexdigest(),
            )

    def test_refuses_to_overwrite_an_exclusion_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text('{"campaign_id":"failed-v1"}\n')
            recorder.record_exclusion(root, status="excluded", stage="build", reason="x")
            with self.assertRaisesRegex(ValueError, "already exists"):
                recorder.record_exclusion(
                    root, status="excluded", stage="build", reason="x"
                )

    def test_records_a_premeasurement_failure_with_explicit_campaign_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "configure.log").write_text("dependency mismatch\n")
            saved = recorder.record_exclusion(
                root,
                campaign_id_override="premeasurement-v2",
                status="excluded_build_failure",
                stage="dependency_probe",
                reason="generated sources and installed protobuf headers differ",
            )
            self.assertEqual(saved["campaign_id"], "premeasurement-v2")
            self.assertEqual(
                [item["path"] for item in saved["files"]], ["configure.log"]
            )


if __name__ == "__main__":
    unittest.main()
