import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "run_vldb_colocation_control.sh"


class VldbColocationControlRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_pins_the_formal_matrix_and_frozen_binary(self) -> None:
        self.assertIn('DEGREES=${DEGREES:-"full 24 16 8 4 1"}', self.text)
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('WARMUPS=${WARMUPS:-1}', self.text)
        self.assertIn('THREADS=${THREADS:-10}', self.text)
        self.assertIn('QUERY_CONTEXTS=${QUERY_CONTEXTS:-10}', self.text)
        self.assertIn('COROUTINES=${COROUTINES:-2}', self.text)
        self.assertIn('EF_SEARCH=${EF_SEARCH:-200}', self.text)
        self.assertIn(
            '2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6',
            self.text,
        )

    def test_changes_only_the_number_of_inline_codes(self) -> None:
        self.assertIn('--lavd 8', self.text)
        self.assertIn('SHINE_CRANE=1', self.text)
        self.assertIn('GB_BITMAP_DEDUP=1', self.text)
        self.assertIn('SHINE_LAVD_COLOC_DEGREE="$degree"', self.text)
        self.assertIn('SHINE_LAVD_HOT_COLD_BATCH=1', self.text)
        self.assertIn('SHINE_LAVD_COLOC_SELFTEST=1', self.text)
        self.assertIn('fingerprint_query_pool.py', self.text)
        self.assertNotIn('SHINE_LAVD_PQ_M', self.text)
        self.assertNotIn('SHINE_LAVD_RABITQ_B', self.text)

    def test_owns_memory_node_pids_and_avoids_global_kills(self) -> None:
        self.assertIn('verify_remote_pid', self.text)
        self.assertIn('/proc/\\$pid/exe', self.text)
        self.assertIn('kill \\"\\$pid\\"', self.text)
        for forbidden in ('pkill', 'pgrep', 'killall', 'kill -9'):
            self.assertNotIn(forbidden, self.text)

    def test_normalizes_only_an_audited_owned_sigterm(self) -> None:
        self.assertIn("owned-stop.pid", self.text)
        self.assertIn('[[ "$rc" == "143"', self.text)
        self.assertIn('[[ "$(cat "$out/owned-stop.pid")" == "$server_pid" ]]', self.text)
        self.assertIn("normalized owned SIGTERM status 143 to 0", self.text)

    def test_is_fail_closed_and_self_summarizing(self) -> None:
        self.assertIn('campaign.json', self.text)
        self.assertIn('Refusing non-empty OUT_ROOT', self.text)
        self.assertIn('summarize_vldb_colocation_control.py', self.text)
        self.assertIn('validate_cell', self.text)
        self.assertIn('fails=0', self.text)

    def test_rehashes_every_cell_input_and_records_observations(self) -> None:
        self.assertIn('verify_immutable_inputs', self.text)
        self.assertIn('observed_inputs', self.text)
        self.assertIn('sha256sum "$GB_BIN"', self.text)
        self.assertIn("sha256sum '$GB_BIN_R' '$INDEX_DUMP'", self.text)
        self.assertIn('sha256sum "$QUERY_PATH"', self.text)
        self.assertIn('sha256sum "$GT_PATH"', self.text)


if __name__ == "__main__":
    unittest.main()
