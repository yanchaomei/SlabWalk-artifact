from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from experiments.sigmetrics import vldb_evidence_bundle as evidence
from experiments.sigmetrics import verify_vldb_binary_ab as verifier


HERE = Path(__file__).parent
SCRIPT = HERE / "run_vldb_binary_ab.sh"


def _write_fake_evidence_helper(root: Path) -> None:
    helper = root / "fake_evidence.py"
    helper.write_text(
        textwrap.dedent(
            r"""
            import hashlib
            import json
            import os
            import socket
            import subprocess
            import sys
            import uuid
            from pathlib import Path


            def emit():
                out = Path(os.environ["OUT"])
                binary = Path(os.environ["GB_BIN"])
                binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                dataset = os.environ["DATASETS"]
                method = os.environ["METHODS"]
                tag = (
                    f"{dataset}_{method}_T{os.environ['THREADS']}_"
                    f"C{os.environ['COROUTINES']}_ef{os.environ['EF']}"
                )
                result_path = out / f"{tag}.json"
                stderr_path = out / f"{tag}.err"
                result = json.loads(result_path.read_text())
                result["queries"].setdefault("local_result_hash_version", 1)
                result_path.write_text(json.dumps(result))

                lines = stderr_path.read_text().splitlines() if stderr_path.exists() else []
                rewritten = []
                defaults = {
                    "header_bytes": 50,
                    "budget_map_bytes": 50,
                    "placement_padding_bytes": 0,
                    "offset_table_bytes": 0,
                    "record_bytes": 900,
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
                    "selected_uid_hash": "0000000000000005",
                }
                prefix = "LAVD_PHYSICAL_ACCOUNTING "
                for line in lines:
                    if line.startswith(prefix):
                        payload = json.loads(line[len(prefix):])
                        for key, value in defaults.items():
                            payload.setdefault(key, value)
                        line = prefix + json.dumps(payload)
                    rewritten.append(line)
                stderr_path.write_text("\n".join(rewritten) + ("\n" if rewritten else ""))

                mn_out = out / f"{tag}.mn.out"
                mn_err = out / f"{tag}.mn.err"
                mn_out.write_text("memory-node stdout\n")
                mn_err.write_text("memory-node stderr\n")
                inputs = {
                    "query": {
                        "path": f"/fake/{dataset}/query.fbin",
                        "sha256": "1" * 64,
                    },
                    "ground_truth": (
                        {
                            "path": f"/fake/{dataset}/groundtruth.ivecs",
                            "sha256": "2" * 64,
                        }
                        if os.environ["COMPUTE_RECALL"] == "1"
                        else None
                    ),
                    "index": [{
                        "host": "fake-mn",
                        "path": f"/fake/{dataset}/index.dat",
                        "sha256": "3" * 64,
                    }],
                }
                input_signature = hashlib.sha256(
                    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                campaign_path = out / "campaign.json"
                campaign = json.loads(campaign_path.read_text())
                protocol = campaign["protocol"]
                protocol.setdefault("compute_host", socket.gethostname())
                campaign.update({
                    "campaign_id": os.environ["CAMPAIGN_ID"],
                    "campaign_uuid": str(uuid.uuid4()),
                    "protocol_fingerprint": hashlib.sha256(
                        json.dumps(
                            protocol, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                })
                campaign_path.write_text(json.dumps(campaign, sort_keys=True))
                expected_checks = {
                    "query_sha256": inputs["query"]["sha256"],
                    "ground_truth_sha256": (
                        inputs["ground_truth"]["sha256"]
                        if inputs["ground_truth"] else None
                    ),
                    "index_sha256": inputs["index"][0]["sha256"],
                }
                artifacts = {}
                for key, path in {
                    "compute_stdout": result_path,
                    "compute_stderr": stderr_path,
                    "memory_node_stdout": mn_out,
                    "memory_node_stderr": mn_err,
                }.items():
                    artifacts[key] = {
                        "path": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                provenance = {
                    "schema_version": 1,
                    "dataset": dataset,
                    "method": method,
                    "campaign": {
                        "campaign_id": campaign["campaign_id"],
                        "campaign_uuid": campaign["campaign_uuid"],
                        "protocol_fingerprint": campaign["protocol_fingerprint"],
                    },
                    "executables": {
                        "compute_node": {
                            "host": socket.gethostname(),
                            "configured_path": str(binary),
                            "pid_exe_path": str(binary.resolve()),
                            "sha256": binary_sha,
                        },
                        "memory_nodes": [{
                            "host": "fake-mn",
                            "configured_path": str(binary),
                            "pid_exe_path": str(binary.resolve()),
                            "sha256": binary_sha,
                        }],
                    },
                    "inputs": inputs,
                    "input_signature": input_signature,
                    "input_verification": {
                        "pre_run": expected_checks,
                        "post_run": expected_checks,
                    },
                    "artifacts": artifacts,
                }
                (out / f"{tag}.provenance.json").write_text(
                    json.dumps(provenance, indent=2, sort_keys=True)
                )
                subprocess.run(
                    [
                        sys.executable,
                        os.environ["EVIDENCE_TOOL"],
                        "seal",
                        "--root",
                        str(out),
                        "--campaign",
                        str(campaign_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
            """
        ).lstrip()
    )


class VldbBinaryAbHarnessTest(unittest.TestCase):
    def test_campaign_uses_frozen_harness_and_recursive_seal(self):
        text = SCRIPT.read_text()
        for token in (
            "vldb_evidence_bundle.py",
            "VLDB_BINARY_AB_HARNESS_FROZEN",
            "verify-harness",
            "HARNESS_MANIFEST_SHA256",
            "campaign_uuid",
            "seal --root",
            "verify --root",
            "SEALED.json",
            "order_stratified",
            'PHYSICAL_HASH_VERSION = 2',
            '"map_hash_scope"',
            '"budget_map_owner_mn"',
        ):
            self.assertIn(token, text)
        self.assertNotIn('output = root / "SHA256SUMS"', text)

    def test_formal_evidence_gates_are_fail_closed_by_default(self):
        text = SCRIPT.read_text()
        self.assertIn("CAPTURE_BUILD_METRICS=${CAPTURE_BUILD_METRICS:-1}", text)
        self.assertIn(
            "REQUIRE_QUERY_INVARIANTS=${REQUIRE_QUERY_INVARIANTS:-1}", text
        )

    def test_source_identity_covers_every_binary_compilation_input(self):
        text = SCRIPT.read_text()
        for path in (
            '"graphbeyond/CMakeLists.txt"',
            '"graphbeyond/src"',
            '"graphbeyond/rdma-library/CMakeLists.txt"',
            '"graphbeyond/rdma-library/FindIBVerbs.cmake"',
            '"graphbeyond/rdma-library/library"',
            '"graphbeyond/thirdparty"',
        ):
            self.assertIn(path, text)

    def test_formal_mode_rejects_odd_repeat_count(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                env={
                    **os.environ,
                    "BIN_A": str(root / "baseline"),
                    "BIN_B": str(root / "optimized"),
                    "RUNNER": "/usr/bin/false",
                    "OUT_ROOT": str(root / "out"),
                    "REPEATS": "3",
                    "CAMPAIGN_KIND": "formal",
                },
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("even", completed.stderr)

    def test_identical_binary_requires_explicit_configuration_mode(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            binary = root / "candidate"
            binary.write_text("#!/bin/sh\n# one binary\n")
            binary.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                env={
                    **os.environ,
                    "BIN_A": str(binary),
                    "BIN_B": str(binary),
                    "RUNNER": "/usr/bin/false",
                    "OUT_ROOT": str(root / "out"),
                    "REPEATS": "1",
                    "CAMPAIGN_KIND": "smoke",
                },
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("identical binary", completed.stderr)

    def test_manifest_marks_missing_git_metadata_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            script = root / "experiments" / "sigmetrics" / SCRIPT.name
            script.parent.mkdir(parents=True)
            script.write_bytes(SCRIPT.read_bytes())
            script.chmod(0o755)
            source_file = root / "graphbeyond" / "CMakeLists.txt"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("cmake_minimum_required(VERSION 3.16)\n")
            source_header = root / "graphbeyond" / "src" / "fixture.hh"
            source_header.parent.mkdir(parents=True)
            source_header.write_text("#pragma once\n")
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            out = root / "out"
            env = {
                **os.environ,
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": "/usr/bin/false",
                "EVIDENCE_TOOL": str(HERE / "vldb_evidence_bundle.py"),
                "OUT_ROOT": str(out),
                "REPEATS": "1",
                "CAMPAIGN_KIND": "smoke",
            }
            completed = subprocess.run(
                ["bash", str(script)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            source = json.loads((out / "campaign.json").read_text())["protocol"][
                "source"
            ]
            self.assertFalse(source["git_available"])
            self.assertIsNone(source["git_head"])
            self.assertIsNone(source["git_dirty"])

    def test_manifest_binds_each_binary_to_its_own_source_tree(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            source_a = root / "source-a"
            source_b = root / "source-b"
            for source, payload, subdir in (
                (source_a, "old", Path()),
                (source_b, "new", Path("graphbeyond")),
            ):
                path = source / subdir / "CMakeLists.txt"
                path.parent.mkdir(parents=True)
                path.write_text(payload)
                header = source / subdir / "src" / "fixture.hh"
                header.parent.mkdir(parents=True)
                header.write_text(payload)
            out = root / "out"
            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                env={
                    **os.environ,
                    "BIN_A": str(root / "baseline"),
                    "BIN_B": str(root / "optimized"),
                    "SOURCE_ROOT_A": str(source_a),
                    "SOURCE_ROOT_B": str(source_b),
                    "RUNNER": "/usr/bin/false",
                    "OUT_ROOT": str(out),
                    "REPEATS": "1",
                    "CAMPAIGN_KIND": "smoke",
                },
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            protocol = json.loads((out / "campaign.json").read_text())["protocol"]
            self.assertEqual(protocol["source_identity_version"], 2)
            source_records = {
                variant: protocol["variants"][variant]["source"]
                for variant in ("A", "B")
            }
            self.assertEqual(source_records["A"]["root"], str(source_a.resolve()))
            self.assertEqual(source_records["B"]["root"], str(source_b.resolve()))
            self.assertEqual(source_records["A"]["layout"], "graphbeyond_project")
            self.assertEqual(source_records["B"]["layout"], "repository")
            self.assertNotEqual(
                source_records["A"]["tree_sha256"],
                source_records["B"]["tree_sha256"],
            )

    def test_alternates_order_and_summarizes_complete_runs(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            _write_fake_evidence_helper(root)
            runner = root / "fake_runner.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import hashlib, json, os, socket
                    from pathlib import Path

                    assert os.environ["DATASETS"] == "DEEP1M"
                    assert os.environ["CAPTURE_PERF"] == "0"
                    assert os.environ["COMPUTE_RECALL"] == "1"
                    assert os.environ["QUERY_CONTEXTS"] in {"4", "10"}
                    out = Path(os.environ["OUT"])
                    out.mkdir(parents=True, exist_ok=True)
                    binary = Path(os.environ["GB_BIN"])
                    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                    gate = int(os.environ["GB_SQ8_PREFIX_GATE"])
                    assert os.environ["GB_QUERY_LATENCY"] == "1"
                    qps = 1100 if gate else 1000
                    json.dump({"protocol": {
                                  "binary_sha256": sha,
                                  "compute_host": socket.gethostname(),
                              }},
                              open(out / "campaign.json", "w"))
                    json.dump({
                        "num_queries": 10000,
                        "timings": {
                            "lavd_build_multi": 100 if gate else 200,
                            "lavd_build_rank": 10 if gate else 40,
                            "lavd_build_materialize": 20 if gate else 100,
                            "lavd_build_record_assemble": 12 if gate else 0,
                            "lavd_build_record_publish": 3 if gate else 0,
                        },
                        "queries": {
                            "processed": 10000,
                            "queries_per_sec": qps,
                            "recall": 0.9,
                            "rdma_posts": 100000,
                            "rdma_reads_in_bytes": 2000000,
                            "local_latency_p50_us": 100 if gate else 110,
                            "local_latency_p95_us": 200 if gate else 220,
                            "local_latency_p99_us": 300 if gate else 330,
                            "local_sq8_prefix_rejections": 25000 if gate else 0,
                            "local_result_hash": 424242,
                        },
                    }, open(out / "DEEP1M_slabwalk_T10_C2_ef200.json", "w"))
                    publication = {
                        "version": 1,
                        "mode": "staged" if gate else "serial",
                        "workers": 20 if gate else 1,
                        "staging_bytes": 67108864 if gate else 0,
                        "records": 100,
                        "record_write_posts": 2 if gate else 100,
                    }
                    policy = {
                        "version": 1,
                        "policy": "indeg",
                        "requested_bytes": 1000,
                        "fixed_bytes": 100,
                        "record_bytes": 900,
                        "admitted_bytes": 1000,
                        "unused_bytes": 0,
                        "selected_records": 100,
                        "total_records": 1000,
                        "selection_hash": 1234,
                        "total_benefit": 5678,
                        "rank_workers": 20,
                        "budget_map_required": True,
                    }
                    physical = {
                        "descriptor_version": 3,
                        "mn": 0,
                        "num_mns": 1,
                        "max_record_bytes": 4096,
                        "max_degree": 64,
                        "colocated_degree": 64,
                        "slot_only": False,
                        "budget_map_required": True,
                        "materialized_bytes": 1000,
                        "actual_write_bytes": 1000,
                    }
                    with open(out / "DEEP1M_slabwalk_T10_C2_ef200.err", "w") as handle:
                        handle.write("LAVD_MATERIALIZATION_POLICY " + json.dumps(policy) + "\\n")
                        handle.write("LAVD_BUILD_PUBLICATION " + json.dumps(publication) + "\\n")
                        handle.write("LAVD_PHYSICAL_ACCOUNTING " + json.dumps(physical) + "\\n")
                    from fake_evidence import emit
                    emit()
                    """
                ).lstrip()
            )
            runner.chmod(0o755)
            out = root / "out"
            env = {**os.environ, **{
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": str(runner),
                "OUT_ROOT": str(out),
                "PYTHONPATH": str(root),
                "REPEATS": "2",
                "PORT": "1290",
                "QUERY_CONTEXTS_A": "4",
                "QUERY_CONTEXTS_B": "10",
                "VARIANT_ENV_A": (
                    "GB_SQ8_PREFIX_GATE=0 GB_QUERY_LATENCY=1 "
                    "SHINE_LAVD_HOTSET=indeg SHINE_LAVD_BUDGET_BYTES=1000"
                ),
                "VARIANT_ENV_B": (
                    "GB_SQ8_PREFIX_GATE=1 GB_QUERY_LATENCY=1 "
                    "SHINE_LAVD_HOTSET=indeg SHINE_LAVD_BUDGET_BYTES=1000"
                ),
                "CAPTURE_BUILD_METRICS": "1",
                "REQUIRE_QUERY_INVARIANTS": "1",
            }}
            completed = subprocess.run(
                ["bash", str(SCRIPT)], env=env, check=False, capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            with (out / "runs.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["variant"] for row in rows], ["A", "B", "B", "A"])
            self.assertEqual(
                [row["query_contexts"] for row in rows], ["4", "10", "10", "4"]
            )
            self.assertTrue(all(row["status"] == "ok" for row in rows))
            self.assertTrue(all(row["result_hash"] == "424242" for row in rows))
            self.assertTrue(all(row["rank_workers"] == "20" for row in rows))
            self.assertTrue(
                all(row["compute_host"] == socket.gethostname() for row in rows)
            )
            self.assertTrue(all(row["physical_signature"] for row in rows))
            with (out / "summary.csv").open() as handle:
                summary = list(csv.DictReader(handle))
            self.assertEqual({row["variant"] for row in summary}, {"A", "B"})
            means = {row["variant"]: float(row["qps_mean"]) for row in summary}
            self.assertEqual(means, {"A": 1000.0, "B": 1100.0})
            p99 = {row["variant"]: float(row["p99_us_mean"]) for row in summary}
            self.assertEqual(p99, {"A": 330.0, "B": 300.0})
            rejection_rate = {
                row["variant"]: float(row["sq8_prefix_rejections_per_query_mean"])
                for row in summary
            }
            self.assertEqual(rejection_rate, {"A": 0.0, "B": 2.5})
            comparison = json.loads((out / "comparison.json").read_text())
            self.assertEqual(comparison["compute_host"], socket.gethostname())
            self.assertEqual(comparison["paired_repeats"], 2)
            self.assertEqual(comparison["paired_qps_delta_B_minus_A_mean"], 100.0)
            self.assertEqual(comparison["paired_qps_delta_B_minus_A_ci95"], 0.0)
            self.assertEqual(comparison["paired_qps_speedup_B_over_A_mean"], 1.1)
            self.assertEqual(comparison["paired_qps_speedup_B_over_A_ci95"], 0.0)
            campaign = json.loads((out / "campaign.json").read_text())
            self.assertEqual(campaign["protocol"]["order"], ["AB", "BA"])
            self.assertEqual(
                campaign["protocol"]["variants"]["A"]["query_contexts"], 4
            )
            self.assertEqual(
                campaign["protocol"]["variants"]["B"]["query_contexts"], 10
            )
            self.assertEqual(
                campaign["protocol"]["variants"]["A"]["environment"],
                {
                    "GB_QUERY_LATENCY": "1",
                    "GB_SQ8_PREFIX_GATE": "0",
                    "SHINE_LAVD_BUDGET_BYTES": "1000",
                    "SHINE_LAVD_HOTSET": "indeg",
                },
            )
            self.assertEqual(
                campaign["protocol"]["variants"]["B"]["environment"],
                {
                    "GB_QUERY_LATENCY": "1",
                    "GB_SQ8_PREFIX_GATE": "1",
                    "SHINE_LAVD_BUDGET_BYTES": "1000",
                    "SHINE_LAVD_HOTSET": "indeg",
                },
            )
            self.assertTrue(campaign["protocol"]["require_query_invariants"])
            self.assertEqual(comparison["p99_us_delta_B_minus_A"], -30.0)
            self.assertEqual(
                comparison["sq8_prefix_rejections_per_query_delta_B_minus_A"],
                2.5,
            )
            self.assertEqual(comparison["paired_build_speedup_A_over_B_mean"], 2.0)
            self.assertEqual(
                comparison["paired_materialize_speedup_A_over_B_mean"], 5.0
            )
            self.assertEqual(comparison["paired_rank_speedup_A_over_B_mean"], 4.0)
            self.assertEqual(comparison["record_write_posts_B_over_A"], 0.02)
            self.assertEqual(comparison["order_stratified"]["AB"]["n"], 1)
            self.assertEqual(comparison["order_stratified"]["BA"]["n"], 1)
            self.assertEqual(
                comparison["order_stratified"]["AB"][
                    "qps_speedup_B_over_A_mean"
                ],
                1.1,
            )
            manifest = out / "SHA256SUMS"
            self.assertTrue(manifest.is_file())
            entries = []
            for line in manifest.read_text().splitlines():
                digest, relative = line.split("  ", 1)
                entries.append(relative)
                self.assertEqual(
                    hashlib.sha256((out / relative).read_bytes()).hexdigest(),
                    digest,
                )
            self.assertNotIn("SHA256SUMS", entries)
            self.assertTrue((out / "SEALED.json").is_file())
            evidence.verify_bundle(out)

            report = verifier.validate_bundle(
                out,
                expected_sha_a=hashlib.sha256(
                    (root / "baseline").read_bytes()
                ).hexdigest(),
                expected_sha_b=hashlib.sha256(
                    (root / "optimized").read_bytes()
                ).hexdigest(),
                expected_compute_host=socket.gethostname(),
            )
            self.assertEqual(report["run_count"], 4)
            self.assertEqual(report["paired_repeats"], 2)
            self.assertEqual(report["paired_build_speedup_A_over_B_mean"], 2.0)

            rows[0]["qps"] = "9999"
            with (out / "runs.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            (out / "SEALED.json").unlink()
            (out / "SHA256SUMS").unlink()
            evidence.seal_bundle(out, out / "campaign.json")
            with self.assertRaisesRegex(ValueError, "raw artifact"):
                verifier.validate_bundle(
                    out,
                    expected_sha_a=hashlib.sha256(
                        (root / "baseline").read_bytes()
                    ).hexdigest(),
                    expected_sha_b=hashlib.sha256(
                        (root / "optimized").read_bytes()
                    ).hexdigest(),
                    expected_compute_host=socket.gethostname(),
                )

    def test_rejects_equal_byte_builds_with_different_physical_layouts(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            _write_fake_evidence_helper(root)
            runner = root / "fake_runner.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import hashlib, json, os
                    from pathlib import Path

                    out = Path(os.environ["OUT"])
                    out.mkdir(parents=True, exist_ok=True)
                    binary = Path(os.environ["GB_BIN"])
                    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                    optimized = binary.name == "optimized"
                    json.dump({"protocol": {"binary_sha256": sha}},
                              open(out / "campaign.json", "w"))
                    result = {
                        "num_queries": 10000,
                        "timings": {
                            "lavd_build_multi": 100,
                            "lavd_build_rank": 20,
                            "lavd_build_materialize": 50,
                            "lavd_build_record_assemble": 10,
                            "lavd_build_record_publish": 5,
                        },
                        "queries": {
                            "processed": 10000,
                            "queries_per_sec": 1000,
                            "recall": 0.9,
                            "rdma_posts": 100000,
                            "rdma_reads_in_bytes": 2000000,
                            "local_result_hash": 424242,
                        },
                    }
                    result_path = out / "DEEP1M_slabwalk_T10_C2_ef200.json"
                    json.dump(result, open(result_path, "w"))
                    publication = {
                        "version": 1,
                        "mode": "staged" if optimized else "serial",
                        "workers": 20 if optimized else 1,
                        "staging_bytes": 67108864 if optimized else 0,
                        "records": 100,
                        "record_write_posts": 2,
                    }
                    policy = {
                        "version": 1,
                        "policy": "indeg",
                        "requested_bytes": 1000,
                        "fixed_bytes": 100,
                        "record_bytes": 900,
                        "admitted_bytes": 1000,
                        "unused_bytes": 0,
                        "selected_records": 100,
                        "total_records": 1000,
                        "selection_hash": 1234,
                        "total_benefit": 5678,
                        "rank_workers": 20,
                        "budget_map_required": True,
                    }
                    physical = {
                        "descriptor_version": 3,
                        "mn": 0,
                        "num_mns": 1,
                        "max_record_bytes": 4096,
                        "max_degree": 65 if optimized else 64,
                        "colocated_degree": 64,
                        "slot_only": False,
                        "budget_map_required": True,
                        "materialized_bytes": 1000,
                        "actual_write_bytes": 1000,
                    }
                    with open(result_path.with_suffix(".err"), "w") as handle:
                        handle.write("LAVD_MATERIALIZATION_POLICY " + json.dumps(policy) + "\\n")
                        handle.write("LAVD_BUILD_PUBLICATION " + json.dumps(publication) + "\\n")
                        handle.write("LAVD_PHYSICAL_ACCOUNTING " + json.dumps(physical) + "\\n")
                    from fake_evidence import emit
                    emit()
                    """
                ).lstrip()
            )
            runner.chmod(0o755)
            env = {**os.environ, **{
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": str(runner),
                "OUT_ROOT": str(root / "out"),
                "PYTHONPATH": str(root),
                "REPEATS": "1",
                "CAMPAIGN_KIND": "smoke",
                "CAPTURE_BUILD_METRICS": "1",
                "REQUIRE_QUERY_INVARIANTS": "1",
            }}
            completed = subprocess.run(
                ["bash", str(SCRIPT)], env=env, check=False, capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "A/B build physical layout signature changed", completed.stderr
            )

    def test_query_invariant_gate_rejects_recall_drift(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            _write_fake_evidence_helper(root)
            runner = root / "fake_runner.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import hashlib, json, os
                    from pathlib import Path

                    out = Path(os.environ["OUT"])
                    out.mkdir(parents=True, exist_ok=True)
                    binary = Path(os.environ["GB_BIN"])
                    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                    recall = 0.89 if binary.name == "optimized" else 0.90
                    json.dump({"protocol": {"binary_sha256": sha}},
                              open(out / "campaign.json", "w"))
                    json.dump({
                        "num_queries": 10000,
                        "queries": {
                            "processed": 10000,
                            "queries_per_sec": 1000,
                            "recall": recall,
                            "rdma_posts": 100000,
                            "rdma_reads_in_bytes": 2000000,
                            "local_result_hash": 424242,
                        },
                    }, open(out / "DEEP1M_slabwalk_T10_C2_ef200.json", "w"))
                    from fake_evidence import emit
                    emit()
                    """
                ).lstrip()
            )
            runner.chmod(0o755)
            env = {**os.environ, **{
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": str(runner),
                "OUT_ROOT": str(root / "out"),
                "PYTHONPATH": str(root),
                "REPEATS": "1",
                "CAMPAIGN_KIND": "smoke",
                "REQUIRE_QUERY_INVARIANTS": "1",
                "CAPTURE_BUILD_METRICS": "0",
            }}
            completed = subprocess.run(
                ["bash", str(SCRIPT)], env=env, check=False, capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("query recall changed", completed.stderr)

    def test_supports_long_no_recall_pool_without_mislabeling_recall(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            _write_fake_evidence_helper(root)
            runner = root / "fake_runner.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import hashlib, json, os
                    from pathlib import Path

                    assert os.environ["COMPUTE_RECALL"] == "0"
                    assert os.environ["TILE"] == "20"
                    out = Path(os.environ["OUT"])
                    out.mkdir(parents=True, exist_ok=True)
                    binary = Path(os.environ["GB_BIN"])
                    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                    json.dump({"protocol": {"binary_sha256": sha}},
                              open(out / "campaign.json", "w"))
                    json.dump({
                        "num_queries": 200000,
                        "queries": {
                            "processed": 200000,
                            "queries_per_sec": 1000,
                            "recall": 0.0,
                            "rdma_posts": 2000000,
                            "rdma_reads_in_bytes": 40000000,
                            "local_result_hash": 424242,
                        },
                    }, open(out / "DEEP1M_slabwalk_T10_C2_ef200.json", "w"))
                    from fake_evidence import emit
                    emit()
                    """
                ).lstrip()
            )
            runner.chmod(0o755)
            out = root / "out"
            env = {**os.environ, **{
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": str(runner),
                "OUT_ROOT": str(out),
                "PYTHONPATH": str(root),
                "REPEATS": "1",
                "CAMPAIGN_KIND": "smoke",
                "COMPUTE_RECALL": "0",
                "QUERY_TILE": "20",
                "CAPTURE_BUILD_METRICS": "0",
            }}
            completed = subprocess.run(
                ["bash", str(SCRIPT)], env=env, check=False, capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            campaign = json.loads((out / "campaign.json").read_text())
            self.assertFalse(campaign["protocol"]["compute_recall"])
            self.assertEqual(campaign["protocol"]["query_tile"], 20)
            self.assertEqual(campaign["protocol"]["query_pool_size"], 200000)
            with (out / "runs.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(all(row["processed"] == "200000" for row in rows))
            self.assertTrue(all(row["compute_recall"] == "0" for row in rows))
            self.assertTrue(all(row["query_tile"] == "20" for row in rows))
            comparison = json.loads((out / "comparison.json").read_text())
            self.assertIsNone(comparison["paired_qps_delta_B_minus_A_ci95"])
            self.assertIsNone(comparison["paired_qps_speedup_B_over_A_ci95"])

    def test_propagates_deep10m_dataset_identity(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            for name in ("baseline", "optimized"):
                binary = root / name
                binary.write_text(f"#!/bin/sh\n# {name}\n")
                binary.chmod(0o755)
            _write_fake_evidence_helper(root)
            runner = root / "fake_runner.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import hashlib, json, os
                    from pathlib import Path

                    assert os.environ["DATASETS"] == "DEEP10M"
                    out = Path(os.environ["OUT"])
                    out.mkdir(parents=True, exist_ok=True)
                    binary = Path(os.environ["GB_BIN"])
                    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                    json.dump({"protocol": {"binary_sha256": sha}},
                              open(out / "campaign.json", "w"))
                    json.dump({
                        "num_queries": 10000,
                        "queries": {
                            "processed": 10000,
                            "queries_per_sec": 500,
                            "recall": 0.95,
                            "rdma_posts": 2000000,
                            "rdma_reads_in_bytes": 400000000,
                            "local_result_hash": 424242,
                        },
                    }, open(out / "DEEP10M_slabwalk_T10_C2_ef200.json", "w"))
                    from fake_evidence import emit
                    emit()
                    """
                ).lstrip()
            )
            runner.chmod(0o755)
            out = root / "out"
            env = {**os.environ, **{
                "BIN_A": str(root / "baseline"),
                "BIN_B": str(root / "optimized"),
                "RUNNER": str(runner),
                "OUT_ROOT": str(out),
                "PYTHONPATH": str(root),
                "DATASET": "DEEP10M",
                "REPEATS": "1",
                "CAMPAIGN_KIND": "smoke",
                "CAPTURE_BUILD_METRICS": "0",
            }}
            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            campaign = json.loads((out / "campaign.json").read_text())
            self.assertEqual(campaign["protocol"]["dataset"], "DEEP10M")
            with (out / "runs.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(
                all("DEEP10M_slabwalk" in row["json"] for row in rows)
            )


class VldbBinaryAbEvidenceGateTest(unittest.TestCase):
    def _write_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        binaries = []
        for name in ("baseline", "optimized"):
            binary = root / name
            binary.write_text(f"#!/bin/sh\n# {name}\n")
            binary.chmod(0o755)
            binaries.append(binary)

        runner = root / "evidence_runner.py"
        runner.write_text(
            textwrap.dedent(
                """
                #!/usr/bin/env python3
                import csv
                import hashlib
                import json
                import os
                import socket
                import subprocess
                import sys
                import uuid
                from pathlib import Path

                out = Path(os.environ["OUT"])
                out.mkdir(parents=True, exist_ok=True)
                if (
                    os.environ.get("FAKE_POSITION_TAMPER") == "1"
                    and out.name == "r1_1_A"
                ):
                    runs = out.parent / "runs.csv"
                    with runs.open(newline="") as handle:
                        prior = list(csv.DictReader(handle))
                    prior[0]["position"] = "1"
                    with runs.open("w", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(prior[0]))
                        writer.writeheader()
                        writer.writerows(prior)
                binary = Path(os.environ["GB_BIN"])
                binary_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
                dataset = os.environ["DATASETS"]
                method = os.environ["METHODS"]
                threads = os.environ["THREADS"]
                coroutines = os.environ["COROUTINES"]
                ef = os.environ["EF"]
                tag = f"{dataset}_{method}_T{threads}_C{coroutines}_ef{ef}"
                result_path = out / f"{tag}.json"
                stderr_path = out / f"{tag}.err"
                is_b = binary.name == "optimized"

                protocol = {
                    "binary_sha256": binary_sha,
                    "compute_host": socket.gethostname(),
                }
                campaign = {
                    "campaign_id": os.environ["CAMPAIGN_ID"],
                    "campaign_uuid": str(uuid.uuid4()),
                    "protocol_fingerprint": hashlib.sha256(
                        json.dumps(
                            protocol, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    "protocol": protocol,
                }
                json.dump(campaign, open(out / "campaign.json", "w"))
                result_hash_version = 2 if (
                    is_b and os.environ.get("FAKE_RESULT_VERSION_DRIFT") == "1"
                ) else 1
                json.dump({
                    "num_queries": 10000,
                    "timings": {
                        "lavd_build_multi": 100,
                        "lavd_build_rank": 20,
                        "lavd_build_materialize": 50,
                        "lavd_build_record_assemble": 10,
                        "lavd_build_record_publish": 5,
                    },
                    "queries": {
                        "processed": 10000,
                        "queries_per_sec": (
                            float("nan")
                            if is_b and os.environ.get("FAKE_NAN_QPS") == "1"
                            else 1000
                        ),
                        "recall": 0.9,
                        "rdma_posts": 100000,
                        "rdma_reads_in_bytes": 2000000,
                        "local_result_hash": 424242,
                        "local_result_hash_version": result_hash_version,
                    },
                }, open(result_path, "w"))

                mode = "serial" if not is_b else "staged"
                workers = 1 if not is_b else 20
                if os.environ.get("FAKE_SAME_SERIAL_MODE") == "1":
                    mode, workers = "serial", 1
                publication = {
                    "version": 1,
                    "mode": mode,
                    "workers": workers,
                    "staging_bytes": 0 if mode == "serial" else 67108864,
                    "records": 100,
                    "record_write_posts": 100 if mode == "serial" else 2,
                }
                policy = {
                    "version": 1,
                    "policy": "indeg",
                    "requested_bytes": 1000,
                    "fixed_bytes": 100,
                    "record_bytes": 900,
                    "admitted_bytes": 1000,
                    "unused_bytes": 0,
                    "selected_records": 100,
                    "total_records": 1000,
                    "selection_hash": 1234,
                    "total_benefit": 5678,
                    "rank_workers": 20,
                    "budget_map_required": True,
                }
                physical = {
                    "descriptor_version": 3,
                    "mn": 0,
                    "num_mns": 1,
                    "max_record_bytes": 4096,
                    "max_degree": 64,
                    "colocated_degree": 64,
                    "slot_only": False,
                    "budget_map_required": True,
                    "record_layout": "variable",
                    "scoring_code": "scalar",
                    "scoring_bits": 8,
                    "header_bytes": 50,
                    "budget_map_bytes": 50,
                    "placement_padding_bytes": 0,
                    "offset_table_bytes": 0,
                    "record_bytes": 900,
                    "materialized_bytes": 1000,
                    "actual_write_bytes": (
                        999
                        if os.environ.get("FAKE_WRITER_BYTE_MISMATCH") == "1"
                        else 1000
                    ),
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
                    "selected_uid_hash": "0000000000000005",
                }
                if os.environ.get("FAKE_MISSING_PHYSICAL_HASH") == "1":
                    del physical["record_payload_hash"]
                if os.environ.get("FAKE_MAP_SCOPE_DRIFT") == "1":
                    physical["map_hash_scope"] = "per_mn_budget_map_source_bytes"
                if os.environ.get("FAKE_MAP_OWNER_DRIFT") == "1":
                    physical["budget_map_owner_mn"] = 1
                with stderr_path.open("w") as handle:
                    handle.write("LAVD_MATERIALIZATION_POLICY " + json.dumps(policy) + "\\n")
                    handle.write("LAVD_BUILD_PUBLICATION " + json.dumps(publication) + "\\n")
                    handle.write("LAVD_PHYSICAL_ACCOUNTING " + json.dumps(physical) + "\\n")

                mn_out = out / f"{tag}.mn.out"
                mn_err = out / f"{tag}.mn.err"
                mn_out.write_text("memory-node stdout\\n")
                mn_err.write_text("memory-node stderr\\n")

                if os.environ.get("FAKE_PROVENANCE") != "missing":
                    input_variant = "b" if (
                        is_b and os.environ.get("FAKE_INPUT_DRIFT") == "1"
                    ) else "a"
                    inputs = {
                        "query": {"path": "/fake/query.fbin", "sha256": "1" * 64},
                        "ground_truth": {
                            "path": "/fake/groundtruth.ivecs",
                            "sha256": "2" * 64,
                        },
                        "index": [{
                            "host": "fake-mn",
                            "path": "/fake/index.dat",
                            "sha256": ("3" if input_variant == "a" else "4") * 64,
                        }],
                    }
                    input_signature = hashlib.sha256(
                        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    mn_sha = (
                        "f" * 64
                        if os.environ.get("FAKE_MN_SHA_MISMATCH") == "1"
                        else binary_sha
                    )
                    artifacts = {}
                    for key, path in {
                        "compute_stdout": result_path,
                        "compute_stderr": stderr_path,
                        "memory_node_stdout": mn_out,
                        "memory_node_stderr": mn_err,
                    }.items():
                        artifacts[key] = {
                            "path": path.name,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    compute_node = {
                        "host": socket.gethostname(),
                        "configured_path": str(binary),
                        "pid_exe_path": str(binary.resolve()),
                        "sha256": binary_sha,
                    }
                    if os.environ.get("FAKE_COMPUTE_HOST_MISSING") == "1":
                        del compute_node["host"]
                    provenance = {
                        "schema_version": 1,
                        "dataset": dataset,
                        "method": method,
                        "campaign": {
                            "campaign_id": campaign["campaign_id"],
                            "campaign_uuid": campaign["campaign_uuid"],
                            "protocol_fingerprint": campaign[
                                "protocol_fingerprint"
                            ],
                        },
                        "executables": {
                            "compute_node": compute_node,
                            "memory_nodes": [{
                                "host": "fake-mn",
                                "configured_path": str(binary),
                                "pid_exe_path": str(binary.resolve()),
                                "sha256": mn_sha,
                            }],
                        },
                        "inputs": inputs,
                        "input_signature": input_signature,
                        "input_verification": {
                            "pre_run": {
                                "query_sha256": inputs["query"]["sha256"],
                                "ground_truth_sha256": inputs[
                                    "ground_truth"
                                ]["sha256"],
                                "index_sha256": inputs["index"][0]["sha256"],
                            },
                            "post_run": {
                                "query_sha256": inputs["query"]["sha256"],
                                "ground_truth_sha256": inputs[
                                    "ground_truth"
                                ]["sha256"],
                                "index_sha256": inputs["index"][0]["sha256"],
                            },
                        },
                        "artifacts": artifacts,
                    }
                    json.dump(
                        provenance,
                        open(out / f"{tag}.provenance.json", "w"),
                        indent=2,
                        sort_keys=True,
                    )
                subprocess.run(
                    [
                        sys.executable,
                        os.environ["EVIDENCE_TOOL"],
                        "seal",
                        "--root",
                        str(out),
                        "--campaign",
                        str(out / "campaign.json"),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                """
            ).lstrip()
        )
        runner.chmod(0o755)
        return binaries[0], binaries[1], runner

    def _run(self, root: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        baseline, optimized, runner = self._write_fixture(root)
        env = {
            **os.environ,
            "BIN_A": str(baseline),
            "BIN_B": str(optimized),
            "RUNNER": str(runner),
            "OUT_ROOT": str(root / "out"),
            "REPEATS": "1",
            "CAMPAIGN_KIND": "smoke",
            "CAPTURE_BUILD_METRICS": "0",
            "REQUIRE_QUERY_INVARIANTS": "1",
            **overrides,
        }
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_shine_method_is_bound_in_campaign_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            completed = self._run(root, METHOD="shine")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            campaign = json.loads((root / "out" / "campaign.json").read_text())
            self.assertEqual(campaign["protocol"]["method"], "shine")
            with (root / "out" / "runs.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertTrue(all("_shine_" in row["json"] for row in rows))
            report = verifier.validate_bundle(
                root / "out",
                expected_sha_a=hashlib.sha256(
                    (root / "baseline").read_bytes()
                ).hexdigest(),
                expected_sha_b=hashlib.sha256(
                    (root / "optimized").read_bytes()
                ).hexdigest(),
                expected_compute_host=socket.gethostname(),
            )
            self.assertEqual(report["method"], "shine")
            self.assertFalse(report["capture_build_metrics"])

    def test_rejects_unknown_method_before_running(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), METHOD="partition")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("METHOD must be slabwalk or shine", completed.stderr)

    def test_rejects_missing_execution_provenance(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), FAKE_PROVENANCE="missing")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("execution provenance", completed.stderr)

    def test_rejects_memory_node_binary_sha_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), FAKE_MN_SHA_MISMATCH="1")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("memory-node binary SHA", completed.stderr)

    def test_rejects_missing_compute_host(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                FAKE_COMPUTE_HOST_MISSING="1",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("compute host", completed.stderr)

    def test_rejects_input_signature_drift_between_variants(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), FAKE_INPUT_DRIFT="1")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("input signature changed", completed.stderr)

    def test_rejects_query_result_hash_version_drift(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), FAKE_RESULT_VERSION_DRIFT="1")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("query-result hash version", completed.stderr)

    def test_rejects_wrong_serial_staged_mode_assignment(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                CAPTURE_BUILD_METRICS="1",
                FAKE_SAME_SERIAL_MODE="1",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("expected build mode", completed.stderr)

    def test_rejects_missing_physical_payload_hash(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                CAPTURE_BUILD_METRICS="1",
                FAKE_MISSING_PHYSICAL_HASH="1",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("record_payload_hash", completed.stderr)

    def test_rejects_budget_map_hash_scope_drift(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                CAPTURE_BUILD_METRICS="1",
                FAKE_MAP_SCOPE_DRIFT="1",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("map_hash_scope drift", completed.stderr)

    def test_rejects_budget_map_owner_drift(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                CAPTURE_BUILD_METRICS="1",
                FAKE_MAP_OWNER_DRIFT="1",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("budget_map_owner_mn drift", completed.stderr)

    def test_rejects_nonfinite_qps(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(Path(tmp_s), FAKE_NAN_QPS="1")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("finite QPS", completed.stderr)

    def test_rejects_unclosed_writer_byte_ledger(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                CAPTURE_BUILD_METRICS="1",
                FAKE_WRITER_BYTE_MISMATCH="1",
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("writer-byte", completed.stderr)

    def test_rejects_tampered_ab_position_schedule(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            completed = self._run(
                Path(tmp_s),
                REPEATS="2",
                CAMPAIGN_KIND="formal",
                FAKE_POSITION_TAMPER="1",
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("position schedule", completed.stderr)

    def test_explicit_same_binary_configuration_campaign_is_labeled(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            completed = self._run(
                root,
                BIN_B=str(root / "baseline"),
                ALLOW_IDENTICAL_BINARY_SHA="1",
                VARIANT_ENV_A="FAKE_CONFIG=serial",
                VARIANT_ENV_B="FAKE_CONFIG=staged",
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            campaign = json.loads((root / "out" / "campaign.json").read_text())
            self.assertEqual(
                campaign["protocol"]["comparison_kind"],
                "same_binary_configuration",
            )


if __name__ == "__main__":
    unittest.main()
