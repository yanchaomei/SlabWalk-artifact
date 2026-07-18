import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "run_vldb_cache_control.sh"


class VldbCacheControlRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_pins_the_formal_matrix_and_frozen_binary(self) -> None:
        self.assertIn('CONDITIONS=${CONDITIONS:-"off c5 c20 c50"}', self.text)
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('WARMUPS=${WARMUPS:-1}', self.text)
        self.assertIn('THREADS=${THREADS:-1}', self.text)
        self.assertIn('QUERY_CONTEXTS=${QUERY_CONTEXTS:-1}', self.text)
        self.assertIn('COROUTINES=${COROUTINES:-8}', self.text)
        self.assertIn('EF_SEARCH=${EF_SEARCH:-100}', self.text)
        self.assertIn('MEMORY_NODE=${MEMORY_NODE:-skv-node4}', self.text)
        self.assertIn('2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6', self.text)

    def test_sweeps_native_shine_cache_without_slabwalk(self) -> None:
        self.assertIn('--lavd 0', self.text)
        self.assertIn("c5) printf '%s\\n' '--cache' '--cache-ratio' '5'", self.text)
        self.assertIn("c20) printf '%s\\n' '--cache' '--cache-ratio' '20'", self.text)
        self.assertIn("c50) printf '%s\\n' '--cache' '--cache-ratio' '50'", self.text)
        self.assertIn('GB_QUERY_LATENCY=1', self.text)
        self.assertIn('fingerprint_query_pool.py', self.text)

    def test_owns_memory_node_pids_and_avoids_global_kills(self) -> None:
        self.assertIn('verify_remote_pid', self.text)
        self.assertIn('/proc/\\$pid/exe', self.text)
        self.assertIn('kill \\"\\$pid\\"', self.text)
        for forbidden in ('pkill', 'pgrep', 'killall', 'kill -9'):
            self.assertNotIn(forbidden, self.text)

    def test_is_fail_closed_and_self_summarizing(self) -> None:
        self.assertIn('campaign.json', self.text)
        self.assertIn('Refusing non-empty OUT_ROOT', self.text)
        self.assertIn('summarize_vldb_cache_control.py', self.text)
        self.assertIn('validate_cell', self.text)


if __name__ == "__main__":
    unittest.main()
