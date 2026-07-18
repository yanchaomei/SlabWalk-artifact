#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("launch_parallel_sift10m_index_node4.sh")


class ParallelSift10mIndexScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_uses_frozen_binary_and_disjoint_hosts(self):
        self.assertIn("EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6", self.text)
        self.assertIn("SOURCE_HOST=${SOURCE_HOST:-skv-node1}", self.text)
        self.assertIn("MEMORY_NODE=${MEMORY_NODE:-skv-node6}", self.text)
        self.assertIn("DESTINATION_NODE=${DESTINATION_NODE:-skv-node2}", self.text)
        self.assertIn("DATASETS=SIFT10M", self.text)

    def test_validates_input_and_dump_hashes(self):
        self.assertIn("source input SHA mismatch", self.text)
        self.assertIn("destination dump SHA mismatch", self.text)
        self.assertIn("sha256sum", self.text)
        self.assertIn(".parallel-partial", self.text)

    def test_never_kills_unrelated_processes(self):
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("kill -9", self.text)


if __name__ == "__main__":
    unittest.main()
