from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_multicn_scaling as multicn


class MultiCnAssemblerTest(unittest.TestCase):
    DATASETS = ("SIFT1M", "DEEP1M", "GIST1M")
    SYSTEMS = ("SHINE", "SlabWalk", "d-HNSW")
    TOOL_HASHES = {
        "assembler": "1" * 64,
        "dhnsw_parser": "2" * 64,
        "query_fingerprinter": "3" * 64,
        "recorder": "4" * 64,
        "runner": "5" * 64,
    }

    @staticmethod
    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def read_rows(path: Path) -> list[dict[str, str]]:
        with path.open() as handle:
            return list(csv.DictReader(handle))

    def make_fixture(self, root: Path, *, weak_scaling: bool = False) -> tuple[Path, Path]:
        manifest = root / "campaign.json"
        expected_queries = {
            "SIFT1M": 10000,
            "DEEP1M": 10000,
            "GIST1M": 1000,
        }
        protocol = {
            "datasets": list(self.DATASETS),
            "systems": list(self.SYSTEMS),
            "cn_counts": [1, 2, 3],
            "repeats": 5,
            "tool_sha256": self.TOOL_HASHES,
        }
        fingerprint = hashlib.sha256(
            json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest.write_text(
            json.dumps(
                {
                    "kind": "vldb_multicn_campaign",
                    "campaign_id": "multicn-fixture",
                    "protocol_fingerprint": fingerprint,
                    "datasets": list(self.DATASETS),
                    "systems": list(self.SYSTEMS),
                    "cn_counts": [1, 2, 3],
                    "repeats": 5,
                    "expected_queries": expected_queries,
                    "slabwalk_binary_sha256": "a" * 64,
                    "dhnsw_binary_sha256": "b" * 64,
                    "dhnsw_runtime_manifest_sha256": "d" * 64,
                    "tool_sha256": self.TOOL_HASHES,
                    "protocol": protocol,
                },
                sort_keys=True,
            )
            + "\n"
        )
        rows = []
        for dataset in self.DATASETS:
            query_hash = hashlib.sha256(dataset.encode()).hexdigest()
            expected_queries = 1000 if dataset == "GIST1M" else 10000
            for system in self.SYSTEMS:
                for cn_count in (1, 2, 3):
                    for repeat in range(5):
                        base_qps = {
                            "SHINE": 1000.0,
                            "SlabWalk": 9000.0,
                            "d-HNSW": 4000.0,
                        }[system]
                        scale = {1: 1.0, 2: 1.9, 3: 2.75}[cn_count]
                        if weak_scaling and system == "SlabWalk" and cn_count == 3:
                            scale = 2.0
                        has_global_latency = system != "d-HNSW" and cn_count == 1
                        metrics = {
                            "processed_queries": expected_queries,
                            "expected_queries": expected_queries,
                            "failed_queries": 0,
                            "qps": base_qps * scale * (1.0 + 0.002 * repeat),
                            "recall": 0.91 if system == "d-HNSW" else 0.97,
                            "p50_us": 100.0 / scale if has_global_latency else None,
                            "p99_us": 180.0 / scale if has_global_latency else None,
                            "posts_per_query": 120.0 if system == "SlabWalk" else (1800.0 if system == "SHINE" else None),
                            "bytes_per_query": 60000.0 if system == "SlabWalk" else (700000.0 if system == "SHINE" else None),
                            "fairness": 0.99,
                        }
                        source = root / f"{dataset}_{system}_{cn_count}_{repeat}.json"
                        source.write_text(
                            json.dumps(
                                {
                                    "kind": "vldb_multicn_raw_source",
                                    "campaign_id": "multicn-fixture",
                                    "protocol_fingerprint": fingerprint,
                                    "dataset": dataset,
                                    "system": system,
                                    "cn_count": cn_count,
                                    "repeat": repeat,
                                    "binary_sha256": "b" * 64 if system == "d-HNSW" else "a" * 64,
                                    "query_canonical_sha256": query_hash,
                                    "groundtruth_canonical_sha256": "c" * 64,
                                    "metrics": metrics,
                                    "latency_scope": (
                                        "all_queries_single_cn"
                                        if has_global_latency
                                        else (
                                            "not_reported_cross_cn_frozen_binary_boundary"
                                            if system != "d-HNSW"
                                            else "not_reported_by_endpoint"
                                        )
                                    ),
                                },
                                sort_keys=True,
                            )
                            + "\n"
                        )
                        rows.append(
                            {
                                "campaign_id": "multicn-fixture",
                                "protocol_fingerprint": fingerprint,
                                "dataset": dataset,
                                "system": system,
                                "cn_count": cn_count,
                                "repeat": repeat,
                                "binary_sha256": "b" * 64 if system == "d-HNSW" else "a" * 64,
                                "query_canonical_sha256": query_hash,
                                "groundtruth_canonical_sha256": "c" * 64,
                                **{
                                    key: "" if value is None else value
                                    for key, value in metrics.items()
                                },
                                "source": source.name,
                                "source_sha256": self.sha256(source),
                            }
                        )
        raw = root / "runs.csv"
        with raw.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return manifest, raw

    def test_complete_campaign_is_promotion_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            out = root / "summary"
            report = multicn.assemble(manifest, raw, out)
            self.assertTrue(report["promotion_ready"])
            self.assertEqual(report["measured_rows"], 135)
            self.assertEqual(report["cells"], 27)
            self.assertTrue((out / "summary.csv").is_file())
            gate = json.loads((out / "gate.json").read_text())
            self.assertTrue(gate["promotion_ready"])
            self.assertEqual(gate["source_files_verified"], 135)

    def test_complete_but_weak_scaling_is_retained_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root, weak_scaling=True)
            out = root / "summary"
            report = multicn.assemble(manifest, raw, out)
            self.assertFalse(report["promotion_ready"])
            gate = json.loads((out / "gate.json").read_text())
            self.assertIn("SlabWalk", " ".join(gate["promotion_failures"]))

    def test_rejects_missing_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            rows = self.read_rows(raw)[:-1]
            with raw.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "matrix|repeat"):
                multicn.assemble(manifest, raw, root / "summary")

    def test_rejects_tampered_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            first = next(root.glob("SIFT1M_SHINE_1_0.json"))
            first.write_text("tampered\n")
            with self.assertRaisesRegex(ValueError, "source SHA"):
                multicn.assemble(manifest, raw, root / "summary")

    def test_rejects_manifest_without_dhnsw_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self.make_fixture(root)
            payload = json.loads(manifest.read_text())
            del payload["dhnsw_runtime_manifest_sha256"]
            manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
            with self.assertRaisesRegex(ValueError, "runtime"):
                multicn.read_manifest(manifest)

    def test_rejects_manifest_without_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self.make_fixture(root)
            payload = json.loads(manifest.read_text())
            del payload["tool_sha256"]["dhnsw_parser"]
            manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
            with self.assertRaisesRegex(ValueError, "tool SHA keys"):
                multicn.read_manifest(manifest)

    def test_rejects_invalid_tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self.make_fixture(root)
            payload = json.loads(manifest.read_text())
            payload["tool_sha256"]["recorder"] = "not-a-sha"
            manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
            with self.assertRaisesRegex(ValueError, "recorder tool SHA"):
                multicn.read_manifest(manifest)

    def test_rejects_tool_identity_not_bound_to_protocol_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _ = self.make_fixture(root)
            payload = json.loads(manifest.read_text())
            payload["protocol"]["tool_sha256"]["recorder"] = "9" * 64
            manifest.write_text(json.dumps(payload, sort_keys=True) + "\n")
            with self.assertRaisesRegex(ValueError, "protocol fingerprint"):
                multicn.read_manifest(manifest)

    def test_rejects_csv_metric_not_bound_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            rows = self.read_rows(raw)
            rows[0]["qps"] = str(float(rows[0]["qps"]) * 10)
            with raw.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "source metric"):
                multicn.assemble(manifest, raw, root / "summary")

    def test_rejects_source_identity_even_with_recomputed_source_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            rows = self.read_rows(raw)
            source = root / rows[0]["source"]
            payload = json.loads(source.read_text())
            payload["binary_sha256"] = "9" * 64
            source.write_text(json.dumps(payload, sort_keys=True) + "\n")
            rows[0]["source_sha256"] = self.sha256(source)
            with raw.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "source identity"):
                multicn.assemble(manifest, raw, root / "summary")

    def test_rejects_initiator_only_latency_as_cross_cn_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, raw = self.make_fixture(root)
            rows = self.read_rows(raw)
            row = next(
                item
                for item in rows
                if item["system"] == "SlabWalk" and item["cn_count"] == "2"
            )
            row["p50_us"] = "50"
            row["p99_us"] = "90"
            source = root / row["source"]
            payload = json.loads(source.read_text())
            payload["metrics"]["p50_us"] = 50.0
            payload["metrics"]["p99_us"] = 90.0
            source.write_text(json.dumps(payload, sort_keys=True) + "\n")
            row["source_sha256"] = self.sha256(source)
            with raw.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "cross-CN|latency scope"):
                multicn.assemble(manifest, raw, root / "summary")


if __name__ == "__main__":
    unittest.main()
