import json
import tempfile
import unittest
from pathlib import Path

import summarize_slab_build_cost as build_summary
import validate_vldb_final_evidence as evidence


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"


def write_build_cost_evidence(directory: Path) -> None:
    raw = directory / "raw"
    raw.mkdir(parents=True)
    campaign = {
        "campaign_id": "build-cost-final",
        "binary_sha256": FINAL_SHA,
        "script_sha256": "a" * 64,
        "tcp_port": 1510,
        "repeats": 5,
        "index_region_bytes": 17179869184,
        "compute_node": "skv-node3",
        "memory_node": "skv-node5",
        "datasets": ["SIFT1M", "DEEP1M", "GIST1M"],
        "builder_threads": 20,
        "query_threads": 1,
        "query_coroutines": 1,
        "layout": "packed_fixed",
        "measurement": "derived_build_only",
    }
    campaign.update(
        {
            "kind": "vldb_build_cost_bundle_v1",
            "script_role": "bundle_assembler",
            "summary_script_sha256": "d" * 64,
        }
    )
    specs = {
        "SIFT1M": ("sift1m", "scalar", 8, 4_616_016_384, 796_799_780),
        "DEEP1M": ("deep1m", "scalar", 8, 3_592_016_384, 668_799_780),
        "GIST1M": ("gist1m", "rabitq", 2, 8_456_016_384, 4_124_799_780),
    }
    for dataset, (meta_name, code, bits, region, authoritative) in specs.items():
        for repeat in range(5):
            stages = {
                "lavd_build_fetch": 500.0 + repeat,
                "lavd_build_parse": 100.0,
                "lavd_build_rank": 0.001,
                "lavd_build_encode": 50.0,
                "lavd_build_metadata": 0.001,
                "lavd_build_materialize": 1_000.0,
            }
            total = sum(stages.values())
            record = {
                "estimated_total_index_size": authoritative,
                "meta": {"dataset": meta_name, "label": f"build_{dataset}_r{repeat}"},
                "num_queries": 10000,
                "num_vectors": 1000000,
                "queries": {"processed": 10000},
                "timings": {"lavd_build_multi": total, **stages},
            }
            json_path = raw / f"{dataset}_r{repeat}.json"
            json_path.write_text(json.dumps(record, sort_keys=True))
            err = "\n".join([
                f"[LAVD][multi] start build, num_mns=1 bits=8 stride=4096 rabitq_b={2 if code == 'rabitq' else 0}",
                "LAVD_PHYSICAL_ACCOUNTING " + json.dumps({
                    "record_layout": "fixed",
                    "scoring_code": code,
                    "scoring_bits": bits,
                    "materialized_bytes": region,
                }, separators=(",", ":")),
                f"[LAVD][build-profile] peak_rss_kb={7_000_000 + repeat}",
                f"Maximum resident set size (kbytes): {7_100_000 + repeat}",
                "",
            ])
            json_path.with_suffix(".err").write_text(err)
            (raw / f"{dataset}_r{repeat}.mn.err").write_text("memory-node stderr\n")

    source_campaigns = []
    provenance_sources = []
    source_root = directory / "provenance" / "source_campaigns"
    for index, dataset in enumerate(("SIFT1M", "DEEP1M", "GIST1M")):
        source_dir = source_root / dataset
        source_dir.mkdir(parents=True)
        source_manifest = dict(campaign)
        source_manifest.update(
            {
                "kind": "source_campaign",
                "campaign_id": f"source-{dataset}",
                "datasets": [dataset],
                "script_sha256": chr(ord("b") + index) * 64,
            }
        )
        source_manifest.pop("script_role", None)
        manifest_path = source_dir / "campaign.json"
        manifest_path.write_text(json.dumps(source_manifest, sort_keys=True))
        entry = {
            "dataset": dataset,
            "campaign_id": source_manifest["campaign_id"],
            "retained_manifest": manifest_path.relative_to(directory).as_posix(),
            "retained_manifest_sha256": evidence.file_sha256(manifest_path),
            "runner_script_sha256": source_manifest["script_sha256"],
        }
        source_campaigns.append(entry)
        provenance_sources.append(dict(entry))

    retained_runs = []
    for dataset in ("SIFT1M", "DEEP1M", "GIST1M"):
        for repeat in range(5):
            for kind, name in (
                ("json", f"{dataset}_r{repeat}.json"),
                ("err", f"{dataset}_r{repeat}.err"),
                ("mn_err", f"{dataset}_r{repeat}.mn.err"),
            ):
                path = raw / name
                retained_runs.append(
                    {
                        "dataset": dataset,
                        "repeat": repeat,
                        "kind": kind,
                        "retained": path.relative_to(directory).as_posix(),
                        "sha256": evidence.file_sha256(path),
                    }
                )
    provenance = {
        "kind": "vldb_build_cost_provenance_v1",
        "assembler": {"sha256": campaign["script_sha256"]},
        "summarizer": {"sha256": campaign["summary_script_sha256"]},
        "source_campaigns": provenance_sources,
        "retained_runs": retained_runs,
        "excluded_campaigns": [],
    }
    provenance_path = directory / "PROVENANCE.json"
    provenance_path.write_text(json.dumps(provenance, sort_keys=True))
    campaign.update(
        {
            "source_campaigns": source_campaigns,
            "provenance_path": "PROVENANCE.json",
            "provenance_sha256": evidence.file_sha256(provenance_path),
        }
    )
    (raw / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))

    parsed = build_summary.collect_runs(raw)
    grouped = build_summary.validate_matrix(
        parsed, ["SIFT1M", "DEEP1M", "GIST1M"], 5
    )
    run_rows, summary_rows, stage_rows = build_summary.summarize(grouped)
    build_summary.write_csv(directory / "runs.csv", run_rows)
    build_summary.write_csv(directory / "summary.csv", summary_rows)
    build_summary.write_csv(directory / "stage_breakdown.csv", stage_rows)


class BuildCostValidationTest(unittest.TestCase):
    def test_parser_accepts_single_mn_staged_snapshot_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stages = {
                "lavd_build_fetch": 450.0,
                "lavd_build_parse": 100.0,
                "lavd_build_rank": 0.001,
                "lavd_build_encode": 50.0,
                "lavd_build_metadata": 0.001,
                "lavd_build_materialize": 1_356.0,
            }
            record = {
                "estimated_total_index_size": 796_799_780,
                "meta": {"dataset": "sift1m", "label": "vldb_build_SIFT1M_r0"},
                "num_queries": 10000,
                "num_vectors": 1000000,
                "queries": {"processed": 10000},
                "timings": {
                    "lavd_build": sum(stages.values()),
                    "crane_build_multi": 250.0,
                    **stages,
                },
            }
            json_path = root / "SIFT1M_r0.json"
            json_path.write_text(json.dumps(record))
            json_path.with_suffix(".err").write_text(
                "\n".join(
                    [
                        'LAVD_BUILD_PUBLICATION {"version":1,"mode":"staged_fixed","workers":20,"staging_bytes":67108864,"records":1000000,"record_write_posts":69}',
                        "[LAVD] build done: N=1000000 m_max0=32 bits=8 stride=4616 budget_f=1 blocks=1000000/1000000 region=4616000000B avg_deg=19.1",
                        "[LAVD][build-profile] peak_rss_kb=5081308",
                        "[LAVD][selftest] checked=64 fails=0 coloc_d=32  PASS",
                        "[LAVD] retained authoritative snapshot for resident upper graph: shards=1",
                        "[CRANE][multi] reused authoritative build snapshot: shards=1",
                        "Maximum resident set size (kbytes): 5200000",
                        "",
                    ]
                )
            )

            run = build_summary.parse_run(json_path)
            self.assertEqual(run.dataset, "SIFT1M")
            self.assertEqual(run.code_name, "sq8")
            self.assertEqual(run.record_mode, "fixed")
            self.assertEqual(run.region_bytes, 4_616_016_384)

    def test_parser_identifies_single_mn_rabitq_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stages = {
                "lavd_build_fetch": 7_500.0,
                "lavd_build_parse": 190.0,
                "lavd_build_rank": 0.001,
                "lavd_build_encode": 11_800.0,
                "lavd_build_metadata": 0.001,
                "lavd_build_materialize": 1_200.0,
            }
            record = {
                "estimated_total_index_size": 4_124_799_780,
                "meta": {"dataset": "gist1m", "label": "vldb_build_GIST1M_r0"},
                "num_queries": 10000,
                "num_vectors": 1000000,
                "queries": {"processed": 10000},
                "timings": {
                    "lavd_build": sum(stages.values()),
                    "crane_build_multi": 820.0,
                    **stages,
                },
            }
            json_path = root / "GIST1M_r0.json"
            json_path.write_text(json.dumps(record))
            json_path.with_suffix(".err").write_text(
                "\n".join(
                    [
                        'LAVD_BUILD_PUBLICATION {"version":1,"mode":"staged_fixed","workers":20,"staging_bytes":67108864,"records":1000000,"record_write_posts":127}',
                        "[LAVD] build done: N=1000000 m_max0=32 bits=8 stride=8456 budget_f=1 blocks=1000000/1000000 region=8456000000B avg_deg=9.79",
                        "[LAVD][build-profile] peak_rss_kb=8922800",
                        "[LAVD][selftest] checked=64 fails=0 coloc_d=32  PASS",
                        "[LAVD][rabitq] CN reconstructed encoder from header: B=2 dim=960 code_bytes=240 rotation_reused=true",
                        "[LAVD] retained authoritative snapshot for resident upper graph: shards=1",
                        "[CRANE][multi] reused authoritative build snapshot: shards=1",
                        "Maximum resident set size (kbytes): 9100000",
                        "",
                    ]
                )
            )

            run = build_summary.parse_run(json_path)
            self.assertEqual(run.code_name, "RaBitQ-2")
            self.assertEqual(run.code_bits_per_dimension, 2)
            self.assertEqual(run.region_bytes, 8_456_016_384)

    def test_accepts_recomputed_five_repeat_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "build_cost"
            write_build_cost_evidence(directory)
            report = evidence.validate_build_cost(directory, FINAL_SHA)
            self.assertEqual(report["measured_rows"], 15)
            self.assertEqual(report["measured_datasets"], 3)
            self.assertEqual(report["retained_raw_files_verified"], 30)

    def test_rejects_tampered_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "build_cost"
            write_build_cost_evidence(directory)
            path = directory / "summary.csv"
            text = path.read_text().replace("1.652002", "99.0", 1)
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "build-cost summary mismatch"):
                evidence.validate_build_cost(directory, FINAL_SHA)

    def test_rejects_nonfinal_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "build_cost"
            write_build_cost_evidence(directory)
            path = directory / "raw" / "campaign.json"
            campaign = json.loads(path.read_text())
            campaign["binary_sha256"] = "b" * 64
            path.write_text(json.dumps(campaign))
            with self.assertRaisesRegex(ValueError, "build-cost binary SHA"):
                evidence.validate_build_cost(directory, FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
