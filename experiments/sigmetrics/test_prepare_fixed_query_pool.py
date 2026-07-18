import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from prepare_fixed_query_pool import prepare_fixed_query_pool


def write_fbin(path: Path, values: np.ndarray) -> None:
    values = np.asarray(values, dtype="<f4")
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", *values.shape))
        values.tofile(handle)


def write_groundtruth(
    path: Path, ids: np.ndarray, distances: np.ndarray | None = None
) -> None:
    ids = np.asarray(ids, dtype="<i4")
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", *ids.shape))
        ids.tofile(handle)
        if distances is not None:
            np.asarray(distances, dtype="<f4").tofile(handle)


def read_bin_matrix(path: Path, dtype: str) -> np.ndarray:
    with path.open("rb") as handle:
        rows, dim = struct.unpack("<II", handle.read(8))
        return np.fromfile(handle, dtype=dtype, count=rows * dim).reshape(rows, dim)


def read_fixed_vectors(path: Path, dtype: str) -> np.ndarray:
    rows = []
    with path.open("rb") as handle:
        while True:
            raw = handle.read(4)
            if not raw:
                break
            dim = struct.unpack("<i", raw)[0]
            row = np.fromfile(handle, dtype=dtype, count=dim)
            if row.size != dim:
                raise AssertionError("truncated fixed-vector row")
            rows.append(row.copy())
    return np.stack(rows)


class PrepareFixedQueryPoolTest(unittest.TestCase):
    def test_materializes_matching_prefix_in_bin_and_vec_formats(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            query = root / "query.fbin"
            groundtruth = root / "groundtruth.bin"
            query_values = np.arange(12, dtype=np.float32).reshape(3, 4)
            ids = np.array([[5, 4, 3], [2, 1, 0], [4, 2, 0]], dtype=np.int32)
            write_fbin(query, query_values)
            write_groundtruth(groundtruth, ids)

            record = prepare_fixed_query_pool(
                query,
                groundtruth,
                limit=2,
                query_fbin=root / "query-u2.fbin",
                groundtruth_bin=root / "groundtruth-u2.bin",
                query_fvecs=root / "query-u2.fvecs",
                groundtruth_ivecs=root / "groundtruth-u2.ivecs",
                manifest=root / "pool.json",
            )

            np.testing.assert_array_equal(
                read_bin_matrix(root / "query-u2.fbin", "<f4"), query_values[:2]
            )
            np.testing.assert_array_equal(
                read_bin_matrix(root / "groundtruth-u2.bin", "<i4"), ids[:2]
            )
            np.testing.assert_array_equal(
                read_fixed_vectors(root / "query-u2.fvecs", "<f4"), query_values[:2]
            )
            np.testing.assert_array_equal(
                read_fixed_vectors(root / "groundtruth-u2.ivecs", "<i4"), ids[:2]
            )
            self.assertEqual(record["selected_rows"], 2)
            self.assertEqual(record["groundtruth_layout"], "ids_only")
            self.assertEqual(json.loads((root / "pool.json").read_text()), record)

    def test_ids_plus_distances_keeps_only_header_width_ids(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            query = root / "query.fbin"
            groundtruth = root / "groundtruth.bin"
            query_values = np.arange(8, dtype=np.float32).reshape(2, 4)
            ids = np.array([[10, 9, 8], [7, 6, 5]], dtype=np.int32)
            distances = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype=np.float32)
            write_fbin(query, query_values)
            write_groundtruth(groundtruth, ids, distances)

            record = prepare_fixed_query_pool(
                query,
                groundtruth,
                limit=2,
                groundtruth_ivecs=root / "groundtruth.ivecs",
            )

            converted = read_fixed_vectors(root / "groundtruth.ivecs", "<i4")
            np.testing.assert_array_equal(converted, ids)
            self.assertEqual(converted.shape, (2, 3))
            self.assertEqual(record["groundtruth_layout"], "ids_then_float_distances")

    def test_rejects_payload_that_matches_neither_supported_layout(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            query = root / "query.fbin"
            groundtruth = root / "groundtruth.bin"
            write_fbin(query, np.arange(8, dtype=np.float32).reshape(2, 4))
            write_groundtruth(groundtruth, np.array([[1, 0], [0, 1]], dtype=np.int32))
            with groundtruth.open("ab") as handle:
                handle.write(b"bad!")

            with self.assertRaisesRegex(ValueError, "ground-truth payload size"):
                prepare_fixed_query_pool(
                    query,
                    groundtruth,
                    limit=2,
                    groundtruth_ivecs=root / "groundtruth.ivecs",
                )

    def test_manifest_hashes_materialized_outputs(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            query = root / "query.fbin"
            groundtruth = root / "groundtruth.bin"
            output = root / "groundtruth.ivecs"
            write_fbin(query, np.arange(8, dtype=np.float32).reshape(2, 4))
            write_groundtruth(groundtruth, np.array([[1, 0], [0, 1]], dtype=np.int32))

            record = prepare_fixed_query_pool(
                query, groundtruth, limit=1, groundtruth_ivecs=output
            )

            expected = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual(record["outputs"]["groundtruth_ivecs"]["sha256"], expected)
            self.assertEqual(record["outputs"]["groundtruth_ivecs"]["rows"], 1)
            self.assertEqual(record["outputs"]["groundtruth_ivecs"]["dim"], 2)


if __name__ == "__main__":
    unittest.main()
