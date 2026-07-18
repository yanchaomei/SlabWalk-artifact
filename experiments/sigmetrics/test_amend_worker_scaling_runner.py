import json
import tempfile
import unittest
from pathlib import Path

import amend_worker_scaling_parser as common
import amend_worker_scaling_runner as amendment


class WorkerRunnerAmendmentTest(unittest.TestCase):
    def test_records_runner_amendment_without_losing_prior_amendments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol = {
                "dhnsw_runner_sha256": "0" * 64,
                "assembler_sha256": "1" * 64,
            }
            original = common.json_bytes(
                {
                    "campaign_id": "worker-test",
                    "protocol": protocol,
                    "protocol_fingerprint": common.protocol_fingerprint(protocol),
                    "assembler_amendment": "assembler_amendment.json",
                }
            )
            (root / "campaign.json").write_bytes(original)
            runner = root / "runner.sh"
            runner.write_text("#!/bin/sh\necho corrected\n")

            amendment.amend(
                root,
                runner,
                expected_old_runner_sha="0" * 64,
            )

            manifest = json.loads((root / "campaign.json").read_text())
            self.assertEqual(
                manifest["protocol"]["dhnsw_runner_sha256"],
                common.file_sha256(runner),
            )
            self.assertEqual(
                manifest["assembler_amendment"], "assembler_amendment.json"
            )
            self.assertEqual(
                manifest["dhnsw_runner_amendment"],
                "dhnsw_runner_amendment.json",
            )
            self.assertEqual(
                (root / "campaign.before-dhnsw-runner-amendment.json").read_bytes(),
                original,
            )

    def test_refuses_repeated_runner_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol = {"dhnsw_runner_sha256": "0" * 64}
            (root / "campaign.json").write_bytes(
                common.json_bytes(
                    {
                        "campaign_id": "worker-test",
                        "protocol": protocol,
                        "protocol_fingerprint": common.protocol_fingerprint(protocol),
                    }
                )
            )
            (root / "dhnsw_runner_amendment.json").write_text("{}\n")
            runner = root / "runner.sh"
            runner.write_text("#!/bin/sh\nexit 0\n")
            with self.assertRaisesRegex(ValueError, "already been applied"):
                amendment.amend(
                    root,
                    runner,
                    expected_old_runner_sha="0" * 64,
                )


if __name__ == "__main__":
    unittest.main()
