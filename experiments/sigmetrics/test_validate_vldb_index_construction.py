import json
import tempfile
import unittest
from pathlib import Path

import validate_vldb_final_evidence as evidence


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


DATASETS = {
    "sift10m": {
        "count": 10_000_000,
        "dim": 128,
        "space": "l2",
        "source_sha": "1" * 64,
        "hnsw_sha": "2" * 64,
        "dump_sha": "3" * 64,
        "source_bytes": 5_120_000_008,
        "hnsw_bytes": 6_605_339_232,
        "dump_bytes": 8_005_659_304,
    },
    "tti10m": {
        "count": 10_000_000,
        "dim": 200,
        "space": "ip",
        "source_sha": "4" * 64,
        "hnsw_sha": "5" * 64,
        "dump_sha": "6" * 64,
        "source_bytes": 8_000_000_008,
        "hnsw_bytes": 9_485_342_088,
        "dump_bytes": 10_885_664_672,
    },
}


def write_index_construction_evidence(
    root: Path, *, binary_sha: str = FINAL_SHA
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for dataset, cfg in DATASETS.items():
        directory = root / dataset
        directory.mkdir()
        build = {
            "status": "complete",
            "completed": cfg["count"],
            "count": cfg["count"],
            "dim": cfg["dim"],
            "space": cfg["space"],
            "m": 16,
            "ef_construction": 100,
            "random_seed": 47,
            "hnswlib_version": "0.8.0",
            "label_policy": "external_label_equals_source_row_id",
            "source_bytes": cfg["source_bytes"],
            "source_sha256": cfg["source_sha"],
            "output_bytes": cfg["hnsw_bytes"],
            "output_sha256": cfg["hnsw_sha"],
            "peak_rss_bytes": 1_000_000,
            "wall_seconds": 10.0,
            "threads": 72,
        }
        conversion = {
            "converter": "convert_hnswlib_dump-v1",
            "source_format": "hnswlib-0.8.0-native-64le",
            "format": "graphbeyond-hnsw-single-mn-v1",
            "source_sha256": cfg["hnsw_sha"],
            "output_sha256": cfg["dump_sha"],
            "source_bytes": cfg["hnsw_bytes"],
            "output_bytes": cfg["dump_bytes"],
            "count": cfg["count"],
            "dim": cfg["dim"],
            "m": 16,
            "max_m0": 32,
            "ef_construction": 100,
            "graph_preserved": True,
            "post_write_validation": "full_graph_payload_and_pointers",
            "deleted_nodes_accepted": False,
        }
        campaign = {
            "count": cfg["count"],
            "dim": cfg["dim"],
            "space": cfg["space"],
            "m": 16,
            "ef_construction": 100,
            "random_seed": 47,
            "frozen_graphbeyond_binary_sha256": binary_sha,
            "builder_sha256": "7" * 64,
            "converter_sha256": "8" * 64,
            "converter_source_sha256": "9" * 64,
        }
        (directory / "build.json").write_text(json.dumps(build))
        (directory / "conversion.json").write_text(json.dumps(conversion))
        (directory / "campaign.json").write_text(json.dumps(campaign))
        for name in ("build.rc", "conversion.rc", "pipeline.rc"):
            (directory / name).write_text("0\n")


class IndexConstructionValidationTest(unittest.TestCase):
    def test_accepts_two_graph_preserving_conversions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_index_construction_evidence(root)
            report = evidence.validate_index_construction(root, FINAL_SHA)
            self.assertEqual(report["measured_cells"], 2)
            self.assertEqual(set(report["datasets"]), {"SIFT10M", "TTI10M"})
            self.assertEqual(
                report["datasets"]["SIFT10M"]["dump_sha256"], "3" * 64
            )

    def test_rejects_conversion_that_did_not_preserve_the_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_index_construction_evidence(root)
            path = root / "tti10m" / "conversion.json"
            obj = json.loads(path.read_text())
            obj["graph_preserved"] = False
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "graph-preserved"):
                evidence.validate_index_construction(root, FINAL_SHA)

    def test_rejects_a_broken_builder_converter_sha_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_index_construction_evidence(root)
            path = root / "sift10m" / "conversion.json"
            obj = json.loads(path.read_text())
            obj["source_sha256"] = "a" * 64
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "source SHA"):
                evidence.validate_index_construction(root, FINAL_SHA)

    def test_rejects_nonzero_pipeline_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_index_construction_evidence(root)
            (root / "sift10m" / "pipeline.rc").write_text("2\n")
            with self.assertRaisesRegex(ValueError, "pipeline.rc"):
                evidence.validate_index_construction(root, FINAL_SHA)

    def test_rejects_wrong_frozen_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_index_construction_evidence(root, binary_sha="a" * 64)
            with self.assertRaisesRegex(ValueError, "frozen binary"):
                evidence.validate_index_construction(root, FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
