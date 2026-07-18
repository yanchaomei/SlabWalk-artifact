#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import assemble_vldb_query_pools as assembler


DATASETS = {
    "DEEP10M": (96, "l2"),
    "SIFT10M": (128, "l2"),
    "TTI10M": (200, "ip"),
}
METHODS = ("SHINE", "SlabWalk", "d-HNSW")


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def manifest(dataset: str, method: str) -> dict[str, object]:
    dim, metric = DATASETS[dataset]
    query_format, gt_format = (
        ("fvecs", "ivecs") if method == "d-HNSW" else ("fbin", "bin")
    )
    return {
        "kind": "query_pool_fingerprint",
        "dataset": dataset,
        "method": method,
        "metric": metric,
        "limit": 10000,
        "query": {
            "path": f"/remote/{dataset}/{method}/query.{query_format}",
            "format": query_format,
            "rows": 10000,
            "source_rows": 10000,
            "dim": dim,
            "bytes": 1,
            "canonical_sha256": digest(f"{dataset}/query/canonical"),
            "file_sha256": digest(f"{dataset}/{method}/query/file"),
        },
        "groundtruth": {
            "path": f"/remote/{dataset}/{method}/groundtruth.{gt_format}",
            "format": gt_format,
            "layout": "ids_only",
            "rows": 10000,
            "source_rows": 10000,
            "k": 100,
            "bytes": 1,
            "canonical_ids_sha256": digest(f"{dataset}/groundtruth/canonical"),
            "file_sha256": digest(f"{dataset}/{method}/groundtruth/file"),
        },
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_sources(root: Path) -> tuple[Path, Path, Path]:
    base = root / "base"
    graph = root / "tti_graph"
    dhnsw = root / "tti_dhnsw.json"
    base.mkdir()
    (base / "README.md").write_text("base query-pool contract\n")
    for dataset in ("DEEP10M", "SIFT10M"):
        for method in METHODS:
            slug = method.lower().replace("-", "")
            write_json(base / f"{dataset.lower()}_{slug}.json", manifest(dataset, method))
    for method in ("SHINE", "SlabWalk"):
        write_json(graph / f"tti10m_{method.lower()}.json", manifest("TTI10M", method))
    write_json(dhnsw, manifest("TTI10M", "d-HNSW"))
    graph_manifest = manifest("TTI10M", "SHINE")
    write_json(
        graph / "tti_exact_groundtruth_spotcheck.json",
        {
            "status": "ok",
            "metric": "ip",
            "top_k": 10,
            "checked_queries": 3,
            "query_indices": [0, 4999, 9999],
            "minimum_overlap": 10,
            "query": {"sha256": graph_manifest["query"]["file_sha256"]},
            "groundtruth": {
                "sha256": graph_manifest["groundtruth"]["file_sha256"]
            },
            "checks": [
                {"query_index": index, "overlap": 10, "exact_set_match": True}
                for index in (0, 4999, 9999)
            ],
        },
    )
    return base, graph, dhnsw


class AssembleVldbQueryPoolsTest(unittest.TestCase):
    def test_assembles_validated_nine_cell_matrix_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base, graph, dhnsw = write_sources(root)
            out = root / "out"

            assembler.assemble(base, graph, dhnsw, out)

            fingerprints = []
            for path in out.glob("*.json"):
                record = json.loads(path.read_text())
                if record.get("kind") == "query_pool_fingerprint":
                    fingerprints.append((record["dataset"], record["method"]))
            self.assertEqual(len(fingerprints), 9)
            self.assertEqual(len(set(fingerprints)), 9)
            self.assertTrue((out / "tti_exact_groundtruth_spotcheck.json").is_file())
            self.assertTrue((out / "PROVENANCE.json").is_file())
            self.assertTrue((out / "VALIDATION.json").is_file())
            self.assertTrue((out / "SHA256SUMS").is_file())
            provenance = json.loads((out / "PROVENANCE.json").read_text())
            self.assertEqual(len(provenance["retained_sources"]), 10)
            self.assertTrue(all(len(row["sha256"]) == 64 for row in provenance["retained_sources"]))

    def test_logical_content_mismatch_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base, graph, dhnsw = write_sources(root)
            record = json.loads(dhnsw.read_text())
            record["query"]["canonical_sha256"] = digest("wrong query")
            write_json(dhnsw, record)
            out = root / "out"

            with self.assertRaisesRegex(ValueError, "query-pool content mismatch"):
                assembler.assemble(base, graph, dhnsw, out)

            self.assertFalse(out.exists())
            self.assertEqual(list(root.glob(".out.staging.*")), [])

    def test_missing_exact_spotcheck_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base, graph, dhnsw = write_sources(root)
            (graph / "tti_exact_groundtruth_spotcheck.json").unlink()
            out = root / "out"

            with self.assertRaisesRegex(ValueError, "spot check"):
                assembler.assemble(base, graph, dhnsw, out)

            self.assertFalse(out.exists())

    def test_refuses_to_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base, graph, dhnsw = write_sources(root)
            out = root / "out"
            out.mkdir()

            with self.assertRaisesRegex(ValueError, "output already exists"):
                assembler.assemble(base, graph, dhnsw, out)


if __name__ == "__main__":
    unittest.main()
