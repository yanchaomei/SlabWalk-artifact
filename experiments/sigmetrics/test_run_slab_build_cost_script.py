import os
import subprocess
import unittest
from pathlib import Path


class SlabBuildCostScriptTest(unittest.TestCase):
    def test_dry_run_uses_configured_memory_node(self) -> None:
        script = Path(__file__).with_name("run_slab_build_cost.sh")
        env = dict(os.environ)
        env.update({
            "BUILD_MN": "skv-node5",
            "DATASETS": "DEEP1M GIST1M",
            "REPEATS": "2",
            "DRY_RUN": "1",
        })
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("dataset=")]
        self.assertEqual(len(lines), 4)
        self.assertTrue(all("mn=skv-node5" in line for line in lines))
        self.assertTrue(all("SHINE_CRANE=1" in line for line in lines))
        self.assertTrue(
            all("SHINE_LAVD_STAGED_BUILD=1" in line for line in lines)
        )
        self.assertTrue(all("SHINE_LAVD_SELFTEST=1" in line for line in lines))

    def test_current_runner_checks_single_mn_staged_build_contract(self) -> None:
        script = Path(__file__).with_name("run_slab_build_cost.sh").read_text()
        self.assertNotIn("SHINE_CRANE=0", script)
        self.assertIn('obj["timings"]["lavd_build"]', script)
        self.assertIn('obj["timings"]["crane_build_multi"]', script)
        self.assertIn("LAVD_BUILD_PUBLICATION", script)
        self.assertIn("reused authoritative build snapshot", script)
        self.assertIn("EXPECTED_SOURCE_TREE_SHA", script)
        self.assertIn('"graphbeyond/rdma-library/library"', script)
        self.assertIn('"graphbeyond/thirdparty"', script)
        self.assertIn('"source": {', script)

    def test_campaign_can_bind_a_construction_only_admission_gate(self) -> None:
        script = Path(__file__).with_name("run_slab_build_cost.sh").read_text()
        self.assertIn("ADMISSION_GATE", script)
        self.assertIn("EXPECTED_ADMISSION_GATE_SHA", script)
        self.assertIn("construction_measurements_only", script)
        self.assertIn('"admission": admission', script)

    def test_rejects_invalid_expected_binary_sha(self) -> None:
        script = Path(__file__).with_name("run_slab_build_cost.sh")
        env = dict(os.environ)
        env.update({
            "EXPECTED_BINARY_SHA": "not-a-sha",
            "DRY_RUN": "1",
        })
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EXPECTED_BINARY_SHA", result.stderr)

    def test_rejects_invalid_expected_source_tree_sha(self) -> None:
        script = Path(__file__).with_name("run_slab_build_cost.sh")
        env = dict(os.environ)
        env.update({
            "EXPECTED_SOURCE_TREE_SHA": "not-a-sha",
            "DRY_RUN": "1",
        })
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EXPECTED_SOURCE_TREE_SHA", result.stderr)


if __name__ == "__main__":
    unittest.main()
