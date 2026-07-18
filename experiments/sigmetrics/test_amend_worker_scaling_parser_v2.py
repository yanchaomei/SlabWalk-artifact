import json
import tempfile
import unittest
from pathlib import Path

import amend_worker_scaling_parser as common
import amend_worker_scaling_parser_v2 as amendment
import parse_dhnsw_frontier as parser
from test_amend_worker_scaling_parser import write_frontier, write_run


class ParserAmendmentV2Test(unittest.TestCase):
    def test_revalidates_existing_rows_and_chains_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign_id = "worker-test"
            binary = "1" * 64
            protocol = {
                "dhnsw_parser_sha256": "0" * 64,
                "dhnsw_client_binary_sha256": binary,
                "workers": [1, 2],
                "ef": 200,
            }
            original = common.json_bytes(
                {
                    "campaign_id": campaign_id,
                    "protocol": protocol,
                    "protocol_fingerprint": common.protocol_fingerprint(protocol),
                    "parser_amendment": "parser_amendment.json",
                }
            )
            (root / "campaign.json").write_bytes(original)
            for workers in (1, 2):
                run = write_run(root, workers, "r0", interleaved=False)
                write_frontier(run, workers, campaign_id, binary)

            amendment.amend(
                root,
                Path(parser.__file__),
                expected_old_parser_sha="0" * 64,
            )

            manifest = json.loads((root / "campaign.json").read_text())
            self.assertEqual(
                manifest["protocol"]["dhnsw_parser_sha256"],
                common.file_sha256(Path(parser.__file__)),
            )
            self.assertEqual(manifest["parser_amendment"], "parser_amendment.json")
            self.assertEqual(
                manifest["parser_amendment_v2"], "parser_amendment_v2.json"
            )
            record = json.loads((root / "parser_amendment_v2.json").read_text())
            self.assertEqual(len(record["revalidated_runs"]), 2)
            self.assertTrue(all(item["row_unchanged"] for item in record["revalidated_runs"]))
            self.assertEqual(
                (root / "campaign.before-parser-amendment-v2.json").read_bytes(),
                original,
            )

    def test_refuses_any_reparsed_measurement_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            campaign_id = "worker-test"
            binary = "1" * 64
            protocol = {
                "dhnsw_parser_sha256": "0" * 64,
                "dhnsw_client_binary_sha256": binary,
                "workers": [1],
            }
            (root / "campaign.json").write_bytes(
                common.json_bytes(
                    {
                        "campaign_id": campaign_id,
                        "protocol": protocol,
                        "protocol_fingerprint": common.protocol_fingerprint(protocol),
                    }
                )
            )
            run = write_run(root, 1, "r0", interleaved=False)
            write_frontier(run, 1, campaign_id, binary)
            frontier = run / "frontier.csv"
            text = frontier.read_text().replace("0.900000", "0.800000", 1)
            frontier.write_text(text)
            original_manifest = (root / "campaign.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "measurement drift"):
                amendment.amend(
                    root,
                    Path(parser.__file__),
                    expected_old_parser_sha="0" * 64,
                )
            self.assertEqual((root / "campaign.json").read_bytes(), original_manifest)
            self.assertFalse((root / "parser_amendment_v2.json").exists())


if __name__ == "__main__":
    unittest.main()
