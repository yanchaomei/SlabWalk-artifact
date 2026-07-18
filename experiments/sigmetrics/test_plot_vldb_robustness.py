import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import plot_vldb_robustness as plotter
from test_validate_vldb_final_evidence import (
    model_control_rows,
    worker_scaling_rows,
    write_colocation_control_evidence,
    write_worker_campaign_provenance,
    write_topology_evidence,
)


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


def measured_rows() -> list[dict[str, object]]:
    cells = {
        "workers": ("1", "8", "16", "40"),
        "coroutines": ("1", "2", "4", "8", "16"),
        "top_k": ("1", "10", "50", "100"),
        "query_distribution": ("uniform", "zipf1.0"),
        "latency_instrumentation": ("off", "on"),
    }
    rows = []
    for factor, values in cells.items():
        for index, value in enumerate(values):
            for repeat in range(5):
                threads = int(value) if factor == "workers" else 10
                latency = value != "off"
                rows.append({
                    "campaign_id": "robustness-final",
                    "protocol_fingerprint": f"protocol-{factor}-{value}",
                    "binary_sha256": FINAL_SHA,
                    "dataset": "DEEP1M",
                    "factor": factor,
                    "value": value,
                    "run_kind": "measure",
                    "repeat": repeat,
                    "threads": threads,
                    "query_contexts": threads,
                    "coroutines": int(value) if factor == "coroutines" else 2,
                    "top_k": int(value) if factor == "top_k" else 10,
                    "ef": 200,
                    "query_suffix": "a1.0-n10000" if value == "zipf1.0" else "uniform",
                    "latency_enabled": 1 if latency else 0,
                    "metric": "l2",
                    "status": "ok",
                    "processed": 10000,
                    "recall": 0.9,
                    "qps": 10000 + index * 1000 + repeat * 10,
                    "p50_us": 80 + index * 10 if latency else "",
                    "p95_us": 100 + index * 20 if latency else "",
                    "p99_us": 120 + index * 30 if latency else "",
                    "posts_per_query": 40 + index * 2,
                    "bytes_per_query": 4096 + index * 1024,
                })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class VldbRobustnessPlotTest(unittest.TestCase):
    def create_inputs(self, root: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
        runs = root / "robustness" / "runs.csv"
        worker_runs = root / "worker_scaling" / "runs.csv"
        rdma_runs = root / "model_controls" / "rdma_tau_runs.csv"
        topology_runs = root / "topology_control" / "runs.csv"
        colocation_root = root / "colocation_control"
        gate = root / "evidence_gate.json"
        write_csv(runs, measured_rows())
        write_csv(worker_runs, worker_scaling_rows(worker_runs.parent))
        write_worker_campaign_provenance(worker_runs.parent)
        write_csv(rdma_runs, model_control_rows())
        write_topology_evidence(topology_runs.parent)
        write_colocation_control_evidence(colocation_root)
        colocation_runs = colocation_root / "summary" / "runs.csv"
        gate.write_text(json.dumps({
            "ready_for_plotting": True,
            "expected_slabwalk_sha256": FINAL_SHA,
            "robustness": {"runs_sha256": hashlib.sha256(runs.read_bytes()).hexdigest()},
            "worker_scaling": {
                "runs_sha256": hashlib.sha256(worker_runs.read_bytes()).hexdigest()
            },
            "model_controls": {
                "runs_sha256": hashlib.sha256(rdma_runs.read_bytes()).hexdigest()
            },
            "topology_control": {
                "runs_sha256": hashlib.sha256(topology_runs.read_bytes()).hexdigest()
            },
            "colocation_control": {
                "runs_sha256": hashlib.sha256(colocation_runs.read_bytes()).hexdigest()
            },
        }))
        return runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate

    def test_loads_all_measured_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(Path(tmp))
            robustness, workers, model_controls, topology, colocation = plotter.load_validated(
                runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate
            )
            self.assertEqual(len(robustness), 85)
            self.assertEqual(len(workers), 60)
            self.assertEqual(len(model_controls), 125)
            self.assertEqual(len(topology), 10)
            self.assertEqual(len(colocation), 30)

    def test_rejects_runs_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(Path(tmp))
            runs.write_text(runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "runs SHA"):
                plotter.load_validated(runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate)

    def test_rejects_worker_runs_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(Path(tmp))
            worker_runs.write_text(worker_runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "worker-scaling runs SHA"):
                plotter.load_validated(runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate)

    def test_rejects_model_controls_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(Path(tmp))
            rdma_runs.write_text(rdma_runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "model-control runs SHA"):
                plotter.load_validated(runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate)

    def test_rejects_topology_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(Path(tmp))
            topology_runs.write_text(topology_runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "topology-control runs SHA"):
                plotter.load_validated(
                    runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate
                )

    def test_rejects_colocation_changed_after_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self.create_inputs(Path(tmp))
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = inputs
            colocation_runs.write_text(colocation_runs.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "co-location runs SHA"):
                plotter.load_validated(
                    runs,
                    worker_runs,
                    rdma_runs,
                    topology_runs,
                    colocation_runs,
                    gate,
                )

    def test_generates_nonempty_vector_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate = self.create_inputs(root)
            out = root / "robustness.pdf"
            plotter.generate(
                runs, worker_runs, rdma_runs, topology_runs, colocation_runs, gate, out
            )
            self.assertGreater(out.stat().st_size, 10000)
            self.assertEqual(out.read_bytes()[:4], b"%PDF")


if __name__ == "__main__":
    unittest.main()
