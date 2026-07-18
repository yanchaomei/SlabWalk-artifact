#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("launch_parallel_dhnsw_10m_node7.sh")


class ParallelDhnsw10mNode7ScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_locks_source_build_and_runtime_provenance(self):
        self.assertIn(
            "EXPECTED_SOURCE_COMMIT=d6f275732275e6009a542a7066d7f695036daaf6",
            self.text,
        )
        self.assertNotIn("EXPECTED_CLIENT_SHA", self.text)
        self.assertNotIn("SOURCE_BIN_ROOT", self.text)
        self.assertIn("SOURCE_NODE=${SOURCE_NODE:-skv-node1}", self.text)
        self.assertIn("RUNTIME_NODE=${RUNTIME_NODE:-skv-node3}", self.text)
        self.assertIn("SOURCE_BUILD_ROOT=${SOURCE_BUILD_ROOT:-$CLOSURE/dhnsw-node7-v3-build}", self.text)
        self.assertIn(
            "EXPECTED_BUILD_MANIFEST_SHA=a5a8bf71e66bd1de1bd31bd607d82459da929e2dde35cfc6ae40754ecaab51e4",
            self.text,
        )
        self.assertIn("source-build/build_manifest.json", self.text)
        self.assertIn('rsync -a --exclude=build/ "$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/"', self.text)
        self.assertIn('"$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/build/run_client"', self.text)
        self.assertIn('"$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/build/run_server"', self.text)
        self.assertNotIn('cmake -S "$DROOT"', self.text)
        self.assertNotIn('cmake --build "$DROOT/build"', self.text)
        self.assertIn("runtime-source.sha256", self.text)
        self.assertIn("runtime-local.sha256", self.text)
        self.assertIn("diff -u", self.text)
        self.assertIn("ldd", self.text)
        self.assertIn("not found", self.text)

    def test_compiled_client_must_match_patched_dataset_paths(self):
        self.assertIn("binary-dataset-paths.txt", self.text)
        self.assertIn("query-u10k.fvecs", self.text)
        self.assertIn("groundtruth-u10k.ivecs", self.text)
        self.assertIn("text10M_query.fvecs", self.text)
        self.assertIn("compiled client/source dataset-path mismatch", self.text)

    def test_reuses_only_hash_checked_local_input_cache(self):
        self.assertIn("LOCAL_INPUT_CACHE", self.text)
        self.assertIn('ln "$cache_path" "$local_path"', self.text)
        self.assertIn("source input SHA mismatch", self.text)
        self.assertIn("input-transfer.tsv", self.text)

    def test_uses_fixed_pool_three_system_frontier_protocol(self):
        self.assertIn("DATASETS='tti10M sift10M'", self.text)
        self.assertIn("EF_LIST='48 64 96 128 200'", self.text)
        self.assertIn("THREADS=10 REPEATS=5", self.text)
        self.assertIn("BUILD_DHNSW=0 PREPARE_DATASETS=0", self.text)
        self.assertIn("prepare_fixed_query_pool.py", self.text)
        self.assertIn("--limit 10000", self.text)
        self.assertIn("query-u10k.fvecs", self.text)
        self.assertIn("groundtruth-u10k.ivecs", self.text)

    def test_verifies_every_remote_input_before_measurement(self):
        self.assertIn("sync_and_verify", self.text)
        self.assertIn("source input SHA mismatch", self.text)
        self.assertIn("text10M_base.fvecs", self.text)
        self.assertIn("bigann_base.fvecs", self.text)
        self.assertIn("idx_10M.ivecs", self.text)
        self.assertIn("query-uniform.fbin", self.text)
        self.assertIn("groundtruth-uniform.bin", self.text)
        self.assertIn("validate_dhnsw_dataset.py", self.text)
        self.assertIn("fingerprint_query_pool.py", self.text)

    def test_uses_node7_loopback_and_exact_cleanup_only(self):
        self.assertIn("SERVER_IP=10.0.0.67 RDMA_IP=10.0.0.67", self.text)
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)
        self.assertNotIn("kill -9", self.text)

    def test_uses_fresh_v3_names_after_excluded_build_attempts(self):
        self.assertIn("dhnsw_10m_node7_snapshot_v3_20260713", self.text)
        self.assertIn("dhnsw_text_sift_node7_final_v3_20260713", self.text)
        self.assertIn("dhnsw-text-sift-node7-final-v3", self.text)
        self.assertIn("vldb-dhnsw-text-sift-node7-final-v3-20260713", self.text)


if __name__ == "__main__":
    unittest.main()
