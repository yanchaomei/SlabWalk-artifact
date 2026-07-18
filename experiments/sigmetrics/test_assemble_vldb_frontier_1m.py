#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_frontier_1m as assembler


class AssembleVldbFrontier1MTest(unittest.TestCase):
    def make_campaign(self, root: Path) -> Path:
        campaign = root / "campaign"
        campaign.mkdir()
        (campaign / "campaign.json").write_text("{}\n")
        for repeat in range(1, 6):
            sw = campaign / f"sw_r{repeat}" / "slabwalk_shine_frontier_raw.csv"
            dhnsw = campaign / f"dhnsw_r{repeat}" / "frontier.csv"
            for path in (sw, dhnsw):
                path.parent.mkdir(parents=True)
                path.write_text(f"run,{repeat}\n")
        for path in (
            campaign / "sw_warmup" / "slabwalk_shine_frontier_raw.csv",
            campaign / "dhnsw_warmup" / "frontier.csv",
        ):
            path.parent.mkdir(parents=True)
            path.write_text("warmup\n")
        return campaign

    def make_split_campaigns(self, root: Path) -> tuple[Path, Path]:
        sw_campaign = root / "sw_campaign"
        dhnsw_campaign = root / "dhnsw_campaign"
        sw_campaign.mkdir()
        dhnsw_campaign.mkdir()
        (sw_campaign / "campaign.json").write_text(
            json.dumps({"campaign_id": "v5-sw", "protocol_fingerprint": "a" * 64})
            + "\n"
        )
        (dhnsw_campaign / "campaign.json").write_text(
            json.dumps({"campaign_id": "frozen-dhnsw", "protocol_fingerprint": "b" * 64})
            + "\n"
        )
        for repeat in range(1, 6):
            sw = sw_campaign / f"sw_r{repeat}" / "slabwalk_shine_frontier_raw.csv"
            dhnsw = dhnsw_campaign / f"dhnsw_r{repeat}" / "frontier.csv"
            sw.parent.mkdir(parents=True)
            dhnsw.parent.mkdir(parents=True)
            sw.write_text(f"run,{repeat}\n")
            dhnsw.write_text(f"run,{repeat}\n")
        return sw_campaign, dhnsw_campaign

    def make_query_pools(self, root: Path) -> Path:
        query_pools = root / "query_pools"
        query_pools.mkdir()
        for dataset in assembler.DATASETS:
            for method in assembler.METHODS:
                slug = method.lower().replace("-", "").replace(" ", "")
                (query_pools / f"{dataset.lower()}_{slug}.json").write_text(
                    json.dumps(
                        {
                            "kind": "query_pool_fingerprint",
                            "dataset": dataset,
                            "method": method,
                        }
                    )
                )
        return query_pools

    def make_promoted_dhnsw_bundle(self, root: Path) -> Path:
        bundle = root / "promoted_dhnsw"
        bundle.mkdir()
        campaign = bundle / "campaign.json"
        campaign.write_text(
            json.dumps(
                {
                    "campaign_id": "frozen-dhnsw",
                    "protocol_fingerprint": "b" * 64,
                }
            )
            + "\n"
        )
        records = []
        for repeat in range(1, 6):
            relative = Path("raw_sources") / "dhnsw" / f"r{repeat}.csv"
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"run,{repeat}\n")
            records.append(
                {
                    "kind": "dhnsw",
                    "run_id": f"r{repeat}",
                    "retained": relative.as_posix(),
                    "sha256": assembler.file_sha256(path),
                }
            )
        (bundle / "PROVENANCE.json").write_text(
            json.dumps(
                {
                    "expected_repeats": 5,
                    "campaign_manifest_sha256": assembler.file_sha256(campaign),
                    "retained_sources": records,
                }
            )
            + "\n"
        )
        assembler.write_sha256s(bundle)
        return bundle

    def test_discovers_five_measured_runs_and_ignores_warmup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            campaign = self.make_campaign(Path(tmp_s))
            sources = assembler.discover_sources(campaign)
            self.assertEqual(len(sources["sw"]), 5)
            self.assertEqual(len(sources["dhnsw"]), 5)
            self.assertFalse(
                any("warmup" in str(path) for paths in sources.values() for path in paths)
            )

    def test_parses_both_run_directory_prefixes_without_python39_helpers(self) -> None:
        self.assertEqual(
            assembler.measured_run_number(
                Path("campaign/sw_r3/slabwalk_shine_frontier_raw.csv")
            ),
            3,
        )
        self.assertEqual(
            assembler.measured_run_number(Path("campaign/dhnsw_r5/frontier.csv")),
            5,
        )
        self.assertNotIn(".removeprefix(", Path(assembler.__file__).read_text())

    def test_rejects_missing_measured_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            campaign = self.make_campaign(Path(tmp_s))
            (campaign / "dhnsw_r4" / "frontier.csv").unlink()
            with self.assertRaisesRegex(ValueError, "expected 5"):
                assembler.discover_sources(campaign)

    def test_requires_the_complete_seven_by_three_query_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            query_pools = self.make_query_pools(Path(tmp_s))
            paths = assembler.discover_query_pools(query_pools)
            self.assertEqual(len(paths), 21)
            next(iter(paths.values())).unlink()
            with self.assertRaisesRegex(ValueError, "query-pool matrix mismatch"):
                assembler.discover_query_pools(query_pools)

    def test_copies_portable_sources_with_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            campaign = self.make_campaign(root)
            staging = root / "staging"
            copied, records = assembler.copy_sources(
                assembler.discover_sources(campaign), staging
            )
            self.assertEqual(len(copied["sw"]), 5)
            self.assertEqual(len(copied["dhnsw"]), 5)
            self.assertEqual(len(records), 10)
            self.assertTrue((staging / "raw_sources" / "sw" / "r1.csv").is_file())
            self.assertTrue(
                (staging / "raw_sources" / "dhnsw" / "r5.csv").is_file()
            )
            self.assertTrue(all(len(record["sha256"]) == 64 for record in records))

    def test_discovers_split_system_campaigns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            sw_campaign, dhnsw_campaign = self.make_split_campaigns(Path(tmp_s))
            sources = assembler.discover_split_sources(sw_campaign, dhnsw_campaign)
            self.assertEqual(len(sources["sw"]), 5)
            self.assertEqual(len(sources["dhnsw"]), 5)
            self.assertTrue(
                all(str(path).startswith(str(sw_campaign)) for path in sources["sw"])
            )
            self.assertTrue(
                all(
                    str(path).startswith(str(dhnsw_campaign))
                    for path in sources["dhnsw"]
                )
            )

    def test_discovers_dhnsw_from_promoted_retained_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            sw_campaign, _ = self.make_split_campaigns(root)
            dhnsw_bundle = self.make_promoted_dhnsw_bundle(root)

            sources = assembler.discover_split_sources(sw_campaign, dhnsw_bundle)

            self.assertEqual(
                [path.name for path in sources["dhnsw"]],
                [f"r{repeat}.csv" for repeat in range(1, 6)],
            )

    def test_rejects_tampered_promoted_dhnsw_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            sw_campaign, _ = self.make_split_campaigns(root)
            dhnsw_bundle = self.make_promoted_dhnsw_bundle(root)
            (dhnsw_bundle / "raw_sources" / "dhnsw" / "r3.csv").write_text(
                "tampered\n"
            )

            with self.assertRaisesRegex(ValueError, "integrity|SHA|drift"):
                assembler.discover_split_sources(sw_campaign, dhnsw_bundle)

    def test_composite_manifest_preserves_both_source_campaigns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            sw_campaign, dhnsw_campaign = self.make_split_campaigns(root)
            staging = root / "staging"
            staging.mkdir()
            records = assembler.write_campaign_evidence(
                sw_campaign, dhnsw_campaign, staging
            )

            manifest = json.loads((staging / "campaign.json").read_text())
            self.assertEqual(manifest["kind"], "composite_frontier_evidence")
            self.assertEqual(
                manifest["method_sources"],
                {
                    "SHINE": "shine_slabwalk",
                    "SlabWalk": "shine_slabwalk",
                    "d-HNSW": "dhnsw",
                },
            )
            self.assertEqual(len(records), 2)
            self.assertEqual(
                {record["campaign_id"] for record in records},
                {"v5-sw", "frozen-dhnsw"},
            )
            for record in records:
                retained = staging / record["retained"]
                self.assertTrue(retained.is_file())
                self.assertEqual(assembler.file_sha256(retained), record["sha256"])


if __name__ == "__main__":
    unittest.main()
