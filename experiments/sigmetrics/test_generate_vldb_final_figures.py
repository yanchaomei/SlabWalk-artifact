import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from matplotlib import rc_context
from matplotlib.backends.backend_pdf import FigureCanvasPdf
from matplotlib.figure import Figure

import aggregate_frontier_repeats as aggregate
import assemble_vldb_query_profile as query_profile_assembler
import assemble_vldb_lifecycle_controls as lifecycle_assembler
import publish_vldb_release as release
import summarize_vldb_resource_ledger as resource_summary
from test_assemble_vldb_query_profile import QueryProfileAssemblerTest
from test_assemble_vldb_lifecycle_controls import write_lifecycle_sources
from test_plot_vldb_resource_ledger import measured_rows as resource_rows
from test_plot_vldb_resource_ledger import write_csv as write_resource_csv
from test_plot_vldb_robustness import measured_rows as robustness_rows
from test_plot_vldb_robustness import write_csv as write_robustness_csv
from test_validate_vldb_build_cost import write_build_cost_evidence
from test_validate_vldb_index_construction import write_index_construction_evidence
from test_validate_vldb_final_evidence import (
    FINAL_SHA,
    bind_existing_frontier_campaign,
    linked_frontier_rows,
    model_control_rows,
    retain_frontier_sources,
    worker_scaling_rows,
    write_worker_campaign_provenance,
    write_cache_control_evidence,
    write_colocation_control_evidence,
    write_10m_build_scaling_evidence,
    write_mechanism_control_evidence,
    write_topology_evidence,
    write_query_pool_evidence,
    write_csv,
)
from test_validate_vldb_frontier_1m import write_bundle as write_frontier_1m_bundle


def write_valid_landscape_pdf(path: Path, label: str) -> None:
    with rc_context({"pdf.fonttype": 42}):
        figure = Figure(figsize=(8.0, 3.0))
        FigureCanvasPdf(figure)
        axis = figure.subplots()
        axis.plot([0, 1, 2], [0, 1, 0], color="#3b6fb6")
        axis.set_title(label)
        figure.savefig(path, format="pdf", bbox_inches="tight")


class FinalFigurePipelineTest(unittest.TestCase):
    def test_release_defaults_to_verified_auto_renderer(self) -> None:
        script = Path(__file__).with_name("generate_vldb_final_figures.sh")
        self.assertIn(
            "export SLABWALK_SVG_RENDERER=${SLABWALK_SVG_RENDERER:-auto}",
            script.read_text(),
        )

    def test_release_reuses_pdf_only_for_byte_identical_svg(self) -> None:
        script = Path(__file__).with_name("generate_vldb_final_figures.sh").read_text()
        self.assertIn('cmp -s "$STAGING/fig_physical_units.svg"', script)
        self.assertIn('"$OUT_DIR/fig_physical_units.svg"', script)
        self.assertIn('cp -- "$OUT_DIR/fig_physical_units.pdf"', script)

    def test_external_release_marker_is_rejected_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external:
            root = Path(tmp)
            evidence = root / "evidence"
            evidence.mkdir()
            marker = Path(external) / "release_bundle.json"
            marker.write_text("EXTERNAL MARKER MUST SURVIVE\n")
            script = Path(__file__).with_name("generate_vldb_final_figures.sh")
            env = dict(os.environ)
            env.update({
                "PUBLICATION_ROOT": str(root),
                "PAPER_DIR": str(root / "paper_vldb"),
                "EVIDENCE_ROOT": str(evidence),
                "OUT_DIR": str(root / "paper_vldb" / "figs"),
                "RELEASE_MANIFEST": str(marker),
            })

            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text(), "EXTERNAL MARKER MUST SURVIVE\n")
            self.assertIn("outside publication root", result.stderr)

    def test_startup_mkdir_failure_invalidates_a_stale_release_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            evidence.mkdir()
            marker = evidence / "release_bundle.json"
            marker.write_text("STALE RELEASE MARKER\n")
            blocked_parent = root / "blocked-parent"
            blocked_parent.write_text("not a directory\n")
            script = Path(__file__).with_name("generate_vldb_final_figures.sh")
            env = dict(os.environ)
            env.update({
                "PUBLICATION_ROOT": str(root),
                "PAPER_DIR": str(root / "paper_vldb"),
                "EVIDENCE_ROOT": str(evidence),
                "OUT_DIR": str(blocked_parent / "figs"),
                "RELEASE_MANIFEST": str(marker),
            })

            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_gate_generates_and_atomically_publishes_complete_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            write_query_pool_evidence(evidence / "query_pools")
            write_frontier_1m_bundle(evidence)
            linked_frontier = linked_frontier_rows(evidence / "query_pools")
            for row in linked_frontier:
                if row["method"] == "SlabWalk":
                    row["qps"] = float(row["qps"]) * 1.5
                    row["posts_per_query"] = 20
            frontier = retain_frontier_sources(evidence / "frontier", linked_frontier)
            write_csv(evidence / "frontier" / "frontier_repeated_raw.csv", frontier)
            write_csv(
                evidence / "frontier" / "frontier_summary.csv",
                aggregate.summarize(frontier, expected_repeats=5),
            )
            bind_existing_frontier_campaign(evidence / "frontier")
            write_robustness_csv(
                evidence / "robustness" / "runs.csv", robustness_rows()
            )
            worker_scaling = evidence / "worker_scaling"
            write_csv(
                worker_scaling / "runs.csv", worker_scaling_rows(worker_scaling)
            )
            write_worker_campaign_provenance(worker_scaling)
            resources = resource_rows()
            write_resource_csv(evidence / "resource_ledger" / "runs.csv", resources)
            write_resource_csv(
                evidence / "resource_ledger" / "summary.csv",
                resource_summary.summarize(resources),
            )
            write_csv(
                evidence / "model_controls" / "rdma_tau_runs.csv",
                model_control_rows(),
            )
            write_topology_evidence(evidence / "topology_control")
            write_build_cost_evidence(evidence / "build_cost")
            write_10m_build_scaling_evidence(
                evidence / "build_scaling_10m", root / "build_scaling_10m_sources"
            )
            write_index_construction_evidence(evidence / "index_construction")
            refresh_summary, refresh_root, tti_summary, tti_root = (
                write_lifecycle_sources(root / "lifecycle_sources")
            )
            lifecycle_assembler.assemble(
                refresh_summary,
                refresh_root,
                tti_summary,
                tti_root,
                evidence / "lifecycle_controls",
            )
            write_cache_control_evidence(evidence / "cache_control")
            write_colocation_control_evidence(evidence / "colocation_control")
            write_mechanism_control_evidence(evidence / "mechanism_controls")
            advisor_fixture = (
                Path(__file__).resolve().parents[2]
                / "results"
                / "vldb_final_evidence"
                / "physical_design_advisor"
            )
            shutil.copytree(
                advisor_fixture,
                evidence / "physical_design_advisor",
            )
            colocation_campaign = json.loads(
                (evidence / "colocation_control" / "campaign.json").read_text()
            )
            mechanism_campaign = json.loads(
                (evidence / "mechanism_controls" / "campaign.json").read_text()
            )
            (root / "profile_source").mkdir()
            profile_source, profile_runner_sha = QueryProfileAssemblerTest().make_campaign(
                root / "profile_source", binary_sha=FINAL_SHA
            )
            query_profile_assembler.assemble(
                profile_source,
                evidence / "query_profile",
                expected_binary_sha=FINAL_SHA,
                expected_runner_sha=profile_runner_sha,
            )
            paper = root / "paper_vldb"
            out = paper / "figs"
            out.mkdir(parents=True)
            existing_figures = (
                "fig_construction_refresh.pdf",
                "fig_search_placement.pdf",
                "fig_slab_layout.pdf",
                "overview.pdf",
            )
            generated_figures = (
                "fig_physical_units.pdf",
                "eval_frontier_curves.pdf",
                "eval_access_scaling.pdf",
                "eval_index_cost.pdf",
                "eval_lifecycle_boundaries.pdf",
            )
            for name in existing_figures:
                write_valid_landscape_pdf(out / name, name)
            for name in (
                "ACM-Reference-Format.bst",
                "acmart.cls",
                "pvldb.sty",
                "refs.bib",
            ):
                (paper / name).write_text(f"fixture {name}\n")
            includes = "\n".join(
                rf"\includegraphics{{figs/{name}}}"
                for name in (*existing_figures, *generated_figures)
            )
            (paper / "main.tex").write_text(
                "\\input{generated_claims.tex}\n" + includes + "\n"
            )
            release_manifest = evidence / "release_bundle.json"
            env = dict(os.environ)
            env.update({
                "EVIDENCE_ROOT": str(evidence),
                "PUBLICATION_ROOT": str(root),
                "PAPER_DIR": str(paper),
                "OUT_DIR": str(out),
                "RELEASE_MANIFEST": str(release_manifest),
                "FINAL_SHA": FINAL_SHA,
                "PROFILE_RUNNER_SHA": profile_runner_sha,
                "EXPECTED_COLOCATION_CAMPAIGN_ID": colocation_campaign["campaign_id"],
                "EXPECTED_COLOCATION_PROTOCOL_FINGERPRINT": colocation_campaign[
                    "protocol_fingerprint"
                ],
                "EXPECTED_MECHANISM_CAMPAIGN_ID": mechanism_campaign["campaign_id"],
                "EXPECTED_MECHANISM_PROTOCOL_FINGERPRINT": mechanism_campaign[
                    "protocol_fingerprint"
                ],
            })
            script = Path(__file__).with_name("generate_vldb_final_figures.sh")
            result = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            gate_path = evidence / "evidence_gate.json"
            self.assertTrue(json.loads(gate_path.read_text())["ready_for_plotting"])
            self.assertEqual(
                json.loads(gate_path.read_text())["frontier_1m"]["query_pool_cells"],
                21,
            )
            self.assertEqual(
                json.loads(gate_path.read_text())["claim_input_sha256"][
                    "frontier_1m_summary"
                ],
                json.loads((evidence / "frontier_1m_gate.json").read_text())[
                    "summary_sha256"
                ],
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["build_cost"]["measured_rows"],
                15,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["build_scaling_10m"]["runs"],
                15,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["index_construction"]["measured_cells"],
                2,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["lifecycle_controls"]["retained_sources_verified"],
                12,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["cache_control"]["measured_rows"],
                20,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["colocation_control"]["measured_rows"],
                30,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["mechanism_controls"]["measured_rows"],
                60,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())["query_profile"]["query_rows"],
                200000,
            )
            self.assertEqual(
                json.loads((evidence / "evidence_gate.json").read_text())[
                    "physical_design_advisor"
                ]["selection_cells"],
                9,
            )
            headline = json.loads((evidence / "headline_candidates.json").read_text())
            self.assertEqual(headline["kind"], "vldb_headline_candidates")
            self.assertEqual(set(headline["datasets"]), {"DEEP10M", "SIFT10M", "TTI10M"})
            claims = json.loads((evidence / "manuscript_claims.json").read_text())
            self.assertEqual(claims["kind"], "vldb_manuscript_claims")
            self.assertEqual(claims["gate_sha256"], release.sha256(gate_path))
            self.assertEqual(claims["frontier"]["recall_floor"], 0.90)
            self.assertEqual(claims["frontier"]["recall_tolerance"], 0.002)
            self.assertTrue(claims["frontier"]["matched_datasets"])
            self.assertEqual(
                set(claims["frontier"]["high_recall_matched_pairs"]),
                set(claims["frontier"]["matched_datasets"]),
            )
            self.assertEqual(set(claims["frontier"]["dhnsw_max_recall"]), {
                "DEEP10M", "SIFT10M", "TTI10M"
            })
            self.assertEqual(
                set(claims["build_scaling_10m"]),
                {"DEEP10M", "SIFT10M", "TTI10M"},
            )
            self.assertAlmostEqual(
                claims["physical_design_advisor"]["heldout_ratio_min"],
                0.9908309455587393,
            )
            for name in (
                "fig_physical_units.pdf",
                "eval_frontier_curves.pdf",
                "eval_access_scaling.pdf",
                "eval_index_cost.pdf",
                "eval_lifecycle_boundaries.pdf",
            ):
                self.assertGreater((out / name).stat().st_size, 10000)
            self.assertGreater((out / "fig_physical_units.svg").stat().st_size, 10000)
            generated_claims = paper / "generated_claims.tex"
            self.assertIn(
                f"% gate-sha256: {release.sha256(gate_path)}",
                generated_claims.read_text(),
            )
            self.assertIn(
                r"\newcommand{\ClaimAdvisorGeoPercent}{99.87}",
                generated_claims.read_text(),
            )
            verification = release.verify_release(root, release_manifest)
            self.assertEqual(verification["entries_verified"], 20)
            manifest = json.loads(release_manifest.read_text())
            expected_paper_targets = {
                "paper_vldb/ACM-Reference-Format.bst",
                "paper_vldb/acmart.cls",
                "paper_vldb/generated_claims.tex",
                "paper_vldb/main.tex",
                "paper_vldb/pvldb.sty",
                "paper_vldb/refs.bib",
                *(f"paper_vldb/figs/{name}" for name in existing_figures),
                *(f"paper_vldb/figs/{name}" for name in generated_figures),
                "paper_vldb/figs/fig_physical_units.svg",
            }
            expected_targets = expected_paper_targets | {
                "evidence/evidence_gate.json",
                "evidence/frontier_1m_gate.json",
                "evidence/headline_candidates.json",
                "evidence/manuscript_claims.json",
            }
            self.assertEqual(set(manifest["entries"]), expected_targets)
            self.assertEqual(
                set(manifest["publication_pdf_targets"]),
                {
                    *(f"paper_vldb/figs/{name}" for name in existing_figures),
                    *(f"paper_vldb/figs/{name}" for name in generated_figures),
                },
            )

            reproducible_targets = (
                evidence / "evidence_gate.json",
                evidence / "frontier_1m_gate.json",
                evidence / "headline_candidates.json",
                evidence / "manuscript_claims.json",
                evidence / "release_bundle.json",
                paper / "generated_claims.tex",
                *(out / name for name in generated_figures),
            )
            first_hashes = {
                str(path.relative_to(root)): release.sha256(path)
                for path in reproducible_targets
            }
            repeated = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            second_hashes = {
                str(path.relative_to(root)): release.sha256(path)
                for path in reproducible_targets
            }
            self.assertEqual(first_hashes, second_hashes)

            wrapper_dir = root / "signal-wrapper"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "python3"
            publisher_done = root / "publisher.done"
            wrapper_pid = root / "publisher-wrapper.pid"
            wrapper.write_text(
                f"""#!{sys.executable}
import os
import signal
import subprocess
import sys
from pathlib import Path

real_python = {sys.executable!r}
result = subprocess.run([real_python, *sys.argv[1:]])
is_publisher = any(Path(arg).name == "publish_vldb_release.py" for arg in sys.argv[1:])
if is_publisher and result.returncode == 0:
    Path(os.environ["VLDB_PUBLISHER_WRAPPER_PID"]).write_text(str(os.getpid()))
    Path(os.environ["VLDB_PUBLISHER_DONE"]).write_text("done\\n")
    signal.pause()
raise SystemExit(result.returncode)
"""
            )
            wrapper.chmod(0o755)
            signal_env = dict(env)
            signal_env["PATH"] = f"{wrapper_dir}:{signal_env['PATH']}"
            signal_env["VLDB_PUBLISHER_DONE"] = str(publisher_done)
            signal_env["VLDB_PUBLISHER_WRAPPER_PID"] = str(wrapper_pid)
            for signum in (signal.SIGTERM, signal.SIGINT):
                publisher_done.unlink(missing_ok=True)
                wrapper_pid.unlink(missing_ok=True)
                process = subprocess.Popen(
                    ["bash", str(script)],
                    env=signal_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 60
                    while not publisher_done.exists() and process.poll() is None:
                        if time.monotonic() >= deadline:
                            self.fail("publisher signal probe timed out")
                        time.sleep(0.05)
                    if process.poll() is not None:
                        stdout, stderr = process.communicate()
                        self.fail(
                            f"pipeline exited before signal probe: {stdout}\n{stderr}"
                        )
                    process.send_signal(signum)
                    os.kill(int(wrapper_pid.read_text()), signum)
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertNotEqual(process.returncode, 0, stdout + stderr)
                    self.assertFalse(release_manifest.exists(), stdout + stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

            model_runs = evidence / "model_controls" / "rdma_tau_runs.csv"
            model_bytes = model_runs.read_bytes()
            release_manifest.write_text('STALE RELEASE MARKER\n')
            model_runs.write_text("invalid,early,gate,input\n")
            try:
                early_failure = subprocess.run(
                    ["bash", str(script)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                model_runs.write_bytes(model_bytes)
            self.assertNotEqual(
                early_failure.returncode,
                0,
                early_failure.stdout + early_failure.stderr,
            )
            self.assertFalse(
                release_manifest.exists(),
                early_failure.stdout + early_failure.stderr,
            )

            (out / existing_figures[0]).write_bytes(b"not-a-pdf\n")
            corrupt_result = subprocess.run(
                ["bash", str(script)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(
                corrupt_result.returncode,
                0,
                corrupt_result.stdout + corrupt_result.stderr,
            )
            self.assertFalse(
                release_manifest.exists(),
                corrupt_result.stdout + corrupt_result.stderr,
            )


if __name__ == "__main__":
    unittest.main()
