#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("build_dhnsw_10m_baseline.sh")


class Dhnsw10mBaselineBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_pins_source_and_patches_before_configure(self) -> None:
        self.assertIn(
            "EXPECTED_SOURCE_COMMIT=d6f275732275e6009a542a7066d7f695036daaf6",
            self.text,
        )
        self.assertIn("git -C \"$SOURCE_DROOT\" archive", self.text)
        self.assertIn("PATCH_ONLY=1", self.text)
        self.assertLess(self.text.index("PATCH_ONLY=1"), self.text.index('cmake -S "$DROOT"'))

    def test_builds_and_checks_both_native_binaries(self) -> None:
        self.assertIn('--target run_client run_server', self.text)
        self.assertIn("binary-dataset-paths.txt", self.text)
        self.assertIn("query-u10k.fvecs", self.text)
        self.assertIn("groundtruth-u10k.ivecs", self.text)
        self.assertIn("text10M_query.fvecs", self.text)
        self.assertIn("compiled client/source dataset-path mismatch", self.text)
        self.assertIn("ldd.txt", self.text)
        self.assertIn("not found", self.text)

    def test_publishes_machine_readable_build_manifest(self) -> None:
        self.assertIn("build_manifest.json", self.text)
        self.assertIn('"kind": "dhnsw_source_matched_build"', self.text)
        self.assertIn('"patch_runner_sha256"', self.text)
        self.assertIn('"patched_source_sha256"', self.text)
        self.assertIn('"binary_sha256"', self.text)
        self.assertIn('"dataset_paths"', self.text)

    def test_never_kills_unrelated_processes(self) -> None:
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("kill -9", self.text)


if __name__ == "__main__":
    unittest.main()
