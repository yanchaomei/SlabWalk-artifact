import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "queue_vldb_profile_after_cache.sh"


class VldbProfileQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_waits_for_the_formal_cache_predecessor(self) -> None:
        self.assertIn(
            "CACHE_SESSION=${CACHE_SESSION:-vldb-cache-control-sift1-final-v2}",
            self.text,
        )
        self.assertIn('while tmux has-session -t "$CACHE_SESSION"', self.text)
        self.assertIn('summary / "validation.json"', self.text)

    def test_validates_the_complete_cache_protocol_and_hashes(self) -> None:
        self.assertIn("cache_complete()", self.text)
        self.assertIn('report.get("measured_runs") != 20', self.text)
        self.assertIn('report.get("measured_cells") != 4', self.text)
        self.assertIn('report.get("retained_cells") != 24', self.text)
        self.assertIn('protocol.get("repeats") != 5', self.text)
        self.assertIn('cache_complete "$CACHE_OUT"', self.text)

    def test_runs_a_frozen_single_worker_sift_profile(self) -> None:
        self.assertIn("DATASETS=SIFT1M", self.text)
        self.assertIn("METHODS=shine", self.text)
        self.assertIn("THREADS=1", self.text)
        self.assertIn("QUERY_CONTEXTS=1", self.text)
        self.assertIn("COROUTINES=8", self.text)
        self.assertIn("EF=100", self.text)
        self.assertIn("MN_SIFT1M=skv-node4", self.text)
        self.assertIn(
            "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6",
            self.text,
        )
        self.assertIn(
            "3d20f0968654a3ad27fa1b4624c5425e1844258a1a75cc905232ac78e68bcf6e",
            self.text,
        )

    def test_avoids_global_process_control(self) -> None:
        for forbidden in ("pkill", "pgrep", "killall", "kill -9"):
            self.assertNotIn(forbidden, self.text)

    def test_retains_the_exact_runner_and_source_hashes(self) -> None:
        self.assertIn("runner_snapshot.sh", self.text)
        self.assertIn("profile_sources.sha256", self.text)
        self.assertIn('sha256sum \'$OUT/runner_snapshot.sh\' \'$GB_BIN\'', self.text)
        self.assertIn("query_profile_sift1_frozen_v2_20260714", self.text)


if __name__ == "__main__":
    unittest.main()
