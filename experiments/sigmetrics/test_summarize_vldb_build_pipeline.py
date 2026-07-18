import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import summarize_vldb_build_pipeline as summary
from experiments.sigmetrics import vldb_evidence_bundle as evidence_bundle


SHA = "7" * 64
HOST = "skv-node3"


def seal_tree(root: Path) -> None:
    manifest = root / "SHA256SUMS"
    seal = root / "SEALED.json"
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path not in {manifest, seal}
    )
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(root).as_posix()}\n"
            for path in paths
        )
    )
    seal.write_text('{"schema_version": 1}\n')


def write_child(
    root: Path,
    *,
    repeat: int,
    position: int,
    workers: int,
    total_ms: float,
    materialize_ms: float,
) -> Path:
    child = root / "raw" / f"r{repeat}_p{position}_t{workers}"
    child.mkdir(parents=True)
    campaign = {
        "protocol": {
            "binary_sha256": SHA,
            "datasets": ["DEEP1M"],
            "policies": ["indeg"],
            "budget_bytes": [536870912],
            "repeats": 1,
            "build_threads": workers,
            "staged_build": True,
            "compute_host": HOST,
        }
    }
    (child / "campaign.json").write_text(json.dumps(campaign))
    raw = child / "raw" / "DEEP1M" / "b536870912" / "indeg" / "r0"
    raw.mkdir(parents=True)
    artifacts = {}
    for name, filename, payload in (
        ("compute_stdout", "result.json", "{}\n"),
        ("compute_stderr", "run.err", "run\n"),
        ("memory_node_stdout", "mn.out", "mn out\n"),
        ("memory_node_stderr", "mn.err", "mn err\n"),
    ):
        path = raw / filename
        path.write_text(payload)
        artifacts[name] = {
            "path": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (raw / "campaign.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "dataset": "DEEP1M",
                "budget_bytes": 536870912,
                "policy": "indeg",
                "repeat": 0,
                "position": 0,
                "kind": "r",
                "input_signature": "6" * 64,
                "executables": {
                    "compute_node": {"sha256": SHA, "host": HOST},
                    "memory_node": {"sha256": SHA},
                },
                "artifacts": artifacts,
            }
        )
    )
    row = {
        "dataset": "DEEP1M",
        "repeat": 0,
        "position": 0,
        "policy": "indeg",
        "binary_sha256": SHA,
        "input_signature": "6" * 64,
        "source_tree_sha256": "5" * 64,
        "compute_host": HOST,
        "requested_bytes": 536870912,
        "fixed_bytes": 4016016,
        "record_bytes": 532854872,
        "admitted_bytes": 536870888,
        "physical_bytes": 536870888,
        "unused_bytes": 24,
        "selected_records": 177098,
        "total_records": 1000000,
        "selection_hash": 17395791157795909511,
        "result_hash_version": 1,
        "result_hash": 8822020575971608226,
        "physical_hash_version": 2,
        "physical_hash_algorithm": "fnv1a64",
        "physical_hash_scope": "field_scoped_physical_artifacts",
        "header_hash_scope": "replicated_header_source_bytes",
        "descriptor_hash_scope": "descriptor_slice_of_replicated_header",
        "map_hash_scope": "global_budget_map_source_bytes",
        "offset_table_hash_scope": "per_mn_offset_table_source_bytes",
        "record_payload_hash_scope": "per_mn_record_payload_source_bytes",
        "selected_uid_hash_scope": "global_selected_uid_u32le_sequence",
        "budget_map_owner_mn": 0,
        "header_hash": "0000000000000000",
        "descriptor_hash": "0000000000000001",
        "map_hash": "0000000000000002",
        "offset_table_hashes": "0000000000000003",
        "record_payload_hashes": "0000000000000004",
        "selected_uid_hash": "0000000000000005",
        "physical_signature": "8" * 64,
        "total_benefit": 1234,
        "build_mode": "staged",
        "build_workers": workers,
        "rank_workers": workers,
        "rank_workers_recorded": 1,
        "staging_bytes": 67108864,
        "record_write_posts": 8,
        "processed": 10000,
        "qps": 410.0 + repeat,
        "recall": 0.98907,
        "p50_us": 9000.0,
        "p95_us": 13000.0,
        "p99_us": 14500.0,
        "posts_per_query": 348.1563,
        "bytes_per_query": 916301.674,
        "build_total_ms": total_ms,
        "build_rank_ms": 220.0,
        "build_materialize_ms": materialize_ms,
        "record_write_posts": 8,
        "build_record_assemble_ms": materialize_ms * 0.7,
        "build_record_publish_ms": materialize_ms * 0.2,
        "result_json": str(raw / "result.json"),
        "stderr": str(raw / "run.err"),
    }
    with (child / "runs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    seal_tree(child)
    return child


def write_index(root: Path, rows: list[dict]) -> Path:
    index = root / "cell_index.csv"
    with index.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "repeat",
                "position",
                "build_threads",
                "child_dir",
                "status",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    return index


def write_bundle(root: Path) -> Path:
    index_rows = []
    for repeat in range(2):
        order = (1, 4) if repeat == 0 else (4, 1)
        for position, workers in enumerate(order):
            child = write_child(
                root,
                repeat=repeat,
                position=position,
                workers=workers,
                total_ms=(1000.0 if workers == 1 else 625.0) + repeat,
                materialize_ms=(400.0 if workers == 1 else 100.0) + repeat,
            )
            index_rows.append(
                {
                    "repeat": repeat,
                    "position": position,
                    "build_threads": workers,
                    "child_dir": child.relative_to(root),
                    "status": "ok",
                }
            )
    index = write_index(root, index_rows)
    runs, summaries, comparison = summary.summarize_campaign(
        index,
        expected_threads=[1, 4],
        expected_repeats=2,
        expected_sha=SHA,
        expected_compute_host=HOST,
    )
    summary._write_csv(root / "runs.csv", runs)
    summary._write_csv(root / "summary.csv", summaries)
    (root / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n"
    )
    campaign = {
        "campaign_id": "build-pipeline-test",
        "campaign_uuid": "00000000-0000-0000-0000-000000000001",
        "protocol_fingerprint": "9" * 64,
        "protocol": {
            "binary_sha256": SHA,
            "dataset": "DEEP1M",
            "policy": "indeg",
            "budget_bytes": 536870912,
            "build_threads": [1, 4],
            "repeats": 2,
            "campaign_kind": "formal",
            "compute_host": HOST,
            "staged_build": True,
        },
    }
    (root / "campaign.json").write_text(
        json.dumps(campaign, indent=2, sort_keys=True) + "\n"
    )
    evidence_bundle.seal_bundle(root, root / "campaign.json")
    return root


class BuildPipelineSummaryTest(unittest.TestCase):
    def test_validates_relocatable_sealed_bundle_semantically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = write_bundle(Path(tmp) / "source")
            relocated = Path(tmp) / "relocated"
            shutil.copytree(source, relocated)

            report = summary.validate_bundle(
                relocated,
                expected_sha=SHA,
                expected_compute_host=HOST,
            )

            self.assertEqual(report["measured_cells"], 4)
            self.assertEqual(report["worker_counts"], [1, 4])
            self.assertEqual(report["repeats"], 2)
            self.assertEqual(report["compute_host"], HOST)

    def test_semantic_summary_tamper_is_rejected_after_valid_reseal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_bundle(Path(tmp) / "bundle")
            summary_path = root / "summary.csv"
            with summary_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["build_total_ms_mean"] = "1.0"
            with summary_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            (root / "SHA256SUMS").unlink()
            (root / "SEALED.json").unlink()
            evidence_bundle.seal_bundle(root, root / "campaign.json")

            with self.assertRaisesRegex(ValueError, "semantic summary mismatch"):
                summary.validate_bundle(
                    root,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_semantic_summary_accepts_one_ulp_float_serialization_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_bundle(Path(tmp) / "bundle")
            summary_path = root / "summary.csv"
            with summary_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            original = float(rows[0]["build_materialize_ms_ci95"])
            rows[0]["build_materialize_ms_ci95"] = repr(
                math.nextafter(original, math.inf)
            )
            with summary_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            (root / "SHA256SUMS").unlink()
            (root / "SEALED.json").unlink()
            evidence_bundle.seal_bundle(root, root / "campaign.json")

            report = summary.validate_bundle(
                root,
                expected_sha=SHA,
                expected_compute_host=HOST,
            )

            self.assertEqual(report["measured_cells"], 4)

    def test_bundle_validator_rejects_expected_host_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_bundle(Path(tmp) / "bundle")

            with self.assertRaisesRegex(ValueError, "compute host"):
                summary.validate_bundle(
                    root,
                    expected_sha=SHA,
                    expected_compute_host="skv-node1",
                )

    def test_verify_cli_reports_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = write_bundle(Path(tmp) / "bundle")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(summary.__file__)),
                    "verify",
                    "--bundle",
                    str(root),
                    "--expected-sha",
                    SHA,
                    "--expected-compute-host",
                    HOST,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["measured_cells"], 4)
            self.assertEqual(report["compute_host"], HOST)

    def test_rejects_unsorted_worker_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            children = [
                write_child(
                    root,
                    repeat=0,
                    position=position,
                    workers=workers,
                    total_ms=1000.0 / workers,
                    materialize_ms=400.0 / workers,
                )
                for position, workers in enumerate((4, 1))
            ]
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": position,
                        "build_threads": workers,
                        "child_dir": children[position].relative_to(root),
                        "status": "ok",
                    }
                    for position, workers in enumerate((4, 1))
                ],
            )

            with self.assertRaisesRegex(ValueError, "unique and sorted"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[4, 1],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_invalid_rotated_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            children = [
                write_child(
                    root,
                    repeat=0,
                    position=position,
                    workers=workers,
                    total_ms=1000.0 / workers,
                    materialize_ms=400.0 / workers,
                )
                for position, workers in enumerate((1, 4))
            ]
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": 0,
                        "build_threads": workers,
                        "child_dir": child.relative_to(root),
                        "status": "ok",
                    }
                    for workers, child in zip((1, 4), children)
                ],
            )

            with self.assertRaisesRegex(ValueError, "rotated worker schedule"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1, 4],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_child_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp)
            root = top / "campaign"
            root.mkdir()
            outside = write_child(
                top / "outside",
                repeat=0,
                position=0,
                workers=1,
                total_ms=1000.0,
                materialize_ms=400.0,
            )
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": 0,
                        "build_threads": 1,
                        "child_dir": outside,
                        "status": "ok",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "escapes campaign root"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_stale_child_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = write_child(
                root,
                repeat=0,
                position=0,
                workers=1,
                total_ms=1000.0,
                materialize_ms=400.0,
            )
            with (child / "runs.csv").open("a") as handle:
                handle.write("tampered\n")
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": 0,
                        "build_threads": 1,
                        "child_dir": child.relative_to(root),
                        "status": "ok",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "child SHA256SUMS"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_nonfinite_worker_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = write_child(
                root,
                repeat=0,
                position=0,
                workers=1,
                total_ms=1000.0,
                materialize_ms=400.0,
            )
            runs = child / "runs.csv"
            with runs.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["qps"] = str(math.inf)
            with runs.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            seal_tree(child)
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": 0,
                        "build_threads": 1,
                        "child_dir": child.relative_to(root),
                        "status": "ok",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "finite qps"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_aggregates_rotated_repeats_and_computes_speedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index_rows = []
            for repeat in range(3):
                order = (1, 4) if repeat % 2 == 0 else (4, 1)
                for position, workers in enumerate(order):
                    child = write_child(
                        root,
                        repeat=repeat,
                        position=position,
                        workers=workers,
                        total_ms=(1000.0 if workers == 1 else 625.0) + repeat,
                        materialize_ms=(400.0 if workers == 1 else 100.0) + repeat,
                    )
                    index_rows.append(
                        {
                            "repeat": repeat,
                            "position": position,
                            "build_threads": workers,
                            "child_dir": child.relative_to(root),
                            "status": "ok",
                        }
                    )
            index_path = root / "cell_index.csv"
            with index_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(index_rows[0]))
                writer.writeheader()
                writer.writerows(index_rows)

            runs, summaries, comparison = summary.summarize_campaign(
                index_path,
                expected_threads=[1, 4],
                expected_repeats=3,
                expected_sha=SHA,
                expected_compute_host=HOST,
            )

            self.assertEqual(len(runs), 6)
            self.assertEqual([row["build_workers"] for row in summaries], [1, 4])
            self.assertEqual([row["rank_workers"] for row in summaries], [1, 4])
            fast = summaries[1]
            self.assertAlmostEqual(fast["build_materialize_speedup_vs_t1"], 401 / 101)
            self.assertAlmostEqual(fast["build_total_speedup_vs_t1"], 1001 / 626)
            self.assertEqual(comparison["identity"]["selection_hash"], 17395791157795909511)
            self.assertEqual(comparison["identity"]["result_hash"], 8822020575971608226)
            self.assertEqual(comparison["identity"]["physical_signature"], "8" * 64)
            self.assertEqual(comparison["identity"]["input_signature"], "6" * 64)
            self.assertEqual(comparison["identity"]["source_tree_sha256"], "5" * 64)
            self.assertEqual(comparison["identity"]["compute_host"], HOST)
            self.assertEqual(
                comparison["identity"]["map_hash_scope"],
                "global_budget_map_source_bytes",
            )
            self.assertEqual(comparison["identity"]["budget_map_owner_mn"], 0)
            self.assertEqual(comparison["best_materialize"]["build_workers"], 4)

    def test_rejects_selection_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child1 = write_child(
                root,
                repeat=0,
                position=0,
                workers=1,
                total_ms=1000.0,
                materialize_ms=400.0,
            )
            child4 = write_child(
                root,
                repeat=0,
                position=1,
                workers=4,
                total_ms=600.0,
                materialize_ms=100.0,
            )
            runs_path = child4 / "runs.csv"
            with runs_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["selection_hash"] = "99"
            with runs_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            seal_tree(child4)
            index = root / "cell_index.csv"
            with index.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=("repeat", "position", "build_threads", "child_dir", "status"),
                )
                writer.writeheader()
                writer.writerows(
                    [
                        {"repeat": 0, "position": 0, "build_threads": 1, "child_dir": child1.relative_to(root), "status": "ok"},
                        {"repeat": 0, "position": 1, "build_threads": 4, "child_dir": child4.relative_to(root), "status": "ok"},
                    ]
                )

            with self.assertRaisesRegex(ValueError, "selection hash drift"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1, 4],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_record_payload_drift_across_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            children = [
                write_child(
                    root,
                    repeat=0,
                    position=position,
                    workers=workers,
                    total_ms=1000.0 / workers,
                    materialize_ms=400.0 / workers,
                )
                for position, workers in enumerate((1, 4))
            ]
            runs_path = children[1] / "runs.csv"
            with runs_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["record_payload_hashes"] = "f" * 16
            rows[0]["physical_signature"] = "9" * 64
            with runs_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            seal_tree(children[1])
            index = root / "cell_index.csv"
            with index.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "repeat",
                        "position",
                        "build_threads",
                        "child_dir",
                        "status",
                    ),
                )
                writer.writeheader()
                for position, workers in enumerate((1, 4)):
                    writer.writerow(
                        {
                            "repeat": 0,
                            "position": position,
                            "build_threads": workers,
                            "child_dir": children[position].relative_to(root),
                            "status": "ok",
                        }
                    )

            with self.assertRaisesRegex(ValueError, "record-payload hashes drift"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1, 4],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )

    def test_rejects_compute_host_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = write_child(
                root,
                repeat=0,
                position=0,
                workers=1,
                total_ms=1000.0,
                materialize_ms=400.0,
            )
            runs_path = child / "runs.csv"
            with runs_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["compute_host"] = "skv-node1"
            with runs_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            seal_tree(child)
            index = write_index(
                root,
                [
                    {
                        "repeat": 0,
                        "position": 0,
                        "build_threads": 1,
                        "child_dir": child.relative_to(root),
                        "status": "ok",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "compute host drift"):
                summary.summarize_campaign(
                    index,
                    expected_threads=[1],
                    expected_repeats=1,
                    expected_sha=SHA,
                    expected_compute_host=HOST,
                )


if __name__ == "__main__":
    unittest.main()
