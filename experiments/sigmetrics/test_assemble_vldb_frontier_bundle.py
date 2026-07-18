#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import assemble_vldb_frontier_bundle as assembler


class AssembleVldbFrontierBundleTest(unittest.TestCase):
    def make_campaigns(self, root: Path) -> tuple[Path, Path, Path]:
        deep = root / "deep"
        sw = root / "sw"
        dh = root / "dh"
        for rep in range(5):
            deep_sw = deep / "raw_sources" / "sw" / f"r{rep + 1}.csv"
            deep_dh = deep / "raw_sources" / "dhnsw" / f"r{rep}.csv"
            text_sw = sw / f"sw_r{rep + 1}" / "slabwalk_shine_frontier_raw.csv"
            text_dh = dh / f"r{rep}" / "frontier.csv"
            for path in (deep_sw, deep_dh, text_sw, text_dh):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"source,{rep}\n")
        (sw / "sw_warmup" / "slabwalk_shine_frontier_raw.csv").parent.mkdir(
            parents=True, exist_ok=True
        )
        (sw / "sw_warmup" / "slabwalk_shine_frontier_raw.csv").write_text(
            "warmup\n"
        )
        (dh / "warmup").mkdir(parents=True)
        (dh / "warmup" / "frontier.csv").write_text("warmup\n")
        return deep, sw, dh

    def make_source_campaign(
        self, root: Path, name: str, campaign_id: str, fingerprint: str
    ) -> Path:
        campaign = root / name
        campaign.mkdir()
        (campaign / "campaign.json").write_text(
            json.dumps(
                {
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                }
            )
            + "\n"
        )
        return campaign

    def test_discovers_exactly_five_measured_sources_per_campaign(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            deep, sw, dh = self.make_campaigns(Path(tmp_s))
            sources = assembler.discover_sources(deep, sw, dh, expected_repeats=5)
            self.assertEqual(len(sources["deep_sw"]), 5)
            self.assertEqual(len(sources["deep_dhnsw"]), 5)
            self.assertEqual(len(sources["text_sift_sw"]), 5)
            self.assertEqual(len(sources["text_sift_dhnsw"]), 5)
            self.assertFalse(any("warmup" in str(path) for paths in sources.values() for path in paths))

    def test_missing_repeat_fails_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            deep, sw, dh = self.make_campaigns(root)
            (dh / "r4" / "frontier.csv").unlink()
            with self.assertRaisesRegex(ValueError, "expected 5"):
                assembler.discover_sources(deep, sw, dh, expected_repeats=5)

    def test_copy_retains_deterministic_relative_sources_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            deep, sw, dh = self.make_campaigns(root)
            sources = assembler.discover_sources(deep, sw, dh, expected_repeats=5)
            staging = root / "staging"
            copied, records = assembler.copy_sources(sources, staging)
            self.assertEqual(len(copied["sw"]), 10)
            self.assertEqual(len(copied["dhnsw"]), 10)
            self.assertEqual(len(records), 20)
            self.assertTrue(
                (staging / "raw_sources" / "deep10m" / "sw" / "r1.csv").is_file()
            )
            self.assertTrue(
                (staging / "raw_sources" / "text_sift" / "dhnsw" / "r4.csv").is_file()
            )
            self.assertTrue(all(len(record["sha256"]) == 64 for record in records))

    def test_writes_dataset_scoped_four_campaign_manifest(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            campaigns = (
                self.make_source_campaign(root, "deep-sw", "deep-sw-id", "a" * 64),
                self.make_source_campaign(root, "deep-dh", "deep-dh-id", "b" * 64),
                self.make_source_campaign(root, "text-sw", "text-sw-id", "c" * 64),
                self.make_source_campaign(root, "text-dh", "text-dh-id", "d" * 64),
            )
            staging = root / "staging"
            staging.mkdir()
            records = assembler.write_campaign_evidence(*campaigns, staging)

            manifest = json.loads((staging / "campaign.json").read_text())
            self.assertEqual(manifest["kind"], "composite_frontier_evidence")
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(
                manifest["cell_sources"],
                {
                    "DEEP10M/SHINE": "deep10m_shine_slabwalk",
                    "DEEP10M/SlabWalk": "deep10m_shine_slabwalk",
                    "DEEP10M/d-HNSW": "deep10m_dhnsw",
                    "SIFT10M/SHINE": "text_sift_shine_slabwalk",
                    "SIFT10M/SlabWalk": "text_sift_shine_slabwalk",
                    "SIFT10M/d-HNSW": "text_sift_dhnsw",
                    "TTI10M/SHINE": "text_sift_shine_slabwalk",
                    "TTI10M/SlabWalk": "text_sift_shine_slabwalk",
                    "TTI10M/d-HNSW": "text_sift_dhnsw",
                },
            )
            self.assertEqual(len(manifest["source_campaigns"]), 4)
            self.assertEqual(len(records), 4)
            self.assertTrue(
                all((staging / record["retained"]).is_file() for record in records)
            )


if __name__ == "__main__":
    unittest.main()
