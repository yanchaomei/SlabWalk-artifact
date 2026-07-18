#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name(
    "queue_vldb_v5_construction_after_finalization.sh"
)
WAIT_HELPER = Path(__file__).with_name("wait_for_stage_marker.sh")

BUILD_DEPENDENCIES = (
    "verify_vldb_construction_admission.py",
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
)


class QueueVldbV5ConstructionAfterFinalizationTest(unittest.TestCase):
    def test_shell_contract_is_marker_gated_and_scope_separated(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('FINALIZER_UNIT=${FINALIZER_UNIT:-}', text)
        self.assertIn('wait_for_stage_marker "$FINALIZATION_COMPLETE"', text)
        self.assertIn("validate_vldb_construction_candidate.py", text)
        self.assertIn("run_vldb_v5_build_after_construction_admission.sh", text)
        self.assertIn('status="not_needed_general_promotion"', text)
        self.assertIn('status="construction_not_admitted"', text)
        self.assertIn('status="construction_measurements_complete"', text)
        self.assertIn('ADMISSION_SCOPE="construction_measurements_only"', text)
        self.assertNotIn("promotion_ready=true", text)
        for forbidden in ("pkill", "killall", "kill -9"):
            self.assertNotIn(forbidden, text)

    def _run_case(
        self, *, promotion_ready: bool, admission_ready: bool
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        tmp_context = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_context.cleanup)
        tmp = Path(tmp_context.name)
        finalization = tmp / "finalization"
        certified = tmp / "certified"
        tooling = tmp / "tooling"
        source = tmp / "source"
        fake_bin = tmp / "fake-bin"
        for directory in (finalization, certified, tooling, source, fake_bin):
            directory.mkdir()

        candidate = tmp / "candidate"
        candidate.write_bytes(b"candidate")
        candidate.chmod(0o755)
        candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()

        promotion = finalization / "promotion_gate.json"
        promotion.write_text(
            json.dumps(
                {
                    "kind": "vldb_candidate_promotion_gate_v1",
                    "promotion_ready": promotion_ready,
                }
            )
            + "\n"
        )
        marker = finalization / "FINALIZATION_COMPLETE.json"
        marker.write_text(
            json.dumps(
                {
                    "kind": "vldb_finalization_complete_v1",
                    "promotion_status": 0 if promotion_ready else 2,
                    "promotion_ready": promotion_ready,
                    "candidate_binary_sha256": candidate_sha,
                    "promotion_report_sha256": hashlib.sha256(
                        promotion.read_bytes()
                    ).hexdigest(),
                }
            )
            + "\n"
        )
        (finalization / "frontier_comparison").mkdir()
        (finalization / "frontier_1m_candidate").mkdir()
        (finalization / "frontier_comparison" / "cells.csv").write_text(
            "dataset,method,ef\n"
        )
        (finalization / "frontier_1m_candidate" / "frontier_repeated_raw.csv").write_text(
            "dataset,method,ef\n"
        )
        (certified / "frontier_repeated_raw.csv").write_text(
            "dataset,method,ef\n"
        )

        (tooling / "wait_for_stage_marker.sh").write_text(WAIT_HELPER.read_text())
        validator = tooling / "validate_vldb_construction_candidate.py"
        validator.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
            f"ready = {admission_ready!r}\n"
            "out.write_text(json.dumps({'kind': 'vldb_construction_candidate_gate_v1', "
            "'construction_ready': ready, 'general_promotion_ready': False, "
            "'scope': 'construction_measurements_only'}) + '\\n')\n"
            "raise SystemExit(0 if ready else 2)\n"
        )
        runner = tooling / "run_vldb_v5_build_after_construction_admission.sh"
        runner.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "mkdir -p \"$OUT_ROOT\"\n"
            "printf '{\"kind\":\"vldb_v5_build_cost_complete_v1\","
            "\"scope\":\"construction_measurements_only\","
            "\"candidate_binary_sha256\":\"%s\","
            "\"candidate_source_tree_sha256\":\"%s\"}\\n' "
            "\"$SHA_B\" \"$SOURCE_TREE_B\" "
            "> \"$OUT_ROOT/BUILD_COST_COMPLETE.json\"\n"
        )
        for name in BUILD_DEPENDENCIES:
            (tooling / name).write_text("placeholder\n")

        (fake_bin / "hostname").write_text("#!/bin/sh\necho skv-node1\n")
        (fake_bin / "tmux").write_text("#!/bin/sh\nexit 1\n")
        (fake_bin / "hostname").chmod(0o755)
        (fake_bin / "tmux").chmod(0o755)
        tooling.chmod(0o555)

        control = tmp / "construction-control"
        output = tmp / "construction-output"
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FINALIZATION_ROOT": str(finalization),
                "FINALIZATION_COMPLETE": str(marker),
                "PROMOTION_GATE": str(promotion),
                "BASELINE_FRONTIER": str(certified / "frontier_repeated_raw.csv"),
                "CONTROL_ROOT": str(control),
                "OUT_ROOT": str(output),
                "TOOLING_DIR": str(tooling),
                "SOURCE_ROOT": str(source),
                "GB_BIN": str(candidate),
                "GB_BIN_R": str(candidate),
                "SHA_B": candidate_sha,
                "SOURCE_TREE_B": "a" * 64,
                "WAIT_SECONDS": "1",
                "CAMPAIGN_ID": "test-construction-fallback",
            }
        )
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True
        )
        return result, control, output

    def test_positive_general_promotion_skips_construction_fallback(self) -> None:
        result, control, output = self._run_case(
            promotion_ready=True, admission_ready=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads((control / "CONSTRUCTION_FALLBACK_COMPLETE.json").read_text())
        self.assertEqual(decision["status"], "not_needed_general_promotion")
        self.assertFalse(output.exists())
        self.assertFalse((control / "construction_candidate_gate.json").exists())

    def test_admitted_negative_runs_scoped_construction_measurements(self) -> None:
        result, control, output = self._run_case(
            promotion_ready=False, admission_ready=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads((control / "CONSTRUCTION_FALLBACK_COMPLETE.json").read_text())
        self.assertEqual(decision["status"], "construction_measurements_complete")
        self.assertEqual(decision["scope"], "construction_measurements_only")
        self.assertTrue((control / "construction_candidate_gate.json").is_file())
        self.assertTrue((output / "BUILD_COST_COMPLETE.json").is_file())

    def test_unadmitted_negative_preserves_no_build_boundary(self) -> None:
        result, control, output = self._run_case(
            promotion_ready=False, admission_ready=False
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        decision = json.loads((control / "CONSTRUCTION_FALLBACK_COMPLETE.json").read_text())
        self.assertEqual(decision["status"], "construction_not_admitted")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
