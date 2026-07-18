#!/usr/bin/env python3
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("queue_vldb_v5_finalize_after_ab.sh")
WAIT_HELPER = Path(__file__).with_name("wait_for_stage_marker.sh")


class QueueVldbV5FinalizeAfterAbTest(unittest.TestCase):
    def test_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_waits_for_ab_and_reverifies_all_inputs(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('AB_UNIT=${AB_UNIT:-}', text)
        self.assertIn('"$TOOLING_DIR/wait_for_stage_marker.sh"', text)
        self.assertIn('wait_for_stage_marker "$AB_COMPLETE" "$AB_UNIT"', text)
        self.assertIn('AB_COMPLETE="$AB_ROOT/AB_COMPLETE.json"', text)
        self.assertIn('marker.get("kind") != "vldb_binary_ab_complete_v1"', text)
        self.assertIn("for repeat in 1 2 3 4 5", text)
        self.assertIn("verify_vldb_frontier_sweep.py", text)
        self.assertIn("verify_vldb_binary_ab.py", text)
        self.assertIn("verify_ab slabwalk GIST1M", text)
        self.assertIn("verify_ab shine DEEP1M", text)
        self.assertIn('vldb_evidence_bundle.py" verify --root "$ab_root"', text)
        self.assertIn(
            '"$TOOLING_DIR/summarize_vldb_materialization_policy.py"', text
        )

    def test_finalizer_emits_a_decision_marker_for_pass_or_measured_rejection(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('FINALIZATION_COMPLETE="$OUT_ROOT/FINALIZATION_COMPLETE.json"', text)
        self.assertIn('"kind": "vldb_finalization_complete_v1"', text)
        self.assertIn('"promotion_status": promotion_status', text)
        self.assertIn("os.replace(temporary, marker)", text)
        self.assertLess(text.index("promotion_rc=$?"), text.index("os.replace(temporary, marker)"))

    def test_assembles_validates_compares_and_gates_in_order(self) -> None:
        text = SCRIPT.read_text()
        assemble = text.index("assemble_vldb_frontier_1m.py")
        validate = text.index("validate_vldb_frontier_1m.py")
        compare = text.index("compare_vldb_frontier_candidate.py")
        promote = text.index("validate_vldb_candidate_promotion.py")
        self.assertLess(assemble, validate)
        self.assertLess(validate, compare)
        self.assertLess(compare, promote)
        self.assertIn('--dhnsw-campaign "$CERTIFIED_FRONTIER"', text)
        self.assertIn('--query-pools "$CERTIFIED_FRONTIER/query_pools"', text)

    def test_negative_gate_is_preserved_as_a_decision_not_a_crash(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn("compare_rc=$?", text)
        self.assertIn("promotion_rc=$?", text)
        self.assertIn("compare_rc != 0 && compare_rc != 2", text)
        self.assertIn("promotion_rc != 0 && promotion_rc != 2", text)
        self.assertIn('exit "$promotion_rc"', text)

    def test_outputs_never_mutate_sealed_inputs(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('CANDIDATE="$OUT_ROOT/frontier_1m_candidate"', text)
        self.assertIn(
            'VALIDATION="$OUT_ROOT/frontier_1m_candidate.validation.json"', text
        )
        self.assertNotIn('$CANDIDATE/validation.json', text)
        for forbidden in ("pkill", "killall", "kill -9"):
            self.assertNotIn(forbidden, text)

    def test_missing_ab_marker_stops_before_finalization_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            frontier = tmp / "frontier"
            ab_root = tmp / "ab"
            certified = tmp / "certified"
            tooling = tmp / "tooling"
            fake_bin = tmp / "fake-bin"
            for directory in (frontier, ab_root, certified, tooling, fake_bin):
                directory.mkdir()
            for name in (
                "wait_for_stage_marker.sh",
                "vldb_evidence_bundle.py",
                "verify_vldb_frontier_sweep.py",
                "verify_vldb_binary_ab.py",
                "summarize_vldb_materialization_policy.py",
                "assemble_vldb_frontier_1m.py",
                "aggregate_frontier_repeats.py",
                "validate_vldb_frontier_1m.py",
                "compare_vldb_frontier_candidate.py",
                "validate_vldb_candidate_promotion.py",
                "publication_metadata.py",
            ):
                (tooling / name).write_text("placeholder\n")
            (tooling / "wait_for_stage_marker.sh").write_text(
                WAIT_HELPER.read_text()
            )
            manifest = certified / "SHA256SUMS"
            campaign = certified / "campaign.json"
            raw = certified / "frontier_repeated_raw.csv"
            manifest.write_bytes(b"manifest")
            campaign.write_bytes(b"campaign")
            raw.write_bytes(b"raw")
            (fake_bin / "hostname").write_text("#!/bin/sh\necho skv-node1\n")
            (fake_bin / "tmux").write_text("#!/bin/sh\nexit 1\n")
            (fake_bin / "hostname").chmod(0o755)
            (fake_bin / "tmux").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "FRONTIER_ROOT": str(frontier),
                    "AB_ROOT": str(ab_root),
                    "CERTIFIED_FRONTIER": str(certified),
                    "OUT_ROOT": str(tmp / "out"),
                    "TOOLING_DIR": str(tooling),
                    "CERTIFIED_MANIFEST_SHA": hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest(),
                    "CERTIFIED_CAMPAIGN_SHA": hashlib.sha256(
                        campaign.read_bytes()
                    ).hexdigest(),
                    "CERTIFIED_RAW_SHA": hashlib.sha256(raw.read_bytes()).hexdigest(),
                    "WAIT_SECONDS": "1",
                }
            )
            result = subprocess.run(
                ["bash", str(SCRIPT)], env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("A/B producer ended without a completion marker", result.stderr)
            self.assertFalse((tmp / "out").exists())


if __name__ == "__main__":
    unittest.main()
