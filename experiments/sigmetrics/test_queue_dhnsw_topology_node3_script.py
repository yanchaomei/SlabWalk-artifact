#!/usr/bin/env python3
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("queue_dhnsw_topology_after_worker_node3.sh")


class QueueDhnswTopologyNode3ScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text()

    def test_waits_for_and_validates_worker_evidence(self):
        self.assertIn("vldb-worker-scaling-final-v1", self.text)
        self.assertIn("validate_worker_scaling", self.text)
        self.assertIn("worker scaling did not pass its formal gate", self.text)

    def test_stages_a_sha_checked_remote_bundle(self):
        self.assertIn("REMOTE_SERVER_HOST=${REMOTE_SERVER_HOST:-skv-node5}", self.text)
        self.assertIn('rsync -a "$DROOT/build/run_server"', self.text)
        self.assertIn('rsync -a "$DROOT/datasets/deep1M/deep1M_base.fvecs"', self.text)
        self.assertIn('rsync -a "$LOCAL_RUNTIME/"', self.text)
        self.assertIn('REMOTE_SERVER_BIN="$REMOTE_ROOT/bin/run_server"', self.text)

    def test_never_uses_global_process_cleanup(self):
        self.assertNotIn("pgrep", self.text)
        self.assertNotIn("pkill", self.text)

    def test_replacement_campaign_is_fresh_and_separately_named(self):
        self.assertIn("topology_control_node3_snapshot_v2_20260713", self.text)
        self.assertIn("dhnsw_topology_final_node3_v2_20260713", self.text)
        self.assertIn(
            "CAMPAIGN_ID=${CAMPAIGN_ID:-dhnsw-topology-final-node3-v2-20260713}",
            self.text,
        )
        self.assertIn("RESUME=${RESUME:-0}", self.text)
        self.assertIn('CAMPAIGN_ID="$CAMPAIGN_ID"', self.text)
        self.assertIn('RESUME="$RESUME"', self.text)


if __name__ == "__main__":
    unittest.main()
