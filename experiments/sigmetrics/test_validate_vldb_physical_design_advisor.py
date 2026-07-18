import unittest

import validate_vldb_physical_design_advisor as validation


DATASETS = ("DEEP1M", "SIFT1M", "GIST1M")
BUDGETS = (536870912, 1073741824, 2147483648)
POLICIES = ("benefit", "indeg", "hop")


def synthetic_rows() -> list[dict[str, object]]:
    rows = []
    training = {
        "benefit": (100.0, 100.0, 100.0),
        "indeg": (99.0, 99.0, 99.0),
        "hop": (80.0, 80.0, 80.0),
    }
    heldout = {
        "benefit": (100.0, 100.0, 100.0),
        "indeg": (101.0, 101.0, 101.0),
        "hop": (80.0, 80.0, 80.0),
    }
    for dataset in DATASETS:
        for budget in BUDGETS:
            for repeat in range(6):
                rotation = repeat % len(POLICIES)
                for position in range(len(POLICIES)):
                    policy = POLICIES[(position + rotation) % len(POLICIES)]
                    samples = training if repeat < 3 else heldout
                    rows.append(
                        {
                            "dataset": dataset,
                            "requested_bytes": budget,
                            "policy": policy,
                            "repeat": repeat,
                            "position": position,
                            "qps": samples[policy][repeat % 3],
                            "recall": 0.95,
                            "physical_bytes": budget - 32,
                            "bytes_per_query": 1000.0,
                            "posts_per_query": 10.0,
                            "binary_sha256": "b" * 64,
                            "input_signature": dataset[0].lower() * 64,
                            "selection_hash": f"{policy}-selection",
                            "physical_signature": f"{policy}-physical",
                        }
                    )
    return rows


class PhysicalDesignAdvisorValidationTest(unittest.TestCase):
    def test_selects_on_first_three_and_passes_last_three(self):
        report = validation.evaluate_rows(
            synthetic_rows(),
            campaign_id="materialization-test",
            protocol_fingerprint="p" * 64,
            input_seal_sha256="s" * 64,
        )

        self.assertTrue(report["promotion_ready"])
        self.assertEqual(report["measured_rows"], 162)
        self.assertEqual(report["selection_cells"], 9)
        self.assertEqual(report["training_repeats"], [0, 1, 2])
        self.assertEqual(report["heldout_repeats"], [3, 4, 5])
        self.assertEqual(report["selected_policies"], {"benefit": 9})
        self.assertAlmostEqual(report["heldout_ratio_min"], 100.0 / 101.0)
        self.assertAlmostEqual(report["heldout_ratio_geomean"], 100.0 / 101.0)
        self.assertTrue(all(row["selected_policy"] == "benefit" for row in report["cells"]))

    def test_fails_the_fixed_gate_when_one_heldout_cell_regresses(self):
        rows = synthetic_rows()
        for row in rows:
            if (
                row["dataset"] == "DEEP1M"
                and row["requested_bytes"] == BUDGETS[0]
                and row["policy"] == "benefit"
                and row["repeat"] >= 3
            ):
                row["qps"] = 90.0

        report = validation.evaluate_rows(
            rows,
            campaign_id="materialization-test",
            protocol_fingerprint="p" * 64,
            input_seal_sha256="s" * 64,
        )

        self.assertFalse(report["promotion_ready"])
        self.assertIn("heldout_cell_ratio", report["promotion_failures"])
        self.assertEqual(report["thresholds"], {
            "recall_min": 0.90,
            "heldout_min_qps_ratio": 0.98,
            "heldout_geomean_qps_ratio": 0.99,
        })

    def test_rejects_position_imbalance_or_an_incomplete_matrix(self):
        rows = synthetic_rows()
        rows[0]["position"] = 2
        with self.assertRaisesRegex(ValueError, "position-balanced"):
            validation.evaluate_rows(
                rows,
                campaign_id="materialization-test",
                protocol_fingerprint="p" * 64,
                input_seal_sha256="s" * 64,
            )

        with self.assertRaisesRegex(ValueError, "matrix"):
            validation.evaluate_rows(
                synthetic_rows()[:-1],
                campaign_id="materialization-test",
                protocol_fingerprint="p" * 64,
                input_seal_sha256="s" * 64,
            )

    def test_rejects_a_budget_or_recall_violation_from_the_source_rows(self):
        rows = synthetic_rows()
        rows[0]["physical_bytes"] = BUDGETS[0] + 1
        with self.assertRaisesRegex(ValueError, "physical byte cap"):
            validation.evaluate_rows(
                rows,
                campaign_id="materialization-test",
                protocol_fingerprint="p" * 64,
                input_seal_sha256="s" * 64,
            )

        rows = synthetic_rows()
        for row in rows:
            if (
                row["dataset"] == "DEEP1M"
                and row["requested_bytes"] == BUDGETS[0]
                and row["policy"] == "benefit"
                and row["repeat"] >= 3
            ):
                row["recall"] = 0.89
        report = validation.evaluate_rows(
            rows,
            campaign_id="materialization-test",
            protocol_fingerprint="p" * 64,
            input_seal_sha256="s" * 64,
        )
        self.assertFalse(report["promotion_ready"])
        self.assertIn("selected_heldout_feasibility", report["promotion_failures"])


if __name__ == "__main__":
    unittest.main()
