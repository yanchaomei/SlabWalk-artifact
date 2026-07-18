import csv
import hashlib
import inspect
import json
import math
import shutil
import statistics
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aggregate_frontier_repeats as aggregate
import assemble_vldb_10m_build_scaling as build_scaling_assembler
import assemble_vldb_query_profile as query_profile_assembler
import assemble_vldb_worker_scaling as worker_assembler
import assemble_vldb_lifecycle_controls as lifecycle_assembler
import summarize_vldb_cache_control as cache_summary
import summarize_vldb_colocation_control as colocation_summary
import summarize_vldb_mechanism_controls as mechanism_summary
import summarize_vldb_resource_ledger as resource_summary
import validate_vldb_final_evidence as evidence
from test_assemble_vldb_lifecycle_controls import write_lifecycle_sources
from test_assemble_vldb_query_profile import QueryProfileAssemblerTest
from test_assemble_vldb_10m_build_scaling import write_campaign as write_10m_build_campaign
from test_summarize_vldb_cache_control import write_fixture as write_cache_fixture
from test_summarize_vldb_colocation_control import write_fixture as write_colocation_fixture
from test_summarize_vldb_mechanism_controls import write_fixture as write_mechanism_fixture
from test_validate_vldb_build_cost import write_build_cost_evidence
from test_validate_vldb_index_construction import write_index_construction_evidence
from worker_campaign_test_fixture import write_campaign_audit


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
DHNSW_SHA = "d" * 64


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_sha256_inventory(root: Path) -> None:
    manifest = root / "SHA256SUMS"
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path != manifest)
    manifest.write_text(
        "".join(
            f"{evidence.file_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in paths
        )
    )


def frontier_rows() -> list[dict[str, object]]:
    rows = []
    for dataset in evidence.FRONTIER_DATASETS:
        for method in evidence.FRONTIER_METHODS:
            for ef in (48, 64, 96, 128, 200):
                for repeat in range(1, 6):
                    rows.append({
                        "dataset": dataset,
                        "method": method,
                        "ef": ef,
                        "run_id": f"r{repeat}",
                        "recall": 0.8 + ef / 2000,
                        "qps": 10000 - ef,
                        "threads": 10,
                        "query_contexts": "" if method == "d-HNSW" else 10,
                        "top_k": 10,
                        "metric": "l2" if dataset != "TTI10M" else "ip",
                        "measurement_mode": "fixed_query_pool",
                        "protocol_fingerprint": digest(
                            f"protocol-{dataset}-{method}-{ef}"
                        ),
                        "campaign_id": (
                            f"sw-{dataset}" if method != "d-HNSW" else f"dhnsw-{dataset}"
                        ),
                        "binary_sha256": DHNSW_SHA if method == "d-HNSW" else FINAL_SHA,
                        "processed_queries": 10000,
                        "expected_queries": 10000,
                        "failed_queries": 0,
                        "p50_us": "" if method == "d-HNSW" else 100,
                        "p95_us": "" if method == "d-HNSW" else 200,
                        "p99_us": "" if method == "d-HNSW" else 300,
                        "mean_latency_us": 500 if method == "d-HNSW" else "",
                        "posts_per_query": "" if method == "d-HNSW" else 40,
                        "bytes_per_query": "" if method == "d-HNSW" else 4096,
                        "network_us": 100 if method == "d-HNSW" else "",
                        "compute_us": 200 if method == "d-HNSW" else "",
                        "meta_us": 50 if method == "d-HNSW" else "",
                        "deserialize_us": 150 if method == "d-HNSW" else "",
                        "source": f"{dataset}/{method}/r{repeat}",
                        "source_sha256": digest(f"{dataset}/{method}/r{repeat}"),
                    })
    return rows


def robustness_rows() -> list[dict[str, object]]:
    cells = {
        "workers": ("1", "8", "16", "40"),
        "coroutines": ("1", "2", "4", "8", "16"),
        "top_k": ("1", "10", "50", "100"),
        "query_distribution": ("uniform", "zipf1.0"),
        "latency_instrumentation": ("off", "on"),
    }
    rows = []
    for factor, values in cells.items():
        for value in values:
            for repeat in range(5):
                threads = int(value) if factor == "workers" else 10
                contexts = threads
                row = {
                    "campaign_id": "robustness-final",
                    "protocol_fingerprint": f"robustness-{factor}-{value}",
                    "binary_sha256": FINAL_SHA,
                    "dataset": "DEEP1M",
                    "factor": factor,
                    "value": value,
                    "run_kind": "measure",
                    "repeat": repeat,
                    "threads": threads,
                    "query_contexts": contexts,
                    "coroutines": int(value) if factor == "coroutines" else 2,
                    "top_k": int(value) if factor == "top_k" else 10,
                    "ef": 200,
                    "query_suffix": "a1.0-n10000" if value == "zipf1.0" else "uniform",
                    "latency_enabled": 0 if value == "off" else 1,
                    "metric": "l2",
                    "status": "ok",
                    "processed": 10000,
                    "recall": 0.9,
                    "qps": 10000,
                    "p50_us": "" if value == "off" else 80,
                    "p95_us": "" if value == "off" else 100,
                    "p99_us": "" if value == "off" else 120,
                    "posts_per_query": 40,
                    "bytes_per_query": 4096,
                }
                rows.append(row)
    return rows


def worker_scaling_rows(directory: Path) -> list[dict[str, object]]:
    rows = []
    query_canonical = digest("DEEP1M/worker-scaling/query-canonical")
    groundtruth_canonical = digest("DEEP1M/worker-scaling/groundtruth-canonical")
    for method in evidence.WORKER_SCALING_METHODS:
        method_slug = method.lower().replace("-", "").replace(" ", "")
        binary = DHNSW_SHA if method == "d-HNSW" else FINAL_SHA
        recall = 0.909 if method == "d-HNSW" else 0.989
        query_file = digest(f"DEEP1M/{method}/query-file")
        groundtruth_file = digest(f"DEEP1M/{method}/groundtruth-file")
        manifest = Path("query_pools") / f"deep1m_{method_slug}.json"
        manifest_record = {
            "kind": "query_pool_fingerprint",
            "dataset": "DEEP1M",
            "method": method,
            "metric": "l2",
            "limit": 10000,
            "query": {
                "rows": 10000,
                "dim": 96,
                "canonical_sha256": query_canonical,
                "file_sha256": query_file,
            },
            "groundtruth": {
                "rows": 10000,
                "k": 100,
                "canonical_ids_sha256": groundtruth_canonical,
                "file_sha256": groundtruth_file,
            },
        }
        manifest_path = directory / manifest
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest_record, sort_keys=True))
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        for workers in evidence.WORKER_SCALING_WORKERS:
            for repeat in range(5):
                source = Path("raw_sources") / f"{method_slug}_w{workers}_r{repeat}.log"
                payload = f"method={method} workers={workers} repeat={repeat}\n"
                source_path = directory / source
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(payload)
                rows.append({
                    "campaign_id": "worker-scaling-final",
                    "protocol_fingerprint": digest(f"worker/{method}/{workers}"),
                    "binary_sha256": binary,
                    "dataset": "DEEP1M",
                    "method": method,
                    "workers": workers,
                    "repeat": repeat,
                    "threads": workers,
                    "query_contexts": "" if method == "d-HNSW" else workers,
                    "coroutines": "" if method == "d-HNSW" else 2,
                    "top_k": 10,
                    "ef": 200,
                    "metric": "l2",
                    "measurement_mode": "fixed_query_pool",
                    "status": "ok",
                    "processed_queries": 10000,
                    "expected_queries": 10000,
                    "failed_queries": 0,
                    "recall": recall,
                    "qps": workers * (450 if method == "d-HNSW" else 1200) + repeat,
                    "query_canonical_sha256": query_canonical,
                    "groundtruth_canonical_sha256": groundtruth_canonical,
                    "query_file_sha256": query_file,
                    "groundtruth_file_sha256": groundtruth_file,
                    "query_pool_manifest": manifest.as_posix(),
                    "query_pool_manifest_sha256": manifest_sha,
                    "source": source.as_posix(),
                    "source_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                })
    return rows


def write_worker_campaign_provenance(directory: Path) -> None:
    campaign = directory.parent / "worker_campaign_source"
    write_campaign_audit(
        campaign,
        campaign_id="worker-scaling-final",
        slabwalk_sha=FINAL_SHA,
        dhnsw_sha=DHNSW_SHA,
    )
    worker_assembler.copy_campaign_provenance(
        campaign, directory, "worker-scaling-final"
    )


def resource_rows() -> list[dict[str, object]]:
    rows = []
    for layout in evidence.RESOURCE_LAYOUTS:
        for mns in evidence.RESOURCE_MN_COUNTS:
            for repeat in range(5):
                rows.append({
                    "dataset": "gist",
                    "layout": layout,
                    "memory_nodes": mns,
                    "repeat": repeat,
                    "num_vectors": 1000000,
                    "num_queries": 1000,
                    "threads": 10,
                    "coroutines_per_thread": 2,
                    "binary_sha256": FINAL_SHA,
                    "manifest_cell_fingerprint": f"manifest-{layout}-{mns}",
                    "campaign_protocol_fingerprint": "resource-final",
                    "recall": 0.9,
                    "qps": 10000,
                    "query_read_bytes_per_query": 4096,
                    "query_read_wrs_per_query": 40,
                    "query_read_submits_per_query": 8,
                    "read_bytes_gini": 0.004,
                    "measured_authoritative_index_bytes": 1000000,
                    "registered_sidecar_bytes": 800000,
                    "materialized_sidecar_bytes": 600000,
                    "actual_sidecar_write_bytes": 500000,
                    "registered_utilization": 0.75,
                    "storage_amplification": 1.6,
                    "cn_peak_rss_kib": 6000000,
                    "mn_peak_rss_sum_kib": 8000000,
                    "mn_peak_rss_max_kib": 2000000,
                    "lavd_build_ms": 1000,
                    "lavd_build_fetch_ms": 100,
                    "lavd_build_encode_ms": 200,
                    "lavd_build_materialize_ms": 700,
                    "resident_upper_build_ms": 25,
                    "query_latency_p50_us": 80,
                    "query_latency_p95_us": 100,
                    "query_latency_p99_us": 120,
                })
    return rows


def write_topology_evidence(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    query_manifest = {
        "kind": "query_pool_fingerprint",
        "dataset": "DEEP1M",
        "method": "d-HNSW",
        "metric": "l2",
        "limit": 10000,
        "query": {
            "rows": 10000,
            "dim": 96,
            "canonical_sha256": digest("topology-query-canonical"),
            "file_sha256": digest("topology-query-file"),
        },
        "groundtruth": {
            "rows": 10000,
            "k": 100,
            "canonical_ids_sha256": digest("topology-gt-canonical"),
            "file_sha256": digest("topology-gt-file"),
        },
    }
    query_path = directory / "query_pool.json"
    query_path.write_text(json.dumps(query_manifest, sort_keys=True))
    query_manifest_sha = hashlib.sha256(query_path.read_bytes()).hexdigest()
    campaign = {
        "campaign_id": "topology-final",
        "protocol": {
            "client_binary_sha256": DHNSW_SHA,
            "server_binary_sha256": "e" * 64,
            "remote_server_sha256": "e" * 64,
            "base_sha256": "f" * 64,
            "remote_base_sha256": "f" * 64,
            "measurement_mode": "fixed_query_pool",
            "queries_per_run": 10000,
            "topologies": ["loopback", "remote"],
        },
    }
    (directory / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))

    rows: list[dict[str, object]] = []
    for topology in ("loopback", "remote"):
        for repeat in range(5):
            source = Path("raw_sources") / f"{topology}_r{repeat}.json"
            source_path = directory / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(json.dumps({
                "kind": "dhnsw_topology_raw_source",
                "topology": topology,
                "repeat": repeat,
            }, sort_keys=True))
            rows.append({
                "campaign_id": "topology-final",
                "protocol_fingerprint": digest(f"topology/{topology}"),
                "binary_sha256": DHNSW_SHA,
                "dataset": "DEEP1M",
                "topology": topology,
                "repeat": repeat,
                "threads": 10,
                "ef": 200,
                "top_k": 10,
                "metric": "l2",
                "measurement_mode": "fixed_query_pool",
                "processed_queries": 10000,
                "expected_queries": 10000,
                "failed_queries": 0,
                "qps": 1000 + repeat + (100 if topology == "loopback" else 0),
                "recall": 0.9,
                "latency_us": 1000 + repeat,
                "network_us": 300 + repeat,
                "compute_us": 400 + repeat,
                "meta_us": 100 + repeat,
                "deserialize_us": 200 + repeat,
                "query_canonical_sha256": query_manifest["query"]["canonical_sha256"],
                "groundtruth_canonical_sha256": query_manifest["groundtruth"]["canonical_ids_sha256"],
                "query_file_sha256": query_manifest["query"]["file_sha256"],
                "groundtruth_file_sha256": query_manifest["groundtruth"]["file_sha256"],
                "query_manifest_sha256": query_manifest_sha,
                "source": source.as_posix(),
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            })
    write_csv(directory / "runs.csv", rows)

    summary = []
    for topology in ("loopback", "remote"):
        cell = [row for row in rows if row["topology"] == topology]
        record: dict[str, object] = {"topology": topology, "n": 5}
        for metric in (
            "qps", "recall", "latency_us", "network_us", "compute_us",
            "meta_us", "deserialize_us",
        ):
            values = [float(row[metric]) for row in cell]
            record[f"{metric}_mean"] = statistics.mean(values)
            record[f"{metric}_ci95"] = 2.776 * statistics.stdev(values) / math.sqrt(5)
        summary.append(record)
    write_csv(directory / "summary.csv", summary)


def model_control_rows() -> list[dict[str, object]]:
    cells = []
    for size in (64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384):
        cells.append(("payload_latency", f"{size}B", "ib_read_lat", size, 4096, 16, 1, 1, 1, 1))
    for mtu in (1024, 2048, 4096):
        cells.append(("mtu_latency", f"MTU {mtu}", "ib_read_lat", 256, mtu, 16, 1, 1, 1, 1))
    for numa in (0, 1):
        cells.append(("numa_latency", f"preferred {numa}", "ib_read_lat", 256, 4096, 16, 1, 1, numa, numa))
    for outstanding in (1, 4, 16):
        cells.append(("outs_msg_rate", f"outs {outstanding}", "ib_read_bw", 256, 4096, outstanding, 1, 1, 1, 1))
    for qps in (1, 2, 4, 8):
        for cq_mod in (1, 16):
            cells.append(("qp_cq_msg_rate", f"{qps}QP CQ{cq_mod}", "ib_read_bw", 256, 4096, 16, qps, cq_mod, 1, 1))

    rows = []
    port = 19500
    for repeat in range(1, 6):
        for sweep, label, tool, size, mtu, outstanding, qps, cq_mod, client_numa, server_numa in cells:
            port += 1
            is_latency = tool == "ib_read_lat"
            rows.append({
                "sweep": sweep,
                "label": label,
                "rep": repeat,
                "tool": tool,
                "size": size,
                "iters": 5000,
                "mtu": mtu,
                "outs": outstanding,
                "qps": qps,
                "cq_mod": cq_mod,
                "client_numa": client_numa,
                "server_numa": server_numa,
                "avg_us": 4.5 if is_latency else "",
                "p99_us": 5.0 if is_latency else "",
                "p999_us": 7.0 if is_latency else "",
                "stdev_us": 0.4 if is_latency else "",
                "bw_peak_gbps": "" if is_latency else 0.0,
                "bw_avg_gbps": "" if is_latency else 13.0,
                "msg_rate_mpps": "" if is_latency else 6.5,
                "reported_mtu": mtu,
                "reported_outs": outstanding,
                "reported_qps": qps,
                "reported_cq_mod": "" if is_latency else cq_mod,
                "cn_host": "skv-node6",
                "mn_host": "skv-node4",
                "mn_ip": "10.0.0.64",
                "device": "mlx5_1",
                "gid_index": 3,
                "port": port,
                "notes": "controlled one-sided RDMA read",
            })
    return rows


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def write_query_pool_evidence(directory: Path) -> None:
    dimensions = {"DEEP10M": 96, "SIFT10M": 128, "TTI10M": 200}
    file_hashes: dict[tuple[str, str], tuple[str, str]] = {}
    directory.mkdir(parents=True, exist_ok=True)
    for dataset, dim in dimensions.items():
        metric = "ip" if dataset == "TTI10M" else "l2"
        for method in evidence.FRONTIER_METHODS:
            vecs = method == "d-HNSW"
            query_file_sha = digest(f"{dataset}/{method}/query-file")
            gt_file_sha = digest(f"{dataset}/{method}/gt-file")
            file_hashes[(dataset, method)] = (query_file_sha, gt_file_sha)
            record = {
                "kind": "query_pool_fingerprint",
                "dataset": dataset,
                "method": method,
                "metric": metric,
                "query": {
                    "path": f"/query/{dataset}/{method}",
                    "format": "fvecs" if vecs else "fbin",
                    "source_rows": 10000,
                    "rows": 10000,
                    "dim": dim,
                    "canonical_sha256": digest(f"{dataset}/canonical-query"),
                    "file_sha256": query_file_sha,
                    "bytes": 10000 * dim * 4,
                },
                "groundtruth": {
                    "path": f"/groundtruth/{dataset}/{method}",
                    "format": "ivecs" if vecs else "bin",
                    "layout": "ids_only",
                    "source_rows": 10000,
                    "rows": 10000,
                    "k": 100,
                    "canonical_ids_sha256": digest(f"{dataset}/canonical-gt"),
                    "file_sha256": gt_file_sha,
                    "bytes": 10000 * 100 * 4,
                },
            }
            safe_method = method.lower().replace("-", "").replace(" ", "")
            (directory / f"{dataset.lower()}_{safe_method}.json").write_text(
                json.dumps(record)
            )

    tti_query_sha, tti_gt_sha = file_hashes[("TTI10M", "SlabWalk")]
    spotcheck = {
        "status": "ok",
        "metric": "ip",
        "top_k": 10,
        "checked_queries": 3,
        "query_indices": [0, 4999, 9999],
        "minimum_overlap": 10,
        "query": {"sha256": tti_query_sha, "rows": 10000, "dim": 200},
        "groundtruth": {"sha256": tti_gt_sha, "rows": 10000, "k": 100},
        "checks": [
            {"query_index": index, "overlap": 10, "exact_set_match": True}
            for index in (0, 4999, 9999)
        ],
    }
    (directory / "tti_exact_groundtruth_spotcheck.json").write_text(
        json.dumps(spotcheck)
    )


def linked_frontier_rows(query_pools: Path) -> list[dict[str, object]]:
    return aggregate.attach_query_pool_evidence(frontier_rows(), query_pools)


def retain_frontier_sources(
    frontier: Path, rows: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Materialize portable raw-source evidence referenced by frontier rows."""
    retained: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        source = Path("raw_sources") / f"source_{index:03d}.csv"
        payload = f"dataset,method,run_id\n{row['dataset']},{row['method']},{row['run_id']}\n"
        path = frontier / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
        linked = dict(row)
        linked["source"] = source.as_posix()
        linked["source_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
        retained.append(linked)
    return retained


def read_frontier_rows(frontier: Path) -> list[dict[str, str]]:
    with (frontier / "frontier_repeated_raw.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def bind_existing_frontier_campaign(frontier: Path) -> None:
    """Bind fixture rows without rewriting their data or aggregate summary."""

    rows = read_frontier_rows(frontier)
    role_specs: dict[str, tuple[list[str], list[str], str, str]] = {}
    cell_sources: dict[str, str] = {}
    for dataset in evidence.FRONTIER_DATASETS:
        for suffix, methods in (
            ("sw", ["SHINE", "SlabWalk"]),
            ("dhnsw", ["d-HNSW"]),
        ):
            role = f"{dataset.lower()}_{suffix}"
            campaign_ids = {
                str(row["campaign_id"])
                for row in rows
                if row["dataset"] == dataset and row["method"] in methods
            }
            if len(campaign_ids) != 1:
                raise AssertionError(
                    f"fixture campaign drift for {dataset}/{suffix}: "
                    f"{sorted(campaign_ids)}"
                )
            role_specs[role] = (
                [dataset],
                methods,
                next(iter(campaign_ids)),
                digest(f"fixture-source/{role}"),
            )
            for method in methods:
                cell_sources[f"{dataset}/{method}"] = role

    source_root = frontier / "source_campaigns"
    source_root.mkdir()
    manifest_sources = []
    provenance_sources = []
    for role, (datasets, methods, campaign_id, fingerprint) in role_specs.items():
        retained = Path("source_campaigns") / f"{role}.json"
        (frontier / retained).write_text(
            json.dumps(
                {
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                }
            )
        )
        retained_sha = evidence.file_sha256(frontier / retained)
        manifest_sources.append(
            {
                "role": role,
                "datasets": datasets,
                "methods": methods,
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
                "manifest": retained.as_posix(),
                "manifest_sha256": retained_sha,
            }
        )
        provenance_sources.append(
            {
                "role": role,
                "datasets": datasets,
                "methods": methods,
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
                "retained": retained.as_posix(),
                "sha256": retained_sha,
            }
        )
    campaign = {
        "schema_version": 2,
        "kind": "composite_frontier_evidence",
        "campaign_id": "fixture-existing-composite",
        "cell_sources": cell_sources,
        "source_campaigns": manifest_sources,
    }
    campaign_path = frontier / "campaign.json"
    campaign_path.write_text(json.dumps(campaign, sort_keys=True))
    (frontier / "PROVENANCE.json").write_text(
        json.dumps(
            {
                "campaign_manifest_sha256": evidence.file_sha256(campaign_path),
                "source_campaigns": provenance_sources,
            },
            sort_keys=True,
        )
    )
    write_sha256_inventory(frontier)


def write_cache_control_evidence(directory: Path) -> None:
    directory.mkdir(parents=True)
    write_cache_fixture(directory)
    cache_summary.summarize(directory, directory / "summary", FINAL_SHA)


def write_colocation_control_evidence(directory: Path) -> None:
    directory.mkdir(parents=True)
    write_colocation_fixture(directory)
    colocation_summary.summarize(directory, directory / "summary", FINAL_SHA)


def write_mechanism_control_evidence(directory: Path) -> None:
    directory.mkdir(parents=True)
    write_mechanism_fixture(directory)
    mechanism_summary.summarize(directory, directory / "summary", FINAL_SHA)


def write_10m_build_scaling_evidence(directory: Path, source_root: Path) -> None:
    deep = source_root / "deep"
    text_sift = source_root / "text_sift"
    write_10m_build_campaign(deep, ["DEEP10M"])
    write_10m_build_campaign(text_sift, ["TTI10M", "SIFT10M"])
    build_scaling_assembler.assemble(
        deep_campaign=deep,
        text_sift_campaign=text_sift,
        out_dir=directory,
        expected_binary_sha=FINAL_SHA,
    )


class FinalEvidenceValidationTest(unittest.TestCase):
    def test_validate_all_requires_every_release_input_at_the_api_boundary(self) -> None:
        signature = inspect.signature(evidence.validate_all)
        required = {
            "topology_control",
            "build_cost",
            "build_scaling_10m_path",
            "index_construction_path",
            "lifecycle_control_path",
            "cache_control_path",
            "colocation_control_path",
            "mechanism_control_path",
            "query_profile_path",
            "expected_profile_runner_sha",
            "expected_colocation_campaign_id",
            "expected_colocation_protocol_fingerprint",
            "expected_mechanism_campaign_id",
            "expected_mechanism_protocol_fingerprint",
        }
        self.assertTrue(required.issubset(signature.parameters))
        for name in required:
            self.assertIs(
                signature.parameters[name].default,
                inspect.Parameter.empty,
                f"validate_all must require {name}",
            )

    def create_complete_tree(
        self, root: Path
    ) -> tuple[Path, Path, Path, Path, Path, Path]:
        frontier = root / "frontier"
        robustness = root / "robustness"
        worker_scaling = root / "worker_scaling"
        resource = root / "resource"
        model_controls = root / "model_controls"
        query_pools = root / "query_pools"
        write_query_pool_evidence(query_pools)
        raw_frontier = retain_frontier_sources(
            frontier, linked_frontier_rows(query_pools)
        )
        write_csv(frontier / "frontier_repeated_raw.csv", raw_frontier)
        write_csv(
            frontier / "frontier_summary.csv",
            aggregate.summarize(raw_frontier, expected_repeats=5),
        )
        write_csv(robustness / "runs.csv", robustness_rows())
        write_csv(worker_scaling / "runs.csv", worker_scaling_rows(worker_scaling))
        write_worker_campaign_provenance(worker_scaling)
        measured_resource_rows = resource_rows()
        write_csv(resource / "runs.csv", measured_resource_rows)
        write_csv(resource / "summary.csv", resource_summary.summarize(measured_resource_rows))
        write_csv(model_controls / "rdma_tau_runs.csv", model_control_rows())
        return frontier, robustness, worker_scaling, resource, model_controls, query_pools

    def bind_composite_frontier_campaign(self, frontier: Path) -> None:
        rows = read_frontier_rows(frontier)
        for row in rows:
            row["campaign_id"] = (
                "frozen-dhnsw" if row["method"] == "d-HNSW" else "v5-sw"
            )
        write_csv(frontier / "frontier_repeated_raw.csv", rows)
        write_csv(
            frontier / "frontier_summary.csv",
            aggregate.summarize(rows, expected_repeats=5),
        )

        source_root = frontier / "source_campaigns"
        source_root.mkdir()
        specs = (
            ("shine_slabwalk", ["SHINE", "SlabWalk"], "v5-sw", "a" * 64),
            ("dhnsw", ["d-HNSW"], "frozen-dhnsw", "b" * 64),
        )
        manifest_sources = []
        provenance_sources = []
        for role, methods, campaign_id, fingerprint in specs:
            retained = Path("source_campaigns") / f"{role}.json"
            source_manifest = {
                "campaign_id": campaign_id,
                "protocol_fingerprint": fingerprint,
            }
            (frontier / retained).write_text(json.dumps(source_manifest))
            retained_sha = evidence.file_sha256(frontier / retained)
            manifest_sources.append(
                {
                    "role": role,
                    "methods": methods,
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                    "manifest": retained.as_posix(),
                    "manifest_sha256": retained_sha,
                }
            )
            provenance_sources.append(
                {
                    "role": role,
                    "methods": methods,
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                    "retained": retained.as_posix(),
                    "sha256": retained_sha,
                }
            )
        campaign = {
            "schema_version": 1,
            "kind": "composite_frontier_evidence",
            "method_sources": {
                "SHINE": "shine_slabwalk",
                "SlabWalk": "shine_slabwalk",
                "d-HNSW": "dhnsw",
            },
            "source_campaigns": manifest_sources,
        }
        campaign_path = frontier / "campaign.json"
        campaign_path.write_text(json.dumps(campaign, sort_keys=True))
        (frontier / "PROVENANCE.json").write_text(
            json.dumps(
                {
                    "campaign_manifest_sha256": evidence.file_sha256(campaign_path),
                    "source_campaigns": provenance_sources,
                },
                sort_keys=True,
            )
        )
        write_sha256_inventory(frontier)

    @staticmethod
    def bind_existing_frontier_campaign(frontier: Path) -> None:
        bind_existing_frontier_campaign(frontier)

    def bind_cell_scoped_frontier_campaign(self, frontier: Path) -> None:
        role_specs = {
            "deep10m_shine_slabwalk": (["DEEP10M"], ["SHINE", "SlabWalk"], "deep-sw", "a" * 64),
            "deep10m_dhnsw": (["DEEP10M"], ["d-HNSW"], "deep-dh", "b" * 64),
            "text_sift_shine_slabwalk": (["SIFT10M", "TTI10M"], ["SHINE", "SlabWalk"], "text-sw", "c" * 64),
            "text_sift_dhnsw": (["SIFT10M", "TTI10M"], ["d-HNSW"], "text-dh", "d" * 64),
        }
        cell_sources = {
            f"{dataset}/{method}": role
            for role, (datasets, methods, _, _) in role_specs.items()
            for dataset in datasets
            for method in methods
        }
        rows = read_frontier_rows(frontier)
        for row in rows:
            role = cell_sources[f"{row['dataset']}/{row['method']}"]
            row["campaign_id"] = role_specs[role][2]
        write_csv(frontier / "frontier_repeated_raw.csv", rows)
        write_csv(
            frontier / "frontier_summary.csv",
            aggregate.summarize(rows, expected_repeats=5),
        )

        source_root = frontier / "source_campaigns"
        source_root.mkdir()
        manifest_sources = []
        provenance_sources = []
        for role, (datasets, methods, campaign_id, fingerprint) in role_specs.items():
            retained = Path("source_campaigns") / f"{role}.json"
            (frontier / retained).write_text(
                json.dumps(
                    {
                        "campaign_id": campaign_id,
                        "protocol_fingerprint": fingerprint,
                    }
                )
            )
            retained_sha = evidence.file_sha256(frontier / retained)
            manifest_sources.append(
                {
                    "role": role,
                    "datasets": datasets,
                    "methods": methods,
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                    "manifest": retained.as_posix(),
                    "manifest_sha256": retained_sha,
                }
            )
            provenance_sources.append(
                {
                    "role": role,
                    "datasets": datasets,
                    "methods": methods,
                    "campaign_id": campaign_id,
                    "protocol_fingerprint": fingerprint,
                    "retained": retained.as_posix(),
                    "sha256": retained_sha,
                }
            )
        campaign = {
            "schema_version": 2,
            "kind": "composite_frontier_evidence",
            "campaign_id": "fixture-cell-scoped-composite",
            "cell_sources": cell_sources,
            "source_campaigns": manifest_sources,
        }
        campaign_path = frontier / "campaign.json"
        campaign_path.write_text(json.dumps(campaign, sort_keys=True))
        (frontier / "PROVENANCE.json").write_text(
            json.dumps(
                {
                    "campaign_manifest_sha256": evidence.file_sha256(campaign_path),
                    "source_campaigns": provenance_sources,
                },
                sort_keys=True,
            )
        )
        write_sha256_inventory(frontier)

    def validate_until_release_inputs(
        self,
        frontier: Path,
        robustness: Path,
        worker_scaling: Path,
        resource: Path,
        model_controls: Path,
        query_pools: Path,
    ) -> dict[str, object]:
        if not (frontier / "campaign.json").exists():
            rows = read_frontier_rows(frontier)
            observed_cells = {(row["dataset"], row["method"]) for row in rows}
            expected_cells = {
                (dataset, method)
                for dataset in evidence.FRONTIER_DATASETS
                for method in evidence.FRONTIER_METHODS
            }
            if observed_cells == expected_cells:
                self.bind_existing_frontier_campaign(frontier)
        missing = frontier.parent / "missing-release-input"
        return evidence.validate_all(
            frontier,
            robustness,
            worker_scaling,
            resource,
            model_controls,
            query_pools,
            FINAL_SHA,
            topology_control=missing,
            build_cost=missing,
            build_scaling_10m_path=missing,
            index_construction_path=missing,
            lifecycle_control_path=missing,
            cache_control_path=missing,
            colocation_control_path=missing,
            expected_colocation_campaign_id="colocation-fixture",
            expected_colocation_protocol_fingerprint="1" * 64,
            mechanism_control_path=missing,
            expected_mechanism_campaign_id="mechanism-fixture",
            expected_mechanism_protocol_fingerprint="2" * 64,
            query_profile_path=missing,
            expected_profile_runner_sha="3" * 64,
        )

    def test_complete_core_evidence_passes_individual_validators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            query_pool_report = evidence.validate_query_pools(query_pools)
            report = {
                "frontier": evidence.validate_frontier(
                    frontier, FINAL_SHA, query_pool_report["links"]
                ),
                "robustness": evidence.validate_robustness(robustness, FINAL_SHA),
                "worker_scaling": evidence.validate_worker_scaling(
                    worker_scaling, FINAL_SHA
                ),
                "resource_ledger": evidence.validate_resource_ledger(
                    resource, FINAL_SHA
                ),
                "model_controls": evidence.validate_model_controls(model_controls),
                "query_pools": query_pool_report,
            }
            self.assertEqual(report["frontier"]["measured_rows"], 225)
            self.assertEqual(report["frontier"]["query_pool_links_verified"], 225)
            self.assertEqual(report["frontier"]["retained_source_links_verified"], 225)
            self.assertEqual(report["robustness"]["measured_cells"], 17)
            self.assertEqual(report["worker_scaling"]["measured_cells"], 12)
            self.assertEqual(report["worker_scaling"]["measured_rows"], 60)
            self.assertEqual(
                report["worker_scaling"]["campaign_provenance"]["amendments_verified"],
                5,
            )
            self.assertEqual(report["resource_ledger"]["measured_cells"], 9)
            self.assertEqual(report["model_controls"]["measured_cells"], 25)
            self.assertEqual(report["model_controls"]["measured_rows"], 125)
            self.assertEqual(report["query_pools"]["measured_cells"], 9)
            self.assertEqual(len(report["robustness"]["runs_sha256"]), 64)
            self.assertEqual(len(report["worker_scaling"]["runs_sha256"]), 64)
            self.assertEqual(len(report["resource_ledger"]["runs_sha256"]), 64)

    def test_frontier_validates_split_campaign_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            self.bind_composite_frontier_campaign(frontier)
            query_pool_report = evidence.validate_query_pools(query_pools)
            report = evidence.validate_frontier(
                frontier, FINAL_SHA, query_pool_report["links"]
            )
            self.assertEqual(report["campaign_provenance"]["mode"], "composite")
            self.assertEqual(
                report["campaign_provenance"]["source_campaigns_verified"], 2
            )

    def test_frontier_validates_dataset_scoped_campaign_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            self.bind_cell_scoped_frontier_campaign(frontier)
            query_pool_report = evidence.validate_query_pools(query_pools)
            report = evidence.validate_frontier(
                frontier, FINAL_SHA, query_pool_report["links"]
            )
            self.assertEqual(
                report["campaign_provenance"]["mode"], "composite_cells"
            )
            self.assertEqual(
                report["campaign_provenance"]["source_campaigns_verified"], 4
            )

    def test_frontier_rejects_remapped_dataset_scoped_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            self.bind_cell_scoped_frontier_campaign(frontier)
            campaign_path = frontier / "campaign.json"
            campaign = json.loads(campaign_path.read_text())
            campaign["cell_sources"]["DEEP10M/SHINE"] = "deep10m_dhnsw"
            campaign_path.write_text(json.dumps(campaign, sort_keys=True))
            provenance_path = frontier / "PROVENANCE.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["campaign_manifest_sha256"] = evidence.file_sha256(
                campaign_path
            )
            provenance_path.write_text(json.dumps(provenance, sort_keys=True))
            write_sha256_inventory(frontier)
            query_pool_report = evidence.validate_query_pools(query_pools)
            with self.assertRaisesRegex(ValueError, "cell set mismatch"):
                evidence.validate_frontier(
                    frontier, FINAL_SHA, query_pool_report["links"]
                )

    def test_frontier_requires_complete_sha256_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            self.bind_cell_scoped_frontier_campaign(frontier)
            (frontier / "SHA256SUMS").unlink()
            query_pool_report = evidence.validate_query_pools(query_pools)
            with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
                evidence.validate_frontier(
                    frontier, FINAL_SHA, query_pool_report["links"]
                )

    def test_frontier_production_mode_requires_campaign_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            query_pool_report = evidence.validate_query_pools(query_pools)
            with self.assertRaisesRegex(ValueError, "campaign provenance"):
                evidence.validate_frontier(
                    frontier,
                    FINAL_SHA,
                    query_pool_report["links"],
                    require_campaign_provenance=True,
                )

    def test_frontier_rejects_tampered_split_campaign_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            self.bind_composite_frontier_campaign(frontier)
            source = frontier / "source_campaigns" / "dhnsw.json"
            source.write_text(source.read_text() + "\n")
            write_sha256_inventory(frontier)
            query_pool_report = evidence.validate_query_pools(query_pools)
            with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                evidence.validate_frontier(
                    frontier, FINAL_SHA, query_pool_report["links"]
                )

    def test_frontier_rejects_a_missing_system_curve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = [
                row for row in read_frontier_rows(frontier)
                if not (row["dataset"] == "SIFT10M" and row["method"] == "d-HNSW")
            ]
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "frontier matrix"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_rejects_nonfinal_slabwalk_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["binary_sha256"] = "a" * 64
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "SlabWalk binary SHA"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_requires_method_native_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            target = next(row for row in rows if row["method"] == "d-HNSW")
            target["mean_latency_us"] = ""
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "mean_latency_us"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_requires_the_fingerprinted_10k_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            for row in rows:
                row["processed_queries"] = 9999
                row["expected_queries"] = 9999
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            write_csv(
                frontier / "frontier_summary.csv",
                aggregate.summarize(rows, expected_repeats=5),
            )
            with self.assertRaisesRegex(ValueError, "expected exactly 10000"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_malformed_protocol_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, _, _, _, _, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            target = rows[0]
            for row in rows:
                if (
                    row["dataset"] == target["dataset"]
                    and row["method"] == target["method"]
                    and row["ef"] == target["ef"]
                ):
                    row["protocol_fingerprint"] = "not-a-sha256"
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            write_csv(
                frontier / "frontier_summary.csv",
                aggregate.summarize(rows, expected_repeats=5),
            )
            query_pool_report = evidence.validate_query_pools(query_pools)
            with self.assertRaisesRegex(ValueError, "protocol fingerprint"):
                evidence.validate_frontier(
                    frontier, FINAL_SHA, query_pool_report["links"]
                )

    def test_frontier_requires_direct_query_pool_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["query_canonical_sha256"] = ""
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "missing query_canonical_sha256"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_requires_raw_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["source_sha256"] = ""
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "missing source_sha256"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_tampered_retained_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            source = frontier / rows[0]["source"]
            source.write_text(source.read_text() + "tampered\n")
            with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_source_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["source"] = "../outside.csv"
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "source path escapes"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_canonical_query_pool_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["query_canonical_sha256"] = digest("wrong-canonical-query")
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "query-pool link mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_physical_query_file_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = read_frontier_rows(frontier)
            rows[0]["query_file_sha256"] = digest("wrong-physical-query-file")
            write_csv(frontier / "frontier_repeated_raw.csv", rows)
            with self.assertRaisesRegex(ValueError, "query-pool link mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_frontier_rejects_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            path = frontier / "frontier_summary.csv"
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["qps_mean"] = "1"
            write_csv(path, rows)
            with self.assertRaisesRegex(ValueError, "frontier summary mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_robustness_rejects_incomplete_repeat_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = robustness_rows()
            rows.pop()
            write_csv(robustness / "runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "robustness repeats"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_worker_scaling_rejects_a_missing_system_worker_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = [
                row for row in worker_scaling_rows(worker_scaling)
                if not (row["method"] == "d-HNSW" and row["workers"] == 40)
            ]
            write_csv(worker_scaling / "runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "worker-scaling matrix"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls,
                    query_pools,
                )

    def test_worker_scaling_rejects_a_broken_amendment_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (
                frontier,
                robustness,
                worker_scaling,
                resource,
                model_controls,
                query_pools,
            ) = self.create_complete_tree(Path(tmp))
            amendment = worker_scaling / "campaign/parser_amendment_v2.json"
            record = json.loads(amendment.read_text())
            record["amended_manifest_sha256"] = "0" * 64
            amendment.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
            provenance_path = worker_scaling / "campaign_provenance.json"
            provenance = json.loads(provenance_path.read_text())
            for item in provenance["files"]:
                if item["path"] == "campaign/parser_amendment_v2.json":
                    item["size_bytes"] = amendment.stat().st_size
                    item["sha256"] = hashlib.sha256(amendment.read_bytes()).hexdigest()
            provenance_path.write_text(
                json.dumps(provenance, indent=2, sort_keys=True) + "\n"
            )
            with self.assertRaisesRegex(ValueError, "amendment chain mismatch"):
                self.validate_until_release_inputs(
                    frontier,
                    robustness,
                    worker_scaling,
                    resource,
                    model_controls,
                    query_pools,
                )

    def test_worker_scaling_rejects_query_pool_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = worker_scaling_rows(worker_scaling)
            rows[0]["query_canonical_sha256"] = digest("different-query-pool")
            write_csv(worker_scaling / "runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "worker-scaling query-manifest mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls,
                    query_pools,
                )

    def test_worker_scaling_rejects_tampered_retained_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = worker_scaling_rows(worker_scaling)
            source = worker_scaling / str(rows[0]["source"])
            source.write_text(source.read_text() + "tampered\n")
            write_csv(worker_scaling / "runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "source SHA mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls,
                    query_pools,
                )

    def test_resource_ledger_requires_tail_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = resource_rows()
            rows[0]["query_latency_p99_us"] = ""
            write_csv(resource / "runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "query_latency_p99_us"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_resource_ledger_rejects_empty_retained_raw_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            (resource / "raw").mkdir()
            with self.assertRaisesRegex(ValueError, "no measured resource-ledger cells"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls,
                    query_pools,
                )

    def test_model_controls_reject_incomplete_repeat_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = model_control_rows()
            rows.pop()
            write_csv(model_controls / "rdma_tau_runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "model-control repeats"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_model_controls_reject_reported_knob_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = model_control_rows()
            rows[0]["reported_mtu"] = 2048
            write_csv(model_controls / "rdma_tau_runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "reported_mtu"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_model_controls_reject_host_or_device_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            rows = model_control_rows()
            rows[-1]["device"] = "mlx5_0"
            write_csv(model_controls / "rdma_tau_runs.csv", rows)
            with self.assertRaisesRegex(ValueError, "host/device/GID drift"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_topology_control_requires_two_complete_five_run_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            topology = Path(tmp) / "topology"
            write_topology_evidence(topology)
            report = evidence.validate_topology_control(topology)
            self.assertEqual(report["measured_cells"], 2)
            self.assertEqual(report["measured_rows"], 10)
            self.assertEqual(report["retained_source_links_verified"], 10)

    def test_topology_control_rejects_tampered_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            topology = Path(tmp) / "topology"
            write_topology_evidence(topology)
            with (topology / "summary.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["qps_mean"] = "1"
            write_csv(topology / "summary.csv", rows)
            with self.assertRaisesRegex(ValueError, "topology summary mismatch"):
                evidence.validate_topology_control(topology)

    def test_cache_control_recomputes_twenty_measured_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache_control"
            write_cache_control_evidence(cache)
            report = evidence.validate_cache_control(cache, FINAL_SHA)
            self.assertEqual(report["measured_rows"], 20)
            self.assertEqual(report["measured_cells"], 4)
            self.assertEqual(report["retained_cells"], 24)

    def test_cache_control_rejects_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache_control"
            write_cache_control_evidence(cache)
            summary_path = cache / "summary" / "summary.csv"
            rows = cache_summary.read_csv(summary_path)
            rows[-1]["qps_mean"] = "1"
            cache_summary.write_csv(summary_path, rows)
            with self.assertRaisesRegex(ValueError, "cache-control summary mismatch"):
                evidence.validate_cache_control(cache, FINAL_SHA)

    def test_cache_control_matrix_error_sorts_multi_value_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache_control"
            write_cache_control_evidence(cache)
            summary_dir = cache / "summary"
            rows = cache_summary.read_csv(summary_dir / "summary.csv")
            for index, row in enumerate(rows):
                row["condition"] = "zeta" if index % 2 == 0 else "alpha"
            cache_summary.write_csv(summary_dir / "summary.csv", rows)

            def retain_mutated_summary(
                _directory: Path, out: Path, _expected_sha: str
            ) -> None:
                shutil.copytree(summary_dir, out)

            with mock.patch.object(
                evidence.cache_control_summary,
                "summarize",
                side_effect=retain_mutated_summary,
            ), self.assertRaises(ValueError) as raised:
                evidence.validate_cache_control(cache, FINAL_SHA)
            self.assertEqual(
                str(raised.exception),
                "cache-control condition matrix mismatch: ['alpha', 'zeta']",
            )

    def test_colocation_control_recomputes_thirty_measured_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "colocation_control"
            write_colocation_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            report = evidence.validate_colocation_control(
                control,
                FINAL_SHA,
                campaign["campaign_id"],
                campaign["protocol_fingerprint"],
            )
            self.assertEqual(report["measured_rows"], 30)
            self.assertEqual(report["measured_cells"], 6)
            self.assertEqual(report["retained_cells"], 36)
            self.assertEqual(report["campaign_id"], campaign["campaign_id"])
            self.assertEqual(
                report["protocol_fingerprint"], campaign["protocol_fingerprint"]
            )

    def test_colocation_control_rejects_expected_campaign_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "colocation_control"
            write_colocation_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            with self.assertRaisesRegex(ValueError, "co-location campaign ID mismatch"):
                evidence.validate_colocation_control(
                    control,
                    FINAL_SHA,
                    "different-colocation-campaign",
                    campaign["protocol_fingerprint"],
                )

    def test_colocation_control_rejects_expected_protocol_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "colocation_control"
            write_colocation_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            with self.assertRaisesRegex(
                ValueError, "co-location protocol fingerprint mismatch"
            ):
                evidence.validate_colocation_control(
                    control,
                    FINAL_SHA,
                    campaign["campaign_id"],
                    "0" * 64,
                )

    def test_colocation_control_rejects_stale_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "colocation_control"
            write_colocation_control_evidence(control)
            summary_path = control / "summary" / "summary.csv"
            rows = colocation_summary.read_csv(summary_path)
            rows[-1]["posts_per_query_mean"] = "1"
            colocation_summary.write_csv(summary_path, rows)
            with self.assertRaisesRegex(ValueError, "co-location summary mismatch"):
                evidence.validate_colocation_control(control, FINAL_SHA)

    def test_colocation_matrix_error_sorts_multi_value_degrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "colocation_control"
            write_colocation_control_evidence(control)
            summary_dir = control / "summary"
            rows = colocation_summary.read_csv(summary_dir / "summary.csv")
            for index, row in enumerate(rows):
                row["degree"] = "zeta" if index % 2 == 0 else "alpha"
            colocation_summary.write_csv(summary_dir / "summary.csv", rows)

            def retain_mutated_summary(
                _directory: Path, out: Path, _expected_sha: str
            ) -> None:
                shutil.copytree(summary_dir, out)

            with mock.patch.object(
                evidence.colocation_control_summary,
                "summarize",
                side_effect=retain_mutated_summary,
            ), self.assertRaises(ValueError) as raised:
                evidence.validate_colocation_control(control, FINAL_SHA)
            self.assertEqual(
                str(raised.exception),
                "co-location degree matrix mismatch: ['alpha', 'zeta']",
            )

    def test_mechanism_controls_recompute_sixty_measured_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            report = evidence.validate_mechanism_controls(
                control,
                FINAL_SHA,
                campaign["campaign_id"],
                campaign["protocol_fingerprint"],
            )
            self.assertEqual(report["measured_rows"], 60)
            self.assertEqual(report["measured_cells"], 12)
            self.assertEqual(report["retained_cells"], 72)
            self.assertEqual(report["retained_source_files"], 360)
            self.assertEqual(report["campaign_id"], campaign["campaign_id"])
            self.assertEqual(
                report["protocol_fingerprint"], campaign["protocol_fingerprint"]
            )

    def test_mechanism_controls_reject_expected_campaign_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            with self.assertRaisesRegex(ValueError, "mechanism-control campaign ID mismatch"):
                evidence.validate_mechanism_controls(
                    control,
                    FINAL_SHA,
                    "different-mechanism-campaign",
                    campaign["protocol_fingerprint"],
                )

    def test_mechanism_controls_reject_expected_protocol_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            campaign = json.loads((control / "campaign.json").read_text())
            with self.assertRaisesRegex(
                ValueError, "mechanism-control protocol fingerprint mismatch"
            ):
                evidence.validate_mechanism_controls(
                    control,
                    FINAL_SHA,
                    campaign["campaign_id"],
                    "0" * 64,
                )

    def test_mechanism_controls_reject_stale_budget_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            summary_path = control / "summary" / "budget_summary.csv"
            rows = mechanism_summary.read_csv(summary_path)
            rows[-1]["qps_mean"] = "1"
            mechanism_summary.write_csv(summary_path, rows)
            with self.assertRaisesRegex(ValueError, "mechanism-control summary mismatch"):
                evidence.validate_mechanism_controls(control, FINAL_SHA)

    def test_mechanism_budget_error_sorts_multi_value_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            summary_dir = control / "summary"
            rows = mechanism_summary.read_csv(summary_dir / "budget_summary.csv")
            for index, row in enumerate(rows):
                row["key"] = "zeta" if index % 2 == 0 else "alpha"
            mechanism_summary.write_csv(summary_dir / "budget_summary.csv", rows)

            def retain_mutated_summary(
                _directory: Path, out: Path, _expected_sha: str
            ) -> None:
                shutil.copytree(summary_dir, out)

            with mock.patch.object(
                evidence.mechanism_control_summary,
                "summarize",
                side_effect=retain_mutated_summary,
            ), self.assertRaises(ValueError) as raised:
                evidence.validate_mechanism_controls(control, FINAL_SHA)
            self.assertEqual(
                str(raised.exception),
                "mechanism-control budget matrix mismatch: ['alpha', 'zeta']",
            )

    def test_mechanism_resident_error_sorts_multi_value_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "mechanism_controls"
            write_mechanism_control_evidence(control)
            summary_dir = control / "summary"
            rows = mechanism_summary.read_csv(summary_dir / "resident_summary.csv")
            for index, row in enumerate(rows):
                row["mode"] = "zeta" if index % 2 == 0 else "alpha"
                row["ef"] = "200" if index % 2 == 0 else "100"
            mechanism_summary.write_csv(summary_dir / "resident_summary.csv", rows)

            def retain_mutated_summary(
                _directory: Path, out: Path, _expected_sha: str
            ) -> None:
                shutil.copytree(summary_dir, out)

            with mock.patch.object(
                evidence.mechanism_control_summary,
                "summarize",
                side_effect=retain_mutated_summary,
            ), self.assertRaises(ValueError) as raised:
                evidence.validate_mechanism_controls(control, FINAL_SHA)
            self.assertEqual(
                str(raised.exception),
                "mechanism-control resident matrix mismatch: "
                "[('alpha', 100), ('zeta', 200)]",
            )

    def test_query_pools_reject_logical_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            path = query_pools / "tti10m_dhnsw.json"
            record = json.loads(path.read_text())
            record["query"]["canonical_sha256"] = digest("different-query-content")
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "query-pool content mismatch"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_query_pools_require_exact_tti_spotcheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(Path(tmp))
            path = query_pools / "tti_exact_groundtruth_spotcheck.json"
            record = json.loads(path.read_text())
            record["status"] = "mismatch"
            record["minimum_overlap"] = 9
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "TTI exact ground-truth spot check"):
                self.validate_until_release_inputs(
                    frontier, robustness, worker_scaling, resource, model_controls, query_pools
                )

    def test_cli_writes_machine_readable_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontier, robustness, worker_scaling, resource, model_controls, query_pools = self.create_complete_tree(root)
            self.bind_existing_frontier_campaign(frontier)
            topology = root / "topology"
            write_topology_evidence(topology)
            build_cost = root / "build_cost"
            write_build_cost_evidence(build_cost)
            build_scaling_10m = root / "build_scaling_10m"
            write_10m_build_scaling_evidence(
                build_scaling_10m, root / "build_scaling_10m_sources"
            )
            index_construction = root / "index_construction"
            write_index_construction_evidence(index_construction)
            refresh_summary, refresh_root, tti_summary, tti_root = (
                write_lifecycle_sources(root / "lifecycle_sources")
            )
            lifecycle = root / "lifecycle"
            lifecycle_assembler.assemble(
                refresh_summary, refresh_root, tti_summary, tti_root, lifecycle
            )
            cache = root / "cache_control"
            write_cache_control_evidence(cache)
            colocation = root / "colocation_control"
            write_colocation_control_evidence(colocation)
            mechanisms = root / "mechanism_controls"
            write_mechanism_control_evidence(mechanisms)
            colocation_campaign = json.loads((colocation / "campaign.json").read_text())
            mechanism_campaign = json.loads((mechanisms / "campaign.json").read_text())
            (root / "profile_source").mkdir()
            profile_source, profile_runner_sha = QueryProfileAssemblerTest().make_campaign(
                root / "profile_source", binary_sha=FINAL_SHA
            )
            query_profile = root / "query_profile"
            query_profile_assembler.assemble(
                profile_source,
                query_profile,
                expected_binary_sha=FINAL_SHA,
                expected_runner_sha=profile_runner_sha,
            )
            out = root / "gate.json"
            rc = evidence.main([
                "--frontier", str(frontier),
                "--robustness", str(robustness),
                "--worker-scaling", str(worker_scaling),
                "--resource-ledger", str(resource),
                "--model-controls", str(model_controls),
                "--query-pools", str(query_pools),
                "--topology-control", str(topology),
                "--build-cost", str(build_cost),
                "--build-scaling-10m", str(build_scaling_10m),
                "--index-construction", str(index_construction),
                "--lifecycle-controls", str(lifecycle),
                "--cache-control", str(cache),
                "--colocation-control", str(colocation),
                "--mechanism-controls", str(mechanisms),
                "--query-profile", str(query_profile),
                "--expected-profile-runner-sha", profile_runner_sha,
                "--expected-slabwalk-sha", FINAL_SHA,
                "--expected-colocation-campaign-id", colocation_campaign["campaign_id"],
                "--expected-colocation-protocol-fingerprint",
                colocation_campaign["protocol_fingerprint"],
                "--expected-mechanism-campaign-id", mechanism_campaign["campaign_id"],
                "--expected-mechanism-protocol-fingerprint",
                mechanism_campaign["protocol_fingerprint"],
                "--out", str(out),
            ])
            self.assertEqual(rc, 0)
            report = json.loads(out.read_text())
            self.assertEqual(report["kind"], "vldb_final_evidence_gate")
            self.assertTrue(report["ready_for_plotting"])
            self.assertEqual(report["topology_control"]["measured_rows"], 10)
            self.assertEqual(report["build_cost"]["measured_rows"], 15)
            self.assertEqual(report["build_scaling_10m"]["runs"], 15)
            self.assertEqual(report["build_scaling_10m"]["datasets"], 3)
            self.assertEqual(report["build_scaling_10m"]["retained_sources"], 45)
            self.assertEqual(report["index_construction"]["measured_cells"], 2)
            self.assertEqual(report["lifecycle_controls"]["retained_sources_verified"], 12)
            self.assertEqual(report["cache_control"]["measured_rows"], 20)
            self.assertEqual(report["colocation_control"]["measured_rows"], 30)
            self.assertEqual(report["mechanism_controls"]["measured_rows"], 60)
            self.assertEqual(report["query_profile"]["query_rows"], 200000)
            self.assertEqual(report["campaign_identities"], {
                "colocation_control": {
                    "campaign_id": colocation_campaign["campaign_id"],
                    "protocol_fingerprint": colocation_campaign[
                        "protocol_fingerprint"
                    ],
                },
                "mechanism_controls": {
                    "campaign_id": mechanism_campaign["campaign_id"],
                    "protocol_fingerprint": mechanism_campaign[
                        "protocol_fingerprint"
                    ],
                },
            })
            self.assertEqual(
                report["claim_input_sha256"]["cache_summary"],
                evidence.file_sha256(cache / "summary" / "summary.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["resource_summary"],
                evidence.file_sha256(resource / "summary.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["build_scaling_10m_summary"],
                evidence.file_sha256(build_scaling_10m / "summary.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["rdma_runs"],
                evidence.file_sha256(model_controls / "rdma_tau_runs.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["robustness_runs"],
                evidence.file_sha256(robustness / "runs.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["topology_summary"],
                evidence.file_sha256(topology / "summary.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["lifecycle_refresh"],
                evidence.file_sha256(lifecycle / "refresh.csv"),
            )
            self.assertEqual(
                report["claim_input_sha256"]["lifecycle_tti"],
                evidence.file_sha256(lifecycle / "tti.csv"),
            )

    def test_atomic_gate_writer_replaces_from_same_directory_and_fsyncs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "gate.json"
            report = {
                "kind": "vldb_final_evidence_gate",
                "ready_for_plotting": True,
            }
            with mock.patch.object(
                evidence.os, "replace", wraps=evidence.os.replace
            ) as replace, mock.patch.object(
                evidence.os, "fsync", wraps=evidence.os.fsync
            ) as fsync:
                evidence._write_gate_atomically(out, report)
            self.assertEqual(json.loads(out.read_text()), report)
            source, destination = replace.call_args.args
            self.assertEqual(Path(source).parent, out.parent)
            self.assertEqual(Path(destination), out)
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(list(out.parent.glob(f".{out.name}.*.tmp")), [])

    def test_failed_validation_removes_a_preexisting_ready_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "gate.json"
            out.write_text(json.dumps({
                "kind": "vldb_final_evidence_gate",
                "ready_for_plotting": True,
            }))
            argv = [
                "--frontier", str(root / "frontier"),
                "--robustness", str(root / "robustness"),
                "--worker-scaling", str(root / "worker"),
                "--resource-ledger", str(root / "resource"),
                "--model-controls", str(root / "model"),
                "--query-pools", str(root / "query_pools"),
                "--topology-control", str(root / "topology"),
                "--build-cost", str(root / "build"),
                "--build-scaling-10m", str(root / "build_10m"),
                "--index-construction", str(root / "index"),
                "--lifecycle-controls", str(root / "lifecycle"),
                "--cache-control", str(root / "cache"),
                "--colocation-control", str(root / "colocation"),
                "--mechanism-controls", str(root / "mechanisms"),
                "--query-profile", str(root / "profile"),
                "--expected-profile-runner-sha", "1" * 64,
                "--expected-slabwalk-sha", FINAL_SHA,
                "--expected-colocation-campaign-id", "colocation-final",
                "--expected-colocation-protocol-fingerprint", "2" * 64,
                "--expected-mechanism-campaign-id", "mechanism-final",
                "--expected-mechanism-protocol-fingerprint", "3" * 64,
                "--out", str(out),
            ]
            with self.assertRaisesRegex(
                ValueError, "missing query-pool evidence directory"
            ):
                evidence.main(argv)
            self.assertFalse(out.exists())

    def test_cli_rejects_output_inside_an_evidence_input_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            query_pools = root / "query_pools"
            query_pools.mkdir()
            out = query_pools / "sentinel.json"
            out.write_text("INPUT MUST SURVIVE\n")
            argv = [
                "--frontier", str(root / "frontier"),
                "--robustness", str(root / "robustness"),
                "--worker-scaling", str(root / "worker"),
                "--resource-ledger", str(root / "resource"),
                "--model-controls", str(root / "model"),
                "--query-pools", str(query_pools),
                "--topology-control", str(root / "topology"),
                "--build-cost", str(root / "build"),
                "--build-scaling-10m", str(root / "build_10m"),
                "--index-construction", str(root / "index"),
                "--lifecycle-controls", str(root / "lifecycle"),
                "--cache-control", str(root / "cache"),
                "--colocation-control", str(root / "colocation"),
                "--mechanism-controls", str(root / "mechanisms"),
                "--query-profile", str(root / "profile"),
                "--expected-profile-runner-sha", "1" * 64,
                "--expected-slabwalk-sha", FINAL_SHA,
                "--expected-colocation-campaign-id", "colocation-final",
                "--expected-colocation-protocol-fingerprint", "2" * 64,
                "--expected-mechanism-campaign-id", "mechanism-final",
                "--expected-mechanism-protocol-fingerprint", "3" * 64,
                "--out", str(out),
            ]

            with self.assertRaisesRegex(ValueError, "overlaps evidence input"):
                evidence.main(argv)
            self.assertEqual(out.read_text(), "INPUT MUST SURVIVE\n")


if __name__ == "__main__":
    unittest.main()
