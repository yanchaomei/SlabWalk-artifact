import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from experiments.sigmetrics import assemble_vldb_query_profile as profile
    from experiments.sigmetrics import validate_vldb_final_evidence as final_evidence
except ModuleNotFoundError:
    import assemble_vldb_query_profile as profile
    import validate_vldb_final_evidence as final_evidence


EXPECTED_BINARY = "2" * 64


class QueryProfileAssemblerTest(unittest.TestCase):
    def make_campaign(
        self,
        root: Path,
        *,
        lost_samples: int = 0,
        binary_sha: str = EXPECTED_BINARY,
    ) -> tuple[Path, str]:
        source = root / "campaign"
        source.mkdir()
        runner = source / "runner_snapshot.sh"
        runner.write_text("#!/usr/bin/env bash\necho profile\n")
        runner_sha = hashlib.sha256(runner.read_bytes()).hexdigest()
        perf_data = source / "SIFT1M_shine_T1_C8_ef100.perf.data"
        perf_data.write_bytes(b"synthetic perf data")
        perf_sha = hashlib.sha256(perf_data.read_bytes()).hexdigest()
        (source / "SIFT1M_shine_T1_C8_ef100.perf.data.sha256").write_text(
            f"{perf_sha}  {perf_data}\n"
        )
        (source / "SIFT1M_shine_T1_C8_ef100.perf.record.status").write_text("0\n")
        (source / "profile_sources.sha256").write_text(
            f"{runner_sha}  {runner}\n{binary_sha}  /frozen/shine\n"
        )
        protocol = {
            "binary_sha256": binary_sha,
            "datasets": ["SIFT1M"],
            "methods": ["shine"],
            "threads": 1,
            "query_contexts_requested": 1,
            "coroutines": 8,
            "ef": 100,
            "top_k": 10,
            "query_tile": 20,
            "profile_seconds": 20,
            "capture_perf": True,
            "compute_recall": False,
            "memory_nodes_by_dataset": {"SIFT1M": "skv-node4"},
            "compute_host": "skv-node7",
        }
        fingerprint = hashlib.sha256(
            json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        (source / "campaign.json").write_text(
            json.dumps(
                {
                    "campaign_id": "profile-test",
                    "protocol_fingerprint": fingerprint,
                    "protocol": protocol,
                }
            )
        )
        (source / "SIFT1M_shine_T1_C8_ef100.json").write_text(
            json.dumps(
                {
                    "num_queries": 200000,
                    "query_contexts": 1,
                    "meta": {
                        "dataset": "sift1m",
                        "compute_threads": 1,
                        "coroutines_per_thread": 8,
                        "query_suffix": "profile20x",
                    },
                    "hnsw_parameters": {"ef_search": 100, "k": 10},
                    "queries": {
                        "compute_recall": "false",
                        "processed": 200000,
                        "queries_per_sec": 716,
                        "rdma_posts": 354000000,
                        "rdma_reads_in_bytes": 18000000000,
                    },
                }
            )
        )
        (source / "SIFT1M_shine_T1_C8_ef100.perf.txt").write_text(
            "\n".join(
                [
                    f"# Total Lost Samples: {lost_samples}",
                    "# Samples: 20K of event 'cycles:u'",
                    "# Overhead  Command  Shared Object  Symbol  IPC [IPC Coverage]",
                    "    17.90%  shine  shine  [.] l2  -  -",
                    "    17.20%  shine  libpthread.so  [.] pthread_spin_lock  -  -",
                    "    12.40%  shine  libc.so  [.] malloc  -  -",
                ]
            )
            + "\n"
        )
        return source, runner_sha

    def test_assembles_a_content_linked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root)
            out = root / "out"
            profile.assemble(
                source,
                out,
                expected_binary_sha=EXPECTED_BINARY,
                expected_runner_sha=runner_sha,
            )
            with (out / "summary" / "summary.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(float(rows[0]["distance_self_percent"]), 17.9)
            self.assertEqual(int(rows[0]["query_rows"]), 200000)
            self.assertEqual(float(rows[0]["posts_per_query"]), 1770.0)
            self.assertTrue((out / "raw_sources" / "runner_snapshot.sh").is_file())
            self.assertTrue((out / "VALIDATION.json").is_file())
            self.assertTrue((out / "SHA256SUMS").is_file())

    def test_rejects_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root)
            with self.assertRaisesRegex(ValueError, "binary SHA"):
                profile.assemble(
                    source,
                    root / "out",
                    expected_binary_sha="3" * 64,
                    expected_runner_sha=runner_sha,
                )

    def test_rejects_lost_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root, lost_samples=1)
            with self.assertRaisesRegex(ValueError, "lost samples"):
                profile.assemble(
                    source,
                    root / "out",
                    expected_binary_sha=EXPECTED_BINARY,
                    expected_runner_sha=runner_sha,
                )

    def test_refuses_an_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root)
            out = root / "out"
            out.mkdir()
            with self.assertRaisesRegex(FileExistsError, "output exists"):
                profile.assemble(
                    source,
                    out,
                    expected_binary_sha=EXPECTED_BINARY,
                    expected_runner_sha=runner_sha,
                )

    def test_final_gate_recomputes_the_profile_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root)
            out = root / "out"
            profile.assemble(
                source,
                out,
                expected_binary_sha=EXPECTED_BINARY,
                expected_runner_sha=runner_sha,
            )
            report = final_evidence.validate_query_profile(
                out, EXPECTED_BINARY, runner_sha
            )
            self.assertEqual(report["query_rows"], 200000)
            self.assertEqual(report["distance_self_percent"], 17.9)

    def test_final_gate_rejects_a_tampered_profile_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, runner_sha = self.make_campaign(root)
            out = root / "out"
            profile.assemble(
                source,
                out,
                expected_binary_sha=EXPECTED_BINARY,
                expected_runner_sha=runner_sha,
            )
            summary = out / "summary" / "summary.csv"
            summary.write_text(summary.read_text().replace("17.9", "99.9"))
            with self.assertRaisesRegex(ValueError, "profile summary mismatch"):
                final_evidence.validate_query_profile(
                    out, EXPECTED_BINARY, runner_sha
                )


if __name__ == "__main__":
    unittest.main()
