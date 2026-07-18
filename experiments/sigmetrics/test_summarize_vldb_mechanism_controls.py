from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import summarize_vldb_mechanism_controls as mechanism_summary


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
BUDGET_KEYS = ("f05", "f10", "f25", "f50", "f75", "full")
BUDGET_VALUES = {
    "f05": 0.05,
    "f10": 0.10,
    "f25": 0.25,
    "f50": 0.50,
    "f75": 0.75,
    "full": 1.0,
}
RESIDENT_MODES = ("remote", "resident")
RESIDENT_EFS = (50, 100, 200)


def command_for(control: str, key: str, ef: int) -> list[str]:
    budget = control == "budget"
    return [
        "shine",
        "--servers", "skv-node5",
        "--initiator",
        "--port", "1316",
        "--index-region-bytes", "4294967296",
        "--lavd-region-bytes", "17179869184",
        "--data-path", "/data/gist200k" if budget else "/data/sift1m",
        "--threads", "16" if budget else "1",
        "--coroutines", "8",
        "--query-contexts", "16" if budget else "1",
        "--query-suffix", "uniform",
        "--load-index",
        "--ef-search", str(100 if budget else ef),
        "--ef-construction", "200" if budget else "100",
        "--m", "32" if budget else "16",
        "--k", "10",
        "--label", f"fixture_{control}_{key}_ef{ef}",
        "--spec-k", "1",
        "--lavd", "8",
    ]


def environment_for(control: str, key: str) -> dict[str, str]:
    if control == "budget":
        environment = {
            "SHINE_LAVD_HOTSET": "indeg",
            "SHINE_LAVD_NATIVE_PACKED_WRITE": "1",
            "SHINE_LAVD_VARBLOCK": "1",
            "SHINE_CRANE": "1",
            "GB_BITMAP_DEDUP": "1",
            "GB_QUERY_LATENCY": "1",
        }
        if key != "full":
            environment["SHINE_LAVD_BUDGET"] = str(BUDGET_VALUES[key])
        return environment
    return {
        "SHINE_LAVD_NATIVE_PACKED_WRITE": "1",
        "SHINE_LAVD_VARBLOCK": "1",
        "SHINE_CRANE": "1" if key == "resident" else "0",
        "GB_BITMAP_DEDUP": "1",
        "GB_QUERY_LATENCY": "1",
    }


def write_fixture(
    root: Path,
    *,
    omit: tuple[str, str, int, int] | None = None,
    bad_descriptor: tuple[str, str, int, int] | None = None,
) -> None:
    protocol = {
        "binary_sha256": FINAL_SHA,
        "budget": {
            "dataset": "GIST200K",
            "fractions": list(BUDGET_KEYS),
            "fraction_values": BUDGET_VALUES,
            "threads": 16,
            "query_contexts": 16,
            "coroutines": 8,
            "ef_search": 100,
            "ef_construction": 200,
            "m": 32,
            "queries_per_run": 1000,
            "hotset": "indeg",
        },
        "resident": {
            "dataset": "SIFT1M",
            "modes": list(RESIDENT_MODES),
            "ef_values": list(RESIDENT_EFS),
            "threads": 1,
            "query_contexts": 1,
            "coroutines": 8,
            "ef_construction": 100,
            "m": 16,
            "queries_per_run": 10000,
        },
        "repeats": 5,
        "warmups": 1,
        "top_k": 10,
        "query_suffix": "uniform",
        "scoring_code": "sq8",
        "record_layout": "packed_variable",
        "memory_node": "skv-node5",
        "tcp_port": 1316,
        "index_region_bytes": 4294967296,
        "lavd_region_bytes": 17179869184,
        "gist_index_dump_sha256": "a" * 64,
        "sift_index_dump_sha256": "b" * 64,
        "gist_query_sha256": "2" * 64,
        "gist_groundtruth_sha256": "3" * 64,
        "sift_query_sha256": "4" * 64,
        "sift_groundtruth_sha256": "5" * 64,
        "runner_sha256": "c" * 64,
        "summarizer_sha256": "d" * 64,
        "fingerprint_tool_sha256": "e" * 64,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (root / "campaign.json").write_text(json.dumps({
        "campaign_id": "mechanism-fixture",
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }))
    query_root = root / "query_pools"
    query_root.mkdir()
    for filename, dataset, rows in (
        ("gist200k_slabwalk.json", "GIST200K", 1000),
        ("sift1m_slabwalk.json", "SIFT1M", 10000),
    ):
        (query_root / filename).write_text(json.dumps({
            "kind": "query_pool_fingerprint",
            "dataset": dataset,
            "method": "SlabWalk",
            "metric": "l2",
            "limit": rows,
            "query": {"rows": rows, "canonical_sha256": "f" * 64},
            "groundtruth": {
                "rows": rows,
                "canonical_ids_sha256": "1" * 64,
            },
        }))

    cells = [
        ("budget", key, 100) for key in BUDGET_KEYS
    ] + [
        ("resident", mode, ef)
        for mode in RESIDENT_MODES
        for ef in RESIDENT_EFS
    ]
    for control, key, ef in cells:
        for run_kind, repeats in (("warmup", 1), ("measure", 5)):
            for repeat in range(repeats):
                identity = (control, key, ef, repeat)
                if run_kind == "measure" and omit == identity:
                    continue
                if control == "budget":
                    cell = root / "raw" / "budget" / key / f"{run_kind}_r{repeat}"
                    queries = 1000
                    vectors = 200000
                    threads = contexts = 16
                    m = 32
                    efc = 200
                    fraction = BUDGET_VALUES[key]
                    materialized = int(8_000_000_000 * fraction)
                    budget_map_bytes = 0 if key == "full" else 800_000
                    qps = 14_000 + 2_000 * fraction + repeat
                    posts_upnav = 0
                else:
                    cell = (
                        root / "raw" / "resident" / key / f"ef{ef}"
                        / f"{run_kind}_r{repeat}"
                    )
                    queries = 10000
                    vectors = 1000000
                    threads = contexts = 1
                    m = 16
                    efc = 100
                    materialized = 3_000_000_000
                    budget_map_bytes = 0
                    qps = (5_000 if key == "resident" else 3_800) - ef + repeat
                    posts_upnav = 0 if key == "resident" else queries * 25
                cell.mkdir(parents=True)
                (cell / "manifest.json").write_text(json.dumps({
                    "campaign_id": "mechanism-fixture",
                    "protocol_fingerprint": fingerprint,
                    "control": control,
                    "key": key,
                    "ef": ef,
                    "run_kind": run_kind,
                    "repeat": repeat,
                    "binary_sha256": FINAL_SHA,
                    "observed_inputs": {
                        "cn_binary": FINAL_SHA,
                        "mn_binary": FINAL_SHA,
                        "index_dump": "a" * 64 if control == "budget" else "b" * 64,
                        "query": "2" * 64 if control == "budget" else "4" * 64,
                        "groundtruth": "3" * 64 if control == "budget" else "5" * 64,
                    },
                    "environment": environment_for(control, key),
                    "command": command_for(control, key, ef),
                }))
                posts_l0 = queries * (90 + ef // 10)
                posts_rerank = queries
                rdma_posts = posts_upnav + posts_l0 + posts_rerank
                rdma_bytes = rdma_posts * 2048
                timings = {
                    "lavd_build_multi": 1000.0,
                    "lavd_build_fetch": 100.0,
                    "lavd_build_parse": 20.0,
                    "lavd_build_rank": 10.0,
                    "lavd_build_encode": 200.0,
                    "lavd_build_metadata": 5.0,
                    "lavd_build_materialize": 665.0,
                    "query_max": queries / qps * 1000,
                }
                if control == "budget" or key == "resident":
                    timings["crane_build_multi"] = 25.0
                (cell / "cn.json").write_text(json.dumps({
                    "meta": {
                        "dataset": "gist200k" if control == "budget" else "sift1m",
                        "compute_threads": threads,
                        "coroutines_per_thread": 8,
                        "memory_nodes": 1,
                        "query_suffix": "uniform",
                    },
                    "query_contexts": contexts,
                    "num_queries": queries,
                    "num_vectors": vectors,
                    "distance": "squared_l2",
                    "hnsw_parameters": {
                        "ef_construction": efc,
                        "ef_search": ef,
                        "k": 10,
                        "m": m,
                    },
                    "queries": {
                        "processed": queries,
                        "queries_per_sec": qps,
                        "recall": 0.97 + repeat * 1e-6,
                        "rdma_posts": rdma_posts,
                        "rdma_wrs": rdma_posts,
                        "rdma_reads_in_bytes": rdma_bytes,
                        "posts_upnav": posts_upnav,
                        "posts_l0": posts_l0,
                        "posts_rerank": posts_rerank,
                        "local_latency_p50_us": 700.0,
                        "local_latency_p95_us": 900.0,
                        "local_latency_p99_us": 1100.0,
                        "local_latency_samples": queries,
                    },
                    "timings": timings,
                }))
                descriptor_version = (
                    1 if run_kind == "measure" and bad_descriptor == identity else 2
                )
                account = {
                    "descriptor_version": 2,
                    "policy": "block_cyclic",
                    "record_layout": "variable",
                    "scoring_code": "scalar",
                    "scoring_bits": 8,
                    "total_slots": vectors,
                    "num_mns": 1,
                    "mn": 0,
                    "local_slots": vectors,
                    "header_bytes": 16384,
                    "budget_map_bytes": budget_map_bytes,
                    "placement_padding_bytes": 0,
                    "offset_table_bytes": 8 * (vectors + 1),
                    "record_bytes": materialized - budget_map_bytes - 16384,
                    "materialized_bytes": materialized,
                    "registered_bytes": 16 * 1024**3,
                    "actual_write_bytes": materialized,
                }
                lines = [
                    "LAVD_PHYSICAL_ACCOUNTING " + json.dumps(account),
                    "[LAVD][native] packed addressing restored from descriptor: "
                    f"N={vectors} mns=1 policy=block_cyclic layout=variable "
                    f"descriptor_version={descriptor_version} "
                    f"packed_bytes={materialized} sparse_bytes=0",
                ]
                if control == "budget" and key != "full":
                    hot = max(1, round(BUDGET_VALUES[key] * vectors))
                    lines.extend([
                        f"[LAVD][multi][budget] f={BUDGET_VALUES[key]} "
                        f"H={hot}/{vectors} hotset=indeg reached={vectors}",
                        f"[LAVD][budget] CN loaded map: N={vectors} H={hot} "
                        f"({100 * BUDGET_VALUES[key]}% co-located)",
                    ])
                if control == "budget" or key == "resident":
                    lines.append(
                        "[CRANE][multi] upper cache built: U=62529 (128-d) "
                        "comp=32014848B entry_uid=1325 num_mns=1"
                    )
                (cell / "cn.err").write_text("\n".join(lines) + "\n")
                mn_dir = cell / "mn"
                mn_dir.mkdir()
                (mn_dir / "mn.err").write_text("clean memory server\n")
                (mn_dir / "status").write_text("0\n")


class VldbMechanismControlSummaryTest(unittest.TestCase):
    def test_canonical_float_discards_cross_runtime_last_bit(self) -> None:
        left = mechanism_summary.canonical_float(36.6940649911244)
        right = mechanism_summary.canonical_float(36.69406499112441)
        self.assertEqual(left, right)

    def test_recomputes_complete_budget_and_resident_matrices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            out = root / "summary"
            report = mechanism_summary.summarize(root, out, FINAL_SHA)
            self.assertEqual(report["measured_runs"], 60)
            self.assertEqual(report["measured_cells"], 12)
            self.assertEqual(report["retained_cells"], 72)
            self.assertEqual(report["retained_source_files"], 360)
            budget = mechanism_summary.read_csv(out / "budget_summary.csv")
            resident = mechanism_summary.read_csv(out / "resident_summary.csv")
            self.assertEqual({row["key"] for row in budget}, set(BUDGET_KEYS))
            self.assertEqual(len(resident), 6)
            full = next(row for row in budget if row["key"] == "full")
            f05 = next(row for row in budget if row["key"] == "f05")
            self.assertLess(
                float(f05["materialized_bytes_mean"]),
                float(full["materialized_bytes_mean"]),
            )
            remote = next(
                row for row in resident if row["mode"] == "remote" and row["ef"] == "100"
            )
            local = next(
                row for row in resident if row["mode"] == "resident" and row["ef"] == "100"
            )
            self.assertGreater(float(remote["posts_upnav_per_query_mean"]), 0)
            self.assertEqual(float(local["posts_upnav_per_query_mean"]), 0)

    def test_rejects_missing_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root, omit=("resident", "remote", 200, 4))
            with self.assertRaisesRegex(ValueError, "missing mechanism cell files|incomplete"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_failed_descriptor_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root, bad_descriptor=("budget", "f25", 100, 2))
            with self.assertRaisesRegex(ValueError, "descriptor readback"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_budget_environment_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            manifest = root / "raw" / "budget" / "f25" / "measure_r0" / "manifest.json"
            obj = json.loads(manifest.read_text())
            obj["environment"]["SHINE_LAVD_BUDGET"] = "0.5"
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "budget environment mismatch"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_resident_upper_graph_that_still_posts_remote_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn = root / "raw" / "resident" / "resident" / "ef100" / "measure_r0" / "cn.json"
            obj = json.loads(cn.read_text())
            obj["queries"]["posts_upnav"] = 1
            obj["queries"]["rdma_wrs"] += 1
            cn.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "resident upper graph did not eliminate"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_resident_upper_graph_mn_count_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn_err = root / "raw" / "resident" / "resident" / "ef100" / "measure_r0" / "cn.err"
            cn_err.write_text(cn_err.read_text().replace("num_mns=1", "num_mns=2"))
            with self.assertRaisesRegex(ValueError, "resident upper graph MN count"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_variable_descriptor_with_sparse_extent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn_err = root / "raw" / "budget" / "f25" / "measure_r0" / "cn.err"
            cn_err.write_text(cn_err.read_text().replace("sparse_bytes=0", "sparse_bytes=1"))
            with self.assertRaisesRegex(ValueError, "packed descriptor readback mismatch"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_per_cell_input_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            manifest = root / "raw" / "budget" / "f50" / "measure_r2" / "manifest.json"
            obj = json.loads(manifest.read_text())
            obj["observed_inputs"]["query"] = "9" * 64
            manifest.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "observed input SHA mismatch"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_source_symlink_outside_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn = root / "raw" / "budget" / "f75" / "measure_r3" / "cn.json"
            outside = Path(tmp) / "outside.json"
            outside.write_bytes(cn.read_bytes())
            cn.unlink()
            cn.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "bundle-contained regular file"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_partial_budget_without_materialization_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn_err = root / "raw" / "budget" / "f10" / "measure_r0" / "cn.err"
            lines = cn_err.read_text().splitlines()
            account = json.loads(lines[0].split(" ", 1)[1])
            account["budget_map_bytes"] = 0
            lines[0] = "LAVD_PHYSICAL_ACCOUNTING " + json.dumps(account)
            cn_err.write_text("\n".join(lines) + "\n")
            with self.assertRaisesRegex(ValueError, "budget map"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)

    def test_rejects_partial_budget_without_cn_map_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "campaign"
            root.mkdir()
            write_fixture(root)
            cn_err = root / "raw" / "budget" / "f10" / "measure_r0" / "cn.err"
            lines = [
                line for line in cn_err.read_text().splitlines()
                if not line.startswith("[LAVD][budget] CN loaded map:")
            ]
            cn_err.write_text("\n".join(lines) + "\n")
            with self.assertRaisesRegex(ValueError, "budget publication/readback"):
                mechanism_summary.summarize(root, root / "summary", FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
