import csv
import json
import tempfile
import unittest
from pathlib import Path

import assemble_vldb_lifecycle_controls as assembler
import validate_vldb_final_evidence as evidence


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_lifecycle_sources(root: Path) -> tuple[Path, Path, Path, Path]:
    refresh_root = root / "refresh_raw"
    refresh_rows = []
    for batch, touched, frac in (
        (1000, 12403, 0.0123958),
        (10000, 98881, 0.0988663),
        (50000, 412589, None),
        (100000, 638132, 0.638156),
    ):
        source = refresh_root / f"K{batch}.err"
        source.parent.mkdir(parents=True, exist_ok=True)
        amp = touched / batch
        if frac is None:
            maintain = (
                f"[LAVD][maintain] inserts={batch} touched={touched} "
                f"blocks/insert={amp} write_amp={amp} (m_max0=32)"
            )
            diff_mb = full_mb = ""
        else:
            diff_mb_value = frac * 800.573
            maintain = "\n".join([
                f"[LAVD][maintain] mode=init inserts={batch} touched={touched} "
                f"blocks/insert={amp} write_amp={amp} read_MB=800.573 "
                "full_idx_MB=800.573 read_frac=1 (m_max0=32)",
                f"[LAVD][maintain] mode=diff inserts={batch} touched={touched} "
                f"blocks/insert={amp} write_amp={amp} read_MB={diff_mb_value} "
                f"full_idx_MB=800.573 read_frac={frac} (m_max0=32)",
            ])
            diff_mb, full_mb = diff_mb_value, 800.573
        source.write_text("\n".join([
            maintain,
            "[LAVD][maintain][selftest] slots=1000000 mismatches=0 PASS (delta==full-rebuild)",
            "[STATUS]: local recall: 0.976620",
        ]))
        refresh_rows.append({
            "batch_inserts": batch,
            "touched_blocks": touched,
            "write_amp_blocks_per_insert": amp,
            "diff_read_frac": "" if frac is None else frac,
            "diff_read_mb": diff_mb,
            "full_index_mb": full_mb,
            "byte_identical": "PASS",
            "recall": 0.97662,
            "source": source.name,
        })
    refresh_summary = root / "refresh_summary.csv"
    write_csv(refresh_summary, refresh_rows)

    configs = (
        ("fp32 baseline", 1),
        ("sq8 Slabs", 1),
        ("sq8 Slabs+upper graph", 1),
        ("RaBitQ-2 Slabs", 1),
        ("RaBitQ-4 Slabs", 1),
        ("fp32 baseline 16T", 16),
        ("sq8 Slabs 16T", 16),
        ("sq8 Slabs+upper graph 16T", 16),
    )
    tti_root = root / "tti_raw"
    tti_rows = []
    for index, (config, threads) in enumerate(configs):
        source = tti_root / f"tti_{index}.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        processed = 10000
        qps = 100 + index
        recall = 0.80 + index / 100
        posts = (500 + index) * processed
        read_bytes = (1_000_000 + index * 1000) * processed
        source.write_text(json.dumps({
            "meta": {"compute_threads": threads},
            "num_queries": processed,
            "queries": {
                "processed": processed,
                "queries_per_sec": qps,
                "recall": recall,
                "rdma_posts": posts,
                "rdma_reads_in_bytes": read_bytes,
            },
        }, sort_keys=True))
        tti_rows.append({
            "config": config,
            "threads": threads,
            "ef": 300,
            "qps": qps,
            "recall": recall,
            "posts_per_query": posts / processed,
            "mb_per_query": read_bytes / processed / 1e6,
            "note": "boundary",
            "source": source.name,
        })
    tti_summary = root / "tti_summary.csv"
    write_csv(tti_summary, tti_rows)
    return refresh_summary, refresh_root, tti_summary, tti_root


class LifecycleControlAssemblyTest(unittest.TestCase):
    def test_assembles_and_validates_retained_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refresh_summary, refresh_root, tti_summary, tti_root = write_lifecycle_sources(root)
            out = root / "out"
            assembler.assemble(
                refresh_summary, refresh_root, tti_summary, tti_root, out
            )
            report = evidence.validate_lifecycle_controls(out)
            self.assertEqual(report["refresh_cells"], 4)
            self.assertEqual(report["tti_cells"], 8)
            self.assertEqual(report["retained_sources_verified"], 12)

    def test_rejects_tampered_retained_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refresh_summary, refresh_root, tti_summary, tti_root = write_lifecycle_sources(root)
            out = root / "out"
            assembler.assemble(
                refresh_summary, refresh_root, tti_summary, tti_root, out
            )
            source = next((out / "raw_sources" / "tti").glob("*.json"))
            source.write_text(source.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "lifecycle inventory.*SHA"):
                evidence.validate_lifecycle_controls(out)


if __name__ == "__main__":
    unittest.main()
