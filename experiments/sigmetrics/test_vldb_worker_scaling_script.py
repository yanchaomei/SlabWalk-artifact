#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("run_vldb_worker_scaling.sh")


class VldbWorkerScalingScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_final_matrix_is_fixed_and_self_contained(self):
        self.assertIn('WORKERS=${WORKERS:-"1 8 16 40"}', self.text)
        self.assertIn('REPEATS=${REPEATS:-5}', self.text)
        self.assertIn('WARMUPS=${WARMUPS:-1}', self.text)
        self.assertIn('assemble_vldb_worker_scaling.py', self.text)
        self.assertIn('fingerprint_query_pool.py', self.text)

    def test_dhnsw_loopback_address_tracks_the_selected_client_host(self):
        self.assertIn('DHNSW_SERVER_IP=${DHNSW_SERVER_IP:-10.0.0.61}', self.text)
        self.assertIn('SERVER_IP="$DHNSW_SERVER_IP" RDMA_IP="$DHNSW_RDMA_IP"', self.text)

    def test_prevalidated_dhnsw_binary_mode_is_explicit_and_audited(self):
        self.assertIn('BUILD_DHNSW=${BUILD_DHNSW:-1}', self.text)
        self.assertIn('DHNSW_LD_LIBRARY_PATH=${DHNSW_LD_LIBRARY_PATH:-}', self.text)
        self.assertIn('[[ -x "$DROOT/build/run_client" && -x "$DROOT/build/run_server" ]]', self.text)
        self.assertIn('"dhnsw_build_mode": "source" if build_dhnsw == "1" else "prevalidated_binary"', self.text)
        self.assertIn('DHNSW_LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH"', self.text)

    def test_subrunners_own_cleanup(self):
        self.assertNotIn('pgrep', self.text)
        self.assertNotIn('pkill', self.text)
        self.assertIn('run_frontier_sweeps.sh', self.text)
        self.assertIn('run_dhnsw_frontier.sh', self.text)


if __name__ == "__main__":
    unittest.main()
