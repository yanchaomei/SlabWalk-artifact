#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import verify_vldb_construction_admission as verifier


SHA_B = "b" * 64
SOURCE_TREE_B = "c" * 64


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


class ConstructionAdmissionVerificationTest(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path]:
        promotion_path = root / "promotion.json"
        promotion = {
            "kind": "vldb_candidate_promotion_gate_v1",
            "promotion_ready": False,
            "failures": ["frontier_comparison"],
            "binary_ab": {
                method: {
                    "ready": True,
                    "failures": [],
                    "verification": {
                        "binary_sha_b": SHA_B,
                        "source_tree_sha_b": SOURCE_TREE_B,
                    },
                }
                for method in ("slabwalk", "shine")
            },
        }
        write_json(promotion_path, promotion)

        inputs = {"promotion_report": promotion_path}
        for name in ("frontier_cells", "candidate_frontier", "baseline_frontier"):
            path = root / f"{name}.csv"
            path.write_text(f"name,value\n{name},1\n")
            inputs[name] = path

        gate_path = root / "construction_gate.json"
        gate = {
            "kind": "vldb_construction_candidate_gate_v1",
            "construction_ready": True,
            "general_promotion_ready": False,
            "scope": "construction_measurements_only",
            "failures": [],
            "inputs": {
                name: {"path": str(path.resolve()), "sha256": sha256(path)}
                for name, path in inputs.items()
            },
        }
        write_json(gate_path, gate)
        return gate_path, promotion_path

    def verify(self, gate_path: Path, promotion_path: Path) -> dict:
        return verifier.verify_construction_admission(
            gate_path,
            promotion_path,
            expected_gate_sha=sha256(gate_path),
            expected_sha_b=SHA_B,
            expected_source_tree_b=SOURCE_TREE_B,
        )

    def test_accepts_linked_gate_and_candidate_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            gate_path, promotion_path = self.make_fixture(Path(tmp_s))
            report = self.verify(gate_path, promotion_path)

        self.assertTrue(report["ready"])
        self.assertEqual(report["scope"], "construction_measurements_only")
        self.assertEqual(report["candidate_binary_sha256"], SHA_B)
        self.assertEqual(report["candidate_source_tree_sha256"], SOURCE_TREE_B)
        self.assertEqual(set(report["verified_inputs"]), {
            "promotion_report",
            "frontier_cells",
            "candidate_frontier",
            "baseline_frontier",
        })

    def test_rejects_gate_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            gate_path, promotion_path = self.make_fixture(Path(tmp_s))
            with self.assertRaisesRegex(ValueError, "construction gate SHA drift"):
                verifier.verify_construction_admission(
                    gate_path,
                    promotion_path,
                    expected_gate_sha="d" * 64,
                    expected_sha_b=SHA_B,
                    expected_source_tree_b=SOURCE_TREE_B,
                )

    def test_rejects_drift_in_any_gate_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            gate_path, promotion_path = self.make_fixture(Path(tmp_s))
            cells = Path(tmp_s) / "frontier_cells.csv"
            cells.write_text("name,value\nfrontier_cells,2\n")
            with self.assertRaisesRegex(ValueError, "frontier_cells SHA drift"):
                self.verify(gate_path, promotion_path)

    def test_rejects_unlinked_promotion_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            gate_path, promotion_path = self.make_fixture(root)
            other = root / "other_promotion.json"
            other.write_bytes(promotion_path.read_bytes())
            with self.assertRaisesRegex(ValueError, "promotion report path drift"):
                self.verify(gate_path, other)

    def test_rejects_wrong_candidate_binary_or_source_tree(self) -> None:
        for field, expected, message in (
            ("binary_sha_b", "e" * 64, "candidate binary SHA drift"),
            ("source_tree_sha_b", "f" * 64, "candidate source-tree SHA drift"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp_s:
                root = Path(tmp_s)
                gate_path, promotion_path = self.make_fixture(root)
                promotion = json.loads(promotion_path.read_text())
                promotion["binary_ab"]["shine"]["verification"][field] = expected
                write_json(promotion_path, promotion)
                gate = json.loads(gate_path.read_text())
                gate["inputs"]["promotion_report"]["sha256"] = sha256(promotion_path)
                write_json(gate_path, gate)
                with self.assertRaisesRegex(ValueError, message):
                    self.verify(gate_path, promotion_path)

    def test_rejects_a_general_promotion_or_failed_ab(self) -> None:
        for mutate, message in (
            (
                lambda report: report.update(
                    {"promotion_ready": True, "failures": []}
                ),
                "original promotion failure contract",
            ),
            (
                lambda report: report["binary_ab"]["slabwalk"].update(
                    {"ready": False, "failures": ["p99_regression"]}
                ),
                "slabwalk A/B is not ready",
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp_s:
                root = Path(tmp_s)
                gate_path, promotion_path = self.make_fixture(root)
                promotion = json.loads(promotion_path.read_text())
                mutate(promotion)
                write_json(promotion_path, promotion)
                gate = json.loads(gate_path.read_text())
                gate["inputs"]["promotion_report"]["sha256"] = sha256(promotion_path)
                write_json(gate_path, gate)
                with self.assertRaisesRegex(ValueError, message):
                    self.verify(gate_path, promotion_path)


if __name__ == "__main__":
    unittest.main()
