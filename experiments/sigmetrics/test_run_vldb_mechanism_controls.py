import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_vldb_mechanism_controls.sh")


class MechanismControlRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_pins_complete_formal_matrices(self) -> None:
        self.assertIn('BUDGET_FRACTIONS=${BUDGET_FRACTIONS:-"f05 f10 f25 f50 f75 full"}', self.text)
        self.assertIn('RESIDENT_MODES=${RESIDENT_MODES:-"remote resident"}', self.text)
        self.assertIn('RESIDENT_EFS=${RESIDENT_EFS:-"50 100 200"}', self.text)
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('WARMUPS=${WARMUPS:-1}', self.text)

    def test_budget_uses_production_packed_variable_path(self) -> None:
        for token in (
            "SHINE_LAVD_BUDGET",
            "SHINE_LAVD_HOTSET=indeg",
            "SHINE_LAVD_NATIVE_PACKED_WRITE=1",
            "SHINE_LAVD_VARBLOCK=1",
            "SHINE_CRANE=1",
        ):
            self.assertIn(token, self.text)
        self.assertNotIn("SHINE_LAVD_SELFTEST=1", self.text)

    def test_budget_fraction_strings_match_manifest_contract(self) -> None:
        self.assertIn("f10) printf '0.1\\n'", self.text)
        self.assertIn("f50) printf '0.5\\n'", self.text)
        self.assertNotIn("f10) printf '0.10\\n'", self.text)
        self.assertNotIn("f50) printf '0.50\\n'", self.text)

    def test_resident_control_changes_only_resident_gate(self) -> None:
        self.assertIn('SHINE_CRANE="$crane"', self.text)
        self.assertIn('run_one resident "$mode" "$ef"', self.text)
        self.assertIn('run_one budget "$key" 100', self.text)

    def test_owns_only_exact_memory_server_pid(self) -> None:
        self.assertIn('server.pid', self.text)
        self.assertIn('server.exe', self.text)
        self.assertIn('readlink -f /proc/\\$pid/exe', self.text)
        self.assertNotIn('pkill', self.text)
        self.assertNotIn('pgrep', self.text)
        self.assertNotIn('killall', self.text)

    def test_normalizes_only_an_audited_owned_sigterm(self) -> None:
        self.assertIn("owned-stop.pid", self.text)
        self.assertIn('[[ "$rc" == "143"', self.text)
        self.assertIn('[[ "$(cat "$out/owned-stop.pid")" == "$server_pid" ]]', self.text)
        self.assertIn("normalized owned SIGTERM status 143 to 0", self.text)

    def test_requires_frozen_binary_and_descriptor_readback(self) -> None:
        self.assertIn("EXPECTED_BINARY_SHA", self.text)
        self.assertIn("sha256sum", self.text)
        self.assertIn("verify_immutable_inputs", self.text)
        self.assertIn("observed_inputs", self.text)
        self.assertIn('sha256sum "$GB_BIN"', self.text)
        self.assertIn("sha256sum '$GB_BIN_R'", self.text)
        self.assertIn("packed addressing restored from descriptor", self.text)
        self.assertIn("LAVD_PHYSICAL_ACCOUNTING", self.text)
        self.assertIn("summarize_vldb_mechanism_controls.py", self.text)


if __name__ == "__main__":
    unittest.main()
