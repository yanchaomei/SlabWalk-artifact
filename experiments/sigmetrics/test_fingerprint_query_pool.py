import json
import struct
import tempfile
import unittest
from pathlib import Path

from fingerprint_query_pool import fingerprint_query_pool


def write_fbin(path: Path, rows: list[list[float]]) -> None:
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", len(rows), len(rows[0])))
        for row in rows:
            handle.write(struct.pack(f"<{len(row)}f", *row))


def write_fvecs(path: Path, rows: list[list[float]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(struct.pack("<i", len(row)))
            handle.write(struct.pack(f"<{len(row)}f", *row))


def write_bin(path: Path, ids: list[list[int]], with_distances: bool) -> None:
    with path.open("wb") as handle:
        handle.write(struct.pack("<II", len(ids), len(ids[0])))
        for row in ids:
            handle.write(struct.pack(f"<{len(row)}i", *row))
        if with_distances:
            for row in ids:
                handle.write(struct.pack(f"<{len(row)}f", *([0.0] * len(row))))


def write_ivecs(path: Path, ids: list[list[int]]) -> None:
    with path.open("wb") as handle:
        for row in ids:
            handle.write(struct.pack("<i", len(row)))
            handle.write(struct.pack(f"<{len(row)}i", *row))


class QueryPoolFingerprintTest(unittest.TestCase):
    def test_canonical_hashes_match_across_diskann_and_vecs_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            queries = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
            ids = [[2, 1], [0, 2], [1, 0]]
            write_fbin(root / "query.fbin", queries)
            write_fvecs(root / "query.fvecs", queries)
            write_bin(root / "groundtruth.bin", ids, with_distances=True)
            write_ivecs(root / "groundtruth.ivecs", ids)

            diskann = fingerprint_query_pool(
                root / "query.fbin",
                root / "groundtruth.bin",
                dataset="TTI10M",
                method="SlabWalk",
                metric="ip",
            )
            vecs = fingerprint_query_pool(
                root / "query.fvecs",
                root / "groundtruth.ivecs",
                dataset="TTI10M",
                method="d-HNSW",
                metric="ip",
            )

            self.assertEqual(
                diskann["query"]["canonical_sha256"],
                vecs["query"]["canonical_sha256"],
            )
            self.assertEqual(
                diskann["groundtruth"]["canonical_ids_sha256"],
                vecs["groundtruth"]["canonical_ids_sha256"],
            )
            self.assertEqual(diskann["query"]["rows"], 3)
            self.assertEqual(diskann["groundtruth"]["k"], 2)
            self.assertEqual(diskann["groundtruth"]["layout"], "ids_then_distances")

    def test_limit_hashes_the_same_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            queries = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
            ids = [[2, 1], [0, 2], [1, 0]]
            write_fbin(root / "query.fbin", queries)
            write_fvecs(root / "query.fvecs", queries[:2])
            write_bin(root / "groundtruth.bin", ids, with_distances=False)
            write_ivecs(root / "groundtruth.ivecs", ids[:2])

            prefix = fingerprint_query_pool(
                root / "query.fbin",
                root / "groundtruth.bin",
                dataset="DEEP10M",
                method="SHINE",
                metric="l2",
                limit=2,
            )
            exact = fingerprint_query_pool(
                root / "query.fvecs",
                root / "groundtruth.ivecs",
                dataset="DEEP10M",
                method="d-HNSW",
                metric="l2",
            )
            self.assertEqual(prefix["query"]["canonical_sha256"], exact["query"]["canonical_sha256"])
            self.assertEqual(
                prefix["groundtruth"]["canonical_ids_sha256"],
                exact["groundtruth"]["canonical_ids_sha256"],
            )

    def test_rejects_inconsistent_vecs_row_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            write_fvecs(root / "query.fvecs", [[1.0, 2.0], [3.0, 4.0]])
            data = bytearray((root / "query.fvecs").read_bytes())
            struct.pack_into("<i", data, 12, 3)
            (root / "query.fvecs").write_bytes(data)
            write_ivecs(root / "groundtruth.ivecs", [[0, 1], [1, 0]])
            with self.assertRaisesRegex(ValueError, "row dimension"):
                fingerprint_query_pool(
                    root / "query.fvecs",
                    root / "groundtruth.ivecs",
                    dataset="SIFT10M",
                    method="d-HNSW",
                    metric="l2",
                )

    def test_writes_machine_readable_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            write_fbin(root / "query.fbin", [[1.0], [2.0]])
            write_bin(root / "groundtruth.bin", [[0], [1]], with_distances=False)
            out = root / "manifest.json"
            record = fingerprint_query_pool(
                root / "query.fbin",
                root / "groundtruth.bin",
                dataset="DEEP10M",
                method="SlabWalk",
                metric="l2",
                out=out,
            )
            self.assertEqual(json.loads(out.read_text()), record)
            self.assertEqual(record["kind"], "query_pool_fingerprint")


if __name__ == "__main__":
    unittest.main()
