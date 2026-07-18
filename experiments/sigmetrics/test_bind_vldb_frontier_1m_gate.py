#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import bind_vldb_frontier_1m_gate as binder


class BindVldbFrontier1MGateTest(unittest.TestCase):
    def write_gates(self, root: Path) -> tuple[Path, Path]:
        main = root / "main.json"
        main.write_text(
            json.dumps(
                {
                    "kind": "vldb_final_evidence_gate",
                    "ready_for_plotting": True,
                    "claim_input_sha256": {},
                }
            )
        )
        frontier = root / "frontier.json"
        frontier.write_text(
            json.dumps(
                {
                    "kind": "vldb_frontier_1m_gate",
                    "ready_for_plotting": True,
                    "datasets": ["SIFT1M"],
                    "methods": ["SHINE", "SlabWalk", "d-HNSW"],
                    "expected_repeats": 5,
                    "measured_rows": 525,
                    "summary_rows": 105,
                    "query_pool_cells": 21,
                    "campaign_id": "vldb-1m-final",
                    "raw_sha256": "a" * 64,
                    "summary_sha256": "b" * 64,
                },
                sort_keys=True,
            )
        )
        return main, frontier

    def test_binds_auxiliary_gate_and_summary_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            main, frontier = self.write_gates(Path(tmp_s))
            payload = binder.bind(main, frontier)
            self.assertEqual(payload["frontier_1m"]["summary_sha256"], "b" * 64)
            self.assertEqual(
                payload["frontier_1m"]["gate_sha256"], binder.sha256(frontier)
            )
            self.assertEqual(
                payload["claim_input_sha256"]["frontier_1m_summary"], "b" * 64
            )
            self.assertEqual(
                payload["claim_input_sha256"]["frontier_1m_gate"],
                binder.sha256(frontier),
            )

    def test_rejects_an_unready_1m_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            main, frontier = self.write_gates(Path(tmp_s))
            payload = json.loads(frontier.read_text())
            payload["ready_for_plotting"] = False
            frontier.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "not plot-ready"):
                binder.bind(main, frontier)


if __name__ == "__main__":
    unittest.main()
