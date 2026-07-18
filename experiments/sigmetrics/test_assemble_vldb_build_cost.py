import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_build_cost as assembler
import validate_vldb_final_evidence as evidence


FINAL_SHA = "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6"
SOURCE_SHA = "f" * 64


SPECS = {
    "SIFT1M": ("sift1m", "scalar", 8, 4_616_016_384, 796_799_780),
    "DEEP1M": ("deep1m", "scalar", 8, 3_592_016_384, 668_799_780),
    "GIST1M": ("gist1m", "rabitq", 2, 8_456_016_384, 4_124_799_780),
}


def write_run(raw: Path, dataset: str, repeat: int) -> None:
    meta_name, code, bits, region, authoritative = SPECS[dataset]
    stages = {
        "lavd_build_fetch": 500.0 + repeat,
        "lavd_build_parse": 100.0,
        "lavd_build_rank": 0.001,
        "lavd_build_encode": 50.0,
        "lavd_build_metadata": 0.001,
        "lavd_build_materialize": 1_000.0,
    }
    record = {
        "estimated_total_index_size": authoritative,
        "meta": {"dataset": meta_name, "label": f"build_{dataset}_r{repeat}"},
        "num_queries": 10000,
        "num_vectors": 1000000,
        "queries": {"processed": 10000},
        "timings": {"lavd_build_multi": sum(stages.values()), **stages},
    }
    json_path = raw / f"{dataset}_r{repeat}.json"
    json_path.write_text(json.dumps(record, sort_keys=True))
    err = "\n".join(
        [
            "[LAVD][multi] start build, num_mns=1 bits=8 stride=4096 "
            f"rabitq_b={2 if code == 'rabitq' else 0}",
            "LAVD_PHYSICAL_ACCOUNTING "
            + json.dumps(
                {
                    "record_layout": "fixed",
                    "scoring_code": code,
                    "scoring_bits": bits,
                    "materialized_bytes": region,
                },
                separators=(",", ":"),
            ),
            f"[LAVD][build-profile] peak_rss_kb={7_000_000 + repeat}",
            f"Maximum resident set size (kbytes): {7_100_000 + repeat}",
            "",
        ]
    )
    json_path.with_suffix(".err").write_text(err)
    (raw / f"{dataset}_r{repeat}.mn.err").write_text("memory-node stderr\n")


def write_campaign(
    root: Path,
    dataset: str,
    *,
    campaign_id: str,
    extra_dataset: str | None = None,
    admission: dict | None = None,
) -> None:
    raw = root / "raw"
    raw.mkdir(parents=True)
    datasets = [dataset] + ([extra_dataset] if extra_dataset else [])
    campaign = {
        "campaign_id": campaign_id,
        "binary_sha256": FINAL_SHA,
        "script_sha256": {"SIFT1M": "a", "DEEP1M": "b", "GIST1M": "c"}[dataset] * 64,
        "tcp_port": 1510,
        "repeats": 5,
        "index_region_bytes": 17179869184,
        "compute_node": "skv-node3",
        "memory_node": "skv-node5",
        "datasets": datasets,
        "builder_threads": 20,
        "query_threads": 1,
        "query_coroutines": 1,
        "layout": "packed_fixed",
        "measurement": "derived_build_only",
        "source": {
            "tree_sha256": SOURCE_SHA,
            "file_count": 101,
            "layout": "repository",
            "tree_scope": ["graphbeyond/CMakeLists.txt", "graphbeyond/src"],
        },
        "admission": admission,
    }
    (raw / "campaign.json").write_text(json.dumps(campaign, sort_keys=True))
    for repeat in range(5):
        write_run(raw, dataset, repeat)
    if extra_dataset:
        (raw / f"{extra_dataset}_r0.json").write_bytes(b"")
        (raw / f"{extra_dataset}_r0.err").write_text("preflight failure\n")
        (raw / "campaign_partition.json").write_text(
            json.dumps(
                {
                    "status": "partial_success",
                    "admitted_cells": [dataset],
                    "excluded_cells": [extra_dataset],
                },
                sort_keys=True,
            )
        )


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_construction_admission(root: Path) -> dict:
    source = root / "admission_source"
    source.mkdir()
    inputs = {}
    for name in (
        "promotion_report",
        "frontier_cells",
        "candidate_frontier",
        "baseline_frontier",
    ):
        suffix = ".json" if name == "promotion_report" else ".csv"
        path = source / f"{name}{suffix}"
        path.write_text(f"{name}\n")
        inputs[name] = {"path": str(path.resolve()), "sha256": file_sha256(path)}
    gate_path = source / "construction_gate.json"
    gate = {
        "kind": "vldb_construction_candidate_gate_v1",
        "construction_ready": True,
        "general_promotion_ready": False,
        "scope": "construction_measurements_only",
        "failures": [],
        "inputs": inputs,
    }
    gate_path.write_text(json.dumps(gate, sort_keys=True))
    return {
        "kind": gate["kind"],
        "path": str(gate_path.resolve()),
        "sha256": file_sha256(gate_path),
        "scope": gate["scope"],
        "construction_ready": True,
        "general_promotion_ready": False,
    }


class BuildCostAssemblerTest(unittest.TestCase):
    def test_assembles_three_campaigns_and_retains_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sift = root / "sift"
            deep = root / "deep"
            gist = root / "gist"
            excluded = root / "excluded_gist_v2"
            out = root / "final"
            write_campaign(sift, "SIFT1M", campaign_id="sift-v4")
            write_campaign(
                deep,
                "DEEP1M",
                campaign_id="deep-v1",
                extra_dataset="GIST1M",
            )
            write_campaign(gist, "GIST1M", campaign_id="gist-v3")
            (excluded / "raw").mkdir(parents=True)
            (excluded / "raw" / "campaign_failure.json").write_text(
                json.dumps({"status": "excluded", "admitted_rows": 0})
            )

            assembler.assemble(
                sift,
                deep,
                gist,
                out,
                expected_binary_sha=FINAL_SHA,
                expected_source_tree_sha=SOURCE_SHA,
                excluded_campaigns=[excluded],
            )

            report = evidence.validate_build_cost(
                out, FINAL_SHA, expected_source_tree_sha=SOURCE_SHA
            )
            self.assertEqual(report["measured_rows"], 15)
            self.assertEqual(report["retained_raw_files_verified"], 30)
            self.assertEqual(report["source_campaigns_verified"], 3)
            self.assertEqual(report["provenance_run_files_verified"], 45)
            self.assertEqual(len(report["summary_script_sha256"]), 64)
            self.assertEqual(report["source_tree_sha256"], SOURCE_SHA)
            self.assertFalse((out / "raw" / "GIST1M_r0.json").stat().st_size == 0)
            self.assertTrue(
                (out / "provenance" / "source_campaigns" / "DEEP1M" /
                 "campaign_partition.json").is_file()
            )
            self.assertTrue((out / "provenance" / "excluded").is_dir())
            self.assertTrue((out / "SHA256SUMS").is_file())

    def test_incomplete_campaign_does_not_publish_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sift = root / "sift"
            deep = root / "deep"
            gist = root / "gist"
            out = root / "final"
            write_campaign(sift, "SIFT1M", campaign_id="sift-v4")
            write_campaign(deep, "DEEP1M", campaign_id="deep-v1")
            write_campaign(gist, "GIST1M", campaign_id="gist-v3")
            (gist / "raw" / "GIST1M_r4.json").unlink()

            with self.assertRaisesRegex(ValueError, "GIST1M.*five complete repeats"):
                assembler.assemble(
                    sift,
                    deep,
                    gist,
                    out,
                    expected_binary_sha=FINAL_SHA,
                    expected_source_tree_sha=SOURCE_SHA,
                )
            self.assertFalse(out.exists())

    def test_rejects_source_tree_drift_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sift = root / "sift"
            deep = root / "deep"
            gist = root / "gist"
            out = root / "final"
            write_campaign(sift, "SIFT1M", campaign_id="sift-v4")
            write_campaign(deep, "DEEP1M", campaign_id="deep-v1")
            write_campaign(gist, "GIST1M", campaign_id="gist-v3")
            manifest = gist / "raw" / "campaign.json"
            campaign = json.loads(manifest.read_text())
            campaign["source"]["tree_sha256"] = "e" * 64
            manifest.write_text(json.dumps(campaign))

            with self.assertRaisesRegex(ValueError, "source tree SHA"):
                assembler.assemble(
                    sift,
                    deep,
                    gist,
                    out,
                    expected_binary_sha=FINAL_SHA,
                    expected_source_tree_sha=SOURCE_SHA,
                )
            self.assertFalse(out.exists())

    def test_retains_and_validates_construction_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = write_construction_admission(root)
            campaigns = {dataset: root / dataset.lower() for dataset in SPECS}
            for dataset, campaign in campaigns.items():
                write_campaign(
                    campaign,
                    dataset,
                    campaign_id=f"{dataset.lower()}-construction",
                    admission=admission,
                )
            out = root / "final"

            assembler.assemble(
                campaigns["SIFT1M"],
                campaigns["DEEP1M"],
                campaigns["GIST1M"],
                out,
                expected_binary_sha=FINAL_SHA,
                expected_source_tree_sha=SOURCE_SHA,
                expected_admission_gate_sha=admission["sha256"],
                expected_admission_scope="construction_measurements_only",
            )
            report = evidence.validate_build_cost(
                out,
                FINAL_SHA,
                expected_source_tree_sha=SOURCE_SHA,
                expected_admission_gate_sha=admission["sha256"],
                expected_admission_scope="construction_measurements_only",
            )

            self.assertEqual(report["admission_gate_sha256"], admission["sha256"])
            self.assertEqual(report["admission_inputs_verified"], 4)
            self.assertTrue(
                (out / "provenance" / "admission" / "construction_gate.json").is_file()
            )
            self.assertEqual(
                len(list((out / "provenance" / "admission" / "inputs").iterdir())),
                4,
            )

    def test_rejects_admission_input_drift_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = write_construction_admission(root)
            campaigns = {dataset: root / dataset.lower() for dataset in SPECS}
            for dataset, campaign in campaigns.items():
                write_campaign(
                    campaign,
                    dataset,
                    campaign_id=f"{dataset.lower()}-construction",
                    admission=admission,
                )
            gate = json.loads(Path(admission["path"]).read_text())
            Path(gate["inputs"]["frontier_cells"]["path"]).write_text("drift\n")
            out = root / "final"

            with self.assertRaisesRegex(ValueError, "frontier_cells.*SHA drift"):
                assembler.assemble(
                    campaigns["SIFT1M"],
                    campaigns["DEEP1M"],
                    campaigns["GIST1M"],
                    out,
                    expected_binary_sha=FINAL_SHA,
                    expected_source_tree_sha=SOURCE_SHA,
                    expected_admission_gate_sha=admission["sha256"],
                    expected_admission_scope="construction_measurements_only",
                )
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
