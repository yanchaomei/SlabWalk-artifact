#!/usr/bin/env python3

import csv
import inspect
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import assemble_vldb_manuscript_claims as claims
import summarize_vldb_headlines as headline_summary


FINAL_SLABWALK_SHA = (
    "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def frontier_rows() -> list[dict[str, object]]:
    rows = []
    for dataset in claims.DATASETS:
        for method in ("SHINE", "d-HNSW", "SlabWalk"):
            for index, ef in enumerate((48, 64, 96, 128, 200)):
                base_recall = 0.90 + 0.01 * index
                base_qps = 20000.0 / (index + 1)
                if method == "SHINE":
                    recall = base_recall
                    qps = base_qps
                    posts = 100.0
                    byte_count = 1000.0
                elif method == "SlabWalk":
                    recall = base_recall + (0.0001 if dataset != "TTI10M" else -0.05)
                    qps = 2.0 * base_qps
                    posts = 10.0
                    byte_count = 600.0
                else:
                    recall = base_recall - 0.10
                    qps = 500.0
                    posts = ""
                    byte_count = ""
                rows.append({
                    "dataset": dataset,
                    "method": method,
                    "ef": ef,
                    "n": 5,
                    "campaign_ids": f"campaign-{dataset}-{method}",
                    "binary_sha256s": (
                        "d" * 64 if method == "d-HNSW" else FINAL_SLABWALK_SHA
                    ),
                    "threads": 10,
                    "query_contexts": "" if method == "d-HNSW" else 10,
                    "top_k": 10,
                    "metric": "ip" if dataset == "TTI10M" else "l2",
                    "expected_queries": 10000,
                    "recall_mean": recall,
                    "recall_ci95": 0.002,
                    "qps_mean": qps,
                    "qps_ci95": 100.0 / (index + 1),
                    "posts_per_query_n": 0 if method == "d-HNSW" else 5,
                    "posts_per_query_mean": posts,
                    "posts_per_query_ci95": "" if method == "d-HNSW" else 0.0,
                    "bytes_per_query_n": 0 if method == "d-HNSW" else 5,
                    "bytes_per_query_mean": byte_count,
                })
    return rows


class ManuscriptClaimsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.headline = self.root / "headline.json"
        self.frontier_summary = self.root / "frontier_summary.csv"
        self.cache = self.root / "cache.csv"
        self.profile = self.root / "profile.csv"
        self.colocation = self.root / "colocation.csv"
        self.budget = self.root / "budget.csv"
        self.resident = self.root / "resident.csv"
        self.resource = self.root / "resource.csv"
        self.resource_runs = self.root / "resource_runs.csv"
        self.worker_runs = self.root / "worker_runs.csv"
        self.rdma_runs = self.root / "rdma_runs.csv"
        self.robustness_runs = self.root / "robustness_runs.csv"
        self.topology_summary = self.root / "topology_summary.csv"
        self.lifecycle_refresh = self.root / "lifecycle_refresh.csv"
        self.lifecycle_tti = self.root / "lifecycle_tti.csv"
        self.build = self.root / "build.csv"
        self.build_scaling_10m = self.root / "build_scaling_10m.csv"
        self.gate = self.root / "evidence_gate.json"
        self.out = self.root / "claims.json"

        write_csv(self.frontier_summary, frontier_rows())

        write_csv(self.cache, [
            {
                "condition": condition,
                "cache_ratio_pct": ratio,
                "n": 5,
                "qps_mean": qps,
                "qps_ci95": 2.0,
                "qps_change_vs_off_pct": delta,
                "recall_mean": 0.9766,
                "posts_per_query_mean": posts,
                "post_reduction_vs_off_pct": reduction,
                "all_repeats_below_off_min": below,
            }
            for condition, ratio, qps, delta, posts, reduction, below in (
                ("off", 0, 700.0, 0.0, 1700.0, 0.0, False),
                ("c5", 5, 650.0, -7.1, 500.0, 70.6, True),
                ("c20", 20, 600.0, -14.3, 350.0, 79.4, True),
                ("c50", 50, 525.0, -25.0, 280.0, 83.5, True),
            )
        ])
        write_csv(self.profile, [{
            "dataset": "SIFT1M",
            "method": "SHINE-derived",
            "threads": 1,
            "query_contexts": 1,
            "coroutines": 8,
            "ef": 100,
            "top_k": 10,
            "query_rows": 200000,
            "samples": 12345,
            "lost_samples": 0,
            "distance_self_percent": 18.25,
            "qps": 702.0,
            "posts_per_query": 1700.0,
            "bytes_per_query": 900000.0,
        }])
        write_csv(self.colocation, [
            {
                "degree": degree,
                "inline_codes": inline,
                "inline_code_fraction": inline / 32.0,
                "n": 5,
                "qps_mean": qps,
                "qps_ci95": 8.0,
                "qps_change_vs_full_pct": 100.0 * (qps / 16000.0 - 1.0),
                "recall_mean": 0.98907,
                "recall_ci95": 0.00001,
                "posts_per_query_mean": posts,
                "posts_per_query_ci95": 1.0,
                "post_increase_vs_full_pct": 100.0 * (posts / 190.0 - 1.0),
                "bytes_per_query_mean": byte_count,
                "bytes_per_query_ci95": 100.0,
                "byte_change_vs_full_pct": 100.0 * (byte_count / 500000.0 - 1.0),
                "p99_us_mean": 1500.0,
                "p99_us_ci95": 10.0,
            }
            for degree, inline, qps, posts, byte_count in (
                ("full", 32, 16000.0, 190.0, 500000.0),
                ("24", 24, 14500.0, 350.0, 580000.0),
                ("16", 16, 12000.0, 510.0, 660000.0),
                ("8", 8, 9700.0, 670.0, 740000.0),
                ("4", 4, 8500.0, 750.0, 780000.0),
                ("1", 1, 7600.0, 810.0, 810000.0),
            )
        ])
        write_csv(self.budget, [
            {
                "key": key,
                "materialized_fraction": fraction,
                "n": 5,
                "qps_mean": 14000.0 + 2000.0 * fraction,
                "qps_ci95": 5.0,
                "qps_change_vs_full_pct": -12.5 * (1.0 - fraction),
                "materialized_bytes_mean": 8_000_000_000 * fraction,
                "materialized_bytes_ci95": 0.0,
                "materialized_byte_fraction_vs_full": fraction,
                "recall_mean": 0.93 + 0.04 * fraction,
                "recall_ci95": 0.0001,
                "posts_per_query_mean": 180.0 + 20.0 * (1.0 - fraction),
                "posts_per_query_ci95": 1.0,
                "wrs_per_query_mean": 250.0,
                "wrs_per_query_ci95": 1.0,
                "bytes_per_query_mean": 500000.0,
                "bytes_per_query_ci95": 100.0,
                "p99_us_mean": 1200.0,
                "p99_us_ci95": 5.0,
                "registered_bytes_mean": 16 * 1024**3,
                "registered_bytes_ci95": 0.0,
                "actual_write_bytes_mean": 8_000_000_000 * fraction,
                "actual_write_bytes_ci95": 0.0,
                "budget_map_bytes_mean": 0 if key == "full" else 800000,
                "budget_map_bytes_ci95": 0.0,
            }
            for key, fraction in (
                ("f05", 0.05), ("f10", 0.10), ("f25", 0.25),
                ("f50", 0.50), ("f75", 0.75), ("full", 1.0),
            )
        ])
        write_csv(self.resident, [
            {
                "mode": mode,
                "ef": ef,
                "n": 5,
                "qps_mean": (5000.0 if mode == "resident" else 3800.0) - ef,
                "qps_ci95": 4.0,
                "qps_change_vs_remote_pct": 0.0 if mode == "remote" else 32.0,
                "posts_upnav_per_query_mean": 25.0 if mode == "remote" else 0.0,
                "posts_upnav_per_query_ci95": 0.2,
                "upnav_reduction_vs_remote_pct": 0.0 if mode == "remote" else 100.0,
                "recall_mean": 0.975,
                "recall_ci95": 0.0001,
                "posts_per_query_mean": 220.0 if mode == "remote" else 195.0,
                "posts_per_query_ci95": 1.0,
                "wrs_per_query_mean": 260.0 if mode == "remote" else 235.0,
                "wrs_per_query_ci95": 1.0,
                "bytes_per_query_mean": 520000.0,
                "bytes_per_query_ci95": 100.0,
                "p99_us_mean": 1000.0 if mode == "resident" else 1300.0,
                "p99_us_ci95": 4.0,
                "upper_nodes_mean": 0 if mode == "remote" else 62529,
                "upper_nodes_ci95": 0.0,
                "upper_bytes_mean": 0 if mode == "remote" else 32014848,
                "upper_bytes_ci95": 0.0,
                "upper_build_ms_mean": 0 if mode == "remote" else 25.0,
                "upper_build_ms_ci95": 0.0,
            }
            for ef in (50, 100, 200)
            for mode in ("remote", "resident")
        ])

        resource_rows = []
        resource_runs = []
        for layout, sidecar in (("legacy", 40.0), ("fixed", 8.0), ("variable", 2.5)):
            for mns in (1, 3, 5):
                resource_rows.append({
                    "dataset": "gist1m",
                    "layout": layout,
                    "memory_nodes": mns,
                    "n": 5,
                    "recall_mean": 0.9257,
                    "qps_mean": 2500.0 + 50.0 * mns,
                    "qps_ci95": 10.0,
                    "query_read_bytes_per_query_mean": 1_000_000.0,
                    "query_read_wrs_per_query_mean": 200.0,
                    "query_read_submits_per_query_mean": 200.0,
                    "read_bytes_gini_mean": 0.0 if mns == 1 else 0.004,
                    "materialized_sidecar_bytes_mean": sidecar * 1024**3,
                    "storage_amplification_mean": 1.0 + sidecar / 4.0,
                    "mn_peak_rss_max_kib_mean": (11.0 - mns) * 1024**2,
                })
                for repeat in range(5):
                    resource_runs.append({
                        "dataset": "gist1m",
                        "layout": layout,
                        "memory_nodes": mns,
                        "repeat": repeat,
                        "num_vectors": 1_000_000,
                        "measured_authoritative_index_bytes": 4.0 * 1024**3,
                        "registered_sidecar_bytes": (sidecar + 0.5) * 1024**3,
                        "materialized_sidecar_bytes": sidecar * 1024**3,
                        "actual_sidecar_write_bytes": (sidecar - 0.1) * 1024**3,
                        "cn_peak_rss_kib": 6.0 * 1024**2,
                    })
        write_csv(self.resource, resource_rows)
        write_csv(self.resource_runs, resource_runs)

        write_csv(self.worker_runs, [
            {
                "dataset": "DEEP1M",
                "method": method,
                "workers": workers,
                "repeat": repeat,
                "qps": base_qps * workers + repeat,
                "recall": recall,
            }
            for method, base_qps, recall in (
                ("SHINE", 100.0, 0.98901),
                ("SlabWalk", 1000.0, 0.98907),
                ("d-HNSW", 300.0, 0.90890),
            )
            for workers in (1, 8, 16, 40)
            for repeat in range(5)
        ])

        model_cells = (
            ("payload_latency", 64, 4096, 16, 1, 1, 1, 1, 4.24, 4.41, ""),
            ("payload_latency", 4096, 4096, 16, 1, 1, 1, 1, 5.57, 5.78, ""),
            ("qp_cq_msg_rate", 256, 4096, 16, 1, 1, 1, 1, "", "", 3.83),
            ("qp_cq_msg_rate", 256, 4096, 16, 2, 16, 1, 1, "", "", 7.21),
            ("mtu_latency", 256, 1024, 16, 1, 1, 1, 1, 4.34, 4.50, ""),
            ("mtu_latency", 256, 4096, 16, 1, 1, 1, 1, 4.36, 4.52, ""),
            ("numa_latency", 256, 4096, 16, 1, 1, 0, 0, 4.368, 4.50, ""),
            ("numa_latency", 256, 4096, 16, 1, 1, 1, 1, 4.364, 4.50, ""),
            ("outs_msg_rate", 256, 4096, 1, 1, 1, 1, 1, "", "", 0.26),
            ("outs_msg_rate", 256, 4096, 16, 1, 1, 1, 1, "", "", 3.86),
        )
        write_csv(self.rdma_runs, [
            {
                "sweep": sweep,
                "rep": repeat,
                "size": size,
                "mtu": mtu,
                "outs": outstanding,
                "qps": qps,
                "cq_mod": cq_mod,
                "client_numa": client_numa,
                "server_numa": server_numa,
                "avg_us": avg_us,
                "p99_us": p99_us,
                "msg_rate_mpps": rate,
            }
            for repeat in range(1, 6)
            for (
                sweep, size, mtu, outstanding, qps, cq_mod, client_numa,
                server_numa, avg_us, p99_us, rate,
            ) in model_cells
        ])

        robustness_cells = (
            ("coroutines", "1", 6716.0, 1641.4, 201.8, 530228.0),
            ("coroutines", "4", 17867.4, 2697.4, 201.8, 530228.0),
            ("coroutines", "16", 18400.4, 13200.7, 204.8, 530228.0),
            ("top_k", "1", 11929.4, 1835.1, 201.8, 530228.0),
            ("top_k", "10", 11932.6, 1825.8, 201.8, 530228.0),
            ("top_k", "50", 11985.8, 1830.3, 201.8, 530228.0),
            ("top_k", "100", 11762.4, 1861.9, 201.8, 530228.0),
            ("query_distribution", "uniform", 12087.2, 1803.3, 201.8, 530228.0),
            ("query_distribution", "zipf1.0", 12249.2, 1798.1, 202.0, 528872.0),
        )
        write_csv(self.robustness_runs, [
            {
                "run_kind": "measure",
                "factor": factor,
                "value": value,
                "repeat": repeat,
                "qps": qps,
                "p99_us": p99,
                "posts_per_query": posts,
                "bytes_per_query": byte_count,
            }
            for repeat in range(5)
            for factor, value, qps, p99, posts, byte_count in robustness_cells
        ])

        write_csv(self.topology_summary, [
            {
                "topology": topology,
                "n": 5,
                "qps_mean": qps,
                "recall_mean": 0.908918,
                "latency_us_mean": latency,
                "network_us_mean": network,
            }
            for topology, qps, latency, network in (
                ("loopback", 4192.7672, 2200.724, 423.8812),
                ("remote", 294.2816, 18463.28, 17941.56),
            )
        ])
        write_csv(self.lifecycle_refresh, [
            {
                "batch_inserts": batch,
                "write_amp_blocks_per_insert": amplification,
                "recall": 0.97662,
                "byte_identical": "PASS",
            }
            for batch, amplification in (
                (1000, 12.403), (10000, 9.8881), (50000, 8.25178), (100000, 6.38132)
            )
        ])
        write_csv(self.lifecycle_tti, [
            {
                "config": config,
                "threads": 1,
                "qps": qps,
                "recall": recall,
                "posts_per_query": posts,
            }
            for config, qps, recall, posts in (
                ("fp32 baseline", 260.0, 0.96105, 3870.3025),
                ("sq8 Slabs", 600.0, 0.86392, 578.4244),
                ("RaBitQ-2 Slabs", 58.0, 0.81522, 579.9653),
                ("RaBitQ-4 Slabs", 53.0, 0.86214, 578.3246),
            )
        ])

        write_csv(self.build, [
            {
                "dataset": dataset,
                "repeats": 5,
                "build_mean_s": build,
                "build_ci95_half_s": ci,
                "build_peak_rss_mean_gib": rss,
                "region_gb": region,
            }
            for dataset, build, ci, rss, region in (
                ("SIFT1M", 20.62, 0.07, 4.83, 4.616),
                ("DEEP1M", 18.49, 0.05, 4.71, 3.592),
                ("GIST1M", 25.48, 0.05, 8.51, 8.456),
            )
        ])
        write_csv(self.build_scaling_10m, [
            {
                "dataset": dataset,
                "n": 5,
                "canonical_ef": ef,
                "build_mean_s": build,
                "build_ci95_half_s": ci,
                "resident_build_mean_s": resident,
                "registered_mean_gib": registered,
                "materialized_mean_gib": materialized,
                "lavd_build_fetch_share_pct": 3.4,
                "lavd_build_parse_share_pct": 0.5,
                "lavd_build_rank_share_pct": 0.0,
                "lavd_build_encode_share_pct": 0.1,
                "lavd_build_metadata_share_pct": 0.2,
                "lavd_build_materialize_share_pct": 95.8,
            }
            for dataset, ef, build, ci, resident, registered, materialized in (
                ("DEEP10M", 48, 278.4, 5.1, 2.2, 40.0, 35.27),
                ("SIFT10M", 64, 301.2, 4.2, 2.4, 40.0, 36.10),
                ("TTI10M", 100, 344.8, 6.0, 2.8, 40.0, 38.20),
            )
        ])
        self.refresh_gate()

    def refresh_gate(self) -> None:
        frontier_summary_sha = claims.sha256(self.frontier_summary)
        gate = {
            "kind": "vldb_final_evidence_gate",
            "ready_for_plotting": True,
            "expected_slabwalk_sha256": FINAL_SLABWALK_SHA,
            "frontier": {"summary_sha256": frontier_summary_sha},
            "cache_control": {"summary_sha256": claims.sha256(self.cache)},
            "colocation_control": {
                "summary_sha256": claims.sha256(self.colocation),
                "campaign_id": "colocation-fixture",
                "protocol_fingerprint": "c" * 64,
            },
            "mechanism_controls": {
                "budget_summary_sha256": claims.sha256(self.budget),
                "resident_summary_sha256": claims.sha256(self.resident),
                "campaign_id": "mechanism-fixture",
                "protocol_fingerprint": "d" * 64,
            },
            "query_profile": {"summary_sha256": claims.sha256(self.profile)},
            "resource_ledger": {
                "summary_sha256": claims.sha256(self.resource),
                "runs_sha256": claims.sha256(self.resource_runs),
            },
            "worker_scaling": {"runs_sha256": claims.sha256(self.worker_runs)},
            "model_controls": {"runs_sha256": claims.sha256(self.rdma_runs)},
            "robustness": {"runs_sha256": claims.sha256(self.robustness_runs)},
            "topology_control": {"summary_sha256": claims.sha256(self.topology_summary)},
            "lifecycle_controls": {
                "refresh_sha256": claims.sha256(self.lifecycle_refresh),
                "tti_sha256": claims.sha256(self.lifecycle_tti),
            },
            "build_cost": {"summary_sha256": claims.sha256(self.build)},
            "build_scaling_10m": {
                "summary_sha256": claims.sha256(self.build_scaling_10m)
            },
        }
        gate["campaign_identities"] = {
            "colocation_control": {
                "campaign_id": gate["colocation_control"]["campaign_id"],
                "protocol_fingerprint": gate["colocation_control"][
                    "protocol_fingerprint"
                ],
            },
            "mechanism_controls": {
                "campaign_id": gate["mechanism_controls"]["campaign_id"],
                "protocol_fingerprint": gate["mechanism_controls"][
                    "protocol_fingerprint"
                ],
            },
        }
        gate["claim_input_sha256"] = {
            "headline_source_summary": frontier_summary_sha,
            "cache_summary": gate["cache_control"]["summary_sha256"],
            "colocation_summary": gate["colocation_control"]["summary_sha256"],
            "budget_summary": gate["mechanism_controls"][
                "budget_summary_sha256"
            ],
            "resident_summary": gate["mechanism_controls"][
                "resident_summary_sha256"
            ],
            "profile_summary": gate["query_profile"]["summary_sha256"],
            "resource_summary": gate["resource_ledger"]["summary_sha256"],
            "resource_runs": gate["resource_ledger"]["runs_sha256"],
            "worker_runs": gate["worker_scaling"]["runs_sha256"],
            "rdma_runs": gate["model_controls"]["runs_sha256"],
            "robustness_runs": gate["robustness"]["runs_sha256"],
            "topology_summary": gate["topology_control"]["summary_sha256"],
            "lifecycle_refresh": gate["lifecycle_controls"]["refresh_sha256"],
            "lifecycle_tti": gate["lifecycle_controls"]["tti_sha256"],
            "build_summary": gate["build_cost"]["summary_sha256"],
            "build_scaling_10m_summary": gate["build_scaling_10m"][
                "summary_sha256"
            ],
        }
        self.gate.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
        headline_summary.summarize(
            self.frontier_summary,
            self.gate,
            self.headline,
            recall_tolerance=0.002,
            recall_floor=0.90,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assemble(self, *, gate: Path | None = None) -> dict[str, object]:
        claims.assemble(
            gate=self.gate if gate is None else gate,
            frontier_summary=self.frontier_summary,
            headline=self.headline,
            cache_summary=self.cache,
            colocation_summary=self.colocation,
            budget_summary=self.budget,
            resident_summary=self.resident,
            profile_summary=self.profile,
            resource_summary=self.resource,
            resource_runs=self.resource_runs,
            worker_runs=self.worker_runs,
            rdma_runs=self.rdma_runs,
            robustness_runs=self.robustness_runs,
            topology_summary=self.topology_summary,
            lifecycle_refresh=self.lifecycle_refresh,
            lifecycle_tti=self.lifecycle_tti,
            build_summary=self.build,
            build_scaling_10m_summary=self.build_scaling_10m,
            out=self.out,
        )
        return json.loads(self.out.read_text())

    def test_assembles_submission_claim_surface(self) -> None:
        report = self.assemble()
        self.assertEqual(report["kind"], "vldb_manuscript_claims")
        self.assertEqual(report["gate_sha256"], claims.sha256(self.gate))
        self.assertEqual(report["campaign_identities"], {
            "colocation_control": {
                "campaign_id": "colocation-fixture",
                "protocol_fingerprint": "c" * 64,
            },
            "mechanism_controls": {
                "campaign_id": "mechanism-fixture",
                "protocol_fingerprint": "d" * 64,
            },
        })
        self.assertEqual(report["frontier"]["matched_datasets"], ["DEEP10M", "SIFT10M"])
        self.assertEqual(report["frontier"]["recall_floor"], 0.90)
        self.assertEqual(report["frontier"]["recall_tolerance"], 0.002)
        self.assertEqual(
            set(report["frontier"]["high_recall_matched_pairs"]),
            {"DEEP10M", "SIFT10M"},
        )
        self.assertAlmostEqual(report["cache_control"]["cache_50"]["qps_change_vs_off_pct"], -25.0)
        self.assertAlmostEqual(report["query_profile"]["distance_self_percent"], 18.25)
        self.assertAlmostEqual(
            report["colocation_control"]["degree_1"]["post_increase_vs_full_pct"],
            100.0 * (810.0 / 190.0 - 1.0),
        )
        self.assertAlmostEqual(
            report["materialization_budget"]["fraction_05"]["materialized_byte_fraction_vs_full"],
            0.05,
        )
        self.assertAlmostEqual(
            report["resident_upper_graph"]["ef100"]["resident"]["upnav_reduction_vs_remote_pct"],
            100.0,
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["five_mn"]["variable"]["sidecar_gib"], 2.5
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["five_mn"]["variable"]["authoritative_gib"], 4.0
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["five_mn"]["variable"]["registered_gib"], 3.0
        )
        self.assertEqual(
            report["resource_ledger"]["five_mn"]["variable"]["cn_address_map_bytes"],
            8 * (1_000_000 + 5),
        )
        self.assertEqual(
            report["resource_ledger"]["five_mn"]["variable"]["cn_address_map_formula"],
            "8*(N+S)",
        )
        self.assertEqual(
            report["resource_ledger"]["five_mn"]["variable"]["num_vectors"],
            1_000_000,
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["five_mn"]["variable"]["cn_address_map_gib"],
            8 * (1_000_000 + 5) / 1024**3,
        )
        self.assertEqual(
            report["resource_ledger"]["five_mn"]["fixed"]["cn_address_map_bytes"],
            0,
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["five_mn"]["variable"]["cn_peak_rss_gib"], 6.0
        )
        self.assertAlmostEqual(
            report["resource_ledger"]["variable_scale"]["qps_1mn"], 2550.0
        )
        self.assertAlmostEqual(
            report["worker_scaling"]["slabwalk"]["qps_1_worker"], 1002.0
        )
        self.assertAlmostEqual(
            report["worker_scaling"]["slabwalk"]["qps_40_workers"], 40002.0
        )
        self.assertAlmostEqual(
            report["worker_scaling"]["slabwalk"]["throughput_gain"],
            40002.0 / 1002.0,
        )
        self.assertAlmostEqual(report["build_cost"]["GIST1M"]["build_mean_s"], 25.48)
        self.assertAlmostEqual(
            report["build_scaling_10m"]["DEEP10M"]["build_mean_s"], 278.4
        )
        self.assertAlmostEqual(
            report["build_scaling_10m"]["TTI10M"]["materialized_mean_gib"], 38.2
        )
        self.assertAlmostEqual(report["rdma_controls"]["payload_64"]["avg_us"], 4.24)
        self.assertAlmostEqual(report["robustness_controls"]["coroutines_16"]["p99_us"], 13200.7)
        self.assertAlmostEqual(report["topology_control"]["remote"]["network_us"], 17941.56)
        self.assertAlmostEqual(report["lifecycle_boundaries"]["refresh"]["max_records_per_node"], 12.403)
        self.assertAlmostEqual(report["lifecycle_boundaries"]["tti"]["sq8"]["recall"], 0.86392)
        self.assertEqual(set(report["source_sha256"]), {
            "headline", "frontier_summary", "cache_summary", "colocation_summary",
            "budget_summary", "resident_summary", "profile_summary",
            "resource_summary", "resource_runs", "worker_runs", "build_summary",
            "build_scaling_10m_summary", "rdma_runs", "robustness_runs",
            "topology_summary", "lifecycle_refresh", "lifecycle_tti",
        })

    def test_rejects_incomplete_resource_matrix(self) -> None:
        with self.resource.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        write_csv(self.resource, rows[:-1])
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "resource matrix"):
            self.assemble()

    def test_rejects_inconsistent_resource_vector_counts(self) -> None:
        with self.resource_runs.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[-1]["num_vectors"] = "999999"
        write_csv(self.resource_runs, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "disagree on vector count"):
            self.assemble()

    def test_rejects_nonfinal_repeat_counts(self) -> None:
        with self.cache.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[-1]["n"] = "3"
        write_csv(self.cache, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "five repeats"):
            self.assemble()

    def test_rejects_cache_direction_reversal(self) -> None:
        with self.cache.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        next(row for row in rows if row["condition"] == "c50")[
            "qps_change_vs_off_pct"
        ] = "1.0"
        write_csv(self.cache, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "cache QPS direction mismatch"):
            self.assemble()

    def test_rejects_cache_post_direction_reversal(self) -> None:
        with self.cache.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        next(row for row in rows if row["condition"] == "c50")[
            "post_reduction_vs_off_pct"
        ] = "-1.0"
        write_csv(self.cache, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "cache post direction mismatch"):
            self.assemble()

    def test_rejects_colocation_qps_direction_reversal(self) -> None:
        with self.colocation.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        next(row for row in rows if row["degree"] == "1")[
            "qps_change_vs_full_pct"
        ] = "1.0"
        write_csv(self.colocation, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "co-location QPS direction mismatch"):
            self.assemble()

    def test_rejects_colocation_post_direction_reversal(self) -> None:
        with self.colocation.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        next(row for row in rows if row["degree"] == "1")[
            "post_increase_vs_full_pct"
        ] = "-1.0"
        write_csv(self.colocation, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "co-location post direction mismatch"):
            self.assemble()

    def test_rejects_colocation_byte_direction_reversal(self) -> None:
        with self.colocation.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        next(row for row in rows if row["degree"] == "1")[
            "byte_change_vs_full_pct"
        ] = "-1.0"
        write_csv(self.colocation, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "co-location byte direction mismatch"):
            self.assemble()

    def test_rejects_resident_qps_direction_reversal(self) -> None:
        with self.resident.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        target = next(
            row
            for row in rows
            if row["mode"] == "resident" and row["ef"] == "100"
        )
        target["qps_change_vs_remote_pct"] = "-1.0"
        write_csv(self.resident, rows)
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "resident QPS direction mismatch"):
            self.assemble()

    def test_rejects_a_headline_without_an_absolute_recall_floor(self) -> None:
        record = json.loads(self.headline.read_text())
        del record["recall_floor"]
        self.headline.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "recall floor"):
            self.assemble()

    def test_rejects_a_claimed_headline_pair_below_the_recall_floor(self) -> None:
        record = json.loads(self.headline.read_text())
        pair = record["datasets"]["DEEP10M"]["high_recall_matched_pair"]
        pair["shine_recall"] = 0.899
        pair["slabwalk_recall"] = 0.900
        pair["recall_delta"] = 0.001
        self.headline.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "below recall floor"):
            self.assemble()

    def test_rejects_a_claimed_headline_pair_outside_recall_tolerance(self) -> None:
        record = json.loads(self.headline.read_text())
        pair = record["datasets"]["SIFT10M"]["high_recall_matched_pair"]
        pair["shine_recall"] = 0.936
        pair["slabwalk_recall"] = 0.940
        pair["recall_delta"] = 0.004
        self.headline.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "exceeds recall tolerance"):
            self.assemble()

    def test_rejects_a_headline_pair_without_qps_improvement(self) -> None:
        record = json.loads(self.headline.read_text())
        record["datasets"]["DEEP10M"]["high_recall_matched_pair"]["qps_speedup"] = 0.9
        record["headline_ranges"]["high_recall_qps_speedup_min"] = 0.9
        self.headline.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "positive improvement"):
            self.assemble()

    def test_rejects_a_headline_pair_without_post_reduction(self) -> None:
        record = json.loads(self.headline.read_text())
        record["datasets"]["DEEP10M"]["high_recall_matched_pair"]["post_reduction"] = 0.9
        record["headline_ranges"]["high_recall_post_reduction_min"] = 0.9
        self.headline.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "positive improvement"):
            self.assemble()

    def test_rejects_incomplete_worker_matrix(self) -> None:
        with self.worker_runs.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        write_csv(self.worker_runs, rows[:-1])
        self.refresh_gate()
        with self.assertRaisesRegex(ValueError, "worker-scaling matrix"):
            self.assemble()

    def test_gate_is_required_at_the_python_api_boundary(self) -> None:
        parameter = inspect.signature(claims.assemble).parameters.get("gate")
        self.assertIsNotNone(parameter)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_frontier_summary_is_required_at_the_python_api_boundary(self) -> None:
        parameter = inspect.signature(claims.assemble).parameters.get(
            "frontier_summary"
        )
        self.assertIsNotNone(parameter)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_cli_requires_gate(self) -> None:
        argv = [
            "--frontier-summary", str(self.frontier_summary),
            "--headline", str(self.headline),
            "--cache-summary", str(self.cache),
            "--colocation-summary", str(self.colocation),
            "--budget-summary", str(self.budget),
            "--resident-summary", str(self.resident),
            "--profile-summary", str(self.profile),
            "--resource-summary", str(self.resource),
            "--resource-runs", str(self.resource_runs),
            "--worker-runs", str(self.worker_runs),
            "--build-summary", str(self.build),
            "--build-scaling-10m-summary", str(self.build_scaling_10m),
            "--out", str(self.out),
        ]
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            claims.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--gate", stderr.getvalue())

    def test_cli_requires_frontier_summary(self) -> None:
        argv = [
            "--gate", str(self.gate),
            "--headline", str(self.headline),
            "--cache-summary", str(self.cache),
            "--colocation-summary", str(self.colocation),
            "--budget-summary", str(self.budget),
            "--resident-summary", str(self.resident),
            "--profile-summary", str(self.profile),
            "--resource-summary", str(self.resource),
            "--resource-runs", str(self.resource_runs),
            "--worker-runs", str(self.worker_runs),
            "--build-summary", str(self.build),
            "--build-scaling-10m-summary", str(self.build_scaling_10m),
            "--out", str(self.out),
        ]
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            claims.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--frontier-summary", stderr.getvalue())

    def test_rejects_semantically_mutated_headline_with_valid_provenance(self) -> None:
        record = json.loads(self.headline.read_text())
        gate_sha = record["gate_sha256"]
        summary_sha = record["summary_sha256"]
        record["datasets"]["DEEP10M"]["high_recall_matched_pair"][
            "qps_speedup"
        ] = 7.1
        record["datasets"]["SIFT10M"]["high_recall_matched_pair"][
            "qps_speedup"
        ] = 8.2
        record["headline_ranges"]["high_recall_qps_speedup_min"] = 7.1
        record["headline_ranges"]["high_recall_qps_speedup_max"] = 8.2
        self.headline.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

        self.assertEqual(record["gate_sha256"], gate_sha)
        self.assertEqual(record["summary_sha256"], summary_sha)
        with self.assertRaisesRegex(
            ValueError,
            "headline payload mismatch against recomputed frontier summary",
        ):
            self.assemble()
        self.assertFalse(self.out.exists())

    def test_rejects_headline_that_changes_the_production_recall_policy(self) -> None:
        record = json.loads(self.headline.read_text())
        record["recall_tolerance"] = 0.003
        self.headline.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(
            ValueError,
            "headline payload mismatch against recomputed frontier summary",
        ):
            self.assemble()
        self.assertFalse(self.out.exists())

    def test_rejects_a_hand_edited_csv_not_recorded_by_the_gate(self) -> None:
        with self.cache.open("a") as handle:
            handle.write("# hand edited\n")
        with self.assertRaisesRegex(ValueError, "cache_summary SHA mismatch"):
            self.assemble()
        self.assertFalse(self.out.exists())

    def test_rejects_a_frontier_summary_not_recorded_by_the_gate(self) -> None:
        with self.frontier_summary.open("a") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(
            ValueError, "frontier_summary SHA mismatch against evidence gate"
        ):
            self.assemble()
        self.assertFalse(self.out.exists())

    def test_rejects_a_headline_derived_from_a_different_gate(self) -> None:
        mismatched = self.root / "mismatched_gate.json"
        gate = json.loads(self.gate.read_text())
        gate["validated_utc"] = "different-gate"
        mismatched.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(ValueError, "headline gate SHA mismatch"):
            self.assemble(gate=mismatched)
        self.assertFalse(self.out.exists())

    def test_rejects_a_gate_whose_claim_hash_map_disagrees_with_its_report(self) -> None:
        gate = json.loads(self.gate.read_text())
        gate["claim_input_sha256"]["cache_summary"] = "0" * 64
        self.gate.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(
            ValueError, "evidence gate claim hash mismatch for cache_summary"
        ):
            self.assemble()
        self.assertFalse(self.out.exists())

    def test_rejects_a_gate_with_the_wrong_kind(self) -> None:
        gate = json.loads(self.gate.read_text())
        gate["kind"] = "not_a_release_gate"
        self.gate.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(ValueError, "evidence gate kind mismatch"):
            self.assemble()

    def test_rejects_a_gate_that_is_not_ready(self) -> None:
        gate = json.loads(self.gate.read_text())
        gate["ready_for_plotting"] = False
        self.gate.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n")
        with self.assertRaisesRegex(ValueError, "not ready_for_plotting"):
            self.assemble()


if __name__ == "__main__":
    unittest.main()
