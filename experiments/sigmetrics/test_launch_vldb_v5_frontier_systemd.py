import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("launch_vldb_v5_frontier_systemd.sh")


class V5FrontierSystemdLauncherTest(unittest.TestCase):
    def test_launcher_uses_persistent_service_and_fail_closed_inputs(self):
        text = SCRIPT.read_text()

        self.assertIn("systemd-run --user", text)
        self.assertIn("StandardOutput=append:$SERVICE_LOG", text)
        self.assertIn("StandardError=append:$SERVICE_LOG", text)
        self.assertIn("SW_FRONTIER_COMPLETE.json", text)
        self.assertIn("refusing incomplete frontier child", text)
        self.assertIn("sha256sum \"$GB_BIN\"", text)
        self.assertIn("--setenv=RESUME=1", text)
        self.assertIn("FRONTIER_LIFECYCLE_ROOT", text)
        self.assertIn('--setenv="FRONTIER_LIFECYCLE_ROOT=$FRONTIER_LIFECYCLE_ROOT"', text)
        self.assertNotIn("tmux", text)

    def test_refuses_an_unsealed_child_before_invoking_systemd(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            source = tmp / "source"
            runner = source / "experiments/sigmetrics/run_frontier_repeated.sh"
            runner.parent.mkdir(parents=True)
            runner.write_text("#!/usr/bin/env bash\nexit 0\n")
            runner.chmod(0o755)
            binary = tmp / "slabwalk"
            binary.write_text("candidate")
            binary.chmod(0o755)
            digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            root = tmp / "campaign"
            (root / "sw_r3").mkdir(parents=True)
            (root / "campaign.json").write_text("{}\n")
            fake_bin = tmp / "fake-bin"
            fake_bin.mkdir()
            marker = tmp / "systemd-invoked"
            fake_systemd = fake_bin / "systemd-run"
            fake_systemd.write_text(
                "#!/usr/bin/env bash\ntouch \"$SYSTEMD_INVOKED\"\nexit 0\n"
            )
            fake_systemd.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "ACTUAL_HOST": "skv-node1",
                    "SOURCE_ROOT": str(source),
                    "OUT_ROOT": str(root),
                    "GB_BIN": str(binary),
                    "EXPECTED_BINARY_SHA": digest,
                    "SYSTEMD_INVOKED": str(marker),
                }
            )
            proc = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(proc.returncode, 2)
            self.assertIn("refusing incomplete frontier child", proc.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
