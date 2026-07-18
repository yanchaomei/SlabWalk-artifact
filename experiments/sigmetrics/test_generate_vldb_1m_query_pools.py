#!/usr/bin/env python3
import unittest
import tempfile
from unittest import mock
from pathlib import Path

import generate_vldb_1m_query_pools as generator


def record(dataset: str, method: str, suffix: str = "") -> dict[str, object]:
    return {
        "dataset": dataset,
        "method": method,
        "query": {
            "rows": 10000,
            "dim": generator.DIMENSIONS[dataset],
            "canonical_sha256": f"query-{dataset}{suffix}",
        },
        "groundtruth": {
            "rows": 10000,
            "k": 100,
            "canonical_ids_sha256": f"gt-{dataset}{suffix}",
        },
    }


class GenerateVldb1MQueryPoolsTest(unittest.TestCase):
    def test_path_matrix_covers_all_cells_and_uses_gist_u10k(self) -> None:
        matrix = generator.matrix_paths(Path("/graph"), Path("/dh/datasets"))
        self.assertEqual(
            set(matrix),
            {
                (dataset, method)
                for dataset in generator.DATASETS
                for method in generator.METHODS
            },
        )
        self.assertEqual(len(matrix), 21)
        self.assertIn(
            "query-u10k.fbin", str(matrix[("GIST1M", "SlabWalk")][0])
        )
        self.assertIn("gist_query.fvecs", str(matrix[("GIST1M", "d-HNSW")][0]))
        self.assertEqual(
            matrix[("BIGANN1M", "SHINE")][0].suffix, ".u8bin"
        )
        self.assertEqual(
            matrix[("SPACEV1M", "SHINE")][0].suffix, ".i8bin"
        )

    def test_record_gate_requires_logical_identity_across_three_systems(self) -> None:
        records = {
            (dataset, method): record(dataset, method)
            for dataset in generator.DATASETS
            for method in generator.METHODS
        }
        generator.validate_records(records)
        records[("GIST1M", "d-HNSW")] = record(
            "GIST1M", "d-HNSW", suffix="-different"
        )
        with self.assertRaisesRegex(ValueError, "content differs"):
            generator.validate_records(records)

    def test_generate_refuses_to_publish_when_any_fingerprint_differs(self) -> None:
        with mock.patch.object(generator, "fingerprint_query_pool") as fingerprint:
            fingerprint.side_effect = lambda query, groundtruth, **kwargs: record(
                kwargs["dataset"],
                kwargs["method"],
                suffix="-different" if kwargs["method"] == "d-HNSW" else "",
            )
            with tempfile.TemporaryDirectory() as tmp_s:
                root = Path(tmp_s)
                with self.assertRaisesRegex(ValueError, "content differs"):
                        generator.generate(root / "graph", root / "dh", root / "out")
                self.assertFalse((root / "out").exists())
                self.assertFalse(any(root.glob(".out.staging.*")))


if __name__ == "__main__":
    unittest.main()
