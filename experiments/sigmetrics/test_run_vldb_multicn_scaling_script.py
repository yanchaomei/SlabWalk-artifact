import subprocess
import unittest
from pathlib import Path


class MultiCnRunnerScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = Path(__file__).with_name("run_vldb_multicn_scaling.sh")
        cls.text = cls.script.read_text()

    def test_shell_syntax_and_dry_run(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(self.script)], text=True, capture_output=True
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        dry = subprocess.run(
            ["bash", str(self.script)],
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "DRY_RUN": "1",
                "EXPECTED_SLABWALK_SHA": "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6",
            },
            text=True,
            capture_output=True,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("cn_counts=1 2 3", dry.stdout)
        self.assertIn("systems=SHINE SlabWalk d-HNSW", dry.stdout)
        self.assertIn("repeats=5", dry.stdout)

    def test_owns_processes_and_avoids_global_cleanup(self) -> None:
        for unsafe in ("killall", "pkill", "kill -9", "rm -rf"):
            self.assertNotIn(unsafe, self.text)
        self.assertIn("verify_remote_pid", self.text)
        self.assertIn("protocol_fingerprint", self.text)
        self.assertIn("query_canonical_sha256", self.text)
        self.assertIn("source_sha256", self.text)

    def test_protocol_is_fixed_and_three_system(self) -> None:
        self.assertIn('CN_COUNTS=${CN_COUNTS:-"1 2 3"}', self.text)
        self.assertIn('SYSTEMS=${SYSTEMS:-"SHINE SlabWalk d-HNSW"}', self.text)
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('--num-clients "$cn_count"', self.text)
        self.assertIn("--clients", self.text)
        self.assertIn('--client-log "$local_dir/c${rank}.stderr"', self.text)
        self.assertNotIn("--client-json", self.text)
        self.assertIn('rsync -aL --partial "$DH_BASE_PATH"', self.text)
        self.assertIn("prepare_dhnsw_runtime_bundle", self.text)
        self.assertIn("deploy_dhnsw_runtime_bundle", self.text)
        self.assertIn("DHNSW_RUNTIME_MANIFEST_SHA", self.text)
        self.assertIn("PARSER=$SCRIPT_DIR/parse_dhnsw_frontier.py", self.text)
        self.assertIn("RUNNER_SHA=$(sha256sum", self.text)
        self.assertIn('"tool_sha256": tool_sha256', self.text)
        self.assertIn('"dhnsw_required_metrics"', self.text)
        self.assertIn('"dhnsw_machine_record_recovery"', self.text)
        self.assertIn(
            "optional_known_Thread_or_numeric_metric_value_prefix_interleaving",
            self.text,
        )
        self.assertIn('"dhnsw_detail_metrics"', self.text)
        self.assertIn("atomic_FRONTIER_THREAD_RESULT", self.text)
        self.assertIn("best_effort_non_gating", self.text)
        self.assertIn('LD_LIBRARY_PATH="$DHNSW_REMOTE_RUNTIME/lib"', self.text)
        self.assertIn("sha256sum -c", self.text)
        self.assertIn("SSH=(ssh -n", self.text)
        self.assertIn(
            'local -a runtime_hosts=("${client_hosts[@]}" "$DHNSW_SERVER_HOST")',
            self.text,
        )
        self.assertIn('"set -e; test -x \'$rank_root/build/run_client\';', self.text)
        self.assertIn('"set -e; test -x \'$server_root/build/run_server\';', self.text)
        self.assertIn("assemble_vldb_multicn_scaling.py", self.text)


if __name__ == "__main__":
    unittest.main()
