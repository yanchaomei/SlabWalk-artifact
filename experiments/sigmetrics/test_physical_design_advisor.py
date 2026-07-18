import json
import math
import tempfile
import unittest
from pathlib import Path

import physical_design_advisor as advisor


def candidate(
    name: str,
    qps: list[float],
    *,
    recall: float = 0.95,
    resources: dict[str, float] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": name,
        "configuration": {"policy": name},
        "provenance": {"source_sha256": (name[0] * 64)},
        "qps_samples": qps,
        "recall": recall,
        "resources": {"mn_bytes": 900.0} if resources is None else resources,
    }


def request(candidates: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "selection_id": "test-selection",
        "constraints": {
            "recall_min": 0.90,
            "resources_max": {"mn_bytes": 1000.0},
        },
        "objective": "qps_ci95_low",
        "candidates": candidates,
        "provenance": {"campaign_sha256": "c" * 64},
    }


class PhysicalDesignAdvisorTest(unittest.TestCase):
    def test_selects_lower_confidence_throughput_not_highest_mean(self):
        unstable = candidate("unstable", [80.0, 110.0, 170.0])
        stable = candidate("stable", [99.0, 100.0, 101.0])

        report = advisor.select_candidate(request([unstable, stable]))

        self.assertTrue(report["selection_ready"])
        self.assertEqual(report["selected"]["candidate_id"], "stable")
        rows = {row["candidate_id"]: row for row in report["candidates"]}
        self.assertGreater(rows["unstable"]["qps_mean"], rows["stable"]["qps_mean"])
        self.assertLess(
            rows["unstable"]["qps_ci95_low"], rows["stable"]["qps_ci95_low"]
        )

    def test_reports_every_resource_recall_and_missing_metric_rejection(self):
        too_large = candidate(
            "large", [100.0, 101.0, 99.0], resources={"mn_bytes": 1001.0}
        )
        low_recall = candidate("lowrec", [120.0, 121.0, 119.0], recall=0.89)
        missing = candidate("missing", [130.0, 131.0, 129.0], resources={})
        feasible = candidate("valid", [90.0, 91.0, 89.0])

        report = advisor.select_candidate(
            request([too_large, low_recall, missing, feasible])
        )
        rows = {row["candidate_id"]: row for row in report["candidates"]}

        self.assertEqual(report["selected"]["candidate_id"], "valid")
        self.assertEqual(rows["large"]["rejection_reasons"], [
            "resource_exceeds:mn_bytes"
        ])
        self.assertEqual(rows["lowrec"]["rejection_reasons"], [
            "recall_below_target"
        ])
        self.assertEqual(rows["missing"]["rejection_reasons"], [
            "missing_resource:mn_bytes"
        ])

    def test_uses_lexical_candidate_id_as_the_final_tie_break(self):
        left = candidate("alpha", [99.0, 100.0, 101.0])
        right = candidate("beta", [99.0, 100.0, 101.0])

        report = advisor.select_candidate(request([right, left]))

        self.assertEqual(report["selected"]["candidate_id"], "alpha")
        self.assertEqual(
            [row["candidate_id"] for row in report["candidates"]],
            ["alpha", "beta"],
        )

    def test_fails_closed_when_no_candidate_is_feasible(self):
        report = advisor.select_candidate(
            request([candidate("only", [1.0, 1.0, 1.0], recall=0.1)])
        )

        self.assertFalse(report["selection_ready"])
        self.assertIsNone(report["selected"])
        self.assertEqual(report["failures"], ["no_feasible_candidate"])

    def test_rejects_duplicate_nonfinite_or_undersampled_candidates(self):
        cases = [
            request([candidate("same", [1.0, 2.0, 3.0]), candidate("same", [2.0, 3.0, 4.0])]),
            request([candidate("nan", [1.0, math.nan, 3.0])]),
            request([candidate("short", [1.0])]),
        ]
        patterns = ("duplicate candidate", "non-finite", "at least two")
        for payload, pattern in zip(cases, patterns):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    advisor.select_candidate(payload)

    def test_atomic_writer_binds_canonical_input_and_tool_hashes(self):
        payload = request([candidate("only", [10.0, 11.0, 12.0])])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "request.json"
            output = root / "selection.json"
            source.write_text(json.dumps(payload, sort_keys=True) + "\n")

            report = advisor.run_file(source, output)

            stored = json.loads(output.read_text())
            self.assertEqual(stored, report)
            self.assertRegex(report["input_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(report["advisor_sha256"], r"^[0-9a-f]{64}$")
            with self.assertRaisesRegex(ValueError, "refusing existing output"):
                advisor.run_file(source, output)


if __name__ == "__main__":
    unittest.main()
