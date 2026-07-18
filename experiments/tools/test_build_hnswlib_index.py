#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import hnswlib
import numpy as np


SCRIPT = Path(__file__).with_name("build_hnswlib_index.py")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_fbin(path: Path, values: np.ndarray) -> None:
    with path.open("wb") as output:
        output.write(struct.pack("<II", *values.shape))
        output.write(values.astype("<f4", copy=False).tobytes())


def write_fvecs(path: Path, values: np.ndarray) -> None:
    dim = values.shape[1]
    with path.open("wb") as output:
        for row in values:
            output.write(struct.pack("<I", dim))
            output.write(row.astype("<f4", copy=False).tobytes())


class BuildHnswlibIndexTests(unittest.TestCase):
    COUNT = 192
    DIM = 8

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        rng = np.random.default_rng(20260714)
        self.vectors = rng.standard_normal((self.COUNT, self.DIM), dtype=np.float32)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_builder(
        self,
        source: Path,
        output: Path,
        manifest: Path,
        *,
        space: str = "l2",
        force: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--base",
            str(source),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
            "--space",
            space,
            "--m",
            "4",
            "--ef-construction",
            "32",
            "--threads",
            "2",
            "--batch-size",
            "48",
            "--random-seed",
            "47",
        ]
        if force:
            command.append("--force")
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_error(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertIn(text.lower(), result.stderr.lower())

    def assert_index_and_manifest(
        self,
        source: Path,
        output: Path,
        manifest: Path,
        source_format: str,
        space: str,
    ) -> None:
        index = hnswlib.Index(space=space, dim=self.DIM)
        index.load_index(str(output), max_elements=self.COUNT)
        self.assertEqual(index.get_current_count(), self.COUNT)
        labels, _ = index.knn_query(self.vectors[:5], k=3, num_threads=1)
        self.assertEqual(labels.shape, (5, 3))
        self.assertTrue(np.all(labels[:, 0] == np.arange(5)))

        record = json.loads(manifest.read_text())
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["source_format"], source_format)
        self.assertEqual(record["space"], space)
        self.assertEqual(record["count"], self.COUNT)
        self.assertEqual(record["dim"], self.DIM)
        self.assertEqual(record["m"], 4)
        self.assertEqual(record["ef_construction"], 32)
        self.assertEqual(record["threads"], 2)
        self.assertEqual(record["batch_size"], 48)
        self.assertEqual(record["random_seed"], 47)
        self.assertEqual(record["source_sha256"], sha256(source))
        self.assertEqual(record["output_sha256"], sha256(output))
        self.assertEqual(record["completed"], self.COUNT)
        self.assertGreater(record["wall_seconds"], 0)
        self.assertGreater(record["peak_rss_bytes"], 0)

    def test_builds_fbin_l2_index_with_auditable_manifest(self) -> None:
        source = self.root / "base.fbin"
        output = self.root / "base.hnswlib"
        manifest = self.root / "build.json"
        write_fbin(source, self.vectors)
        result = self.run_builder(source, output, manifest)
        self.assert_success(result)
        self.assert_index_and_manifest(source, output, manifest, "fbin", "l2")
        progress = json.loads((manifest.with_suffix(manifest.suffix + ".progress")).read_text())
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["completed"], self.COUNT)

    def test_builds_fvecs_ip_index(self) -> None:
        vectors = self.vectors.copy()
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        self.vectors = vectors
        source = self.root / "base.fvecs"
        output = self.root / "base-ip.hnswlib"
        manifest = self.root / "build-ip.json"
        write_fvecs(source, vectors)
        result = self.run_builder(source, output, manifest, space="ip")
        self.assert_success(result)
        self.assert_index_and_manifest(source, output, manifest, "fvecs", "ip")

    def test_refuses_existing_artifacts_without_force(self) -> None:
        source = self.root / "base.fbin"
        output = self.root / "base.hnswlib"
        manifest = self.root / "build.json"
        write_fbin(source, self.vectors)
        self.assert_success(self.run_builder(source, output, manifest))
        original_sha = sha256(output)
        self.assert_error(self.run_builder(source, output, manifest), "overwrite")
        self.assertEqual(sha256(output), original_sha)
        self.assert_success(self.run_builder(source, output, manifest, force=True))

    def test_rejects_malformed_fvecs_rows(self) -> None:
        source = self.root / "bad.fvecs"
        write_fvecs(source, self.vectors)
        data = bytearray(source.read_bytes())
        row_bytes = 4 + self.DIM * 4
        struct.pack_into("<I", data, 3 * row_bytes, self.DIM + 1)
        source.write_bytes(data)
        result = self.run_builder(source, self.root / "bad.hnswlib", self.root / "bad.json")
        self.assert_error(result, "dimension")


if __name__ == "__main__":
    unittest.main(verbosity=2)
