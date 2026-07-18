import unittest

import validate_vldb_construction_candidate as gate


ALLOWED_CELL = ("GIST1M", "SlabWalk", 100.0)


def promotion_report():
    return {
        "kind": "vldb_candidate_promotion_gate_v1",
        "promotion_ready": False,
        "failures": ["frontier_comparison"],
        "frontier": {
            "kind": "vldb_frontier_candidate_comparison_v1",
            "promotion_ready": False,
            "compared_cells": 70,
            "invariant_failures": 0,
            "performance_failures": 1,
            "thresholds": {
                "min_qps_ratio": 0.95,
                "min_qps_ci_low": 0.90,
                "max_p99_ratio": 1.10,
                "max_p99_ci_high": 1.25,
            },
        },
        "binary_ab": {
            "slabwalk": {"ready": True, "failures": []},
            "shine": {"ready": True, "failures": []},
        },
    }


def comparison_cells():
    rows = []
    for index in range(70):
        row = {
            "dataset": "SIFT1M",
            "method": "SHINE",
            "ef": 48.0 + index,
            "invariant_ok": 1,
            "performance_ok": 1,
            "qps_mean_ratio": 1.0,
            "qps_ratio_ci95_low": 0.99,
            "p99_mean_ratio": 1.0,
            "p99_ratio_ci95_high": 1.02,
        }
        rows.append(row)
    rows[17].update(
        {
            "dataset": ALLOWED_CELL[0],
            "method": ALLOWED_CELL[1],
            "ef": ALLOWED_CELL[2],
            "performance_ok": 0,
            "qps_mean_ratio": 0.99,
            "qps_ratio_ci95_low": 0.96,
            "p99_mean_ratio": 1.23,
            "p99_ratio_ci95_high": 2.1,
        }
    )
    return rows


def frontier_rows(p99_values):
    rows = []
    for repeat, p99_us in enumerate(p99_values, 1):
        rows.append(
            {
                "dataset": ALLOWED_CELL[0],
                "method": ALLOWED_CELL[1],
                "ef": ALLOWED_CELL[2],
                "run_id": f"r{repeat}",
                "p99_us": float(p99_us),
                "qps": 8100.0,
            }
        )
    return rows


class ConstructionCandidateGateTest(unittest.TestCase):
    def test_accepts_one_retained_tail_when_all_other_controls_pass(self):
        report = gate.evaluate_construction_candidate(
            promotion_report(),
            comparison_cells(),
            frontier_rows([6528.722, 2971.349, 2935.27, 3002.0, 2998.0]),
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertTrue(report["construction_ready"])
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["tail_control"]["tail_run_ids"], ["r1"])
        self.assertEqual(report["tail_control"]["normal_run_count"], 4)
        self.assertFalse(report["general_promotion_ready"])

    def test_rejects_a_second_cross_date_performance_failure(self):
        cells = comparison_cells()
        cells[3]["performance_ok"] = 0
        promotion = promotion_report()
        promotion["frontier"]["performance_failures"] = 2

        report = gate.evaluate_construction_candidate(
            promotion,
            cells,
            frontier_rows([6528.722, 2971.349, 2935.27, 3002.0, 2998.0]),
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertFalse(report["construction_ready"])
        self.assertIn("frontier_failure_shape", report["failures"])

    def test_rejects_invariant_or_same_host_ab_failure(self):
        promotion = promotion_report()
        promotion["frontier"]["invariant_failures"] = 1
        promotion["binary_ab"]["slabwalk"] = {
            "ready": False,
            "failures": ["p99_regression"],
        }

        report = gate.evaluate_construction_candidate(
            promotion,
            comparison_cells(),
            frontier_rows([6528.722, 2971.349, 2935.27, 3002.0, 2998.0]),
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertFalse(report["construction_ready"])
        self.assertIn("query_work_invariants", report["failures"])
        self.assertIn("slabwalk_ab", report["failures"])

    def test_rejects_two_tail_runs_or_an_unstable_normal_run(self):
        for values in (
            [6528.722, 6100.0, 2935.27, 3002.0, 2998.0],
            [6528.722, 2971.349, 2935.27, 3400.0, 2998.0],
        ):
            with self.subTest(values=values):
                report = gate.evaluate_construction_candidate(
                    promotion_report(),
                    comparison_cells(),
                    frontier_rows(values),
                    frontier_rows(
                        [3002.755, 3005.034, 2980.215, 2988.734, 3031.164]
                    ),
                    allowed_cell=ALLOWED_CELL,
                )
                self.assertFalse(report["construction_ready"])
                self.assertIn("isolated_tail", report["failures"])

    def test_rejects_an_isolated_tail_in_any_run_other_than_retained_r1(self):
        report = gate.evaluate_construction_candidate(
            promotion_report(),
            comparison_cells(),
            frontier_rows([3000.0, 6528.722, 2935.27, 3002.0, 2998.0]),
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertFalse(report["construction_ready"])
        self.assertIn("retained_tail_identity", report["failures"])

    def test_rejects_a_qps_failure_disguised_as_the_allowed_tail_cell(self):
        cells = comparison_cells()
        cells[17]["qps_ratio_ci95_low"] = 0.80

        report = gate.evaluate_construction_candidate(
            promotion_report(),
            cells,
            frontier_rows([6528.722, 2971.349, 2935.27, 3002.0, 2998.0]),
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertFalse(report["construction_ready"])
        self.assertIn("allowed_cell_not_p99_only", report["failures"])

    def test_rejects_duplicate_or_incomplete_repeat_identity(self):
        candidate = frontier_rows([6528.722, 2971.349, 2935.27, 3002.0, 2998.0])
        candidate[-1]["run_id"] = "r4"

        report = gate.evaluate_construction_candidate(
            promotion_report(),
            comparison_cells(),
            candidate,
            frontier_rows([3002.755, 3005.034, 2980.215, 2988.734, 3031.164]),
            allowed_cell=ALLOWED_CELL,
        )

        self.assertFalse(report["construction_ready"])
        self.assertIn("tail_repeat_contract", report["failures"])


if __name__ == "__main__":
    unittest.main()
