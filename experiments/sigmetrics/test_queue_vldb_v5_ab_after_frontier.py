#!/usr/bin/env python3
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_v5_ab_after_frontier.sh")
WAIT_HELPER = Path(__file__).with_name("wait_for_stage_marker.sh")


class QueueVldbV5AbAfterFrontierTest(unittest.TestCase):
    def test_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_queue_is_fail_closed_before_ab(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('FRONTIER_UNIT=${FRONTIER_UNIT:-}', text)
        self.assertIn('"$TOOLING_DIR/wait_for_stage_marker.sh"', text)
        self.assertIn(
            'wait_for_stage_marker "$FRONTIER_COMPLETE" "$FRONTIER_UNIT"', text
        )
        self.assertIn('FRONTIER_COMPLETE="$FRONTIER_ROOT/SW_FRONTIER_COMPLETE.json"', text)
        self.assertIn('marker.get("kind") != "vldb_sw_frontier_complete_v1"', text)
        self.assertIn("for repeat in 1 2 3 4 5", text)
        self.assertIn("verify_vldb_frontier_sweep.py", text)
        self.assertIn("--expected-run-kind measure", text)
        self.assertIn("--min-points 5", text)
        self.assertLess(
            text.index('marker.get("kind") != "vldb_sw_frontier_complete_v1"'),
            text.index("install_remote_binary()"),
        )

    def test_runs_both_method_specific_controls(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn("run_ab slabwalk GIST1M 100 18100", text)
        self.assertIn("run_ab shine DEEP1M 100 18101", text)
        self.assertIn("REPEATS=6 CAMPAIGN_KIND=formal", text)
        self.assertIn("CAPTURE_BUILD_METRICS=0", text)
        self.assertIn("verify_vldb_binary_ab.py", text)

    def test_independent_verification_does_not_mutate_sealed_ab_root(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('local verification="$POST_ROOT/$(basename "$out").verification.json"', text)
        self.assertIn('> "$verification.tmp"', text)
        self.assertIn('mv "$verification.tmp" "$verification"', text)
        self.assertIn('vldb_evidence_bundle.py" verify --root "$out"', text)
        self.assertNotIn('$out/independent_verification.json', text)

    def test_ab_failures_are_explicitly_propagated(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('if ! env \\', text)
        self.assertIn('echo "$method $dataset A/B failed" >&2', text)
        self.assertIn('return 1', text)

    def test_successful_ab_stage_emits_an_atomic_completion_marker(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('AB_COMPLETE="$POST_ROOT/AB_COMPLETE.json"', text)
        self.assertIn('"kind": "vldb_binary_ab_complete_v1"', text)
        self.assertIn("os.replace(temporary, marker)", text)
        self.assertLess(text.index("run_ab shine DEEP1M"), text.index("os.replace(temporary, marker)"))

    def test_process_cleanup_never_uses_global_kills(self) -> None:
        text = SCRIPT.read_text()
        for forbidden in ("pkill", "killall", "kill -9"):
            self.assertNotIn(forbidden, text)

    def test_missing_frontier_marker_stops_before_ab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            frontier = tmp / "frontier"
            tooling = tmp / "tooling"
            source_a = tmp / "source-a"
            source_b = tmp / "source-b"
            fake_bin = tmp / "fake-bin"
            for directory in (frontier, tooling, source_a, source_b, fake_bin):
                directory.mkdir()
            for name in (
                "wait_for_stage_marker.sh",
                "vldb_evidence_bundle.py",
                "verify_vldb_frontier_sweep.py",
                "run_vldb_binary_ab.sh",
                "run_vldb_query_profile.sh",
                "verify_vldb_binary_ab.py",
                "summarize_vldb_materialization_policy.py",
            ):
                (tooling / name).write_text("placeholder\n")
            (tooling / "wait_for_stage_marker.sh").write_text(
                WAIT_HELPER.read_text()
            )
            binary_a = tmp / "baseline"
            binary_b = tmp / "candidate"
            binary_a.write_bytes(b"baseline")
            binary_b.write_bytes(b"candidate")
            binary_a.chmod(0o755)
            binary_b.chmod(0o755)
            (fake_bin / "hostname").write_text("#!/bin/sh\necho skv-node1\n")
            (fake_bin / "tmux").write_text("#!/bin/sh\nexit 1\n")
            (fake_bin / "hostname").chmod(0o755)
            (fake_bin / "tmux").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "FRONTIER_ROOT": str(frontier),
                    "POST_ROOT": str(tmp / "post"),
                    "TOOLING_DIR": str(tooling),
                    "SOURCE_ROOT_A": str(source_a),
                    "SOURCE_ROOT_B": str(source_b),
                    "BIN_A": str(binary_a),
                    "BIN_B": str(binary_b),
                    "SHA_A": hashlib.sha256(binary_a.read_bytes()).hexdigest(),
                    "SHA_B": hashlib.sha256(binary_b.read_bytes()).hexdigest(),
                    "WAIT_SECONDS": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)], env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(
                "frontier producer ended without a completion marker", result.stderr
            )
            self.assertFalse((tmp / "post").exists())


if __name__ == "__main__":
    unittest.main()
