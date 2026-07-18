import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from validate_dhnsw_dataset import validate_dataset


def write_fvecs(path: Path, values: np.ndarray) -> None:
    with path.open("wb") as handle:
        for row in values.astype("<f4", copy=False):
            handle.write(struct.pack("<i", row.size))
            row.tofile(handle)


def write_ivecs(path: Path, values: np.ndarray) -> None:
    with path.open("wb") as handle:
        for row in values.astype("<i4", copy=False):
            handle.write(struct.pack("<i", row.size))
            row.tofile(handle)


class DhnswDatasetValidationTest(unittest.TestCase):
    def test_accepts_ground_truth_within_base_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.fvecs"
            gt = root / "groundtruth.ivecs"
            write_fvecs(base, np.arange(20, dtype=np.float32).reshape(5, 4))
            write_ivecs(gt, np.array([[0, 4], [3, 1]], dtype=np.int32))

            record = validate_dataset(base, gt, expected_queries=2, min_k=2)

            self.assertEqual(record["base_rows"], 5)
            self.assertEqual(record["base_dim"], 4)
            self.assertEqual(record["groundtruth_rows"], 2)
            self.assertEqual(record["groundtruth_k"], 2)
            self.assertEqual(record["max_groundtruth_id"], 4)

    def test_records_and_validates_the_exact_query_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.fvecs"
            query = root / "query.fvecs"
            gt = root / "groundtruth.ivecs"
            write_fvecs(base, np.arange(20, dtype=np.float32).reshape(5, 4))
            write_fvecs(query, np.arange(8, dtype=np.float32).reshape(2, 4))
            write_ivecs(gt, np.array([[0, 4], [3, 1]], dtype=np.int32))

            record = validate_dataset(
                base, gt, query_fvecs=query, expected_queries=2, min_k=2
            )

            self.assertEqual(record["query_rows"], 2)
            self.assertEqual(record["query_dim"], 4)
            self.assertEqual(len(record["query_sha256"]), 64)

    def test_rejects_query_pool_that_does_not_match_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.fvecs"
            query = root / "query.fvecs"
            gt = root / "groundtruth.ivecs"
            write_fvecs(base, np.arange(20, dtype=np.float32).reshape(5, 4))
            write_fvecs(query, np.arange(4, dtype=np.float32).reshape(1, 4))
            write_ivecs(gt, np.array([[0, 4], [3, 1]], dtype=np.int32))

            with self.assertRaisesRegex(ValueError, "query/ground-truth row count"):
                validate_dataset(
                    base, gt, query_fvecs=query, expected_queries=2, min_k=2
                )

    def test_rejects_ground_truth_from_a_larger_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.fvecs"
            gt = root / "groundtruth.ivecs"
            write_fvecs(base, np.arange(20, dtype=np.float32).reshape(5, 4))
            write_ivecs(gt, np.array([[0, 5]], dtype=np.int32))

            with self.assertRaisesRegex(ValueError, "outside base domain"):
                validate_dataset(base, gt, expected_queries=1, min_k=2)

    def test_rejects_inconsistent_row_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base.fvecs"
            gt = root / "groundtruth.ivecs"
            write_fvecs(base, np.arange(20, dtype=np.float32).reshape(5, 4))
            with gt.open("wb") as handle:
                handle.write(struct.pack("<i2i", 2, 0, 1))
                handle.write(struct.pack("<i3i", 3, 1, 2, 3))

            with self.assertRaisesRegex(ValueError, "fixed-width"):
                validate_dataset(base, gt, expected_queries=2, min_k=2)


if __name__ == "__main__":
    unittest.main()
