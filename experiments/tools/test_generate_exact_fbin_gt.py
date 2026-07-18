import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from generate_exact_fbin_gt import read_fbin, sha256_file, write_ibin_atomic


class ExactGroundTruthFormatTest(unittest.TestCase):
    def test_fbin_shape_and_atomic_ibin_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fbin = root / "tiny.fbin"
            values = np.arange(12, dtype="<f4").reshape(3, 4)
            with fbin.open("wb") as handle:
                handle.write(struct.pack("<II", 3, 4))
                values.tofile(handle)
            mapped = read_fbin(fbin)
            np.testing.assert_array_equal(mapped, values)

            ids = np.array([[2, 1], [0, 2], [1, 0]], dtype=np.int64)
            ibin = root / "tiny_gt.bin"
            write_ibin_atomic(ibin, ids)
            with ibin.open("rb") as handle:
                self.assertEqual(struct.unpack("<II", handle.read(8)), (3, 2))
                actual = np.fromfile(handle, dtype="<u4").reshape(3, 2)
            np.testing.assert_array_equal(actual, ids)
            self.assertEqual(len(sha256_file(ibin)), 64)

    def test_fbin_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.fbin"
            path.write_bytes(struct.pack("<II", 2, 3) + b"\x00" * 4)
            with self.assertRaises(ValueError):
                read_fbin(path)


if __name__ == "__main__":
    unittest.main()
