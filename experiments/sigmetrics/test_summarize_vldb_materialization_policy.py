import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.sigmetrics import summarize_vldb_materialization_policy as summary
from experiments.sigmetrics import vldb_evidence_bundle as evidence_bundle


BUNDLE_SHA = "a" * 64
BUNDLE_HOST = "skv-node3"
BUNDLE_BUDGET = 536_870_912


def policy_protocol(*, repeats: int = 2) -> dict:
    return {
        "datasets": ["DEEP1M"],
        "policies": ["benefit", "indeg", "hop"],
        "budget_bytes": [536_870_912],
        "repeats": repeats,
        "staged_build": True,
        "build_threads": 20,
        "compute_host": "skv-node3",
    }


def policy_row(
    *,
    repeat: int,
    position: int,
    policy: str,
    result_hash: int = 424242,
) -> dict:
    return {
        "dataset": "DEEP1M",
        "repeat": repeat,
        "position": position,
        "policy": policy,
        "binary_sha256": "a" * 64,
        "input_signature": "c" * 64,
        "source_tree_sha256": "e" * 64,
        "compute_host": "skv-node3",
        "requested_bytes": 536_870_912,
        "fixed_bytes": 12_016_392,
        "record_bytes": 524_854_480,
        "admitted_bytes": 536_870_872,
        "physical_bytes": 536_870_872,
        "unused_bytes": 40,
        "selected_records": 176_200,
        "total_records": 1_000_000,
        "selection_hash": 1234,
        "result_hash_version": 1,
        "result_hash": result_hash,
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
        "physical_signature": f"{policy[0]}" * 64,
        "total_benefit": 169_574_781,
        "build_mode": "staged",
        "build_workers": 20,
        "rank_workers": 20,
        "rank_workers_recorded": 1,
        "staging_bytes": 67_108_864,
        "record_write_posts": 8,
        "processed": 10_000,
        "qps": 406.0,
        "recall": 0.98907,
        "p50_us": 8_000.0,
        "p95_us": 9_000.0,
        "p99_us": 10_000.0,
        "posts_per_query": 348.1768,
        "bytes_per_query": 915_559.74,
        "build_total_ms": 10_630.0,
        "build_rank_ms": 5_323.0,
        "build_materialize_ms": 3_909.0,
        "build_record_assemble_ms": 1_200.0,
        "build_record_publish_ms": 40.0,
    }


def complete_policy_rows(*, repeats: int = 2) -> list[dict]:
    policies = policy_protocol(repeats=repeats)["policies"]
    rows = []
    for repeat in range(repeats):
        rotation = repeat % len(policies)
        for position in range(len(policies)):
            policy = policies[(position + rotation) % len(policies)]
            rows.append(
                policy_row(
                    repeat=repeat,
                    position=position,
                    policy=policy,
                )
            )
    return rows


def write_policy_bundle(root: Path) -> Path:
    root.mkdir(parents=True)
    policies = ["benefit", "indeg", "hop"]
    protocol = {
        "binary_sha256": BUNDLE_SHA,
        "memory_node_binary_sha256": BUNDLE_SHA,
        "datasets": ["DEEP1M"],
        "policies": policies,
        "budget_bytes": [BUNDLE_BUDGET],
        "repeats": 3,
        "campaign_kind": "formal",
        "warmups": 0,
        "workers": 16,
        "coroutines": 4,
        "query_contexts": 16,
        "staged_build": True,
        "build_threads": 20,
        "compute_host": BUNDLE_HOST,
        "input_signatures": {"DEEP1M": "c" * 64},
        "source": {"tree_sha256": "e" * 64},
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    campaign = {
        "campaign_id": "materialization-policy-test",
        "campaign_uuid": "00000000-0000-0000-0000-000000000002",
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }
    campaign_path = root / "campaign.json"
    campaign_path.write_text(json.dumps(campaign, indent=2, sort_keys=True) + "\n")

    rows = []
    for repeat in range(3):
        rotation = repeat % len(policies)
        for position in range(len(policies)):
            policy = policies[(position + rotation) % len(policies)]
            cell = (
                root
                / "raw"
                / "DEEP1M"
                / f"b{BUNDLE_BUDGET}"
                / policy
                / f"r{repeat}"
            )
            cell.mkdir(parents=True)
            policy_index = policies.index(policy)
            result = cell / "result.json"
            stderr = cell / "run.err"
            mn_out = cell / "mn.out"
            mn_err = cell / "mn.err"
            result.write_text(
                json.dumps(
                    {
                        "num_queries": 10_000,
                        "queries": {
                            "processed": 10_000,
                            "queries_per_sec": 400.0 + policy_index + repeat,
                            "recall": 0.98907,
                            "local_latency_p50_us": 8_000.0,
                            "local_latency_p95_us": 9_000.0,
                            "local_latency_p99_us": 10_000.0,
                            "rdma_posts": 3_481_768,
                            "rdma_reads_in_bytes": 9_155_597_400,
                            "local_result_hash_version": 1,
                            "local_result_hash": 424242,
                        },
                        "timings": {
                            "lavd_build_multi": 2_900.0 + repeat,
                            "lavd_build_rank": 220.0,
                            "lavd_build_materialize": 1_000.0 + repeat,
                            "lavd_build_record_assemble": 700.0 + repeat,
                            "lavd_build_record_publish": 45.0,
                        },
                    }
                )
                + "\n"
            )
            selected_uid_hash = f"{policy_index + 5:016x}"
            stderr.write_text(
                "LAVD_MATERIALIZATION_POLICY "
                + json.dumps(
                    {
                        "version": 1,
                        "policy": policy,
                        "requested_bytes": BUNDLE_BUDGET,
                        "fixed_bytes": 12_016_392,
                        "record_bytes": 524_854_480,
                        "admitted_bytes": 536_870_872,
                        "unused_bytes": 40,
                        "selected_records": 176_200,
                        "total_records": 1_000_000,
                        "selection_hash": 1_234 + policy_index,
                        "total_benefit": 169_574_781 + policy_index,
                        "rank_workers": 20,
                    },
                    separators=(",", ":"),
                )
                + "\nLAVD_BUILD_PUBLICATION "
                + json.dumps(
                    {
                        "version": 1,
                        "mode": "staged",
                        "workers": 20,
                        "staging_bytes": 67_108_864,
                        "records": 176_200,
                        "record_write_posts": 8,
                    },
                    separators=(",", ":"),
                )
                + "\nLAVD_PHYSICAL_ACCOUNTING "
                + json.dumps(
                    {
                        "mn": 0,
                        "num_mns": 1,
                        "header_bytes": 16_384,
                        "budget_map_bytes": 4_000_000,
                        "placement_padding_bytes": 0,
                        "offset_table_bytes": 8_000_008,
                        "record_bytes": 524_854_480,
                        "materialized_bytes": 536_870_872,
                        "actual_write_bytes": 536_870_872,
                        "descriptor_version": 3,
                        "max_record_bytes": 4096,
                        "max_degree": 64,
                        "colocated_degree": 64,
                        "slot_only": False,
                        "budget_map_required": True,
                        "record_layout": "variable",
                        "scoring_code": "scalar",
                        "scoring_bits": 8,
                        "hash_version": 2,
                        "hash_algorithm": "fnv1a64",
                        "hash_scope": "field_scoped_physical_artifacts",
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
                        "offset_table_hash": "0000000000000003",
                        "record_payload_hash": "0000000000000004",
                        "selected_uid_hash": selected_uid_hash,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            mn_out.write_text("mn out\n")
            mn_err.write_text("mn err\n")
            artifacts = {}
            for name, artifact in {
                "compute_stdout": result,
                "compute_stderr": stderr,
                "memory_node_stdout": mn_out,
                "memory_node_stderr": mn_err,
            }.items():
                artifacts[name] = {
                    "path": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            (cell / "campaign.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "campaign_id": campaign["campaign_id"],
                        "campaign_uuid": campaign["campaign_uuid"],
                        "protocol_fingerprint": fingerprint,
                        "dataset": "DEEP1M",
                        "budget_bytes": BUNDLE_BUDGET,
                        "policy": policy,
                        "repeat": repeat,
                        "position": position,
                        "kind": "r",
                        "input_signature": "c" * 64,
                        "executables": {
                            "compute_node": {
                                "host": BUNDLE_HOST,
                                "sha256": BUNDLE_SHA,
                            },
                            "memory_node": {"sha256": BUNDLE_SHA},
                        },
                        "input_verification": {
                            "pre_run": {"query": {"sha256": "d" * 64}},
                            "post_run": {"query": {"sha256": "d" * 64}},
                        },
                        "artifacts": artifacts,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            rows.append(
                summary.load_cell(
                    result,
                    stderr,
                    dataset="DEEP1M",
                    repeat=repeat,
                    position=position,
                    expected_policy=policy,
                    expected_budget=BUNDLE_BUDGET,
                    binary_sha256=BUNDLE_SHA,
                    input_signature="c" * 64,
                    source_tree_sha256="e" * 64,
                    compute_host=BUNDLE_HOST,
                )
            )

    summary._write_csv(root / "runs.csv", rows)
    summary._write_csv(
        root / "summary.csv", summary.summarize_rows(rows, protocol=protocol)
    )
    evidence_bundle.seal_bundle(root, campaign_path)
    return root


class MaterializationPolicySummaryTest(unittest.TestCase):
    def test_summary_equivalence_allows_only_cross_runtime_float_ulp_noise(self):
        stored = [
            {
                "dataset": "DEEP1M",
                "n": "6",
                "qps_mean": "1349.5",
                "p95_us_ci95": "103.71943496152815",
            }
        ]
        cross_runtime = [
            {
                "dataset": "DEEP1M",
                "n": "6",
                "qps_mean": "1349.5",
                "p95_us_ci95": "103.71943496152814",
            }
        ]
        changed_measurement = [dict(cross_runtime[0])]
        changed_measurement[0]["p95_us_ci95"] = "103.72"
        changed_identity = [dict(cross_runtime[0])]
        changed_identity[0]["n"] = "7"

        self.assertTrue(summary._equivalent_summary_rows(stored, cross_runtime))
        self.assertFalse(
            summary._equivalent_summary_rows(stored, changed_measurement)
        )
        self.assertFalse(summary._equivalent_summary_rows(stored, changed_identity))

    def test_frozen_summarizer_loads_the_explicit_evidence_module(self):
        source_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            frozen = Path(tmp)
            summarizer = frozen / "summarizer__materialization.py"
            evidence = frozen / "evidence_tool__bundle.py"
            shutil.copy2(
                source_dir / "summarize_vldb_materialization_policy.py",
                summarizer,
            )
            shutil.copy2(source_dir / "vldb_evidence_bundle.py", evidence)
            env = os.environ.copy()
            env["VLDB_EVIDENCE_BUNDLE_MODULE"] = str(evidence)

            completed = subprocess.run(
                [sys.executable, str(summarizer), "--help"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_validates_relocatable_sealed_bundle_semantically(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_policy_bundle(Path(tmp) / "source")
            relocated = Path(tmp) / "relocated"
            shutil.copytree(source, relocated)

            report = summary.validate_bundle(
                relocated,
                expected_sha=BUNDLE_SHA,
                expected_compute_host=BUNDLE_HOST,
            )

            self.assertEqual(report["measured_cells"], 9)
            self.assertEqual(report["policies"], ["benefit", "indeg", "hop"])
            self.assertEqual(report["repeats"], 3)

    def test_semantic_summary_tamper_is_rejected_after_valid_reseal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_policy_bundle(Path(tmp) / "bundle")
            summary_path = root / "summary.csv"
            with summary_path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["qps_mean"] = "1.0"
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
                    expected_sha=BUNDLE_SHA,
                    expected_compute_host=BUNDLE_HOST,
                )

    def test_bundle_validator_rejects_expected_host_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_policy_bundle(Path(tmp) / "bundle")

            with self.assertRaisesRegex(ValueError, "compute host"):
                summary.validate_bundle(
                    root,
                    expected_sha=BUNDLE_SHA,
                    expected_compute_host="skv-node1",
                )

    def test_verify_cli_reports_semantic_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_policy_bundle(Path(tmp) / "bundle")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(summary.__file__)),
                    "verify",
                    "--bundle",
                    str(root),
                    "--expected-sha",
                    BUNDLE_SHA,
                    "--expected-compute-host",
                    BUNDLE_HOST,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(completed.stdout)
            self.assertEqual(report["measured_cells"], 9)
            self.assertEqual(report["compute_host"], BUNDLE_HOST)

    def test_physical_identity_requires_explicit_global_map_scope(self):
        shard = {
            "num_mns": 1,
            "mn": 0,
            "descriptor_version": 3,
            "hash_version": 2,
            "hash_algorithm": "fnv1a64",
            "hash_scope": "field_scoped_physical_artifacts",
            "header_hash_scope": "replicated_header_source_bytes",
            "descriptor_hash_scope": "descriptor_slice_of_replicated_header",
            "offset_table_hash_scope": "per_mn_offset_table_source_bytes",
            "record_payload_hash_scope": "per_mn_record_payload_source_bytes",
            "selected_uid_hash_scope": "global_selected_uid_u32le_sequence",
            "budget_map_owner_mn": 0,
            "budget_map_bytes": 4_000_000,
            "max_record_bytes": 4096,
            "max_degree": 64,
            "colocated_degree": 64,
            "slot_only": False,
            "budget_map_required": True,
            "record_layout": "variable",
            "scoring_code": "scalar",
            "scoring_bits": 8,
            "header_hash": "0000000000000000",
            "descriptor_hash": "0000000000000001",
            "map_hash": "0000000000000002",
            "offset_table_hash": "0000000000000003",
            "record_payload_hash": "0000000000000004",
            "selected_uid_hash": "0000000000000005",
        }

        with self.assertRaisesRegex(ValueError, "map_hash_scope"):
            summary._physical_hash_identity([shard])

    def test_physical_identity_rejects_cross_mn_abi_drift(self):
        base = {
            "num_mns": 2,
            "descriptor_version": 3,
            "hash_version": 2,
            "hash_algorithm": "fnv1a64",
            "hash_scope": "field_scoped_physical_artifacts",
            "header_hash_scope": "replicated_header_source_bytes",
            "descriptor_hash_scope": "descriptor_slice_of_replicated_header",
            "map_hash_scope": "global_budget_map_source_bytes",
            "offset_table_hash_scope": "per_mn_offset_table_source_bytes",
            "record_payload_hash_scope": "per_mn_record_payload_source_bytes",
            "selected_uid_hash_scope": "global_selected_uid_u32le_sequence",
            "budget_map_owner_mn": 0,
            "max_record_bytes": 4096,
            "max_degree": 64,
            "colocated_degree": 64,
            "slot_only": False,
            "budget_map_required": True,
            "record_layout": "variable",
            "scoring_code": "scalar",
            "scoring_bits": 8,
            "header_hash": "0000000000000000",
            "descriptor_hash": "0000000000000001",
            "map_hash": "0000000000000002",
            "offset_table_hash": "0000000000000003",
            "record_payload_hash": "0000000000000004",
            "selected_uid_hash": "0000000000000005",
        }
        shards = [
            {**base, "mn": 0, "budget_map_bytes": 4_000_000},
            {**base, "mn": 1, "budget_map_bytes": 0, "max_degree": 63},
        ]

        with self.assertRaisesRegex(ValueError, "physical ABI drift"):
            summary._physical_hash_identity(shards)

    def test_summary_requires_the_complete_declared_matrix(self):
        rows = complete_policy_rows()

        with self.assertRaisesRegex(ValueError, "incomplete or duplicate"):
            summary.summarize_rows(rows[:-1], protocol=policy_protocol())

    def test_summary_rejects_cross_policy_result_drift(self):
        rows = complete_policy_rows()
        rows[-1]["result_hash"] += 1

        with self.assertRaisesRegex(ValueError, "query-result hash drift"):
            summary.summarize_rows(rows, protocol=policy_protocol())

    def test_summary_rejects_nonfinite_measurements(self):
        rows = complete_policy_rows()
        rows[0]["qps"] = math.nan

        with self.assertRaisesRegex(ValueError, "finite qps"):
            summary.summarize_rows(rows, protocol=policy_protocol())

    def test_summary_rejects_observed_mode_or_worker_mismatch(self):
        rows = complete_policy_rows()
        rows[0]["build_workers"] = 1

        with self.assertRaisesRegex(ValueError, "build worker"):
            summary.summarize_rows(rows, protocol=policy_protocol())

    def test_summary_rejects_compute_host_drift(self):
        rows = complete_policy_rows()
        rows[0]["compute_host"] = "skv-node1"

        with self.assertRaisesRegex(ValueError, "compute host drift"):
            summary.summarize_rows(rows, protocol=policy_protocol())

    def test_summary_accepts_complete_cyclic_protocol(self):
        rows = complete_policy_rows()

        records = summary.summarize_rows(rows, protocol=policy_protocol())

        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["n"] == 2 for record in records))

    def test_summary_rejects_unbalanced_formal_policy_rotation(self):
        protocol = policy_protocol(repeats=2)
        protocol["campaign_kind"] = "formal"

        with self.assertRaisesRegex(ValueError, "position-balanced"):
            summary.summarize_rows(
                complete_policy_rows(repeats=2),
                protocol=protocol,
            )

    def test_load_cell_closes_exact_physical_byte_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result.json"
            stderr = root / "run.err"
            result.write_text(
                json.dumps(
                    {
                        "num_queries": 10_000,
                        "queries": {
                            "processed": 10_000,
                            "queries_per_sec": 406,
                            "recall": 0.98907,
                            "local_latency_p50_us": 8_000.0,
                            "local_latency_p95_us": 9_000.0,
                            "local_latency_p99_us": 10_000.0,
                            "rdma_posts": 3_481_768,
                            "rdma_reads_in_bytes": 9_155_597_400,
                            "local_result_hash_version": 1,
                            "local_result_hash": 424242,
                        },
                        "timings": {
                            "lavd_build_multi": 10_630.0,
                            "lavd_build_rank": 5_323.0,
                            "lavd_build_materialize": 3_909.0,
                            "lavd_build_record_assemble": 1_200.0,
                            "lavd_build_record_publish": 40.0,
                        },
                    }
                )
            )
            stderr.write_text(
                "LAVD_MATERIALIZATION_POLICY "
                '{"version":1,"policy":"benefit",'
                '"requested_bytes":536870912,"fixed_bytes":12016392,'
                '"record_bytes":524854480,"admitted_bytes":536870872,'
                '"unused_bytes":40,"selected_records":176200,'
                '"total_records":1000000,"selection_hash":1234,'
                '"total_benefit":169574781,"reached":1000000,'
                '"rank_workers":20}\n'
                "LAVD_BUILD_PUBLICATION "
                '{"version":1,"mode":"staged","workers":20,'
                '"staging_bytes":67108864,"records":176200,'
                '"record_write_posts":8}\n'
                "LAVD_PHYSICAL_ACCOUNTING "
                '{"mn":0,"num_mns":1,"header_bytes":16384,"budget_map_bytes":4000000,'
                '"placement_padding_bytes":0,"offset_table_bytes":8000008,'
                '"record_bytes":524854480,"materialized_bytes":536870872,'
                '"actual_write_bytes":536870872,"descriptor_version":3,'
                '"max_record_bytes":4096,"max_degree":64,'
                '"colocated_degree":64,"slot_only":false,'
                '"budget_map_required":true,"record_layout":"variable",'
                '"scoring_code":"scalar","scoring_bits":8,'
                '"hash_version":2,"hash_algorithm":"fnv1a64",'
                '"hash_scope":"field_scoped_physical_artifacts",'
                '"header_hash_scope":"replicated_header_source_bytes",'
                '"descriptor_hash_scope":"descriptor_slice_of_replicated_header",'
                '"map_hash_scope":"global_budget_map_source_bytes",'
                '"offset_table_hash_scope":"per_mn_offset_table_source_bytes",'
                '"record_payload_hash_scope":"per_mn_record_payload_source_bytes",'
                '"selected_uid_hash_scope":"global_selected_uid_u32le_sequence",'
                '"budget_map_owner_mn":0,'
                '"header_hash":"0000000000000000",'
                '"descriptor_hash":"0000000000000001",'
                '"map_hash":"0000000000000002",'
                '"offset_table_hash":"0000000000000003",'
                '"record_payload_hash":"0000000000000004",'
                '"selected_uid_hash":"0000000000000005"}\n'
            )

            row = summary.load_cell(
                result,
                stderr,
                dataset="DEEP1M",
                repeat=0,
                position=1,
                expected_policy="benefit",
                expected_budget=536_870_912,
                binary_sha256="a" * 64,
                input_signature="c" * 64,
                source_tree_sha256="e" * 64,
                compute_host="skv-node3",
            )

            self.assertEqual(row["admitted_bytes"], 536_870_872)
            self.assertEqual(row["physical_bytes"], 536_870_872)
            self.assertEqual(row["unused_bytes"], 40)
            self.assertEqual(row["selected_records"], 176_200)
            self.assertAlmostEqual(row["posts_per_query"], 348.1768)
            self.assertAlmostEqual(row["bytes_per_query"], 915_559.74)
            self.assertEqual(row["build_total_ms"], 10_630.0)
            self.assertEqual(row["build_mode"], "staged")
            self.assertEqual(row["record_write_posts"], 8)
            self.assertEqual(row["rank_workers"], 20)
            self.assertEqual(row["rank_workers_recorded"], 1)
            self.assertEqual(row["build_record_assemble_ms"], 1_200.0)
            self.assertEqual(row["build_record_publish_ms"], 40.0)
            self.assertEqual(row["result_hash_version"], 1)
            self.assertEqual(row["result_hash"], 424242)
            self.assertEqual(row["physical_hash_version"], 2)
            self.assertEqual(row["physical_hash_algorithm"], "fnv1a64")
            self.assertEqual(row["record_payload_hashes"], "0000000000000004")
            self.assertEqual(len(row["physical_signature"]), 64)
            self.assertEqual(row["input_signature"], "c" * 64)
            self.assertEqual(row["source_tree_sha256"], "e" * 64)
            self.assertEqual(row["compute_host"], "skv-node3")

            stderr.write_text(
                stderr.read_text().replace(
                    '"record_payload_hash":"0000000000000004",', ""
                )
            )
            with self.assertRaisesRegex(ValueError, "record_payload_hash"):
                summary.load_cell(
                    result,
                    stderr,
                    dataset="DEEP1M",
                    repeat=0,
                    position=1,
                    expected_policy="benefit",
                    expected_budget=536_870_912,
                    binary_sha256="a" * 64,
                    input_signature="c" * 64,
                    source_tree_sha256="e" * 64,
                    compute_host="skv-node3",
                )

    def test_load_cell_rejects_planner_writer_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = root / "result.json"
            stderr = root / "run.err"
            result.write_text(
                json.dumps(
                    {
                        "num_queries": 1,
                        "queries": {
                            "processed": 1,
                            "queries_per_sec": 1,
                            "recall": 1,
                            "rdma_posts": 1,
                            "rdma_reads_in_bytes": 1,
                            "local_result_hash_version": 1,
                            "local_result_hash": 1,
                        },
                        "timings": {},
                    }
                )
            )
            stderr.write_text(
                "LAVD_MATERIALIZATION_POLICY "
                '{"version":1,"policy":"hop","requested_bytes":100,'
                '"fixed_bytes":10,"record_bytes":80,"admitted_bytes":90,'
                '"unused_bytes":10,"selected_records":1,"total_records":2,'
                '"selection_hash":1,"total_benefit":1,"reached":2}\n'
                "LAVD_BUILD_PUBLICATION "
                '{"version":1,"mode":"serial","workers":1,'
                '"staging_bytes":0,"records":1,"record_write_posts":1}\n'
                "LAVD_PHYSICAL_ACCOUNTING "
                '{"mn":0,"num_mns":1,"header_bytes":5,"budget_map_bytes":5,'
                '"placement_padding_bytes":0,"offset_table_bytes":0,'
                '"record_bytes":79,"materialized_bytes":89,'
                '"actual_write_bytes":89,"descriptor_version":3,'
                '"max_record_bytes":4096,"max_degree":64,'
                '"colocated_degree":64,"slot_only":false,'
                '"budget_map_required":true,"record_layout":"variable",'
                '"scoring_code":"scalar","scoring_bits":8,'
                '"hash_version":2,"hash_algorithm":"fnv1a64",'
                '"hash_scope":"field_scoped_physical_artifacts",'
                '"header_hash_scope":"replicated_header_source_bytes",'
                '"descriptor_hash_scope":"descriptor_slice_of_replicated_header",'
                '"map_hash_scope":"global_budget_map_source_bytes",'
                '"offset_table_hash_scope":"per_mn_offset_table_source_bytes",'
                '"record_payload_hash_scope":"per_mn_record_payload_source_bytes",'
                '"selected_uid_hash_scope":"global_selected_uid_u32le_sequence",'
                '"budget_map_owner_mn":0,'
                '"header_hash":"0000000000000000",'
                '"descriptor_hash":"0000000000000001",'
                '"map_hash":"0000000000000002",'
                '"offset_table_hash":"0000000000000003",'
                '"record_payload_hash":"0000000000000004",'
                '"selected_uid_hash":"0000000000000005"}\n'
            )

            with self.assertRaisesRegex(ValueError, "planner/writer"):
                summary.load_cell(
                    result,
                    stderr,
                    dataset="SIFT1M",
                    repeat=0,
                    position=0,
                    expected_policy="hop",
                    expected_budget=100,
                    binary_sha256="b" * 64,
                    input_signature="d" * 64,
                    source_tree_sha256="f" * 64,
                    compute_host="skv-node3",
                )


if __name__ == "__main__":
    unittest.main()
