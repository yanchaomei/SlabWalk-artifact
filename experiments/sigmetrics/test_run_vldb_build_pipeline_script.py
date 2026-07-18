import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class BuildPipelineScriptContractTest(unittest.TestCase):
    def test_runner_declares_rotated_builder_protocol(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        text = script.read_text()
        for token in (
            "BUILD_THREADS_LIST",
            "run_vldb_materialization_policy.sh",
            "summarize_vldb_build_pipeline.py",
            "EXPECTED_BINARY_SHA",
            "cell_index.csv",
            "protocol_fingerprint",
            "outer_repeat",
            "SHINE_LAVD_STAGED_BUILD",
            "rank_workers",
            "sha256sum -c SHA256SUMS",
            "vldb_evidence_bundle.py",
            "VLDB_BUILD_HARNESS_FROZEN",
            "INNER_SUMMARIZER",
            "verify-harness",
            "HARNESS_MANIFEST_SHA256",
            'SUMMARIZER="$INNER_SUMMARIZER"',
            "seal --root",
            "verify --root",
            "SEALED.json",
            "COMPUTE_HOST=$(hostname)",
            '"compute_host": compute_host',
            "build-pipeline compute host drift",
            "--expected-compute-host",
        ):
            self.assertIn(token, text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("killall", text)
        self.assertNotIn('output = root / "SHA256SUMS"', text)

    def test_dry_run_accepts_non_power_of_two_worker(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "DATASET": "DEEP1M",
                "BUILD_THREADS_LIST": "1 7 20",
                "REPEATS": "3",
                "WARMUPS": "1",
            }
        )
        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("build_threads=1 7 20", completed.stdout)
        self.assertIn("order=r0:1,7,20;r1:7,20,1;r2:20,1,7", completed.stdout)

    def test_default_formal_campaign_completes_worker_position_balance(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("campaign_kind=formal", completed.stdout)
        self.assertIn("repeats=7", completed.stdout)

    def test_formal_campaign_rejects_incomplete_worker_position_balance(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "CAMPAIGN_KIND": "formal",
                "BUILD_THREADS_LIST": "1 2 4",
                "REPEATS": "2",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("position-balanced", completed.stderr)

    def test_smoke_campaign_allows_partial_worker_rotation(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "CAMPAIGN_KIND": "smoke",
                "BUILD_THREADS_LIST": "1 2 4",
                "REPEATS": "2",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("campaign_kind=smoke", completed.stdout)

    def test_dry_run_rejects_unsorted_worker_protocol(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        env = os.environ.copy()
        env.update(
            {
                "EXPECTED_BINARY_SHA": "a" * 64,
                "DRY_RUN": "1",
                "BUILD_THREADS_LIST": "4 1 2",
            }
        )

        completed = subprocess.run(
            ["bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("strictly increasing", completed.stderr)

    def test_rejects_child_that_ignores_requested_builder_workers(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            binary = root / "candidate"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            expected_sha = subprocess.check_output(
                ["shasum", "-a", "256", str(binary)], text=True
            ).split()[0]
            inner = root / "fake_inner.py"
            inner.write_text(
                """#!/usr/bin/env python3
import csv, hashlib, json, os, socket
from pathlib import Path
out = Path(os.environ["OUT_ROOT"])
out.mkdir(parents=True)
sha = hashlib.sha256(Path(os.environ["GB_BIN"]).read_bytes()).hexdigest()
json.dump({"protocol": {"binary_sha256": sha, "datasets": ["DEEP1M"],
    "policies": ["indeg"], "budget_bytes": [536870912], "repeats": 1,
    "build_threads": 20, "staged_build": True,
    "compute_host": socket.gethostname()}},
    open(out / "campaign.json", "w"))
row = {"binary_sha256": sha, "build_mode": "staged", "build_workers": 20,
       "compute_host": socket.gethostname()}
with open(out / "runs.csv", "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(row))
    writer.writeheader(); writer.writerow(row)
"""
            )
            inner.chmod(0o755)
            summarizer = root / "unused_summarizer.py"
            summarizer.write_text("raise SystemExit('must not be reached')\n")
            out = root / "out"
            env = os.environ.copy()
            env.update(
                {
                    "EXPECTED_BINARY_SHA": expected_sha,
                    "GB_BIN": str(binary),
                    "GB_BIN_R": str(binary),
                    "INNER_RUNNER": str(inner),
                    "SUMMARIZER": str(summarizer),
                    "OUT_ROOT": str(out),
                    "BUILD_THREADS_LIST": "1",
                    "REPEATS": "1",
                    "WARMUPS": "0",
                }
            )
            completed = subprocess.run(
                ["bash", str(script)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("builder worker drift", completed.stderr)
            self.assertEqual((out / "cell_index.csv").read_text().count("ok"), 0)

    def test_rejects_child_that_uses_wrong_rank_workers(self) -> None:
        script = Path(__file__).with_name("run_vldb_build_pipeline.sh")
        with tempfile.TemporaryDirectory() as tmp_s:
            root = Path(tmp_s)
            binary = root / "candidate"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            expected_sha = subprocess.check_output(
                ["shasum", "-a", "256", str(binary)], text=True
            ).split()[0]
            inner = root / "fake_inner.py"
            inner.write_text(
                """#!/usr/bin/env python3
import csv, hashlib, json, os, socket
from pathlib import Path
out = Path(os.environ["OUT_ROOT"])
out.mkdir(parents=True)
sha = hashlib.sha256(Path(os.environ["GB_BIN"]).read_bytes()).hexdigest()
requested = int(os.environ["BUILD_THREADS"])
json.dump({"protocol": {"binary_sha256": sha, "datasets": ["DEEP1M"],
    "policies": ["indeg"], "budget_bytes": [536870912], "repeats": 1,
    "build_threads": requested, "staged_build": True,
    "compute_host": socket.gethostname()}},
    open(out / "campaign.json", "w"))
row = {"binary_sha256": sha, "build_mode": "staged",
       "build_workers": requested, "rank_workers": 20,
       "rank_workers_recorded": 1, "compute_host": socket.gethostname()}
with open(out / "runs.csv", "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(row))
    writer.writeheader(); writer.writerow(row)
"""
            )
            inner.chmod(0o755)
            summarizer = root / "unused_summarizer.py"
            summarizer.write_text("raise SystemExit('must not be reached')\n")
            out = root / "out"
            env = os.environ.copy()
            env.update(
                {
                    "EXPECTED_BINARY_SHA": expected_sha,
                    "GB_BIN": str(binary),
                    "GB_BIN_R": str(binary),
                    "INNER_RUNNER": str(inner),
                    "SUMMARIZER": str(summarizer),
                    "OUT_ROOT": str(out),
                    "BUILD_THREADS_LIST": "1",
                    "REPEATS": "1",
                    "WARMUPS": "0",
                }
            )
            completed = subprocess.run(
                ["bash", str(script)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("rank worker drift", completed.stderr)
            self.assertEqual((out / "cell_index.csv").read_text().count("ok"), 0)


if __name__ == "__main__":
    unittest.main()
