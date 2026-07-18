import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import vldb_evidence_bundle as evidence


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "run_vldb_query_profile.sh"


class VldbQueryProfileRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_memory_nodes_are_explicitly_overridable(self) -> None:
        self.assertIn("MN_SIFT1M=${MN_SIFT1M:-skv-node5}", self.text)
        self.assertIn("MN_GIST1M=${MN_GIST1M:-skv-node3}", self.text)
        self.assertIn("MN_DEEP1M=${MN_DEEP1M:-skv-node5}", self.text)
        self.assertIn("MN_DEEP10M=${MN_DEEP10M:-skv-node5}", self.text)
        self.assertIn(
            'SIFT1M) printf \'%s|%s|%s|%s|%s|%s|%s\\n\' "$MN_SIFT1M"',
            self.text,
        )
        self.assertIn(
            'GIST1M) printf \'%s|%s|%s|%s|%s|%s|%s\\n\' "$MN_GIST1M"',
            self.text,
        )
        self.assertIn(
            'DEEP1M) printf \'%s|%s|%s|%s|%s|%s|%s\\n\' "$MN_DEEP1M"',
            self.text,
        )
        self.assertIn(
            'DEEP10M) printf \'%s|%s|%s|%s|%s|%s|%s\\n\' "$MN_DEEP10M"',
            self.text,
        )

    def test_campaign_records_the_resolved_memory_nodes(self) -> None:
        self.assertIn('"memory_nodes_by_dataset": {', self.text)
        self.assertIn('"SIFT1M": mn_sift1m,', self.text)
        self.assertIn('"GIST1M": mn_gist1m,', self.text)
        self.assertIn('"DEEP1M": mn_deep1m,', self.text)
        self.assertIn('"DEEP10M": mn_deep10m,', self.text)

    def test_gist_profile_matches_the_formal_frontier_layout(self) -> None:
        self.assertIn("$GB_DATA/gist1m", self.text)
        self.assertIn("LAVD_GIST1_REGION_BYTES=${LAVD_GIST1_REGION_BYTES:-9663676416}", self.text)
        self.assertIn("SHINE_LAVD_RABITQ_B=2", self.text)
        self.assertIn("SHINE_LAVD_STAGED_BUILD=1", self.text)
        self.assertIn("SHINE_LAVD_SELFTEST=1", self.text)
        self.assertIn("$INDEX_REGION_1M_BYTES\" u10k", self.text)

    def test_owns_processes_without_global_kills(self) -> None:
        self.assertIn("verify_remote_pid", self.text)
        self.assertIn('/proc/\\$pid/exe', self.text)
        self.assertIn('kill $pid', self.text)
        for forbidden in ("pkill", "pgrep", "killall", "kill -9"):
            self.assertNotIn(forbidden, self.text)

    def test_watchdog_does_not_leave_an_orphaned_sleep(self) -> None:
        self.assertNotIn('( sleep "$TIMEOUT_S";', self.text)
        self.assertIn(
            'python3 - "$TIMEOUT_S" "$cn_pid" "$cn_starttime" "$expected"',
            self.text,
        )
        self.assertIn("except ProcessLookupError:", self.text)

    def test_process_identity_includes_binary_digests(self) -> None:
        self.assertIn("sha256sum /proc/\\$pid/exe", self.text)
        self.assertIn('"$GB_BIN_SHA256"', self.text)
        self.assertIn("verify_local_cn_pid", self.text)
        self.assertIn("actual_sha", self.text)

    def test_process_identity_is_bound_to_pid_starttime(self) -> None:
        self.assertIn("server.starttime", self.text)
        self.assertIn("expected_starttime", self.text)
        self.assertIn("ACTIVE_CN_STARTTIME", self.text)
        self.assertIn("/proc/$pid/stat", self.text)

    def test_memory_node_shutdown_is_confirmed_before_log_copy(self) -> None:
        stop = self.text.index('stop_mn "$mn" "$remote_dir"')
        copy = self.text.index('scp -q "$mn:$remote_dir/mn.out"')
        self.assertLess(stop, copy)
        self.assertIn("Memory-node process did not terminate", self.text)
        self.assertNotIn("stop_mn \"$mn\" \"$remote_dir\" || true", self.text)

    def test_memory_node_logs_are_mandatory(self) -> None:
        self.assertIn('"$mn:$remote_dir/mn.out" "$OUT/$tag.mn.out"', self.text)
        self.assertIn('"$mn:$remote_dir/mn.err" "$OUT/$tag.mn.err"', self.text)
        self.assertNotIn(
            'scp -q "$mn:$remote_dir/mn.err" "$OUT/$tag.mn.err" 2>/dev/null || true',
            self.text,
        )

    def test_each_run_emits_execution_and_input_provenance(self) -> None:
        self.assertIn('"$OUT/$tag.provenance.json"', self.text)
        self.assertIn('"schema_version": 1', self.text)
        self.assertIn('"compute_node": {', self.text)
        self.assertIn('"memory_nodes": [', self.text)
        self.assertIn('"input_signature": input_signature', self.text)
        self.assertIn('"ground_truth": ground_truth', self.text)
        self.assertIn('"index": [', self.text)

    def test_compute_host_is_bound_to_campaign_and_process_provenance(self) -> None:
        self.assertIn("COMPUTE_HOST=$(hostname)", self.text)
        self.assertIn('"compute_host": host', self.text)
        self.assertIn('"host": cn_host', self.text)
        self.assertIn("query-profile compute host drift", self.text)

    def test_each_cell_revalidates_inputs_after_execution(self) -> None:
        self.assertIn("verify_cell_inputs", self.text)
        self.assertGreaterEqual(self.text.count("verify_cell_inputs"), 3)
        self.assertIn('"pre_run"', self.text)
        self.assertIn('"post_run"', self.text)

    def test_runner_uses_immutable_harness_and_seals_bundle(self) -> None:
        for token in (
            "vldb_evidence_bundle.py",
            "VLDB_QUERY_PROFILE_HARNESS_FROZEN",
            "snapshot",
            "verify-harness",
            "HARNESS_MANIFEST_SHA256",
            "seal --root",
            "verify --root",
            "SEALED.json",
        ):
            self.assertIn(token, self.text)

    def test_dry_run_produces_a_recursively_verified_sealed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            output = Path(tmp_s) / "campaign"
            env = os.environ.copy()
            true_binary = shutil.which("true")
            self.assertIsNotNone(true_binary)
            env.update(
                {
                    "OUT": str(output),
                    "CAMPAIGN_ID": "query-profile-contract",
                    "GB_BIN": str(true_binary),
                    "GB_BIN_R": str(true_binary),
                    "DATASETS": "DEEP1M",
                    "METHODS": "shine",
                    "DRY_RUN": "1",
                    "CAPTURE_PERF": "0",
                }
            )
            subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=HERE.parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue((output / "SEALED.json").is_file())
            evidence.verify_bundle(output)


if __name__ == "__main__":
    unittest.main()
