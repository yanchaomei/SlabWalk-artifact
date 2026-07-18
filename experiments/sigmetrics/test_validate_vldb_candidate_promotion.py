#!/usr/bin/env python3
import unittest

import validate_vldb_candidate_promotion as promotion


SHA_A = "a" * 64
SHA_B = "b" * 64
TREE_A = "c" * 64
TREE_B = "d" * 64


def verification(method: str) -> dict[str, object]:
    return {
        "dataset": promotion.EXPECTED_DATASETS[method],
        "method": method,
        "capture_build_metrics": False,
        "paired_repeats": 6,
        "run_count": 12,
        "binary_sha_a": SHA_A,
        "binary_sha_b": SHA_B,
        "source_tree_sha_a": TREE_A,
        "source_tree_sha_b": TREE_B,
    }


def comparison() -> dict[str, object]:
    return {
        "paired_repeats": 6,
        "order_stratified": {"AB": {"n": 3}, "BA": {"n": 3}},
        "paired_recall_delta_B_minus_A_mean": 0.0,
        "paired_recall_delta_B_minus_A_ci95": 0.0,
        "paired_posts_per_query_delta_B_minus_A_mean": 0.0,
        "paired_posts_per_query_delta_B_minus_A_ci95": 0.0,
        "paired_bytes_per_query_delta_B_minus_A_mean": 0.0,
        "paired_bytes_per_query_delta_B_minus_A_ci95": 0.0,
        "paired_qps_speedup_B_over_A_mean": 1.01,
        "paired_qps_speedup_B_over_A_ci95": 0.02,
        "paired_p99_us_delta_B_minus_A_mean": 2.0,
        "paired_p99_us_delta_B_minus_A_ci95": 4.0,
    }


def evaluate_method(method: str, values: dict[str, object]) -> dict[str, object]:
    return promotion.evaluate_ab(
        method,
        verification(method),
        values,
        100.0,
        expected_sha_a=SHA_A,
        expected_sha_b=SHA_B,
        expected_source_tree_a=TREE_A,
        expected_source_tree_b=TREE_B,
    )


class ValidateVldbCandidatePromotionTest(unittest.TestCase):
    def test_complete_three_gate_result_passes(self) -> None:
        front = {
            "kind": "vldb_frontier_candidate_comparison_v1",
            "promotion_ready": True,
            "invariant_failures": 0,
            "performance_failures": 0,
        }
        report = promotion.evaluate(
            front,
            {
                method: evaluate_method(method, comparison())
                for method in promotion.METHODS
            },
        )
        self.assertTrue(report["promotion_ready"])

    def test_shine_qps_regression_blocks_promotion(self) -> None:
        values = comparison()
        values["paired_qps_speedup_B_over_A_mean"] = 0.96
        values["paired_qps_speedup_B_over_A_ci95"] = 0.02
        shine = evaluate_method("shine", values)
        self.assertFalse(shine["ready"])
        self.assertIn("qps_regression", shine["failures"])

    def test_tail_uncertainty_blocks_promotion(self) -> None:
        values = comparison()
        values["paired_p99_us_delta_B_minus_A_mean"] = 6.0
        values["paired_p99_us_delta_B_minus_A_ci95"] = 5.0
        slabwalk = evaluate_method("slabwalk", values)
        self.assertFalse(slabwalk["ready"])
        self.assertIn("p99_regression", slabwalk["failures"])

    def test_source_tree_mismatch_blocks_method_gate(self) -> None:
        record = verification("shine")
        record["source_tree_sha_b"] = "e" * 64
        report = promotion.evaluate_ab(
            "shine",
            record,
            comparison(),
            100.0,
            expected_sha_a=SHA_A,
            expected_sha_b=SHA_B,
            expected_source_tree_a=TREE_A,
            expected_source_tree_b=TREE_B,
        )
        self.assertIn("candidate_source_tree", report["failures"])

    def test_wrong_boundary_dataset_blocks_method_gate(self) -> None:
        record = verification("slabwalk")
        record["dataset"] = "DEEP1M"
        report = promotion.evaluate_ab(
            "slabwalk",
            record,
            comparison(),
            100.0,
            expected_sha_a=SHA_A,
            expected_sha_b=SHA_B,
            expected_source_tree_a=TREE_A,
            expected_source_tree_b=TREE_B,
        )
        self.assertIn("verification_contract", report["failures"])


if __name__ == "__main__":
    unittest.main()
