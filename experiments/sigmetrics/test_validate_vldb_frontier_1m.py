#!/usr/bin/env python3
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import aggregate_frontier_repeats as aggregate
import assemble_vldb_frontier_1m as assembler
import validate_vldb_frontier_1m as validator


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
DHNSW_SHA = "d" * 64


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_query_pools(root: Path) -> Path:
    directory = root / "query_pools"
    directory.mkdir(parents=True)
    for dataset in assembler.DATASETS:
        for method in assembler.METHODS:
            query_format = (
                "fvecs"
                if method == "d-HNSW"
                else validator.GRAPH_QUERY_FORMATS[dataset]
            )
            gt_format = "ivecs" if method == "d-HNSW" else "bin"
            slug = method.lower().replace("-", "").replace(" ", "")
            record = {
                "kind": "query_pool_fingerprint",
                "dataset": dataset,
                "method": method,
                "metric": validator.METRICS[dataset],
                "query": {
                    "path": f"/query/{dataset}/{method}",
                    "format": query_format,
                    "source_rows": 10000,
                    "rows": 10000,
                    "dim": validator.DIMENSIONS[dataset],
                    "canonical_sha256": digest(f"{dataset}/query-canonical"),
                    "file_sha256": digest(f"{dataset}/{method}/query-file"),
                    "bytes": 10000,
                },
                "groundtruth": {
                    "path": f"/groundtruth/{dataset}/{method}",
                    "format": gt_format,
                    "layout": "ids_only",
                    "source_rows": 10000,
                    "rows": 10000,
                    "k": 100,
                    "canonical_ids_sha256": digest(f"{dataset}/gt-canonical"),
                    "file_sha256": digest(f"{dataset}/{method}/gt-file"),
                    "bytes": 10000,
                },
            }
            (directory / f"{dataset.lower()}_{slug}.json").write_text(
                json.dumps(record, sort_keys=True)
            )
    return directory


def write_bundle(root: Path) -> Path:
    bundle = root / "frontier_1m"
    query_pools = write_query_pools(bundle)
    query_links = aggregate.load_query_pool_evidence(query_pools)
    source_meta: dict[tuple[str, int], tuple[str, str]] = {}
    for kind in ("sw", "dhnsw"):
        for repeat in range(1, 6):
            relative = f"raw_sources/{kind}/r{repeat}.csv"
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"kind,repeat\n{kind},{repeat}\n")
            source_meta[(kind, repeat)] = (relative, validator.file_sha256(path))

    rows: list[dict[str, object]] = []
    for dataset in assembler.DATASETS:
        for method in assembler.METHODS:
            for ef in (48, 64, 96, 128, 200):
                for repeat in range(1, 6):
                    kind = "dhnsw" if method == "d-HNSW" else "sw"
                    source, source_sha = source_meta[(kind, repeat)]
                    row: dict[str, object] = {
                        "dataset": dataset,
                        "method": method,
                        "ef": ef,
                        "run_id": f"r{repeat}",
                        "recall": 0.75 + ef / 1000,
                        "qps": 12000 - ef - repeat,
                        "threads": 10,
                        "query_contexts": "" if method == "d-HNSW" else 10,
                        "top_k": 10,
                        "metric": validator.METRICS[dataset],
                        "measurement_mode": "fixed_query_pool",
                        "protocol_fingerprint": digest(f"{dataset}/{method}/{ef}"),
                        "campaign_id": "vldb-1m-fixture",
                        "binary_sha256": DHNSW_SHA if method == "d-HNSW" else FINAL_SHA,
                        "variant": {
                            "SHINE": "shine_path",
                            "SlabWalk": "slabwalk_expansion",
                            "d-HNSW": "fixed_routing_partition",
                        }[method],
                        "lavd_bits": "" if method == "d-HNSW" else (8 if method == "SlabWalk" else 0),
                        "index_region_bytes": "" if method == "d-HNSW" else validator.INDEX_REGION_BYTES,
                        "lavd_region_bytes": "" if method == "d-HNSW" else (
                            validator.SLAB_REGION_BYTES[dataset]
                            if method == "SlabWalk"
                            else 0
                        ),
                        "layout_env": (
                            "native_dhnsw"
                            if method == "d-HNSW"
                            else (
                                "SHINE_CRANE=1 SHINE_LAVD_RABITQ_B=2 GB_BITMAP_DEDUP=1"
                                if method == "SlabWalk" and dataset == "GIST1M"
                                else (
                                    "SHINE_CRANE=1 GB_BITMAP_DEDUP=1"
                                    if method == "SlabWalk"
                                    else "none"
                                )
                            )
                        ),
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
                        "source": source,
                        "source_sha256": source_sha,
                    }
                    link = query_links[(dataset, method)]
                    for field in aggregate.QUERY_POOL_LINK_FIELDS:
                        row[field] = link[field]
                    rows.append(row)
    write_csv(bundle / "frontier_repeated_raw.csv", rows)
    write_csv(bundle / "frontier_summary.csv", aggregate.summarize(rows, 5))
    (bundle / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "vldb-1m-fixture",
                "protocol": {
                    "repeats": 5,
                    "threads": 10,
                    "query_contexts": 10,
                    "top_k": 10,
                    "measurement_mode": "fixed_query_pool",
                },
            },
            sort_keys=True,
        )
    )
    (bundle / "PROVENANCE.json").write_text(
        json.dumps(
            {
                "expected_repeats": 5,
                "expected_datasets": list(assembler.DATASETS),
                "expected_methods": list(assembler.METHODS),
            },
            sort_keys=True,
        )
    )
    assembler.write_sha256s(bundle)
    return bundle


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def convert_to_composite_bundle(bundle: Path, source_root: Path) -> None:
    raw_path = bundle / "frontier_repeated_raw.csv"
    rows = read_rows(raw_path)
    for row in rows:
        row["campaign_id"] = (
            "frozen-dhnsw" if row["method"] == "d-HNSW" else "v5-sw"
        )
    write_csv(raw_path, rows)
    write_csv(bundle / "frontier_summary.csv", aggregate.summarize(rows, 5))

    common_protocol = {
        "repeats": 5,
        "threads": 10,
        "query_contexts": 10,
        "top_k": 10,
        "measurement_mode": "fixed_query_pool",
    }
    sw_campaign = source_root / "sw_campaign"
    dhnsw_campaign = source_root / "dhnsw_campaign"
    sw_campaign.mkdir(parents=True)
    dhnsw_campaign.mkdir(parents=True)
    (sw_campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "v5-sw",
                "protocol_fingerprint": "a" * 64,
                "protocol": {**common_protocol, "gb_binary_sha256": FINAL_SHA},
            },
            sort_keys=True,
        )
    )
    (dhnsw_campaign / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "frozen-dhnsw",
                "protocol_fingerprint": "b" * 64,
                "protocol": common_protocol,
            },
            sort_keys=True,
        )
    )
    source_records = assembler.write_campaign_evidence(
        sw_campaign, dhnsw_campaign, bundle
    )
    provenance = json.loads((bundle / "PROVENANCE.json").read_text())
    provenance["source_campaigns"] = source_records
    provenance["campaign_manifest_sha256"] = validator.file_sha256(
        bundle / "campaign.json"
    )
    (bundle / "PROVENANCE.json").write_text(
        json.dumps(provenance, sort_keys=True)
    )
    assembler.write_sha256s(bundle)


class ValidateVldbFrontier1MTest(unittest.TestCase):
    def test_validation_report_must_be_fresh_and_outside_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            with self.assertRaisesRegex(ValueError, "outside.*bundle"):
                validator.validate_report_path(bundle, bundle / "validation.json")

            report = root / "validation.json"
            report.write_text("existing\n")
            with self.assertRaisesRegex(ValueError, "existing"):
                validator.validate_report_path(bundle, report)

    def test_complete_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            report = validator.validate(write_bundle(Path(tmp_s)), FINAL_SHA)
            self.assertTrue(report["ready_for_plotting"])
            self.assertEqual(report["measured_rows"], 525)
            self.assertEqual(report["summary_rows"], 105)
            self.assertEqual(report["query_pool_cells"], 21)

    def test_split_campaign_bundle_passes_with_explicit_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            convert_to_composite_bundle(bundle, root / "sources")
            report = validator.validate(bundle, FINAL_SHA)
            self.assertEqual(report["campaign_mode"], "composite")
            self.assertEqual(report["source_campaigns_verified"], 2)
            self.assertTrue(report["campaign_id"].startswith("vldb-frontier-1m-composite-"))

    def test_split_campaign_bundle_rejects_tampered_source_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            convert_to_composite_bundle(bundle, root / "sources")
            source = bundle / "source_campaigns" / "dhnsw.json"
            source.write_text(source.read_text() + "\n")
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "retained source link mismatch"):
                validator.validate(bundle, FINAL_SHA)

    def test_split_campaign_bundle_rejects_method_source_remap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            convert_to_composite_bundle(bundle, root / "sources")
            campaign_path = bundle / "campaign.json"
            campaign = json.loads(campaign_path.read_text())
            campaign["method_sources"]["SHINE"] = "dhnsw"
            campaign_path.write_text(json.dumps(campaign, sort_keys=True))
            provenance_path = bundle / "PROVENANCE.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["campaign_manifest_sha256"] = validator.file_sha256(
                campaign_path
            )
            provenance_path.write_text(json.dumps(provenance, sort_keys=True))
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "method-to-source map"):
                validator.validate(bundle, FINAL_SHA)

    def test_split_campaign_bundle_rejects_wrong_slabwalk_source_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            convert_to_composite_bundle(bundle, root / "sources")
            source_path = bundle / "source_campaigns" / "shine_slabwalk.json"
            source = json.loads(source_path.read_text())
            source["protocol"]["gb_binary_sha256"] = "c" * 64
            source_path.write_text(json.dumps(source, sort_keys=True))
            source_sha = validator.file_sha256(source_path)
            campaign_path = bundle / "campaign.json"
            campaign = json.loads(campaign_path.read_text())
            next(
                record
                for record in campaign["source_campaigns"]
                if record["role"] == "shine_slabwalk"
            )["manifest_sha256"] = source_sha
            campaign_path.write_text(json.dumps(campaign, sort_keys=True))
            provenance_path = bundle / "PROVENANCE.json"
            provenance = json.loads(provenance_path.read_text())
            next(
                record
                for record in provenance["source_campaigns"]
                if record["role"] == "shine_slabwalk"
            )["sha256"] = source_sha
            provenance["campaign_manifest_sha256"] = validator.file_sha256(
                campaign_path
            )
            provenance_path.write_text(json.dumps(provenance, sort_keys=True))
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "source binary SHA"):
                validator.validate(bundle, FINAL_SHA)

    def test_split_campaign_bundle_rejects_row_bound_to_wrong_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            bundle = write_bundle(root)
            convert_to_composite_bundle(bundle, root / "sources")
            raw_path = bundle / "frontier_repeated_raw.csv"
            rows = read_rows(raw_path)
            for row in rows:
                if row["dataset"] == "DEEP1M" and row["method"] == "SlabWalk":
                    row["campaign_id"] = "frozen-dhnsw"
            write_csv(raw_path, rows)
            write_csv(bundle / "frontier_summary.csv", aggregate.summarize(rows, 5))
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "campaign mismatch"):
                validator.validate(bundle, FINAL_SHA)

    def test_missing_system_curve_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            bundle = write_bundle(Path(tmp_s))
            raw = bundle / "frontier_repeated_raw.csv"
            rows = [
                row
                for row in read_rows(raw)
                if not (row["dataset"] == "GIST1M" and row["method"] == "d-HNSW")
            ]
            write_csv(raw, rows)
            write_csv(bundle / "frontier_summary.csv", aggregate.summarize(rows, 5))
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "frontier matrix"):
                validator.validate(bundle, FINAL_SHA)

    def test_cross_system_query_content_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            bundle = write_bundle(Path(tmp_s))
            path = bundle / "query_pools" / "bigann1m_dhnsw.json"
            record = json.loads(path.read_text())
            record["query"]["canonical_sha256"] = "a" * 64
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "query-pool content mismatch"):
                validator.validate_query_pools(bundle / "query_pools")

    def test_summary_not_recomputed_from_raw_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            bundle = write_bundle(Path(tmp_s))
            raw = bundle / "frontier_repeated_raw.csv"
            rows = read_rows(raw)
            rows[0]["qps"] = "1"
            write_csv(raw, rows)
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "summary mismatch"):
                validator.validate(bundle, FINAL_SHA)

    def test_undersized_slab_region_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            bundle = write_bundle(Path(tmp_s))
            raw = bundle / "frontier_repeated_raw.csv"
            rows = read_rows(raw)
            for row in rows:
                if row["dataset"] == "GIST1M" and row["method"] == "SlabWalk":
                    row["lavd_region_bytes"] = str(6 * 1024**3)
            write_csv(raw, rows)
            write_csv(bundle / "frontier_summary.csv", aggregate.summarize(rows, 5))
            assembler.write_sha256s(bundle)
            with self.assertRaisesRegex(ValueError, "Slab layout contract"):
                validator.validate(bundle, FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
