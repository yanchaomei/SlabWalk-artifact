import csv
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_10m_build_scaling as assembler


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"

SPECS = {
    "DEEP10M": ("deep10m", 48, 38_000_000_000),
    "TTI10M": ("tti-10m", 100, 42_000_000_000),
    "SIFT10M": ("sift10m", 64, 40_000_000_000),
}


def write_campaign(root: Path, datasets: list[str]) -> None:
    source_names = ["TEXT10M" if name == "TTI10M" else name for name in datasets]
    campaign = {
        "campaign_id": "fixture-" + "-".join(name.lower() for name in datasets),
        "protocol": {
            "gb_binary_sha256": FINAL_SHA,
            "measurement_mode": "fixed_query_pool",
            "threads": 10,
            "query_contexts": 10,
            "coroutines": 2,
            "top_k": 10,
            "repeats": 5,
            "datasets_sw": source_names,
        },
        "protocol_fingerprint": "a" * 64,
    }
    (root / "campaign.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))

    fields = [
        "dataset",
        "method",
        "variant",
        "binary_sha256",
        "run_id",
        "run_kind",
        "measurement_mode",
        "threads",
        "query_contexts",
        "coroutines",
        "top_k",
        "ef",
        "processed",
        "expected_queries",
        "failed_queries",
        "json",
        "stderr",
        "status",
    ]
    for repeat in range(1, 6):
        run_dir = root / f"sw_r{repeat}"
        run_dir.mkdir()
        rows = []
        for dataset in datasets:
            meta_name, ef, materialized = SPECS[dataset]
            source_name = "TEXT10M" if dataset == "TTI10M" else dataset
            stem = (
                f"{source_name}_slabwalk_expansion_r{repeat}_measure_T10_ef{ef}"
            )
            json_path = run_dir / f"{stem}.json"
            err_path = run_dir / f"{stem}.err"
            stages = {
                "lavd_build_fetch": 10_000.0 + repeat,
                "lavd_build_parse": 2_000.0,
                "lavd_build_rank": 0.01,
                "lavd_build_encode": 3_000.0,
                "lavd_build_metadata": 500.0,
                "lavd_build_materialize": 200_000.0 + repeat * 100,
            }
            build_ms = sum(stages.values()) + 25.0
            record = {
                "meta": {
                    "dataset": meta_name,
                    "label": stem,
                    "compute_threads": 10,
                    "coroutines_per_thread": 2,
                    "memory_nodes": 1,
                },
                "num_vectors": 10_000_000,
                "num_queries": 10_000,
                "query_contexts": 10,
                "hnsw_parameters": {"ef_search": ef, "k": 10},
                "lavd_region_registered_bytes_total": 42_949_672_960,
                "queries": {"processed": 10_000, "recall": 0.95},
                "timings": {
                    "lavd_build_multi": build_ms,
                    "crane_build_multi": 2_000.0 + repeat,
                    **stages,
                },
            }
            json_path.write_text(json.dumps(record, sort_keys=True))
            accounting = {
                "descriptor_version": 2,
                "policy": "block_cyclic",
                "record_layout": "variable",
                "scoring_code": "scalar",
                "scoring_bits": 8,
                "total_slots": 10_000_000,
                "num_mns": 1,
                "mn": 0,
                "offset_table_bytes": 80_000_008,
                "record_bytes": materialized - 80_016_392,
                "materialized_bytes": materialized,
                "registered_bytes": 42_949_672_960,
                "actual_write_bytes": materialized,
            }
            err_path.write_text(
                "LAVD_PHYSICAL_ACCOUNTING "
                + json.dumps(accounting, separators=(",", ":"))
                + "\n[LAVD][multi] build done: N=10000000 edges=1 avg_deg=1\n"
            )
            rows.append(
                {
                    "dataset": source_name,
                    "method": "SlabWalk",
                    "variant": "slabwalk_expansion",
                    "binary_sha256": FINAL_SHA,
                    "run_id": f"r{repeat}",
                    "run_kind": "measure",
                    "measurement_mode": "fixed_query_pool",
                    "threads": 10,
                    "query_contexts": 10,
                    "coroutines": 2,
                    "top_k": 10,
                    "ef": ef,
                    "processed": 10_000,
                    "expected_queries": 10_000,
                    "failed_queries": 0,
                    "json": str(json_path),
                    "stderr": str(err_path),
                    "status": "ok",
                }
            )
        with (run_dir / "slabwalk_shine_frontier_raw.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


class TenMillionBuildScalingAssemblerTest(unittest.TestCase):
    def test_assembles_one_canonical_build_per_dataset_and_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])

            assembler.assemble(
                deep_campaign=deep,
                text_sift_campaign=text_sift,
                out_dir=out,
                expected_binary_sha=FINAL_SHA,
            )
            report = assembler.validate_bundle(out, FINAL_SHA)

            self.assertEqual(report["runs"], 15)
            self.assertEqual(report["datasets"], 3)
            self.assertEqual(report["retained_sources"], 45)
            with (out / "summary.csv").open(newline="") as handle:
                summary = list(csv.DictReader(handle))
            self.assertEqual({row["dataset"] for row in summary}, set(SPECS))
            self.assertTrue(all(int(row["n"]) == 5 for row in summary))
            self.assertTrue((out / "SHA256SUMS").is_file())
            self.assertTrue((out / "PROVENANCE.json").is_file())

    def test_missing_selected_source_cannot_publish_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            missing = next((text_sift / "sw_r5").glob("SIFT10M_slabwalk*.json"))
            missing.unlink()

            with self.assertRaisesRegex(ValueError, "SIFT10M.*repeat 5"):
                assembler.assemble(
                    deep_campaign=deep,
                    text_sift_campaign=text_sift,
                    out_dir=out,
                    expected_binary_sha=FINAL_SHA,
                )
            self.assertFalse(out.exists())

    def test_wrong_binary_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            campaign = json.loads((deep / "campaign.json").read_text())
            campaign["protocol"]["gb_binary_sha256"] = "f" * 64
            (deep / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))

            with self.assertRaisesRegex(ValueError, "binary SHA"):
                assembler.assemble(
                    deep_campaign=deep,
                    text_sift_campaign=text_sift,
                    out_dir=out,
                    expected_binary_sha=FINAL_SHA,
                )
            self.assertFalse(out.exists())

    def test_protocol_error_names_the_drifted_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            measurement = next(
                (text_sift / "sw_r1").glob(
                    "TEXT10M_slabwalk_expansion_r1_measure_T10_ef100.json"
                )
            )
            payload = json.loads(measurement.read_text())
            payload["meta"]["dataset"] = "obsolete-alias"
            measurement.write_text(json.dumps(payload, sort_keys=True))

            with self.assertRaisesRegex(ValueError, r"TTI10M.*meta\.dataset"):
                assembler.assemble(
                    deep_campaign=deep,
                    text_sift_campaign=text_sift,
                    out_dir=out,
                    expected_binary_sha=FINAL_SHA,
                )
            self.assertFalse(out.exists())

    def test_validator_reparses_retained_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            assembler.assemble(
                deep_campaign=deep,
                text_sift_campaign=text_sift,
                out_dir=out,
                expected_binary_sha=FINAL_SHA,
            )

            measurement = out / "raw" / "TTI10M" / "r1" / "measurement.json"
            payload = json.loads(measurement.read_text())
            payload["timings"]["lavd_build_multi"] += 1_000.0
            measurement.write_text(json.dumps(payload, sort_keys=True))
            with (out / "runs.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                if row["dataset"] == "TTI10M" and row["repeat"] == "1":
                    row["source_json_sha256"] = assembler.sha256(measurement)
            with (out / "runs.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=assembler.RUN_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            assembler.write_sha256s(out)

            with self.assertRaisesRegex(
                ValueError, r"retained source|measurement|build time"
            ):
                assembler.validate_bundle(out, FINAL_SHA)

    def test_validator_pins_the_assembler_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            assembler.assemble(
                deep_campaign=deep,
                text_sift_campaign=text_sift,
                out_dir=out,
                expected_binary_sha=FINAL_SHA,
            )
            campaign = json.loads((out / "campaign.json").read_text())
            campaign["assembler_sha256"] = "f" * 64
            (out / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))
            assembler.write_sha256s(out)

            with self.assertRaisesRegex(ValueError, "assembler SHA"):
                assembler.validate_bundle(out, FINAL_SHA)

    def test_validator_rejects_signed_extra_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "deep"
            text_sift = root / "text_sift"
            out = root / "out"
            write_campaign(deep, ["DEEP10M"])
            write_campaign(text_sift, ["TTI10M", "SIFT10M"])
            assembler.assemble(
                deep_campaign=deep,
                text_sift_campaign=text_sift,
                out_dir=out,
                expected_binary_sha=FINAL_SHA,
            )
            (out / "untracked-interpretation.txt").write_text("not evidence\n")
            assembler.write_sha256s(out)

            with self.assertRaisesRegex(ValueError, "inventory"):
                assembler.validate_bundle(out, FINAL_SHA)


if __name__ == "__main__":
    unittest.main()
