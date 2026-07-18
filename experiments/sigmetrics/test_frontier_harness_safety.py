#!/usr/bin/env python3
from pathlib import Path
import subprocess
import unittest


HERE = Path(__file__).parent


class FrontierHarnessSafetyTest(unittest.TestCase):
    def test_sw_frontier_uses_pid_owned_memory_nodes_and_protocol_rows(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn("verify_remote_pid", text)
        self.assertIn("protocol_fingerprint", text)
        self.assertIn("campaign_id", text)
        self.assertIn("binary_sha256", text)
        self.assertIn("measurement_mode", text)
        self.assertIn("QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}", text)
        self.assertIn('query_contexts', text)
        self.assertIn('--query-contexts "$QUERY_CONTEXTS"', text)
        self.assertIn('obj["query_contexts"] == int(query_contexts)', text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertIn("tcp_port", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)

    def test_sw_frontier_retries_atomic_remote_identity_probes(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        for token in (
            "REMOTE_IDENTITY_RETRIES=${REMOTE_IDENTITY_RETRIES:-3}",
            "probe_remote_process_instance()",
            'for attempt in $(seq 1 "$REMOTE_IDENTITY_RETRIES")',
            'case "$mn_probe" in',
            "same)",
            "exited)",
            "mismatch)",
            '"identity_failure_reason": identity_failure_reason',
        ):
            self.assertIn(token, text)

    def test_local_pid_probe_treats_process_exit_as_observed_state(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        start = text.index("local_pid_starttime() {")
        end = text.index("\n}", start) + 2
        function = text[start:end]
        probe = subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                + function
                + "\nvalue=$(local_pid_starttime 2147483647)\n"
                + "printf 'survived:%s\\n' \"$value\"\n",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout, "survived:\n")

    def test_sw_frontier_records_durable_lifecycle_boundaries_and_signals(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        for token in (
            "FRONTIER_LIFECYCLE_LOG=${FRONTIER_LIFECYCLE_LOG:-}",
            "write_lifecycle_event()",
            "os.fsync(handle.fileno())",
            "os.replace(temporary, latest)",
            "frontier_on_signal()",
            "frontier_on_exit()",
            "trap 'frontier_on_signal HUP 129' HUP",
            "trap 'frontier_on_signal INT 130' INT",
            "trap 'frontier_on_signal TERM 143' TERM",
            'write_lifecycle_event "cn_reaped" "$rc"',
            'write_lifecycle_event "mn_final_probe_complete" "$rc"',
            'write_lifecycle_event "mn_copy_start" "$rc"',
            'write_lifecycle_event "mn_copy_complete" "$rc"',
            'write_lifecycle_event "execution_manifest_written" "$rc"',
            'write_lifecycle_event "csv_committed" "$rc"',
            "import time",
            'int(time.monotonic() * 1_000_000_000)',
        ):
            self.assertIn(token, text)
        self.assertNotIn("os.clock_gettime_ns", text)
        self.assertLess(
            text.index('write_lifecycle_event "mn_copy_start" "$rc"'),
            text.index('> "$mn_out"'),
        )
        self.assertLess(
            text.index('write_lifecycle_event "execution_manifest_written" "$rc"'),
            text.index('write_lifecycle_event "csv_committed" "$rc"'),
        )

    def test_repeated_campaign_separates_lifecycle_logs_by_child(self):
        text = (HERE / "run_frontier_repeated.sh").read_text()
        for token in (
            "FRONTIER_LIFECYCLE_ROOT=${FRONTIER_LIFECYCLE_ROOT:-}",
            'mkdir -p "$FRONTIER_LIFECYCLE_ROOT"',
            'lifecycle_log="$FRONTIER_LIFECYCLE_ROOT/sw_${run_id}.jsonl"',
            'FRONTIER_LIFECYCLE_LOG="$lifecycle_log"',
        ):
            self.assertIn(token, text)

    def test_sw_frontier_freezes_inputs_and_seals_semantic_raw_evidence(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        for token in (
            "EXPECTED_BINARY_SHA",
            "VLDB_FRONTIER_HARNESS_FROZEN",
            "verify-harness",
            "HARNESS_MANIFEST_SHA256",
            "verify_vldb_frontier_sweep.py",
            "input_manifest.tsv",
            "input_signature",
            "server.sha256",
            "server.starttime",
            "ACTIVE_CN_STARTTIME",
            "sha256sum /proc/",
            "SEALED.json",
            "SHA256SUMS",
            "seal --root",
            "verify --root",
        ):
            self.assertIn(token, text)
        self.assertIn('verify_input_manifest "$dataset" "pre_run"', text)
        self.assertIn('verify_input_manifest "$dataset" "post_run"', text)
        self.assertIn('ALLOW_MISSING_DATASETS=${ALLOW_MISSING_DATASETS:-0}', text)
        self.assertIn("Refusing missing formal frontier dataset", text)
        self.assertNotIn('echo "SKIP $dataset:', text)

    def test_sw_frontier_fingerprints_the_dataset_native_query_encoding(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn("query_extension_for_dataset()", text)
        mapping = text.split("query_extension_for_dataset()", 1)[1].split("}", 1)[0]
        self.assertIn('BIGANN1M) printf \'u8bin\\n\'', mapping)
        self.assertIn('SPACEV1M) printf \'i8bin\\n\'', mapping)
        self.assertIn("*) printf 'fbin\\n'", mapping)
        self.assertIn('query_extension=$(query_extension_for_dataset "$dataset")', text)
        self.assertIn(
            'local query_file="${data%/}/queries/query-${query_suffix}.${query_extension}"',
            text,
        )

    def test_frontier_input_stager_is_sha_checked_and_atomic(self):
        text = (HERE / "stage_frontier_dataset_inputs.sh").read_text()
        for token in (
            "SOURCE_HOST=${SOURCE_HOST:-skv-node1}",
            "BIGANN1M|bigann1m|base.u8bin|query-uniform.u8bin",
            "SPACEV1M|spacev1m|base.i8bin|query-uniform.i8bin",
            "TURING1M|turing1m|base.fbin|query-uniform.fbin",
            "TEXT1M|tti1m|base.fbin|query-uniform.fbin",
            'source_sha=$(ssh -n -o BatchMode=yes "$SOURCE_HOST"',
            'test "$source_sha" = "$staged_sha"',
            'mv "$CURRENT_TMP" "$dst"',
        ):
            self.assertIn(token, text)
        self.assertNotIn("rm -rf", text)

    def test_sw_frontier_preflights_ordinary_page_capacity(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn(
            "MN_MEMORY_HEADROOM_BYTES=${MN_MEMORY_HEADROOM_BYTES:-2147483648}",
            text,
        )
        self.assertIn("preflight_mn_memory()", text)
        self.assertIn("/^MemAvailable:/", text)
        self.assertIn(
            'required_bytes=$((index_region_bytes + lavd_region_bytes + '
            'MN_MEMORY_HEADROOM_BYTES))',
            text,
        )
        call = 'preflight_mn_memory "$mn" "$index_region_bytes" "$region_bytes"'
        start = 'start_mn "$mn" "$remote_mn_dir" "$index_region_bytes"'
        self.assertIn(call, text)
        self.assertLess(text.index(call), text.index(start, text.index(call)))

    def test_sw_frontier_position_balances_method_order(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        repeated = (HERE / "run_frontier_repeated.sh").read_text()
        self.assertIn("METHOD_ORDER_OFFSET=${METHOD_ORDER_OFFSET:-0}", text)
        self.assertIn('"method_order_offset": int(method_order_offset)', text)
        self.assertIn('if (( (point_index + METHOD_ORDER_OFFSET) % 2 == 0 )); then', text)
        self.assertIn('METHOD_ORDER_OFFSET="$method_order_offset"', repeated)
        self.assertIn('method_order_offset=$((rep % 2))', repeated)

    def test_formal_frontier_requires_at_least_five_points(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn("MIN_POINTS=${MIN_POINTS:-5}", text)
        self.assertIn("Refusing fewer than five formal frontier points", text)
        self.assertIn('--min-points "$MIN_POINTS"', text)

    def test_frontier_code_policy_matches_paper_contract(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        gist = text.split("GIST1M)", 1)[1].split(";;", 1)[0]
        deep10 = text.split("DEEP10M)", 1)[1].split(";;", 1)[0]
        sift10 = text.split("SIFT10M)", 1)[1].split(";;", 1)[0]
        self.assertIn("SHINE_LAVD_RABITQ_B=2", gist)
        self.assertNotIn("SHINE_LAVD_RABITQ_B", deep10)
        self.assertNotIn("SHINE_LAVD_RABITQ_B", sift10)
        self.assertIn(
            "LAVD_10M_REGION_BYTES=${LAVD_10M_REGION_BYTES:-42949672960}",
            text,
        )

    def test_every_1m_slab_frontier_uses_an_explicit_sufficient_region(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn(
            'LAVD_PARALLEL_BUILD_ENV="SHINE_LAVD_BUILD_THREADS=20 '
            'SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2 '
            'SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1"',
            text,
        )
        dataset_dispatch = text.split(
            "# Datasets that already have memory-node index dumps", 1
        )[1]
        capacities = {
            "SIFT1M": ("LAVD_SIFT1_REGION_BYTES", 5 * 1024**3, 4_616_016_384),
            "GIST1M": ("LAVD_GIST1_REGION_BYTES", 9 * 1024**3, 8_456_016_384),
            "DEEP1M": ("LAVD_DEEP1_REGION_BYTES", 4 * 1024**3, 3_592_016_384),
            "BIGANN1M": ("LAVD_BIGANN1_REGION_BYTES", 5 * 1024**3, 4_616_016_384),
            "SPACEV1M": ("LAVD_SPACEV1_REGION_BYTES", 4 * 1024**3, 3_720_016_384),
            "TURING1M": ("LAVD_TURING1_REGION_BYTES", 4 * 1024**3, 3_720_016_384),
            "TEXT1M": ("LAVD_TEXT1_REGION_BYTES", 8 * 1024**3, 6_920_016_384),
        }
        for dataset, (variable, capacity, materialized) in capacities.items():
            self.assertGreater(capacity, materialized)
            self.assertIn(f"{variable}=${{{variable}:-{capacity}}}", text)
            case = dataset_dispatch.split(f"    {dataset})", 1)[1].split(";;", 1)[0]
            self.assertIn(f'"${variable}"', case)
            self.assertIn("$LAVD_PARALLEL_BUILD_ENV", case)
            self.assertNotRegex(case, r'\$[A-Z0-9_]+_EFS" 0\s*$')

    def test_tti_frontier_materializes_the_same_auditable_10k_pool(self):
        text = (HERE / "run_frontier_sweeps.sh").read_text()
        self.assertIn("prepare_fixed_query_pool.py", text)
        self.assertIn('--limit 10000', text)
        self.assertIn('--query-fbin "$query_out"', text)
        self.assertIn('--groundtruth-bin "$groundtruth_out"', text)
        self.assertIn('query-u10k.fbin', text)
        self.assertIn('groundtruth-u10k.bin', text)
        text10m = text.split("TEXT10M)", 1)[1].split(";;", 1)[0]
        self.assertIn('"u10k"', text10m)

    def test_resource_ledger_uses_pid_owned_memory_nodes(self):
        text = (HERE / "run_vldb_resource_ledger.sh").read_text()
        self.assertIn("server.pid", text)
        self.assertIn("verify_remote_pid", text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertIn("index_region_bytes", text)
        self.assertIn("tcp_port", text)
        self.assertIn("RESUME", text)
        self.assertIn("campaign.json", text)
        self.assertIn("protocol_fingerprint", text)
        self.assertIn("SKIP complete", text)
        self.assertIn("QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}", text)
        self.assertIn('"query_contexts": int(query_contexts)', text)
        self.assertIn('--query-contexts "$QUERY_CONTEXTS"', text)
        self.assertIn('data["query_contexts"] == expected_contexts', text)
        self.assertIn("Refusing incomplete resource-ledger cell", text)
        self.assertNotIn('  rm -rf "$cell"', text)
        self.assertRegex(
            text,
            r"warmups,\s+threads,\s+coroutines,\s+query_contexts,\s+build_threads,",
        )
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)

    def test_repeated_campaign_refuses_implicit_stale_resume(self):
        text = (HERE / "run_frontier_repeated.sh").read_text()
        self.assertIn("RESUME", text)
        self.assertIn("campaign.json", text)
        self.assertIn("CAMPAIGN_ID", text)
        self.assertIn("SW_PORT", text)
        self.assertIn("DHNSW_PORT", text)
        self.assertIn("DHNSW_RDMA_PORT", text)
        self.assertIn('BUILD_DHNSW="$build"', text)
        self.assertIn('PREPARE_DATASETS="$prepare"', text)
        self.assertIn('--ef-list "$EF_LIST"', text)
        self.assertIn("QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}", text)
        self.assertIn('"query_contexts": int(sys.argv[10])', text)
        self.assertIn('QUERY_CONTEXTS="$QUERY_CONTEXTS"', text)
        self.assertIn('--expected-query-contexts "$QUERY_CONTEXTS"', text)
        self.assertIn('EXPECTED_DATASETS=${EXPECTED_DATASETS:-${DATASETS_SW// /,}}', text)
        self.assertIn('--expected-datasets "$EXPECTED_DATASETS"', text)
        self.assertIn("sw_run_complete", text)
        self.assertIn("dhnsw_run_complete", text)
        self.assertIn("SKIP complete SW", text)
        self.assertIn("SKIP complete d-HNSW", text)
        self.assertIn("Refusing incomplete SW run directory", text)
        self.assertIn("Refusing incomplete d-HNSW run directory", text)
        self.assertNotIn('  rm -rf "$out"', text)
        self.assertNotIn('local run_id=$1 run_kind=$2 out=', text)
        self.assertNotIn("Repeated 10M frontier", text)

    def test_repeated_campaign_accepts_only_verified_sealed_children(self):
        text = (HERE / "run_frontier_repeated.sh").read_text()
        for token in (
            "EXPECTED_BINARY_SHA",
            "vldb_evidence_bundle.py",
            "verify_vldb_frontier_sweep.py",
            "SEALED.json",
            "SHA256SUMS",
            "verify --root",
            "seal --root",
        ):
            self.assertIn(token, text)
        self.assertIn('EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA"', text)
        self.assertIn("child frontier seal verification failed", text)

    def test_repeated_campaign_emits_an_atomic_sw_completion_marker(self):
        text = (HERE / "run_frontier_repeated.sh").read_text()
        self.assertIn("write_sw_completion_marker()", text)
        self.assertIn("SW_FRONTIER_COMPLETE.json", text)
        self.assertIn('"kind": "vldb_sw_frontier_complete_v1"', text)
        self.assertIn('"campaign_id": campaign_id', text)
        self.assertIn('"binary_sha256": binary_sha', text)
        self.assertIn("os.replace(temporary, marker)", text)
        sw_phase = text.split("if contains_phase sw; then", 1)[1].split(
            "if contains_phase dhnsw; then", 1
        )[0]
        self.assertLess(
            sw_phase.index('sw_run_complete "$OUT_ROOT/sw_r$rep"'),
            sw_phase.index("write_sw_completion_marker"),
        )

    def test_dhnsw_repeated_campaign_fingerprints_and_parses_every_run(self):
        text = (HERE / "run_dhnsw_repeated.sh").read_text()
        self.assertIn("campaign.json", text)
        self.assertIn("client_binary_sha256", text)
        self.assertIn("server_binary_sha256", text)
        self.assertIn("parse_dhnsw_frontier.py", text)
        self.assertIn("run_kind", text)
        self.assertIn("RESUME", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)

    def test_index_builder_uses_pid_owned_memory_node(self):
        text = (HERE / "build_frontier_indexes.sh").read_text()
        self.assertIn("verify_remote_pid", text)
        self.assertIn("campaign.json", text)
        self.assertIn("binary_sha256", text)
        self.assertIn("index_region_bytes_by_scale", text)
        self.assertIn("TIMEOUT_S=${TIMEOUT_S:-43200}", text)
        self.assertIn('"timeout_seconds": int(timeout_s)', text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)

    def test_build_cost_harness_uses_owned_process_and_explicit_port(self):
        text = (HERE / "run_slab_build_cost.sh").read_text()
        self.assertIn("verify_remote_pid", text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)
        self.assertNotIn("tmux", text)

    def test_robustness_harness_is_pid_owned_and_records_tail_latency(self):
        text = (HERE / "run_vldb_robustness.sh").read_text()
        self.assertIn("verify_remote_pid", text)
        self.assertIn('GB_QUERY_LATENCY="$latency_enabled"', text)
        self.assertIn("latency_instrumentation,on", text)
        self.assertIn("protocol_fingerprint", text)
        self.assertIn("campaign_id", text)
        self.assertIn(
            "read -r factor value threads query_contexts coroutines top_k ef query_suffix latency_enabled <&3",
            text,
        )
        self.assertIn(
            "factor,value,threads,query_contexts,coroutines,top_k,ef,query_suffix,latency_enabled",
            text,
        )
        self.assertIn('--query-contexts "$query_contexts"', text)
        self.assertIn('obj["query_contexts"] == int(sys.argv[3])', text)
        self.assertIn('"query_contexts": int(query_contexts)', text)
        self.assertIn('done 3< "$MATRIX"', text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertIn("index_region_bytes", text)
        self.assertIn("tcp_port", text)
        for coroutines in (1, 2, 4, 8, 16):
            self.assertIn(f"coroutines,{coroutines},", text)
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)

    def test_query_profile_attaches_only_after_query_phase_marker(self):
        text = (HERE / "run_vldb_query_profile.sh").read_text()
        self.assertIn("verify_remote_pid", text)
        self.assertIn("**QUERY**: running worker threads", text)
        self.assertIn("PERF_CMD", text)
        self.assertIn("PERF_REPORT_CMD", text)
        self.assertIn("PERF_DATA_FIXUP_CMD", text)
        self.assertIn("CAPTURE_PERF", text)
        self.assertIn("COMPUTE_RECALL", text)
        self.assertIn("QUERY_CONTEXTS", text)
        self.assertIn('"capture_perf"', text)
        self.assertIn('"compute_recall"', text)
        self.assertIn('"query_contexts_requested"', text)
        self.assertIn('"${context_args[@]}"', text)
        self.assertIn('"${perf_cmd[@]}" record', text)
        self.assertIn('"${perf_report_cmd[@]}" report', text)
        self.assertIn('"${perf_data_fixup_cmd[@]}"', text)
        self.assertIn("perf.record.status", text)
        self.assertIn("report_rc", text)
        for field in (
            '"datasets"', '"methods"', '"threads"', '"coroutines"',
            '"ef"', '"top_k"', '"binary_sha256"', '"tcp_port"',
            '"index_region_bytes_by_scale"',
            '"lavd_region_bytes_by_dataset"',
            '"perf_report_command"', '"perf_data_fixup_command"',
        ):
            self.assertIn(field, text)
        self.assertIn("--no-recall", text)
        self.assertGreaterEqual(text.count("--port"), 2)
        self.assertGreaterEqual(text.count("--index-region-bytes"), 2)
        self.assertIn(
            "LAVD_DEEP10_REGION_BYTES=${LAVD_DEEP10_REGION_BYTES:-42949672960}",
            text,
        )
        self.assertNotIn("pkill", text)
        self.assertNotIn("pgrep -x", text)


if __name__ == "__main__":
    unittest.main()
