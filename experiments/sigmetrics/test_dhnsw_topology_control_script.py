#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("run_dhnsw_topology_control.sh")


class DhnswTopologyControlScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_compares_loopback_and_remote_with_one_client_protocol(self):
        self.assertIn('TOPOLOGIES=${TOPOLOGIES:-"loopback remote"}', self.text)
        self.assertIn('CLIENT_IP=${CLIENT_IP:-10.0.0.61}', self.text)
        self.assertIn('REMOTE_SERVER_IP=${REMOTE_SERVER_IP:-10.0.0.66}', self.text)
        self.assertIn('REMOTE_SERVER_HOST=${REMOTE_SERVER_HOST:-skv-node6}', self.text)
        self.assertIn('--worker_threads="$THREADS"', self.text)
        self.assertIn('--ef_override="$EF" --fixed_query_pool=true', self.text)

    def test_uses_repeats_query_fingerprint_and_final_parser(self):
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('WARMUPS=${WARMUPS:-1}', self.text)
        self.assertIn('fingerprint_query_pool.py', self.text)
        self.assertIn('parse_dhnsw_frontier.py', self.text)
        self.assertIn('query_canonical_sha256', self.text)
        self.assertIn('source_sha256', self.text)

    def test_cleanup_is_strictly_pid_owned(self):
        self.assertIn('verify_local_server_pid', self.text)
        self.assertIn('verify_remote_server_pid', self.text)
        self.assertIn('readlink -f /proc/', self.text)
        self.assertNotIn('pgrep', self.text)
        self.assertNotIn('pkill', self.text)

    def test_remote_bundle_is_separate_and_sha_verified(self):
        self.assertIn('REMOTE_SERVER_BIN=${REMOTE_SERVER_BIN:-$SERVER_BIN}', self.text)
        self.assertIn('REMOTE_BASE=${REMOTE_BASE:-$BASE}', self.text)
        self.assertIn('REMOTE_DHNSW_LD_LIBRARY_PATH=${REMOTE_DHNSW_LD_LIBRARY_PATH:-$DHNSW_LD_LIBRARY_PATH}', self.text)
        self.assertIn('remote_server_sha', self.text)
        self.assertIn('remote_base_sha', self.text)
        self.assertIn('[[ "$REMOTE_SERVER_SHA" == "$SERVER_SHA" ]]', self.text)
        self.assertIn('[[ "$REMOTE_BASE_SHA" == "$BASE_SHA" ]]', self.text)

    def test_client_runs_from_build_directory_for_relative_dataset_paths(self):
        self.assertIn('cd "$DROOT/build"', self.text)
        self.assertIn('numactl --preferred=1 ./run_client', self.text)
        self.assertNotIn('numactl --preferred=1 "$CLIENT_BIN" \\\n+      --server_address=', self.text)


if __name__ == "__main__":
    unittest.main()
