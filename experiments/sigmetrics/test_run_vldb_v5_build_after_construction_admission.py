#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name(
    "run_vldb_v5_build_after_construction_admission.sh"
)


class RunVldbV5BuildAfterConstructionAdmissionTest(unittest.TestCase):
    def test_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_verifies_admission_before_creating_output(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn("verify_vldb_construction_admission.py", text)
        self.assertIn("EXPECTED_CONSTRUCTION_GATE_SHA", text)
        self.assertLess(
            text.index("verify_vldb_construction_admission.py"),
            text.index('mkdir -p "$OUT_ROOT"'),
        )
        self.assertIn('[[ ! -e "$OUT_ROOT" ]]', text)
        self.assertNotIn("FINALIZATION_COMPLETE", text)
        self.assertNotIn("tmux", text)

    def test_binds_admission_to_the_complete_build_matrix(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('DATASETS="SIFT1M DEEP1M GIST1M"', text)
        self.assertIn("REPEATS=5", text)
        self.assertIn('ADMISSION_GATE="$CONSTRUCTION_GATE"', text)
        self.assertIn(
            'EXPECTED_ADMISSION_GATE_SHA="$EXPECTED_CONSTRUCTION_GATE_SHA"', text
        )
        self.assertIn('ADMISSION_SCOPE="construction_measurements_only"', text)
        self.assertIn('vldb_evidence_bundle.py" seal', text)
        self.assertIn('vldb_evidence_bundle.py" verify', text)
        self.assertIn("assemble_vldb_build_cost.py", text)
        self.assertIn(
            '--expected-admission-gate-sha "$EXPECTED_CONSTRUCTION_GATE_SHA"', text
        )
        self.assertIn(
            'expected_admission_gate_sha=expected_admission_gate_sha', text
        )
        self.assertIn("validate_build_cost", text)
        for forbidden in ("pkill", "killall", "kill -9"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
