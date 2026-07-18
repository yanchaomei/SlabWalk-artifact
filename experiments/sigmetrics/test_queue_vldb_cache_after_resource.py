#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("queue_vldb_cache_after_resource.sh")


class QueueCacheAfterResourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_waits_for_resource_ledger_and_checks_its_outputs(self):
        self.assertIn("vldb-resource-gist-final-v3", self.text)
        self.assertIn("resource_ledger_gist_final_v3_20260714", self.text)
        for name in ("runs.csv", "per_mn.csv", "summary.csv"):
            self.assertIn(name, self.text)

    def test_runs_the_frozen_fixed_pool_cache_protocol(self):
        self.assertIn(
            "EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6",
            self.text,
        )
        self.assertIn('CONDITIONS="off c5 c20 c50"', self.text)
        self.assertIn("REPEATS=5", self.text)
        self.assertIn("WARMUPS=1", self.text)
        self.assertIn("THREADS=1", self.text)
        self.assertIn("QUERY_CONTEXTS=1", self.text)
        self.assertIn("COROUTINES=8", self.text)
        self.assertIn("EF_SEARCH=100", self.text)
        self.assertIn("MEMORY_NODE=skv-node4", self.text)
        self.assertIn("cache_control_sift1_final_v1_20260714", self.text)

    def test_is_fail_closed_and_process_safe(self):
        self.assertIn('[[ ! -e "$OUT" ]]', self.text)
        self.assertIn("validation.json", self.text)
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("kill -9", self.text)
        self.assertNotIn("rm -rf", self.text)


if __name__ == "__main__":
    unittest.main()
