#!/usr/bin/env python3
import math
import unittest
from pathlib import Path

import summarize_vldb_resource_ledger as ledger


class ResourceLedgerParserTest(unittest.TestCase):
    def test_parses_time_v_peak_rss(self):
        text = "Maximum resident set size (kbytes): 8929604\n"
        self.assertEqual(ledger.parse_max_rss_kib(text), 8929604)

    def test_parses_all_physical_accounts(self):
        text = "\n".join(
            [
                'LAVD_PHYSICAL_ACCOUNTING {"mn":0,"materialized_bytes":20,"actual_write_bytes":18}',
                'LAVD_PHYSICAL_ACCOUNTING {"mn":1,"materialized_bytes":21,"actual_write_bytes":19}',
            ]
        )
        rows = ledger.parse_accounting(text)
        self.assertEqual([row["mn"] for row in rows], [0, 1])
        self.assertEqual(sum(row["materialized_bytes"] for row in rows), 41)

    def test_student_t_confidence_interval(self):
        half = ledger.t_ci_half([10.0, 12.0, 14.0, 16.0, 18.0])
        self.assertTrue(math.isclose(half, 3.926, rel_tol=0.01))

    def test_staged_read_extents_are_the_measured_authoritative_bytes(self):
        text = "\n".join(
            [
                "[LAVD][multi] MN 0 staged-read 100B via 64B MR",
                "[LAVD][multi] MN 1 staged-read 220B via 64B MR",
                "[LAVD][multi] MN 2 staged-read 330B via 64B MR",
            ]
        )
        self.assertEqual(ledger.parse_staged_read_bytes(text, 3), [100, 220, 330])

    def test_staged_read_extents_require_exact_mn_coverage(self):
        text = "[LAVD][multi] MN 0 staged-read 100B via 64B MR\n"
        with self.assertRaisesRegex(ValueError, "cover all MNs"):
            ledger.parse_staged_read_bytes(text, 2)

    def test_selects_the_single_available_resident_build_timing(self):
        self.assertEqual(
            ledger.required_one_of_numbers(
                {"crane_build_multi": 12.5},
                ("crane_build_multi", "crane_build"),
                Path("run.json"),
            ),
            12.5,
        )
        self.assertEqual(
            ledger.required_one_of_numbers(
                {"crane_build": 13.5},
                ("crane_build_multi", "crane_build"),
                Path("run.json"),
            ),
            13.5,
        )

    def test_rejects_ambiguous_resident_build_timings(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ledger.required_one_of_numbers(
                {"crane_build_multi": 12.5, "crane_build": 13.5},
                ("crane_build_multi", "crane_build"),
                Path("run.json"),
            )

    def test_manifest_protocol_ignores_only_run_identity(self):
        base = "\n".join(
            [
                "tag=gist1m_fixed_s1_measure_r0",
                "layout=fixed",
                "memory_nodes=1",
                "hosts=skv-node3",
                "capacity_per_mn=9663676416",
                "index_region_bytes=4294967296",
                "binary_sha256=abc123",
                "cn_host=skv-node1",
                "started_utc=2026-07-12T22:34:24Z",
                "layout_env=SHINE_LAVD_NATIVE_PACKED_WRITE=1",
                "build_threads=20",
                "build_cpu_base=1",
                "build_cpu_stride=2",
            ]
        )
        changed_identity = base.replace("r0", "r1").replace("22:34:24", "22:40:24")
        manifest0 = ledger.parse_manifest_text(base, Path("manifest0.txt"))
        manifest1 = ledger.parse_manifest_text(changed_identity, Path("manifest1.txt"))
        self.assertEqual(
            ledger.manifest_cell_fingerprint(manifest0),
            ledger.manifest_cell_fingerprint(manifest1),
        )

    def test_matrix_rejects_protocol_drift_between_repeats(self):
        runs = [
            {
                "layout": "fixed",
                "memory_nodes": 1,
                "repeat": 0,
                "manifest_cell_fingerprint": "cell-a",
                "campaign_protocol_fingerprint": "campaign-a",
            },
            {
                "layout": "fixed",
                "memory_nodes": 1,
                "repeat": 1,
                "manifest_cell_fingerprint": "cell-b",
                "campaign_protocol_fingerprint": "campaign-a",
            },
        ]
        with self.assertRaisesRegex(ValueError, "manifest drift"):
            ledger.validate_matrix(runs, ["fixed"], [1], 2)

    def test_matrix_rejects_campaign_protocol_drift(self):
        runs = [
            {
                "layout": "fixed",
                "memory_nodes": 1,
                "repeat": 0,
                "manifest_cell_fingerprint": "cell-a",
                "campaign_protocol_fingerprint": "campaign-a",
            },
            {
                "layout": "variable",
                "memory_nodes": 1,
                "repeat": 0,
                "manifest_cell_fingerprint": "cell-b",
                "campaign_protocol_fingerprint": "campaign-b",
            },
        ]
        with self.assertRaisesRegex(ValueError, "campaign protocol drift"):
            ledger.validate_matrix(runs, ["fixed", "variable"], [1], 1)

    def test_validates_complete_monotonic_latency_quantiles(self):
        parsed = ledger.parse_query_latency(
            {
                "local_latency_samples": 10000,
                "local_latency_p50_us": 100.0,
                "local_latency_p95_us": 250.0,
                "local_latency_p99_us": 500.0,
            },
            processed=10000,
            required=True,
            source=Path("run.json"),
        )
        self.assertEqual(parsed["query_latency_p99_us"], 500.0)

    def test_rejects_missing_required_latency_quantiles(self):
        with self.assertRaisesRegex(ValueError, "latency samples"):
            ledger.parse_query_latency({}, 10000, True, Path("run.json"))


if __name__ == "__main__":
    unittest.main()
