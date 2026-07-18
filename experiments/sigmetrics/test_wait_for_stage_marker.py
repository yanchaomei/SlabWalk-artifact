#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).with_name("wait_for_stage_marker.sh")


class WaitForStageMarkerTest(unittest.TestCase):
    def run_wait(
        self,
        root: Path,
        *,
        unit: str = "",
        session: str = "",
        marker_exists: bool = False,
        systemd_state: str = "",
        tmux_status: int = 1,
        marker_on_sleep: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        marker = root / "COMPLETE.json"
        if marker_exists:
            marker.write_text("{}\n")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            + "printf '%s\\n' \"$FAKE_SYSTEMD_STATE\"\n"
        )
        tmux = fake_bin / "tmux"
        tmux.write_text(f"#!/usr/bin/env bash\nexit {tmux_status}\n")
        sleep = fake_bin / "sleep"
        if marker_on_sleep:
            sleep.write_text("#!/usr/bin/env bash\nprintf '{}\\n' > \"$WAIT_MARKER\"\n")
        else:
            sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
        for executable in (systemctl, tmux, sleep):
            executable.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_SYSTEMD_STATE": systemd_state,
                "WAIT_MARKER": str(marker),
            }
        )
        command = (
            f"source {HELPER!s}\n"
            f"wait_for_stage_marker {marker!s} {unit!r} {session!r} 1 test-stage\n"
        )
        return subprocess.run(
            ["bash", "-c", command],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_existing_marker_returns_without_consulting_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            result = self.run_wait(Path(tmp_s), marker_exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_active_systemd_unit_waits_until_marker_is_durable(self) -> None:
        state = "\n".join(
            (
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=running",
                "ExecMainStatus=0",
                "Result=success",
            )
        )
        with tempfile.TemporaryDirectory() as tmp_s:
            result = self.run_wait(
                Path(tmp_s),
                unit="frontier",
                systemd_state=state,
                marker_on_sleep=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_systemd_unit_without_marker_fails_closed(self) -> None:
        state = "\n".join(
            (
                "LoadState=loaded",
                "ActiveState=failed",
                "SubState=failed",
                "ExecMainStatus=2",
                "Result=exit-code",
            )
        )
        with tempfile.TemporaryDirectory() as tmp_s:
            result = self.run_wait(Path(tmp_s), unit="frontier", systemd_state=state)
        self.assertEqual(result.returncode, 2)
        self.assertIn("test-stage producer ended without a completion marker", result.stderr)
        self.assertIn("ExecMainStatus=2", result.stderr)

    def test_tmux_fallback_waits_for_legacy_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            result = self.run_wait(
                Path(tmp_s), session="legacy", tmux_status=0, marker_on_sleep=True
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_supervisor_and_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            result = self.run_wait(Path(tmp_s))
        self.assertEqual(result.returncode, 2)
        self.assertIn("test-stage producer ended without a completion marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
