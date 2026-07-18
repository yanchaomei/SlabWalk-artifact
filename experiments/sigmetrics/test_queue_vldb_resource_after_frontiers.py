#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("queue_vldb_resource_after_frontiers.sh")


class QueueResourceAfterFrontiersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_waits_for_every_competing_frontier(self):
        for session in (
            "vldb-frontier-text-sift-sw-final-v1",
            "vldb-frontier-text-sift-sw-recovery-v2",
            "vldb-dhnsw-text-sift-final-v3",
            "vldb-frontier-deep10-sw-final-v1",
        ):
            self.assertIn(session, self.text)
        self.assertIn("session_exists", self.text)

    def test_uses_final_binary_and_strict_resource_protocol(self):
        self.assertIn(
            "EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6",
            self.text,
        )
        self.assertIn("LAYOUTS=\"legacy fixed variable\"", self.text)
        self.assertIn("MN_COUNTS=\"1 3 5\"", self.text)
        self.assertIn("REPEATS=5", self.text)
        self.assertIn("WARMUPS=1", self.text)
        self.assertIn("--require-latency", self.text)
        self.assertIn("resource_ledger_gist_final_v3_20260714", self.text)

    def test_is_fail_closed_and_process_safe(self):
        self.assertIn('[[ ! -e "$OUT" ]]', self.text)
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("kill -9", self.text)
        self.assertNotIn("rm -rf", self.text)


if __name__ == "__main__":
    unittest.main()
