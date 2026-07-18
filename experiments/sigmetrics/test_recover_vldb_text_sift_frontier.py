#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("recover_vldb_text_sift_frontier.sh")


class RecoverTextSiftFrontierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_waits_without_modifying_active_campaigns(self):
        self.assertIn("PRIMARY_QUEUE=${PRIMARY_QUEUE:-vldb-frontier-text-sift-sw-final-v1}", self.text)
        self.assertIn("HEDGE_SESSION=${HEDGE_SESSION:-vldb-index-tti10m-parallel-v1}", self.text)
        self.assertIn("SIFT_SESSION=${SIFT_SESSION:-vldb-index-sift10m-parallel-v1}", self.text)
        self.assertIn("frontier_text_sift_sw_final_v2_20260714", self.text)
        self.assertIn("primary campaign already complete", self.text)

    def test_revalidates_inputs_and_runs_five_repeats(self):
        self.assertIn("spotcheck_groundtruth.py", self.text)
        self.assertIn("fingerprint_query_pool.py", self.text)
        self.assertIn('REPEATS=5', self.text)
        self.assertIn('THREADS=10', self.text)
        self.assertIn('QUERY_CONTEXTS=10', self.text)
        self.assertIn('DATASETS_SW="TEXT10M SIFT10M"', self.text)
        self.assertIn('PHASES=sw', self.text)

    def test_is_process_safe_and_fail_closed(self):
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("kill -9", self.text)
        self.assertNotIn("rm -rf", self.text)
        self.assertIn('[[ ! -e "$OUT_ROOT" ]]', self.text)
        self.assertIn("incomplete recovery frontier", self.text)


if __name__ == "__main__":
    unittest.main()
