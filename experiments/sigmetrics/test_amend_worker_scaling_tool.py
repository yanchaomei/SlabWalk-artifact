import json
import tempfile
import unittest
from pathlib import Path

import amend_worker_scaling_parser as parser_amendment
import amend_worker_scaling_tool as tool_amendment


class WorkerToolAmendmentTest(unittest.TestCase):
    def write_campaign(self, root: Path) -> bytes:
        protocol = {
            "assembler_sha256": "0" * 64,
            "dhnsw_parser_sha256": "1" * 64,
        }
        content = parser_amendment.json_bytes(
            {
                "campaign_id": "worker-test",
                "protocol": protocol,
                "protocol_fingerprint": parser_amendment.protocol_fingerprint(
                    protocol
                ),
            }
        )
        (root / "campaign.json").write_bytes(content)
        return content

    def test_records_assembler_only_amendment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.write_campaign(root)
            tool = root / "assembler.py"
            tool.write_text("print('new assembler')\n")
            tool_amendment.amend(
                root,
                tool,
                expected_old_tool_sha="0" * 64,
            )
            manifest = json.loads((root / "campaign.json").read_text())
            self.assertEqual(
                manifest["protocol"]["assembler_sha256"],
                parser_amendment.file_sha256(tool),
            )
            self.assertEqual(
                (root / "campaign.before-assembler-amendment.json").read_bytes(),
                original,
            )
            record = json.loads((root / "assembler_amendment.json").read_text())
            self.assertEqual(record["campaign_id"], "worker-test")
            self.assertEqual(record["old_tool_sha256"], "0" * 64)

    def test_refuses_unexpected_old_hash_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.write_campaign(root)
            tool = root / "assembler.py"
            tool.write_text("print('new assembler')\n")
            with self.assertRaisesRegex(ValueError, "unexpected old tool SHA"):
                tool_amendment.amend(
                    root,
                    tool,
                    expected_old_tool_sha="f" * 64,
                )
            self.assertEqual((root / "campaign.json").read_bytes(), original)
            self.assertFalse((root / "assembler_amendment.json").exists())


if __name__ == "__main__":
    unittest.main()
