#!/usr/bin/env bash
# Queue the d-HNSW topology sensitivity after the formal worker campaign.
set -euo pipefail

POLL_S=${POLL_S:-300}
while tmux has-session -t vldb-worker-scaling-final-v1 2>/dev/null; do
  printf '%s waiting for vldb-worker-scaling-final-v1\n' "$(date -Is)"
  sleep "$POLL_S"
done

CLOSURE=/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713
SNAPSHOT=$CLOSURE/topology_control_snapshot_20260713
WORKER=$CLOSURE/evidence/worker_scaling_final_v1_20260713
OUT=$CLOSURE/evidence/dhnsw_topology_final_v1_20260713

cd "$CLOSURE"
DROOT="$WORKER/dhnsw-source" \
OUT="$OUT" \
CAMPAIGN_ID=dhnsw-topology-final-v1-20260713 \
REMOTE_SERVER_HOST=skv-node6 REMOTE_SERVER_IP=10.0.0.66 \
CLIENT_IP=10.0.0.61 PORT=50220 RDMA_PORT=8960 \
RESUME=1 \
bash "$SNAPSHOT/experiments/sigmetrics/run_dhnsw_topology_control.sh"
