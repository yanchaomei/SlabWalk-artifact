import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_colocation_after_mechanism.sh")


class QueueVldbColocationAfterMechanismTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_waits_for_the_exact_hardened_mechanism_campaign(self) -> None:
        self.assertIn(
            'PREDECESSOR_SESSION=${PREDECESSOR_SESSION:-vldb-mechanism-controls-final-v6}',
            self.text,
        )
        self.assertIn('while tmux has-session -t "$PREDECESSOR_SESSION"', self.text)
        self.assertIn("vldb-mechanism-controls-final-v6-20260715", self.text)
        self.assertIn("mechanism_controls_final_v6_20260715", self.text)
        self.assertIn("measured_runs", self.text)
        self.assertIn("retained_source_files", self.text)

    def test_pins_the_new_colocation_snapshot_and_output(self) -> None:
        self.assertIn("colocation_snapshot_v4_20260715", self.text)
        self.assertIn("colocation_control_deep1_final_v4_20260715", self.text)
        self.assertIn("vldb-colocation-deep1-final-v4-20260715", self.text)
        self.assertIn(
            "b1d18539b1d73b1cff17d9aa3333eecbf32ca92ffdb2d373a3c78388fce007a4",
            self.text,
        )
        self.assertIn(
            "cdcd91484f3358d4896145321ab8c66e250095295dca9d0cad95979967ea33e5",
            self.text,
        )

    def test_is_fail_closed_and_avoids_global_process_kills(self) -> None:
        self.assertIn('[[ ! -e "$OUT" ]]', self.text)
        self.assertIn("mechanism predecessor did not complete", self.text)
        self.assertIn("co-location control complete", self.text)
        for forbidden in ("pkill", "pgrep", "killall", "kill -9"):
            self.assertNotIn(forbidden, self.text)


if __name__ == "__main__":
    unittest.main()
