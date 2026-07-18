import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import seal_vldb_multicn_campaign as seal


DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
SYSTEMS = ("SHINE", "SlabWalk", "d-HNSW")
CN_COUNTS = (1, 2, 3)
TOOL_ROLES = (
    "assembler",
    "dhnsw_parser",
    "query_fingerprinter",
    "recorder",
    "runner",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_campaign(root: Path) -> Path:
    root.mkdir()
    harness = root / "harness"
    harness.mkdir()
    entries = {}
    tool_sha256 = {}
    for role in (*TOOL_ROLES, "sealer"):
        path = harness / f"{role}__tool.py"
        path.write_text(f"{role}\n")
        digest = sha256(path)
        entries[role] = {
            "path": path.name,
            "source_path": f"/frozen/{path.name}",
            "bytes": path.stat().st_size,
            "sha256": digest,
            "executable": False,
        }
        if role in TOOL_ROLES:
            tool_sha256[role] = digest
    (harness / "harness.json").write_text(
        json.dumps({"schema_version": 1, "entries": entries}, sort_keys=True)
        + "\n"
    )

    protocol = {
        "datasets": list(DATASETS),
        "systems": list(SYSTEMS),
        "cn_counts": list(CN_COUNTS),
        "repeats": 5,
        "tool_sha256": tool_sha256,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    campaign = {
        "kind": "vldb_multicn_campaign",
        "campaign_id": "formal-test",
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
        "tool_sha256": tool_sha256,
        "datasets": list(DATASETS),
        "systems": list(SYSTEMS),
        "cn_counts": list(CN_COUNTS),
        "repeats": 5,
    }
    campaign_path = root / "campaign.json"
    campaign_path.write_text(json.dumps(campaign, sort_keys=True) + "\n")

    rows = []
    sources = root / "sources"
    sources.mkdir()
    for dataset in DATASETS:
        for system in SYSTEMS:
            for cn_count in CN_COUNTS:
                for repeat in range(5):
                    source = sources / f"{dataset}_{system}_{cn_count}_{repeat}.json"
                    source.write_text(
                        json.dumps(
                            {
                                "kind": "vldb_multicn_raw_source",
                                "campaign_id": campaign["campaign_id"],
                                "protocol_fingerprint": fingerprint,
                                "dataset": dataset,
                                "system": system,
                                "cn_count": cn_count,
                                "repeat": repeat,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    rows.append(
                        {
                            "campaign_id": campaign["campaign_id"],
                            "protocol_fingerprint": fingerprint,
                            "dataset": dataset,
                            "system": system,
                            "cn_count": cn_count,
                            "repeat": repeat,
                            "source": source.relative_to(root).as_posix(),
                            "source_sha256": sha256(source),
                        }
                    )
    runs_path = root / "runs.csv"
    write_csv(runs_path, rows)

    summary = root / "summary"
    summary.mkdir()
    write_csv(summary / "runs.csv", rows)
    write_csv(
        summary / "summary.csv",
        [
            {
                "dataset": dataset,
                "system": system,
                "cn_count": cn_count,
                "n": 5,
            }
            for dataset in DATASETS
            for system in SYSTEMS
            for cn_count in CN_COUNTS
        ],
    )
    (summary / "gate.json").write_text(
        json.dumps(
            {
                "kind": "vldb_multicn_promotion_gate",
                "campaign_id": campaign["campaign_id"],
                "protocol_fingerprint": fingerprint,
                "promotion_ready": False,
                "promotion_failures": ["pre-registered scale gate"],
                "measured_rows": 135,
                "cells": 27,
                "source_files_verified": 135,
            },
            sort_keys=True,
        )
        + "\n"
    )
    (summary / "campaign.json").write_text(
        json.dumps(
            {
                **campaign,
                "input_manifest": str(campaign_path),
                "input_manifest_sha256": sha256(campaign_path),
                "input_runs": str(runs_path),
                "input_runs_sha256": sha256(runs_path),
            },
            sort_keys=True,
        )
        + "\n"
    )
    (root / "runner.log").write_text("formal campaign complete\n")
    return root


class MulticnCampaignSealTest(unittest.TestCase):
    def test_seals_complete_negative_campaign_without_rewriting_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_campaign(Path(temporary) / "campaign")
            campaign_before = (root / "campaign.json").read_bytes()

            record = seal.seal_campaign(root)
            verified = seal.verify_campaign(root)

            self.assertEqual((root / "campaign.json").read_bytes(), campaign_before)
            self.assertFalse(record["promotion_ready"])
            self.assertEqual(record["measured_rows"], 135)
            self.assertEqual(record["cells"], 27)
            self.assertEqual(record["source_files_verified"], 135)
            self.assertEqual(verified, record)

    def test_rejects_tampering_after_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_campaign(Path(temporary) / "campaign")
            seal.seal_campaign(root)
            (root / "runner.log").write_text("changed\n")

            with self.assertRaisesRegex(ValueError, "inventory SHA mismatch"):
                seal.verify_campaign(root)

    def test_rejects_incomplete_matrix_before_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_campaign(Path(temporary) / "campaign")
            with (root / "runs.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))[:-1]
            write_csv(root / "runs.csv", rows)

            with self.assertRaisesRegex(ValueError, "raw run matrix"):
                seal.seal_campaign(root)

    def test_rejects_harness_hash_drift_from_campaign_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_campaign(Path(temporary) / "campaign")
            harness_path = root / "harness" / "harness.json"
            harness = json.loads(harness_path.read_text())
            harness["entries"]["runner"]["sha256"] = "f" * 64
            harness_path.write_text(json.dumps(harness, sort_keys=True) + "\n")

            with self.assertRaisesRegex(ValueError, "harness.*SHA"):
                seal.seal_campaign(root)

    def test_refuses_a_symlink_or_existing_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_campaign(Path(temporary) / "campaign")
            (root / "unsafe").symlink_to(root / "runner.log")
            with self.assertRaisesRegex(ValueError, "symlink"):
                seal.seal_campaign(root)

            (root / "unsafe").unlink()
            seal.seal_campaign(root)
            with self.assertRaisesRegex(ValueError, "already sealed"):
                seal.seal_campaign(root)


if __name__ == "__main__":
    unittest.main()
