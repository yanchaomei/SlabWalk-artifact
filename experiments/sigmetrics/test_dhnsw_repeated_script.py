#!/usr/bin/env python3
import os
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


HERE = Path(__file__).parent
SCRIPT = HERE / "run_dhnsw_repeated.sh"


class DhnswRepeatedScriptTest(unittest.TestCase):
    def test_runtime_bundle_is_part_of_campaign_provenance(self):
        text = SCRIPT.read_text()
        self.assertIn("RUNTIME_MANIFEST_SHA", text)
        self.assertIn('"runtime_library_path": runtime_path', text)
        self.assertIn('"runtime_manifest_sha256": runtime_sha', text)

    def test_fake_warmup_and_measure_campaign_reaches_summary(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            droot = tmp / "dh"
            build = droot / "build"
            build.mkdir(parents=True)
            for name in ("run_client", "run_server"):
                path = build / name
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)
            runner = tmp / "runner.sh"
            runner.write_text(
                "#!/bin/sh\n"
                "mkdir -p \"$OUT\"\n"
                "printf '%s,%s\\n' \"$BUILD_DHNSW\" \"$PREPARE_DATASETS\" > \"$OUT/runner_env\"\n"
                "exit 0\n"
            )
            runner.chmod(0o755)
            parser = tmp / "parser.py"
            parser.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import csv, sys
                    assert sys.argv[sys.argv.index("--ef-list") + 1] == "48 64"
                    out = sys.argv[sys.argv.index("--out") + 1]
                    campaign_id = sys.argv[sys.argv.index("--campaign-id") + 1]
                    binary_sha256 = sys.argv[sys.argv.index("--binary-sha256") + 1]
                    base = {
                        "dataset":"deep1M", "ef":"48", "campaign_id":campaign_id,
                        "protocol_fingerprint":"p", "binary_sha256":binary_sha256,
                        "threads":"1", "duration_s":"20",
                        "measurement_mode":"fixed_query_pool",
                        "processed_queries":"10000", "expected_queries":"10000",
                        "failed_queries":"0", "wall_seconds":"1", "top_k":"10",
                        "metric":"l2", "query_rows":"10000",
                        "ground_truth_rows":"10000",
                        "query_rows_per_ground_truth":"1", "qps_recomputed":"10000",
                        "recall":"0.9", "latency_us":"100", "network_us":"20",
                        "compute_us":"60", "meta_us":"10", "deserialize_us":"30",
                        "raw_qps_buggy":"9999", "server_rss_before_gb":"2",
                        "server_rss_after_gb":"2.1", "status":"ok",
                    }
                    rows = []
                    for ef in sys.argv[sys.argv.index("--ef-list") + 1].split():
                        row = dict(base); row["ef"] = ef; rows.append(row)
                    with open(out, "w", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                        writer.writeheader(); writer.writerows(rows)
                    """
                ).lstrip()
            )
            parser.chmod(0o755)
            out = tmp / "campaign"
            env = os.environ | {
                "DROOT": str(droot),
                "OUT_ROOT": str(out),
                "RUNNER": str(runner),
                "PARSER": str(parser),
                "DATASETS": "deep1M",
                "EF_LIST": "48 64",
                "THREADS": "1",
                "REPEATS": "1",
                "BUILD_DHNSW": "1",
                "PREPARE_DATASETS": "1",
            }
            subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True)
            self.assertTrue((out / "campaign.json").is_file())
            protocol = json.loads((out / "campaign.json").read_text())["protocol"]
            self.assertEqual(len(protocol["runner_sha256"]), 64)
            self.assertEqual(len(protocol["parser_sha256"]), 64)
            self.assertEqual(protocol["runner_path"], str(runner))
            self.assertEqual(protocol["parser_path"], str(parser))
            self.assertTrue((out / "warmup" / "frontier.csv").is_file())
            self.assertTrue((out / "r0" / "frontier.csv").is_file())
            self.assertTrue((out / "summary" / "summary.csv").is_file())
            self.assertEqual((out / "warmup" / "runner_env").read_text().strip(), "1,1")
            self.assertEqual((out / "r0" / "runner_env").read_text().strip(), "0,0")

            resume_env = env | {"RESUME": "1"}
            resumed = subprocess.run(
                ["bash", str(SCRIPT)],
                env=resume_env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("SKIP complete r0", resumed.stdout)

            frontier = out / "r0" / "frontier.csv"
            lines = frontier.read_text().splitlines()
            frontier.write_text("\n".join(lines[:2]) + "\n")
            partial = subprocess.run(
                ["bash", str(SCRIPT)],
                env=resume_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(partial.returncode, 0)
            self.assertIn("Refusing incomplete run directory", partial.stderr)


if __name__ == "__main__":
    unittest.main()
