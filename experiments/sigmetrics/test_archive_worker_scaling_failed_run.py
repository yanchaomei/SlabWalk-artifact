import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import archive_worker_scaling_failed_run as archive


class FailedRunArchiveTest(unittest.TestCase):
    def test_archives_incomplete_run_with_content_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(
                json.dumps({"campaign_id": "worker-test"}) + "\n"
            )
            run = root / "raw" / "dhnsw" / "w40" / "r0"
            run.mkdir(parents=True)
            payload = b"interleaved client log\n"
            (run / "deep1M_ef200_client.log").write_bytes(payload)
            (run / "frontier.csv").write_text("status\nincomplete\n")

            archive.archive_failed_run(
                root,
                Path("raw/dhnsw/w40/r0"),
                archive_name="before-runner-fix",
                reason="stdout interleaving left no complete aggregate detail record",
            )

            destination = (
                root / "failed_runs" / "dhnsw" / "w40" / "r0-before-runner-fix"
            )
            self.assertFalse(run.exists())
            self.assertEqual(
                (destination / "deep1M_ef200_client.log").read_bytes(), payload
            )
            record = json.loads(
                (root / "failed_run_archive_w40_r0_before-runner-fix.json").read_text()
            )
            files = {item["path"]: item["sha256"] for item in record["files"]}
            self.assertEqual(
                files["deep1M_ef200_client.log"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(record["campaign_id"], "worker-test")

    def test_refuses_to_archive_successful_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "campaign.json").write_text(
                json.dumps({"campaign_id": "worker-test"}) + "\n"
            )
            run = root / "raw" / "dhnsw" / "w40" / "r0"
            run.mkdir(parents=True)
            (run / "frontier.csv").write_text("status\nok\n")
            with self.assertRaisesRegex(ValueError, "successful run"):
                archive.archive_failed_run(
                    root,
                    Path("raw/dhnsw/w40/r0"),
                    archive_name="before-runner-fix",
                    reason="should not archive",
                )
            self.assertTrue(run.is_dir())


if __name__ == "__main__":
    unittest.main()
