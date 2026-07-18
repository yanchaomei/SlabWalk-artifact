import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_worker_scaling as assembler
import validate_vldb_final_evidence as evidence
from worker_campaign_test_fixture import write_campaign_audit


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
DHNSW_SHA = "d" * 64


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_query_manifests(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for method in evidence.WORKER_SCALING_METHODS:
        slug = assembler.method_slug(method)
        record = {
            "kind": "query_pool_fingerprint",
            "dataset": "DEEP1M",
            "method": method,
            "metric": "l2",
            "limit": 10000,
            "query": {
                "rows": 10000,
                "source_rows": 10000,
                "dim": 96,
                "canonical_sha256": digest("canonical-query"),
                "file_sha256": digest(f"{method}/query-file"),
            },
            "groundtruth": {
                "rows": 10000,
                "source_rows": 10000,
                "k": 100,
                "canonical_ids_sha256": digest("canonical-groundtruth"),
                "file_sha256": digest(f"{method}/groundtruth-file"),
            },
        }
        (directory / f"deep1m_{slug}.json").write_text(json.dumps(record))


def write_raw_campaign(root: Path, omit: tuple[str, int, int] | None = None) -> None:
    for workers in evidence.WORKER_SCALING_WORKERS:
        for repeat in range(5):
            sw_dir = root / "sw" / f"w{workers}" / f"r{repeat}"
            sw_rows = []
            for method in ("SHINE", "SlabWalk"):
                if omit == (method, workers, repeat):
                    continue
                measurement = sw_dir / f"{assembler.method_slug(method)}.json"
                measurement.parent.mkdir(parents=True, exist_ok=True)
                measurement.write_text(json.dumps({
                    "num_queries": 10000,
                    "query_contexts": workers,
                    "queries": {
                        "processed": 10000,
                        "recall": 0.989,
                        "queries_per_sec": workers * 1200 + repeat,
                    },
                }))
                sw_rows.append({
                    "dataset": "DEEP1M",
                    "method": method,
                    "binary_sha256": FINAL_SHA,
                    "run_id": f"r{repeat}",
                    "run_kind": "measure",
                    "measurement_mode": "fixed_query_pool",
                    "threads": workers,
                    "query_contexts": workers,
                    "coroutines": 2,
                    "top_k": 10,
                    "metric": "l2",
                    "ef": 200,
                    "processed": 10000,
                    "expected_queries": 10000,
                    "failed_queries": 0,
                    "recall": 0.989,
                    "qps": workers * 1200 + repeat,
                    "json": str(measurement),
                    "stderr": "",
                    "status": "ok",
                })
            if sw_rows:
                write_csv(sw_dir / "slabwalk_shine_frontier_raw.csv", sw_rows)

            if omit == ("d-HNSW", workers, repeat):
                continue
            dh_dir = root / "dhnsw" / f"w{workers}" / f"r{repeat}"
            write_csv(dh_dir / "frontier.csv", [{
                "dataset": "deep1M",
                "ef": 200,
                "binary_sha256": DHNSW_SHA,
                "threads": workers,
                "measurement_mode": "fixed_query_pool",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
                "top_k": 10,
                "metric": "l2",
                "qps_recomputed": workers * 450 + repeat,
                "recall": 0.909,
                "status": "ok",
            }])
            (dh_dir / "deep1M_ef200_client.log").write_text(
                f"workers={workers} repeat={repeat} completed=10000\n"
            )


class AssembleWorkerScalingTest(unittest.TestCase):
    def test_assembles_a_self_contained_validated_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            pools = root / "query_pools"
            out = root / "final"
            write_raw_campaign(raw)
            write_query_manifests(pools)
            write_campaign_audit(
                root / "campaign",
                campaign_id="worker-scaling-final",
                slabwalk_sha=FINAL_SHA,
                dhnsw_sha=DHNSW_SHA,
            )
            report = assembler.assemble(
                raw,
                pools,
                root / "campaign",
                out,
                campaign_id="worker-scaling-final",
                expected_slabwalk_sha=FINAL_SHA,
            )
            self.assertEqual(report["measured_rows"], 60)
            self.assertEqual(report["measured_cells"], 12)
            self.assertEqual(len(list((out / "raw_sources").glob("*.json"))), 60)
            self.assertEqual(len(list((out / "query_pools").glob("*.json"))), 3)
            self.assertTrue((out / "campaign_provenance.json").is_file())
            self.assertTrue((out / "campaign" / "campaign.json").is_file())
            self.assertTrue(
                (
                    out
                    / "campaign/failed_runs/dhnsw/w40/r0-before-runner-fix"
                    / "deep1M_ef200_client.log"
                ).is_file()
            )
            retained = json.loads(
                (out / "raw_sources" / "dhnsw_w1_r0.json").read_text()
            )
            self.assertEqual(retained["detail_source"], "client_log")
            self.assertIsNone(retained["benchmark_details"])

    def test_rejects_a_missing_raw_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            pools = root / "query_pools"
            write_raw_campaign(raw, omit=("d-HNSW", 40, 4))
            write_query_manifests(pools)
            write_campaign_audit(
                root / "campaign",
                campaign_id="worker-scaling-final",
                slabwalk_sha=FINAL_SHA,
                dhnsw_sha=DHNSW_SHA,
            )
            with self.assertRaisesRegex(ValueError, "missing d-HNSW input"):
                assembler.assemble(
                    raw,
                    pools,
                    root / "campaign",
                    root / "final",
                    campaign_id="worker-scaling-final",
                    expected_slabwalk_sha=FINAL_SHA,
                )


if __name__ == "__main__":
    unittest.main()
