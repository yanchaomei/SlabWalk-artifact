#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import bind_vldb_physical_design_advisor_gate as binder


class BindVldbPhysicalDesignAdvisorGateTest(unittest.TestCase):
    def write_inputs(self, root: Path) -> tuple[Path, Path, Path, Path]:
        main = root / "main.json"
        main.write_text(
            json.dumps(
                {
                    "kind": "vldb_final_evidence_gate",
                    "ready_for_plotting": True,
                    "claim_input_sha256": {},
                },
                sort_keys=True,
            )
        )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "kind": "vldb_physical_design_advisor_validation",
                    "campaign_id": "materialization-fixture",
                    "protocol_fingerprint": "f" * 64,
                    "measured_rows": 162,
                    "selection_cells": 9,
                    "training_repeats": [0, 1, 2],
                    "heldout_repeats": [3, 4, 5],
                    "thresholds": {
                        "recall_min": 0.90,
                        "heldout_min_qps_ratio": 0.98,
                        "heldout_geomean_qps_ratio": 0.99,
                    },
                    "selected_policies": {"benefit": 6, "indeg": 3},
                    "heldout_ratio_min": 0.9908309455587393,
                    "heldout_ratio_geomean": 0.9986928421693378,
                    "promotion_ready": True,
                    "promotion_failures": [],
                    "claim_boundary": (
                        "strict post-hoc split over a pre-existing sealed campaign; "
                        "this is an auditable offline deployment policy, not a "
                        "prospective or online optimizer"
                    ),
                },
                sort_keys=True,
            )
        )
        source_seal = root / "source-seal.json"
        source_seal.write_text('{"kind":"sealed-source"}\n')
        validation_seal = root / "validation-seal.json"
        validation_seal.write_text('{"kind":"sealed-validation"}\n')
        return main, report, source_seal, validation_seal

    def test_binds_the_sealed_advisor_report_into_the_claim_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main, report, source_seal, validation_seal = self.write_inputs(Path(tmp))
            payload = binder.bind(main, report, source_seal, validation_seal)

            section = payload["physical_design_advisor"]
            self.assertEqual(section["selection_cells"], 9)
            self.assertEqual(section["selected_policies"], {"benefit": 6, "indeg": 3})
            self.assertAlmostEqual(section["heldout_ratio_min"], 0.9908309455587393)
            self.assertEqual(section["report_sha256"], binder.sha256(report))
            self.assertEqual(section["source_seal_sha256"], binder.sha256(source_seal))
            self.assertEqual(
                section["validation_seal_sha256"], binder.sha256(validation_seal)
            )
            self.assertEqual(
                payload["claim_input_sha256"]["physical_design_advisor_report"],
                binder.sha256(report),
            )

    def test_rejects_a_failed_or_weakened_advisor_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main, report, source_seal, validation_seal = self.write_inputs(Path(tmp))
            payload = json.loads(report.read_text())
            payload["promotion_ready"] = False
            payload["promotion_failures"] = ["heldout_cell_ratio"]
            report.write_text(json.dumps(payload, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "not promotion-ready"):
                binder.bind(main, report, source_seal, validation_seal)

            payload["promotion_ready"] = True
            payload["promotion_failures"] = []
            payload["thresholds"]["heldout_min_qps_ratio"] = 0.90
            report.write_text(json.dumps(payload, sort_keys=True))
            with self.assertRaisesRegex(ValueError, "fixed thresholds"):
                binder.bind(main, report, source_seal, validation_seal)


if __name__ == "__main__":
    unittest.main()
