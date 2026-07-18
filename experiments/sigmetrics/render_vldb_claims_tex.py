#!/usr/bin/env python3
"""Render deterministic LaTeX macros from gated VLDB manuscript claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def value(data: dict[str, Any], *path: str) -> float:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise ValueError(f"missing claim: {'.'.join(path)}")
        current = current[key]
    if not isinstance(current, (int, float)) or not math.isfinite(float(current)):
        raise ValueError(f"invalid numeric claim: {'.'.join(path)}")
    return float(current)


def macro(name: str, rendered: str) -> str:
    if re.fullmatch(r"[A-Za-z]+", name) is None:
        raise ValueError(f"LaTeX macro names must contain letters only: {name}")
    return rf"\newcommand{{\{name}}}{{{rendered}}}"


def render_bytes(claims_bytes: bytes) -> bytes:
    data = json.loads(claims_bytes)
    if data.get("kind") != "vldb_manuscript_claims":
        raise ValueError("manuscript claim kind mismatch")
    gate_sha = str(data.get("gate_sha256", ""))
    if not SHA_RE.fullmatch(gate_sha):
        raise ValueError("missing or invalid gate SHA")
    claims_sha = hashlib.sha256(claims_bytes).hexdigest()

    macros: list[tuple[str, str]] = [
        ("ClaimGateSha", gate_sha),
        ("ClaimFrontierRecallFloor", f"{value(data, 'frontier', 'recall_floor'):.2f}"),
        ("ClaimFrontierRecallTolerance", f"{value(data, 'frontier', 'recall_tolerance'):.3f}"),
        ("ClaimFrontierQpsMin", f"{value(data, 'frontier', 'high_recall_qps_speedup_min'):.2f}"),
        ("ClaimFrontierQpsMax", f"{value(data, 'frontier', 'high_recall_qps_speedup_max'):.2f}"),
        ("ClaimFrontierPostsMin", f"{value(data, 'frontier', 'high_recall_post_reduction_min'):.2f}"),
        ("ClaimFrontierPostsMax", f"{value(data, 'frontier', 'high_recall_post_reduction_max'):.2f}"),
        ("ClaimDhnswDeepRecall", f"{value(data, 'frontier', 'dhnsw_max_recall', 'DEEP10M', 'recall'):.3f}"),
        ("ClaimDhnswSiftRecall", f"{value(data, 'frontier', 'dhnsw_max_recall', 'SIFT10M', 'recall'):.3f}"),
        ("ClaimDhnswTtiRecall", f"{value(data, 'frontier', 'dhnsw_max_recall', 'TTI10M', 'recall'):.3f}"),
        ("ClaimCachePostReduction", f"{value(data, 'cache_control', 'cache_50', 'post_reduction_vs_off_pct'):.1f}"),
        ("ClaimCacheQpsLoss", f"{abs(value(data, 'cache_control', 'cache_50', 'qps_change_vs_off_pct')):.1f}"),
        ("ClaimProfileDistanceShare", f"{value(data, 'query_profile', 'distance_self_percent'):.2f}"),
        ("ClaimColocationQpsLoss", f"{abs(value(data, 'colocation_control', 'degree_1', 'qps_change_vs_full_pct')):.1f}"),
        ("ClaimColocationPostIncrease", f"{value(data, 'colocation_control', 'degree_1', 'post_increase_vs_full_pct'):.1f}"),
        ("ClaimColocationByteIncrease", f"{value(data, 'colocation_control', 'degree_1', 'byte_change_vs_full_pct'):.1f}"),
        ("ClaimColocationRecall", f"{value(data, 'colocation_control', 'degree_1', 'recall_mean'):.5f}"),
        ("ClaimResidentNodes", f"{round(value(data, 'resident_upper_graph', 'ef100', 'resident', 'upper_nodes_mean')):,}"),
        ("ClaimResidentBytesMB", f"{value(data, 'resident_upper_graph', 'ef100', 'resident', 'upper_bytes_mean') / 1_000_000:.1f}"),
        ("ClaimResidentQpsGain", f"{value(data, 'resident_upper_graph', 'ef100', 'resident', 'qps_change_vs_remote_pct'):.1f}"),
        ("ClaimResidentRemotePosts", f"{value(data, 'resident_upper_graph', 'ef100', 'remote', 'posts_upnav_per_query_mean'):.1f}"),
        ("ClaimBudgetFiveGiB", f"{value(data, 'materialization_budget', 'fraction_05', 'materialized_bytes_mean') / 1024**3:.2f}"),
        ("ClaimBudgetFullGiB", f"{value(data, 'materialization_budget', 'full', 'materialized_bytes_mean') / 1024**3:.2f}"),
        ("ClaimWorkerOneKqps", f"{value(data, 'worker_scaling', 'slabwalk', 'qps_1_worker') / 1000:.2f}"),
        ("ClaimWorkerFortyKqps", f"{value(data, 'worker_scaling', 'slabwalk', 'qps_40_workers') / 1000:.2f}"),
        ("ClaimWorkerGain", f"{value(data, 'worker_scaling', 'slabwalk', 'throughput_gain'):.1f}"),
        ("ClaimWorkerRecall", f"{value(data, 'worker_scaling', 'slabwalk', 'recall_mean'):.3f}"),
        ("ClaimLegacyAmplification", f"{value(data, 'resource_ledger', 'five_mn', 'legacy', 'storage_amplification'):.2f}"),
        ("ClaimFixedAmplification", f"{value(data, 'resource_ledger', 'five_mn', 'fixed', 'storage_amplification'):.2f}"),
        ("ClaimVariableAmplification", f"{value(data, 'resource_ledger', 'five_mn', 'variable', 'storage_amplification'):.2f}"),
        ("ClaimVariableSidecarGiB", f"{value(data, 'resource_ledger', 'five_mn', 'variable', 'sidecar_gib'):.2f}"),
        ("ClaimVariableCnRssGiB", f"{value(data, 'resource_ledger', 'five_mn', 'variable', 'cn_peak_rss_gib'):.2f}"),
        ("ClaimVariableOneMnQps", f"{value(data, 'resource_ledger', 'variable_scale', 'qps_1mn'):.1f}"),
        ("ClaimVariableFiveMnQps", f"{value(data, 'resource_ledger', 'variable_scale', 'qps_5mn'):.1f}"),
        ("ClaimVariableOneMnRssGiB", f"{value(data, 'resource_ledger', 'variable_scale', 'max_mn_rss_gib_1mn'):.2f}"),
        ("ClaimVariableFiveMnRssGiB", f"{value(data, 'resource_ledger', 'variable_scale', 'max_mn_rss_gib_5mn'):.2f}"),
        ("ClaimVariableReadGini", f"{value(data, 'resource_ledger', 'variable_scale', 'max_read_bytes_gini'):.4f}"),
        ("ClaimRdmaPayloadSmallMeanUs", f"{value(data, 'rdma_controls', 'payload_64', 'avg_us'):.2f}"),
        ("ClaimRdmaPayloadLargeMeanUs", f"{value(data, 'rdma_controls', 'payload_4096', 'avg_us'):.2f}"),
        ("ClaimRdmaPayloadSmallTailUs", f"{value(data, 'rdma_controls', 'payload_64', 'p99_us'):.2f}"),
        ("ClaimRdmaPayloadLargeTailUs", f"{value(data, 'rdma_controls', 'payload_4096', 'p99_us'):.2f}"),
        ("ClaimRdmaPayloadMeanSpan", f"{value(data, 'rdma_controls', 'payload_avg_span'):.2f}"),
        ("ClaimRdmaPayloadTailSpan", f"{value(data, 'rdma_controls', 'payload_p99_span'):.2f}"),
        ("ClaimRdmaQpOneMops", f"{value(data, 'rdma_controls', 'qp1_cq1_mops'):.2f}"),
        ("ClaimRdmaQpTwoMops", f"{value(data, 'rdma_controls', 'qp2_cq16_mops'):.2f}"),
        ("ClaimRdmaMtuMeanSpan", f"{value(data, 'rdma_controls', 'mtu_mean_span'):.2f}"),
        ("ClaimRdmaNumaDiffPct", f"{value(data, 'rdma_controls', 'numa_mean_difference_pct'):.2f}"),
        ("ClaimRdmaOutsOneMops", f"{value(data, 'rdma_controls', 'outs1_mops'):.2f}"),
        ("ClaimRdmaOutsSixteenMops", f"{value(data, 'rdma_controls', 'outs16_mops'):.2f}"),
        ("ClaimCoroOneKqps", f"{value(data, 'robustness_controls', 'coroutines_1', 'qps') / 1000:.2f}"),
        ("ClaimCoroFourKqps", f"{value(data, 'robustness_controls', 'coroutines_4', 'qps') / 1000:.2f}"),
        ("ClaimCoroFourTailMs", f"{value(data, 'robustness_controls', 'coroutines_4', 'p99_us') / 1000:.2f}"),
        ("ClaimCoroSixteenTailMs", f"{value(data, 'robustness_controls', 'coroutines_16', 'p99_us') / 1000:.2f}"),
        ("ClaimCoroFourToSixteenGainPct", f"{value(data, 'robustness_controls', 'qps_gain_4_to_16_pct'):.1f}"),
        ("ClaimTopKQpsSpanPct", f"{value(data, 'robustness_controls', 'top_k_qps_span_pct'):.1f}"),
        ("ClaimZipfQpsDiffPct", f"{value(data, 'robustness_controls', 'zipf_qps_difference_pct'):.1f}"),
        ("ClaimZipfTailDiffPct", f"{value(data, 'robustness_controls', 'zipf_p99_difference_pct'):.1f}"),
        ("ClaimTopologyRecall", f"{value(data, 'topology_control', 'remote', 'recall'):.3f}"),
        ("ClaimTopologyLoopbackKqps", f"{value(data, 'topology_control', 'loopback', 'qps') / 1000:.2f}"),
        ("ClaimTopologyRemoteKqps", f"{value(data, 'topology_control', 'remote', 'qps') / 1000:.3f}"),
        ("ClaimTopologyLoopbackLatencyMs", f"{value(data, 'topology_control', 'loopback', 'latency_us') / 1000:.2f}"),
        ("ClaimTopologyRemoteLatencyMs", f"{value(data, 'topology_control', 'remote', 'latency_us') / 1000:.2f}"),
        ("ClaimTopologyRemoteNetworkMs", f"{value(data, 'topology_control', 'remote', 'network_us') / 1000:.2f}"),
        ("ClaimRefreshMinRecords", f"{value(data, 'lifecycle_boundaries', 'refresh', 'min_records_per_node'):.1f}"),
        ("ClaimRefreshMaxRecords", f"{value(data, 'lifecycle_boundaries', 'refresh', 'max_records_per_node'):.1f}"),
        ("ClaimRefreshRecall", f"{value(data, 'lifecycle_boundaries', 'refresh', 'recall'):.5f}"),
        ("ClaimTtiSqEightRecall", f"{value(data, 'lifecycle_boundaries', 'tti', 'sq8', 'recall'):.3f}"),
        ("ClaimTtiRqTwoRecall", f"{value(data, 'lifecycle_boundaries', 'tti', 'rabitq2', 'recall'):.3f}"),
        ("ClaimTtiRqFourRecall", f"{value(data, 'lifecycle_boundaries', 'tti', 'rabitq4', 'recall'):.3f}"),
        ("ClaimTtiFpRecall", f"{value(data, 'lifecycle_boundaries', 'tti', 'fp32', 'recall'):.3f}"),
        ("ClaimTtiFpQps", f"{round(value(data, 'lifecycle_boundaries', 'tti', 'fp32', 'qps')):,}"),
        ("ClaimTtiFpPosts", f"{round(value(data, 'lifecycle_boundaries', 'tti', 'fp32', 'posts_per_query')):,}"),
    ]
    dataset_macros = {
        "SIFT1M": "Sift",
        "DEEP1M": "Deep",
        "GIST1M": "Gist",
    }
    for dataset, label in dataset_macros.items():
        macros.extend([
            (f"ClaimBuildOneM{label}Seconds", f"{value(data, 'build_cost', dataset, 'build_mean_s'):.2f}"),
            (f"ClaimBuildOneM{label}CiSeconds", f"{value(data, 'build_cost', dataset, 'build_ci95_half_s'):.2f}"),
            (f"ClaimBuildOneM{label}RssGiB", f"{value(data, 'build_cost', dataset, 'build_peak_rss_mean_gib'):.2f}"),
            (f"ClaimBuildOneM{label}RegionGiB", f"{value(data, 'build_cost', dataset, 'region_gb') * 1e9 / 1024**3:.2f}"),
        ])
    ten_m_macros = {
        "DEEP10M": "Deep",
        "SIFT10M": "Sift",
        "TTI10M": "Tti",
    }
    for dataset, label in ten_m_macros.items():
        macros.extend([
            (f"ClaimBuildTenM{label}Seconds", f"{value(data, 'build_scaling_10m', dataset, 'build_mean_s'):.1f}"),
            (f"ClaimBuildTenM{label}CiSeconds", f"{value(data, 'build_scaling_10m', dataset, 'build_ci95_half_s'):.1f}"),
            (f"ClaimBuildTenM{label}ResidentSeconds", f"{value(data, 'build_scaling_10m', dataset, 'resident_build_mean_s'):.2f}"),
            (f"ClaimBuildTenM{label}GiB", f"{value(data, 'build_scaling_10m', dataset, 'materialized_mean_gib'):.1f}"),
        ])

    lines = [
        "% Generated from gated manuscript_claims.json. Do not edit.",
        f"% gate-sha256: {gate_sha}",
        f"% claims-sha256: {claims_sha}",
        *(macro(name, rendered) for name, rendered in macros),
        "",
    ]
    return "\n".join(lines).encode()


def render(source: Path, out: Path) -> None:
    rendered = render_bytes(source.read_bytes())
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(f".{out.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    render(args.claims, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
