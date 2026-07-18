from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import render_vldb_claims_tex as renderer


GATE_SHA = "a" * 64


def fixture() -> dict[str, object]:
    return {
        "kind": "vldb_manuscript_claims",
        "gate_sha256": GATE_SHA,
        "frontier": {
            "recall_floor": 0.90,
            "recall_tolerance": 0.002,
            "high_recall_qps_speedup_min": 9.9887,
            "high_recall_qps_speedup_max": 13.4618,
            "high_recall_post_reduction_min": 17.1344,
            "high_recall_post_reduction_max": 27.2230,
            "dhnsw_max_recall": {
                "DEEP10M": {"recall": 0.923096},
                "SIFT10M": {"recall": 0.904626},
                "TTI10M": {"recall": 0.601342},
            },
        },
        "cache_control": {
            "cache_50": {
                "post_reduction_vs_off_pct": 83.1595,
                "qps_change_vs_off_pct": -23.2392,
            }
        },
        "query_profile": {"distance_self_percent": 5.16},
        "colocation_control": {
            "degree_1": {
                "qps_change_vs_full_pct": -65.2215,
                "post_increase_vs_full_pct": 98.3574,
                "byte_change_vs_full_pct": 54.88,
                "recall_mean": 0.98907,
            }
        },
        "resident_upper_graph": {
            "ef100": {
                "remote": {"posts_upnav_per_query_mean": 25.0},
                "resident": {
                    "qps_change_vs_remote_pct": 30.25,
                    "upper_nodes_mean": 62529.0,
                    "upper_bytes_mean": 32014848.0,
                },
            }
        },
        "materialization_budget": {
            "fraction_05": {"materialized_bytes_mean": 1.5 * 1024**3},
            "full": {"materialized_bytes_mean": 9.0 * 1024**3},
        },
        "worker_scaling": {
            "slabwalk": {
                "qps_1_worker": 1240.0,
                "qps_40_workers": 14740.0,
                "throughput_gain": 11.887,
                "recall_mean": 0.98907,
            }
        },
        "resource_ledger": {
            "five_mn": {
                "legacy": {"storage_amplification": 11.2408},
                "fixed": {"storage_amplification": 3.0482},
                "variable": {
                    "storage_amplification": 1.6302,
                    "sidecar_gib": 2.423,
                    "cn_peak_rss_gib": 8.54,
                    "max_mn_rss_gib": 5.51,
                },
            },
            "variable_scale": {
                "qps_1mn": 2960.4,
                "qps_5mn": 2969.4,
                "max_mn_rss_gib_1mn": 10.01,
                "max_mn_rss_gib_5mn": 5.51,
                "max_read_bytes_gini": 0.004754,
            },
        },
        "rdma_controls": {
            "payload_64": {"avg_us": 4.238, "p99_us": 4.410},
            "payload_4096": {"avg_us": 5.566, "p99_us": 5.782},
            "payload_avg_span": 1.31336,
            "payload_p99_span": 1.31111,
            "qp1_cq1_mops": 3.83093,
            "qp2_cq16_mops": 7.20571,
            "mtu_mean_span": 1.00646,
            "numa_mean_difference_pct": 0.09166,
            "outs1_mops": 0.26307,
            "outs16_mops": 3.85735,
        },
        "robustness_controls": {
            "coroutines_1": {"qps": 6716.0, "p99_us": 1641.4028},
            "coroutines_4": {"qps": 17867.4, "p99_us": 2697.4404},
            "coroutines_16": {"qps": 18400.4, "p99_us": 13200.7402},
            "qps_gain_4_to_16_pct": 2.98309,
            "top_k_qps_span_pct": 1.86387,
            "zipf_qps_difference_pct": 1.34026,
            "zipf_p99_difference_pct": 0.28984,
        },
        "topology_control": {
            "loopback": {
                "qps": 4192.7672,
                "recall": 0.908918,
                "latency_us": 2200.724,
                "network_us": 423.8812,
            },
            "remote": {
                "qps": 294.2816,
                "recall": 0.908918,
                "latency_us": 18463.28,
                "network_us": 17941.56,
            },
        },
        "lifecycle_boundaries": {
            "refresh": {
                "min_records_per_node": 6.38132,
                "max_records_per_node": 12.403,
                "recall": 0.97662,
            },
            "tti": {
                "sq8": {"qps": 600.0, "recall": 0.86392, "posts_per_query": 578.4244},
                "rabitq2": {"qps": 58.0, "recall": 0.81522, "posts_per_query": 579.9653},
                "rabitq4": {"qps": 53.0, "recall": 0.86214, "posts_per_query": 578.3246},
                "fp32": {"qps": 260.0, "recall": 0.96105, "posts_per_query": 3870.3025},
            },
        },
        "build_cost": {
            "SIFT1M": {
                "build_mean_s": 20.62,
                "build_ci95_half_s": 0.42,
                "build_peak_rss_mean_gib": 4.83,
                "region_gb": 4.616016384,
            },
            "DEEP1M": {
                "build_mean_s": 18.49,
                "build_ci95_half_s": 0.31,
                "build_peak_rss_mean_gib": 4.71,
                "region_gb": 3.5970351104,
            },
            "GIST1M": {
                "build_mean_s": 25.48,
                "build_ci95_half_s": 0.55,
                "build_peak_rss_mean_gib": 8.51,
                "region_gb": 8.46108557312,
            },
        },
        "build_scaling_10m": {
            "DEEP10M": {"build_mean_s": 278.4026, "build_ci95_half_s": 4.9479, "resident_build_mean_s": 2.1589, "materialized_mean_gib": 35.2646},
            "SIFT10M": {"build_mean_s": 247.1841, "build_ci95_half_s": 0.4570, "resident_build_mean_s": 3.6296, "materialized_mean_gib": 28.8971},
            "TTI10M": {"build_mean_s": 290.0644, "build_ci95_half_s": 0.9621, "resident_build_mean_s": 4.2666, "materialized_mean_gib": 39.1540},
        },
    }


class RenderVldbClaimsTexTest(unittest.TestCase):
    def test_rejects_macro_names_that_are_not_tex_control_words(self) -> None:
        with self.assertRaisesRegex(ValueError, "letters only"):
            renderer.macro("ClaimP99", "1.0")

    def test_render_bytes_binds_exact_claims_snapshot(self) -> None:
        claims_bytes = json.dumps(
            fixture(), sort_keys=True, separators=(",", ":")
        ).encode()
        rendered = renderer.render_bytes(claims_bytes)
        claims_sha = hashlib.sha256(claims_bytes).hexdigest()

        self.assertIn(
            f"% claims-sha256: {claims_sha}\n".encode(),
            rendered,
        )
        self.assertEqual(rendered, renderer.render_bytes(claims_bytes))

    def test_renders_deterministic_gate_bound_macros(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "claims.json"
            source.write_text(json.dumps(fixture(), sort_keys=True))
            first = root / "first.tex"
            second = root / "second.tex"
            renderer.render(source, first)
            renderer.render(source, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            text = first.read_text()
            self.assertIn(f"% gate-sha256: {GATE_SHA}", text)
            self.assertIn(
                f"% claims-sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}",
                text,
            )
            self.assertIn(r"\newcommand{\ClaimFrontierRecallFloor}{0.90}", text)
            self.assertIn(r"\newcommand{\ClaimFrontierRecallTolerance}{0.002}", text)
            self.assertIn(r"\newcommand{\ClaimFrontierQpsMin}{9.99}", text)
            self.assertIn(r"\newcommand{\ClaimFrontierPostsMax}{27.22}", text)
            self.assertIn(r"\newcommand{\ClaimCachePostReduction}{83.2}", text)
            self.assertIn(r"\newcommand{\ClaimCacheQpsLoss}{23.2}", text)
            self.assertIn(r"\newcommand{\ClaimResidentNodes}{62,529}", text)
            self.assertIn(r"\newcommand{\ClaimResidentBytesMB}{32.0}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMSiftCiSeconds}{0.42}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMDeepCiSeconds}{0.31}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMGistCiSeconds}{0.55}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMSiftRegionGiB}{4.30}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMDeepRegionGiB}{3.35}", text)
            self.assertIn(r"\newcommand{\ClaimBuildOneMGistRegionGiB}{7.88}", text)
            self.assertIn(r"\newcommand{\ClaimBuildTenMDeepSeconds}{278.4}", text)
            self.assertIn(r"\newcommand{\ClaimRdmaPayloadSmallMeanUs}{4.24}", text)
            self.assertIn(r"\newcommand{\ClaimRdmaPayloadLargeTailUs}{5.78}", text)
            self.assertIn(r"\newcommand{\ClaimRdmaPayloadTailSpan}{1.31}", text)
            self.assertIn(r"\newcommand{\ClaimRdmaNumaDiffPct}{0.09}", text)
            self.assertIn(r"\newcommand{\ClaimCoroFourKqps}{17.87}", text)
            self.assertIn(r"\newcommand{\ClaimCoroSixteenTailMs}{13.20}", text)
            self.assertIn(r"\newcommand{\ClaimTopologyRemoteKqps}{0.294}", text)
            self.assertIn(r"\newcommand{\ClaimRefreshRecall}{0.97662}", text)
            self.assertIn(r"\newcommand{\ClaimTtiFpPosts}{3,870}", text)

    def test_rejects_claims_without_gate_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "claims.json"
            data = fixture()
            data.pop("gate_sha256")
            source.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "gate SHA"):
                renderer.render(source, root / "claims.tex")


if __name__ == "__main__":
    unittest.main()
