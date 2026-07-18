#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("run_dhnsw_frontier.sh")


class DhnswFrontierScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_query_ground_truth_mapping_is_shape_driven(self):
        self.assertIn("query_rows == ground_truth_rows", self.text)
        self.assertIn("query_rows == 2 * ground_truth_rows", self.text)
        self.assertIn("QUERY_GT_SHAPE", self.text)
        emitted_sampling = self.text.split("new_sampling = '''", 1)[1].split("'''", 1)[0]
        self.assertNotIn('query_data_path.find("text10M")', emitted_sampling)
        self.assertIn("gt_i /= query_rows_per_ground_truth", emitted_sampling)

    def test_server_cleanup_is_pid_owned(self):
        self.assertIn("verify_owned_server_pid", self.text)
        self.assertIn("DHNSW_LD_LIBRARY_PATH", self.text)
        self.assertGreaterEqual(self.text.count('LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH"'), 2)
        self.assertNotIn("pgrep -x run_server", self.text)
        self.assertNotIn('timeout "$TIMEOUT_SERVER_S" numactl', self.text)

    def test_watchdog_is_one_killable_process_without_orphan_sleep(self):
        self.assertIn("time.sleep(timeout_s)", self.text)
        self.assertIn("os.readlink", self.text)
        self.assertIn("os.kill(server_pid, signal.SIGTERM)", self.text)
        self.assertNotIn('(\n    sleep "$TIMEOUT_SERVER_S"', self.text)

    def test_server_rss_comes_from_run_server_proc_status(self):
        self.assertIn("VmRSS|VmHWM", self.text)
        self.assertIn("server_exe", self.text)

    def test_sift10m_inputs_require_exact_shapes(self):
        self.assertIn("validate_fixed_vec_file", self.text)
        self.assertIn('validate_fixed_vec_file "$DROOT/datasets/sift10M/bigann_base.fvecs" 10000000 128', self.text)
        self.assertIn('validate_fixed_vec_file "$DROOT/datasets/sift10M/bigann_query.fvecs" 10000 128', self.text)
        self.assertIn('validate_fixed_vec_file "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" 10000 100', self.text)

    def test_ground_truth_ids_are_validated_against_base_cardinality(self):
        self.assertIn("validate_dhnsw_dataset.py", self.text)
        self.assertIn("validate_prepared_datasets", self.text)
        self.assertIn("--expected-queries 10000 --min-k 10", self.text)
        self.assertIn('validate_dataset deep10M', self.text)
        self.assertIn('validate_dataset sift10M', self.text)
        prepare_gate = self.text.split('if [[ "$PREPARE_DATASETS" == "1" ]]', 1)[1]
        self.assertIn("validate_prepared_datasets", prepare_gate)

    def test_tti10m_uses_one_auditable_10k_query_and_gt_prefix(self):
        self.assertIn("prepare_fixed_query_pool.py", self.text)
        self.assertIn('query-u10k.fvecs', self.text)
        self.assertIn('groundtruth-u10k.ivecs', self.text)
        self.assertIn('--limit 10000', self.text)
        self.assertIn('--manifest "$WORK/text10M/query-pool-u10k.json"', self.text)
        config = self.text.split('static std::map<std::string, DatasetConfig> config_map', 1)[1]
        text10m = config.split('{{ "text10M", {{', 1)[1].split('}}},', 1)[0]
        self.assertIn('query-u10k.fvecs', text10m)
        self.assertIn('groundtruth-u10k.ivecs', text10m)
        self.assertIn('--query "$query"', self.text)

    def test_generic_gt_converter_never_infers_k_from_distance_payload(self):
        converter = self.text.split("convert_ibin_to_ivecs()", 1)[1].split(
            "convert_bvecs_to_fvecs()", 1
        )[0]
        self.assertIn("expected_ids = n * dim_header", converter)
        self.assertIn("(expected_ids * 4, expected_ids * 8)", converter)
        self.assertIn("count=expected_ids", converter)
        self.assertIn("reshape(n, dim_header)", converter)
        self.assertNotIn("dim_actual = payload_ints // n", converter)

    def test_each_ef_uses_a_fresh_fixed_pool_client(self):
        self.assertIn("DEFINE_bool(fixed_query_pool", self.text)
        self.assertIn("query_index < n_query_data_thread", self.text)
        self.assertIn("FRONTIER_THREAD_RESULT", self.text)
        self.assertIn("FRONTIER_QUERY_POOL", self.text)
        self.assertIn("pthread_barrier_wait(&frontier_benchmark_barrier)", self.text)
        self.assertIn('for ef in $EF_LIST', self.text)
        self.assertIn('--ef_override="$ef" --fixed_query_pool=true', self.text)
        self.assertIn('${dataset}_ef${ef}_client.log', self.text)

    def test_each_client_requires_a_fresh_aggregate_detail_file(self):
        self.assertIn('mkdir -p "$DROOT/benchs/pipeline/test"', self.text)
        self.assertIn('rm -f "$detail_src" "$result_src"', self.text)
        self.assertIn('! -s "$detail_src"', self.text)
        self.assertIn('missing fresh aggregate benchmark details', self.text)
        self.assertNotIn(
            'cp -f "$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt"',
            self.text,
        )

    def test_fair_config_uses_every_query_once(self):
        self.assertIn("1, 0, 1, 10, 10, 5000", self.text)

    def test_prevalidated_dataset_tree_can_skip_conversion_explicitly(self):
        self.assertIn("PREPARE_DATASETS", self.text)
        self.assertIn('if [[ "$PREPARE_DATASETS" == "1" ]]', self.text)
        self.assertIn("Using prevalidated d-HNSW dataset tree", self.text)
        self.assertIn("BUILD_DHNSW", self.text)
        self.assertIn('if [[ "$BUILD_DHNSW" == "1" ]]', self.text)
        self.assertIn("Using prebuilt d-HNSW binaries", self.text)

    def test_isolated_source_tree_bootstraps_cmake_before_build(self):
        self.assertIn('cmake -S "$DROOT" -B "$DROOT/build" -DCMAKE_BUILD_TYPE=Release', self.text)
        self.assertIn('if [[ ! -s "$DROOT/build/CMakeCache.txt" ]]', self.text)


if __name__ == "__main__":
    unittest.main()
