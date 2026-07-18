from __future__ import annotations

import unittest

from experiments.sigmetrics import summarize_vldb_rss_sidecar as rss


class VldbRssSidecarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cells = [
            {
                "repeat": 1,
                "position": 1,
                "variant": "A",
                "label": "serial_records",
                "pid": 101,
                "starttime": "9001",
                "staged_build": 0,
                "budget_bytes": 4096,
            },
            {
                "repeat": 2,
                "position": 1,
                "variant": "B",
                "label": "staged_64m",
                "pid": 202,
                "starttime": "9002",
                "staged_build": 1,
                "budget_bytes": 4096,
            },
        ]
        self.samples = [
            {
                "timestamp_utc": "2026-07-16T00:00:00+00:00",
                "pid": 101,
                "starttime": "9001",
                "staged_build": 0,
                "budget_bytes": 4096,
                "vmrss_kib": 100,
                "vmhwm_kib": 120,
                "vmsize_kib": 1000,
            },
            {
                "timestamp_utc": "2026-07-16T00:00:01+00:00",
                "pid": 101,
                "starttime": "9001",
                "staged_build": 0,
                "budget_bytes": 4096,
                "vmrss_kib": 110,
                "vmhwm_kib": 130,
                "vmsize_kib": 1000,
            },
            {
                "timestamp_utc": "2026-07-16T00:00:02+00:00",
                "pid": 202,
                "starttime": "9002",
                "staged_build": 1,
                "budget_bytes": 4096,
                "vmrss_kib": 80,
                "vmhwm_kib": 90,
                "vmsize_kib": 900,
            },
            {
                "timestamp_utc": "2026-07-16T00:00:03+00:00",
                "pid": 202,
                "starttime": "9002",
                "staged_build": 1,
                "budget_bytes": 4096,
                "vmrss_kib": 85,
                "vmhwm_kib": 95,
                "vmsize_kib": 900,
            },
        ]

    def test_correlates_pid_starttime_and_summarizes_peak_hwm(self) -> None:
        runs, summary = rss.correlate_samples(
            self.samples,
            self.cells,
            min_serial=1,
            min_staged=1,
            min_samples_per_process=2,
        )

        self.assertEqual(len(runs), 2)
        by_variant = {row["variant"]: row for row in runs}
        self.assertEqual(by_variant["A"]["peak_vmhwm_kib"], 130)
        self.assertEqual(by_variant["B"]["peak_vmhwm_kib"], 95)
        summary_by_variant = {row["variant"]: row for row in summary}
        self.assertEqual(summary_by_variant["A"]["n"], 1)
        self.assertEqual(summary_by_variant["B"]["peak_vmhwm_mean_kib"], 95)

    def test_rejects_unknown_process_starttime(self) -> None:
        self.samples[0]["starttime"] = "9999"
        with self.assertRaisesRegex(ValueError, "not bound"):
            rss.correlate_samples(
                self.samples,
                self.cells,
                min_serial=1,
                min_staged=1,
                min_samples_per_process=1,
            )

    def test_rejects_staged_flag_or_budget_drift(self) -> None:
        self.samples[-1]["staged_build"] = 0
        with self.assertRaisesRegex(ValueError, "configuration drift"):
            rss.correlate_samples(
                self.samples,
                self.cells,
                min_serial=1,
                min_staged=1,
                min_samples_per_process=1,
            )

    def test_requires_declared_coverage(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage"):
            rss.correlate_samples(
                self.samples,
                self.cells,
                min_serial=2,
                min_staged=1,
                min_samples_per_process=1,
            )


if __name__ == "__main__":
    unittest.main()
