from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import sample_vldb_process_rss as sampler


class VldbProcessRssSamplerTest(unittest.TestCase):
    def test_reads_identity_configuration_and_status_from_proc_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "candidate"
            binary.write_bytes(b"binary")
            process = root / "proc" / "123"
            process.mkdir(parents=True)
            (process / "exe").symlink_to(binary)
            fields = ["S"] + ["0"] * 18 + ["777"] + ["0"] * 8
            (process / "stat").write_text("123 (candidate) " + " ".join(fields))
            (process / "environ").write_bytes(
                b"SHINE_LAVD_STAGED_BUILD=1\0"
                b"SHINE_LAVD_BUDGET_BYTES=42949672960\0"
            )
            (process / "status").write_text(
                "VmRSS:\t100 KiB\nVmHWM:\t120 KiB\nVmSize:\t1000 KiB\n"
            )

            rows = sampler.collect_samples(
                root / "proc",
                binary.resolve(),
                "2026-07-16T00:00:00+00:00",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pid"], 123)
            self.assertEqual(rows[0]["starttime"], "777")
            self.assertEqual(rows[0]["staged_build"], "1")
            self.assertEqual(rows[0]["budget_bytes"], "42949672960")
            self.assertEqual(rows[0]["vmhwm_kib"], 120)

    def test_ignores_other_executables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "candidate"
            other = root / "other"
            binary.write_bytes(b"candidate")
            other.write_bytes(b"other")
            process = root / "proc" / "7"
            process.mkdir(parents=True)
            (process / "exe").symlink_to(other)

            self.assertEqual(
                sampler.collect_samples(root / "proc", binary.resolve(), "now"),
                [],
            )


if __name__ == "__main__":
    unittest.main()
