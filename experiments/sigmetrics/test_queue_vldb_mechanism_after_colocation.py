import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_mechanism_after_colocation.sh")


class VldbMechanismQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_waits_for_and_validates_colocation_predecessor(self) -> None:
        self.assertIn(
            'PREDECESSOR_SESSION=${PREDECESSOR_SESSION:-vldb-colocation-deep1-final-v2}',
            self.text,
        )
        self.assertIn('while tmux has-session -t "$PREDECESSOR_SESSION"', self.text)
        self.assertIn('summary / "validation.json"', self.text)

    def test_requires_complete_colocation_protocol_and_hashes(self) -> None:
        self.assertIn("colocation_complete()", self.text)
        self.assertIn('report.get("measured_runs") != 30', self.text)
        self.assertIn('report.get("measured_cells") != 6', self.text)
        self.assertIn('report.get("retained_cells") != 36', self.text)
        self.assertIn('protocol.get("repeats") != 5', self.text)
        self.assertIn('colocation_complete "$PREDECESSOR_OUT"', self.text)

    def test_runs_the_frozen_budget_and_resident_matrices(self) -> None:
        self.assertIn('MEMORY_NODE=skv-node5', self.text)
        self.assertIn('PORT=1316', self.text)
        self.assertIn('BUDGET_FRACTIONS="f05 f10 f25 f50 f75 full"', self.text)
        self.assertIn('RESIDENT_MODES="remote resident"', self.text)
        self.assertIn('RESIDENT_EFS="50 100 200"', self.text)
        self.assertIn('REPEATS=5 WARMUPS=1', self.text)
        self.assertIn('EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA"', self.text)

    def test_verifies_snapshot_and_both_summaries(self) -> None:
        self.assertIn('sha256sum "$RUNNER"', self.text)
        self.assertIn('sha256sum "$SUMMARIZER"', self.text)
        self.assertIn('refusing existing mechanism-control output', self.text)
        self.assertIn('$OUT/summary/budget_summary.csv', self.text)
        self.assertIn('$OUT/summary/resident_summary.csv', self.text)
        self.assertIn('$OUT/summary/provenance.json', self.text)
        self.assertIn("mechanism_controls_final_v6_20260715", self.text)
        self.assertIn("mechanism_controls_snapshot_v7_20260715", self.text)

    def test_avoids_global_process_kills(self) -> None:
        for forbidden in ("pkill", "pgrep", "killall", "kill -9"):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
