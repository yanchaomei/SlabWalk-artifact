#!/usr/bin/env python3
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_v5_build_after_promotion.sh")
WAIT_HELPER = Path(__file__).with_name("wait_for_stage_marker.sh")


class QueueVldbV5BuildAfterPromotionTest(unittest.TestCase):
    def test_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_waits_for_finalizer_and_requires_positive_gate(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('FINALIZER_UNIT=${FINALIZER_UNIT:-}', text)
        self.assertIn('"$TOOLING_DIR/wait_for_stage_marker.sh"', text)
        self.assertIn(
            'wait_for_stage_marker "$FINALIZATION_COMPLETE" "$FINALIZER_UNIT"',
            text,
        )
        self.assertIn('FINALIZATION_COMPLETE=${FINALIZATION_COMPLETE:?', text)
        self.assertIn('marker.get("kind") != "vldb_finalization_complete_v1"', text)
        self.assertIn('marker.get("promotion_status") != 0', text)
        self.assertIn('report.get("kind") != "vldb_candidate_promotion_gate_v1"', text)
        self.assertIn('report.get("promotion_ready") is not True', text)
        self.assertIn('verification.get("binary_sha_b") != expected_sha', text)
        self.assertLess(
            text.index('report.get("promotion_ready") is not True'),
            text.index('bash "$TOOLING_DIR/run_slab_build_cost.sh"'),
        )

    def test_runs_and_revalidates_the_complete_build_matrix(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('DATASETS="SIFT1M DEEP1M GIST1M"', text)
        self.assertIn('REPEATS=5', text)
        self.assertIn('EXPECTED_BINARY_SHA="$SHA_B"', text)
        self.assertIn('EXPECTED_SOURCE_TREE_SHA="$SOURCE_TREE_B"', text)
        self.assertIn('--expected-source-tree-sha "$SOURCE_TREE_B"', text)
        self.assertIn('vldb_evidence_bundle.py" seal', text)
        self.assertIn('vldb_evidence_bundle.py" verify', text)
        self.assertIn("assemble_vldb_build_cost.py", text)
        self.assertIn("validate_build_cost", text)
        self.assertIn('VALIDATION="$OUT_ROOT/build_cost_candidate.validation.json"', text)
        self.assertNotIn('$BUNDLE/validation.json', text)

    def test_preflights_the_validator_import_closure(self) -> None:
        text = SCRIPT.read_text()
        for dependency in (
            "aggregate_frontier_repeats.py",
            "assemble_vldb_10m_build_scaling.py",
            "assemble_vldb_query_profile.py",
            "assemble_vldb_lifecycle_controls.py",
            "summarize_vldb_cache_control.py",
            "summarize_vldb_colocation_control.py",
            "summarize_vldb_mechanism_controls.py",
            "summarize_vldb_resource_ledger.py",
            "publication_metadata.py",
        ):
            self.assertIn(f'"$TOOLING_DIR/{dependency}"', text)

    def test_refuses_mutable_or_existing_outputs(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('[[ ! -e "$OUT_ROOT" ]]', text)
        self.assertIn('[[ ! -w "$TOOLING_DIR" ]]', text)
        for forbidden in ("pkill", "killall", "kill -9"):
            self.assertNotIn(forbidden, text)

    def test_missing_finalization_marker_stops_before_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            tooling = tmp / "tooling"
            source = tmp / "source"
            fake_bin = tmp / "fake-bin"
            for directory in (tooling, source, fake_bin):
                directory.mkdir()
            for name in (
                "wait_for_stage_marker.sh",
                "run_slab_build_cost.sh",
                "summarize_slab_build_cost.py",
                "assemble_vldb_build_cost.py",
                "validate_vldb_final_evidence.py",
                "vldb_evidence_bundle.py",
                "aggregate_frontier_repeats.py",
                "assemble_vldb_10m_build_scaling.py",
                "assemble_vldb_query_profile.py",
                "assemble_vldb_lifecycle_controls.py",
                "summarize_vldb_cache_control.py",
                "summarize_vldb_colocation_control.py",
                "summarize_vldb_mechanism_controls.py",
                "summarize_vldb_resource_ledger.py",
                "publication_metadata.py",
            ):
                (tooling / name).write_text("placeholder\n")
            (tooling / "wait_for_stage_marker.sh").write_text(
                WAIT_HELPER.read_text()
            )
            binary = tmp / "candidate"
            binary.write_bytes(b"candidate")
            binary.chmod(0o755)
            (fake_bin / "hostname").write_text("#!/bin/sh\necho skv-node1\n")
            (fake_bin / "tmux").write_text("#!/bin/sh\nexit 1\n")
            (fake_bin / "hostname").chmod(0o755)
            (fake_bin / "tmux").chmod(0o755)
            tooling.chmod(0o555)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "PROMOTION_GATE": str(tmp / "promotion.json"),
                    "FINALIZATION_COMPLETE": str(tmp / "finalization.json"),
                    "OUT_ROOT": str(tmp / "out"),
                    "TOOLING_DIR": str(tooling),
                    "GB_BIN": str(binary),
                    "GB_BIN_R": str(binary),
                    "SOURCE_ROOT": str(source),
                    "SHA_B": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "SOURCE_TREE_B": "a" * 64,
                    "WAIT_SECONDS": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)], env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(
                "finalizer producer ended without a completion marker", result.stderr
            )
            self.assertFalse((tmp / "out").exists())


if __name__ == "__main__":
    unittest.main()
