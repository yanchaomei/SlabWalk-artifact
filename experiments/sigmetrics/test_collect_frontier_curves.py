import csv
import json
import tempfile
import unittest
from pathlib import Path

import collect_frontier_curves as collect


class FrontierSourceValidityTest(unittest.TestCase):
    def test_rejects_campaign_marked_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "r0"
            run.mkdir()
            (root / "campaign_validity.json").write_text(
                json.dumps({"status": "invalid", "reason": "wrong GT corpus"})
            )
            source = run / "frontier.csv"
            with source.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["dataset", "ef", "recall", "qps_recomputed", "status"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "dataset": "deep10M",
                        "ef": "48",
                        "recall": "0",
                        "qps_recomputed": "444",
                        "status": "ok",
                    }
                )

            with self.assertRaisesRegex(ValueError, "wrong GT corpus"):
                collect.add_dhnsw_rows([], source)


if __name__ == "__main__":
    unittest.main()
