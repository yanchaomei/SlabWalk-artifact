import os
import subprocess
import unittest
from pathlib import Path


class MaterializationPolicyScriptContractTest(unittest.TestCase):
    def test_runner_carries_exact_byte_and_provenance_contract(self):
        script = Path(__file__).with_name("run_vldb_materialization_policy.sh")
        text = script.read_text()

        for token in (
            "SHINE_LAVD_BUDGET_BYTES",
            "SHINE_LAVD_HOTSET",
            "SHINE_LAVD_NATIVE_PACKED_WRITE=1",
            "SHINE_LAVD_VARBLOCK=1",
            "SHINE_LAVD_STAGED_BUILD",
            "EXPECTED_BINARY_SHA",
            "protocol_fingerprint",
            "summarize_vldb_materialization_policy.py",
            "benefit indeg hop",
            "server.pid",
            "server.exe",
            "GB_PHASEF_LOG",
            "phasef_sha256",
            "server.sha256",
            "sha256sum /proc/",
            "input_signature",
            "verify_input_manifest",
            "SHA256SUMS",
            "vldb_evidence_bundle.py",
            "VLDB_MATERIALIZATION_HARNESS_FROZEN",
            "verify-harness",
            "HARNESS_MANIFEST_SHA256",
            "server.starttime",
            "ACTIVE_CN_STARTTIME",
            'verify_input_manifest "$dataset" "pre_run"',
            'verify_input_manifest "$dataset" "post_run"',
            "seal --root",
            "verify --root",
            "SEALED.json",
            '"graphbeyond/rdma-library/FindIBVerbs.cmake"',
            '"graphbeyond/thirdparty"',
            "COMPUTE_HOST=$(hostname)",
            '"compute_host": compute_host',
            '"host": compute_host',
            "materialization compute host drift",
            "VLDB_EVIDENCE_BUNDLE_MODULE",
        ):
            self.assertIn(token, text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)
        self.assertNotIn(r"\\$1", text)
        self.assertNotIn(
            'scp -q "$MEMORY_NODE:$remote_dir/mn.err" "$cell_dir/mn.err" || true',
            text,
        )
        self.assertNotIn(
            'scp -q "$MEMORY_NODE:$remote_dir/mn.out" "$cell_dir/mn.out" || true',
            text,
        )
        self.assertIn('--campaign "$OUT_ROOT/campaign.json"', text)
        self.assertIn(
            "SUMMARIZER=${SUMMARIZER:-$MATERIALIZATION_SOURCE_SCRIPT_DIR/summarize_vldb_materialization_policy.py}",
            text,
        )
        self.assertGreaterEqual(text.count("VLDB_EVIDENCE_BUNDLE_MODULE"), 2)

    def test_runner_does_not_use_a_handwritten_final_manifest(self):
        text = Path(__file__).with_name(
            "run_vldb_materialization_policy.sh"
        ).read_text()
        self.assertNotIn('output = root / "SHA256SUMS"', text)
        self.assertNotIn("stop_remote \"$remote_dir\" || true", text)

    def test_runner_dry_run_works_with_system_bash(self):
        script = Path(__file__).with_name("run_vldb_materialization_policy.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "DATASETS": "DEEP1M",
                "POLICIES": "benefit indeg hop",
                "BUDGET_BYTES": "536870912",
                "CAMPAIGN_KIND": "smoke",
                "REPEATS": "1",
                "WARMUPS": "0",
                "THREADS": "1",
                "QUERY_CONTEXTS": "1",
                "BUILD_THREADS": "7",
            }
        )
        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("datasets=DEEP1M", completed.stdout)
        self.assertIn("build_threads=7", completed.stdout)
        self.assertIn("campaign_kind=smoke", completed.stdout)

    def test_default_formal_campaign_completes_policy_position_balance(self):
        script = Path(__file__).with_name("run_vldb_materialization_policy.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "DATASETS": "DEEP1M",
                "BUDGET_BYTES": "536870912",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("campaign_kind=formal", completed.stdout)
        self.assertIn("repeats=6", completed.stdout)

    def test_gist_uses_the_complete_authoritative_u10k_query_pool(self):
        script = Path(__file__).with_name("run_vldb_materialization_policy.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "DATASETS": "GIST1M",
                "POLICIES": "benefit indeg hop",
                "BUDGET_BYTES": "536870912",
                "CAMPAIGN_KIND": "smoke",
                "REPEATS": "1",
                "WARMUPS": "0",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "dataset_spec=GIST1M:u10k:query-u10k.fbin:"
            "groundtruth-u10k.bin:10000",
            completed.stdout,
        )

    def test_formal_campaign_rejects_incomplete_policy_position_balance(self):
        script = Path(__file__).with_name("run_vldb_materialization_policy.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "DATASETS": "DEEP1M",
                "POLICIES": "benefit indeg hop",
                "BUDGET_BYTES": "536870912",
                "CAMPAIGN_KIND": "formal",
                "REPEATS": "2",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("position-balanced", completed.stderr)


if __name__ == "__main__":
    unittest.main()
