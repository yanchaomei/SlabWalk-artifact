import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_colocation_after_cache.sh")


class VldbColocationQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_waits_for_and_validates_the_profile_predecessor(self) -> None:
        self.assertIn(
            'PROFILE_SESSION=${PROFILE_SESSION:-vldb-profile-sift1-frozen-v2}',
            self.text,
        )
        self.assertIn('while tmux has-session -t "$PROFILE_SESSION"', self.text)
        self.assertIn("profile_complete()", self.text)
        self.assertIn('profile_complete "$PROFILE_OUT"', self.text)
        self.assertIn('tag = "SIFT1M_shine_T1_C8_ef100"', self.text)
        self.assertIn('perf_path = root / f"{tag}.perf.data"', self.text)

    def test_runs_the_frozen_matrix_on_disjoint_nodes(self) -> None:
        self.assertIn('MEMORY_NODE=skv-node5', self.text)
        self.assertIn('PORT=1314', self.text)
        self.assertIn('DEGREES="full 24 16 8 4 1"', self.text)
        self.assertIn('THREADS=10 QUERY_CONTEXTS=10 COROUTINES=2 EF_SEARCH=200', self.text)
        self.assertIn('EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA"', self.text)

    def test_verifies_immutable_snapshot_and_output(self) -> None:
        self.assertIn('sha256sum "$RUNNER"', self.text)
        self.assertIn('sha256sum "$SUMMARIZER"', self.text)
        self.assertIn('refusing existing co-location output', self.text)
        self.assertIn('$OUT/summary/validation.json', self.text)
        self.assertIn('$OUT/summary/runs.csv', self.text)
        self.assertIn('$OUT/summary/summary.csv', self.text)
        self.assertIn("colocation_control_deep1_final_v2_20260714", self.text)

    def test_avoids_global_process_kills(self) -> None:
        for forbidden in ('pkill', 'pgrep', 'killall', 'kill -9'):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
