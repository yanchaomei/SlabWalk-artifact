import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from spotcheck_groundtruth import spotcheck_groundtruth


def write_bin(path: Path, values: np.ndarray, dtype: str) -> None:
    values = np.asarray(values, dtype=dtype)
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", *values.shape))
        values.tofile(handle)


def write_gt(path: Path, ids: np.ndarray, with_distances: bool) -> None:
    ids = np.asarray(ids, dtype="<i4")
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", *ids.shape))
        ids.tofile(handle)
        if with_distances:
            np.zeros_like(ids, dtype="<f4").tofile(handle)


class SpotcheckGroundtruthTest(unittest.TestCase):
    def test_ip_matches_ids_plus_distances_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base = np.array([[1, 0], [0, 1], [0.8, 0.2], [-1, 0]], dtype=np.float32)
            query = np.array([[1, 0], [0, 1]], dtype=np.float32)
            ids = np.array([[0, 2], [1, 2]], dtype=np.int32)
            write_bin(root / "base.fbin", base, "<f4")
            write_bin(root / "query.fbin", query, "<f4")
            write_gt(root / "groundtruth.bin", ids, with_distances=True)

            record = spotcheck_groundtruth(
                root / "base.fbin",
                root / "query.fbin",
                root / "groundtruth.bin",
                metric="ip",
                query_indices=[0, 1],
                top_k=2,
                block_rows=2,
            )

            self.assertEqual(record["checked_queries"], 2)
            self.assertEqual(record["minimum_overlap"], 2)
            self.assertEqual(record["groundtruth_layout"], "ids_then_float_distances")

    def test_l2_reports_a_mismatched_ground_truth(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            base = np.array([[0, 0], [1, 0], [4, 0]], dtype=np.float32)
            query = np.array([[0, 0]], dtype=np.float32)
            write_bin(root / "base.fbin", base, "<f4")
            write_bin(root / "query.fbin", query, "<f4")
            write_gt(
                root / "groundtruth.bin",
                np.array([[2, 1]], dtype=np.int32),
                with_distances=False,
            )

            record = spotcheck_groundtruth(
                root / "base.fbin",
                root / "query.fbin",
                root / "groundtruth.bin",
                metric="l2",
                query_indices=[0],
                top_k=2,
                block_rows=2,
            )

            self.assertEqual(record["minimum_overlap"], 1)
            self.assertEqual(record["status"], "mismatch")

    def test_require_exact_raises_when_any_topk_set_differs(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            write_bin(root / "base.fbin", np.array([[0], [1], [4]]), "<f4")
            write_bin(root / "query.fbin", np.array([[0]]), "<f4")
            write_gt(
                root / "groundtruth.bin",
                np.array([[2, 1]], dtype=np.int32),
                with_distances=False,
            )

            with self.assertRaisesRegex(ValueError, "exact top-2 spot check failed"):
                spotcheck_groundtruth(
                    root / "base.fbin",
                    root / "query.fbin",
                    root / "groundtruth.bin",
                    metric="l2",
                    query_indices=[0],
                    top_k=2,
                    block_rows=2,
                    require_exact=True,
                )


if __name__ == "__main__":
    unittest.main()
