import json
import tempfile
import unittest
from pathlib import Path

import record_partial_campaign as recorder


class PartialCampaignRecordTest(unittest.TestCase):
    def test_records_admitted_and_excluded_cells_with_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(json.dumps({"campaign_id": "c1"}))
            (root / "DEEP1M_r0.json").write_text("measured")
            (root / "GIST1M_r0.err").write_text("failed")
            record = recorder.record_partition(
                root,
                admitted_cells=["DEEP1M"],
                admitted_rows=5,
                excluded_cells=["GIST1M"],
                failed_stage="pre-build input validation",
                reason="missing query directory",
            )
            self.assertEqual(record["status"], "partial_success")
            self.assertEqual(record["measured_rows_admitted"], 5)
            self.assertEqual(len(record["files"]), 3)
            self.assertTrue((root / "campaign_partition.json").is_file())

    def test_rejects_overlapping_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(json.dumps({"campaign_id": "c1"}))
            with self.assertRaisesRegex(ValueError, "disjoint"):
                recorder.record_partition(
                    root,
                    admitted_cells=["DEEP1M"],
                    admitted_rows=5,
                    excluded_cells=["DEEP1M"],
                    failed_stage="input",
                    reason="bad input",
                )


if __name__ == "__main__":
    unittest.main()
