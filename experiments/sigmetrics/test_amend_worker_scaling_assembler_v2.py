import json
import tempfile
import unittest
from pathlib import Path

import amend_worker_scaling_assembler_v2 as amendment
import amend_worker_scaling_parser as common


class WorkerAssemblerV2AmendmentTest(unittest.TestCase):
    def test_chains_second_assembler_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol = {
                "assembler_sha256": "0" * 64,
                "dhnsw_parser_sha256": "1" * 64,
            }
            original = common.json_bytes(
                {
                    "campaign_id": "worker-test",
                    "protocol": protocol,
                    "protocol_fingerprint": common.protocol_fingerprint(protocol),
                    "assembler_amendment": "assembler_amendment.json",
                    "dhnsw_runner_amendment": "dhnsw_runner_amendment.json",
                }
            )
            (root / "campaign.json").write_bytes(original)
            tool = root / "assembler.py"
            tool.write_text("print('campaign-aware assembler')\n")

            amendment.amend(
                root,
                tool,
                expected_old_assembler_sha="0" * 64,
            )

            manifest = json.loads((root / "campaign.json").read_text())
            self.assertEqual(
                manifest["protocol"]["assembler_sha256"],
                common.file_sha256(tool),
            )
            self.assertEqual(
                manifest["assembler_amendment"], "assembler_amendment.json"
            )
            self.assertEqual(
                manifest["assembler_amendment_v2"],
                "assembler_amendment_v2.json",
            )
            self.assertEqual(
                (root / "campaign.before-assembler-amendment-v2.json").read_bytes(),
                original,
            )

    def test_refuses_unexpected_old_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol = {"assembler_sha256": "0" * 64}
            original = common.json_bytes(
                {
                    "campaign_id": "worker-test",
                    "protocol": protocol,
                    "protocol_fingerprint": common.protocol_fingerprint(protocol),
                }
            )
            (root / "campaign.json").write_bytes(original)
            tool = root / "assembler.py"
            tool.write_text("print('new')\n")
            with self.assertRaisesRegex(ValueError, "unexpected old assembler SHA"):
                amendment.amend(
                    root,
                    tool,
                    expected_old_assembler_sha="f" * 64,
                )
            self.assertEqual((root / "campaign.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
