#!/usr/bin/env bash
# Gate and run the node3-to-node5 d-HNSW topology sensitivity campaign.
set -euo pipefail

POLL_S=${POLL_S:-60}
CLOSURE=${CLOSURE:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$CLOSURE/topology_control_node3_snapshot_v2_20260713}
WORKER=${WORKER:-$CLOSURE/evidence/worker_scaling_final_node3_v1_20260713}
OUT=${OUT:-$CLOSURE/evidence/dhnsw_topology_final_node3_v2_20260713}
DROOT=${DROOT:-$WORKER/dhnsw-source}
LOCAL_RUNTIME=${LOCAL_RUNTIME:-/home/kvgroup/chaomei/dhnsw-pilot-20260713/runtime-lib}
REMOTE_SERVER_HOST=${REMOTE_SERVER_HOST:-skv-node5}
REMOTE_SERVER_IP=${REMOTE_SERVER_IP:-10.0.0.65}
REMOTE_ROOT=${REMOTE_ROOT:-$CLOSURE/topology_remote_node5_v2_20260713}
EXPECTED_SLABWALK_SHA=${EXPECTED_SLABWALK_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
CAMPAIGN_ID=${CAMPAIGN_ID:-dhnsw-topology-final-node3-v2-20260713}
RESUME=${RESUME:-0}

while tmux has-session -t vldb-worker-scaling-final-v1 2>/dev/null; do
  printf '%s waiting for vldb-worker-scaling-final-v1\n' "$(date -Is)"
  sleep "$POLL_S"
done

if ! PYTHONPATH="$SNAPSHOT/experiments/sigmetrics" python3 - \
    "$WORKER/summary" "$EXPECTED_SLABWALK_SHA" <<'PY'
import sys
from pathlib import Path
import validate_vldb_final_evidence as evidence

evidence.validate_worker_scaling(Path(sys.argv[1]), sys.argv[2])
PY
then
  echo "worker scaling did not pass its formal gate; topology control will not start" >&2
  exit 2
fi

for path in \
  "$DROOT/build/run_client" \
  "$DROOT/build/run_server" \
  "$DROOT/datasets/deep1M/deep1M_base.fvecs" \
  "$LOCAL_RUNTIME/libprotobuf.so.27.2.0"; do
  [[ -s "$path" ]] || { echo "Missing topology staging input: $path" >&2; exit 2; }
done

ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
  "mkdir -p '$REMOTE_ROOT/bin' '$REMOTE_ROOT/data' '$REMOTE_ROOT/runtime-lib'"
rsync -a "$DROOT/build/run_server" \
  "$REMOTE_SERVER_HOST:$REMOTE_ROOT/bin/run_server"
rsync -a "$DROOT/datasets/deep1M/deep1M_base.fvecs" \
  "$REMOTE_SERVER_HOST:$REMOTE_ROOT/data/deep1M_base.fvecs"
rsync -a "$LOCAL_RUNTIME/" \
  "$REMOTE_SERVER_HOST:$REMOTE_ROOT/runtime-lib/"

DROOT="$DROOT" \
OUT="$OUT" \
CAMPAIGN_ID="$CAMPAIGN_ID" \
CLIENT_IP=10.0.0.63 \
REMOTE_SERVER_HOST="$REMOTE_SERVER_HOST" REMOTE_SERVER_IP="$REMOTE_SERVER_IP" \
REMOTE_SERVER_BIN="$REMOTE_ROOT/bin/run_server" \
REMOTE_BASE="$REMOTE_ROOT/data/deep1M_base.fvecs" \
DHNSW_LD_LIBRARY_PATH="$LOCAL_RUNTIME" \
REMOTE_DHNSW_LD_LIBRARY_PATH="$REMOTE_ROOT/runtime-lib" \
PORT=50320 RDMA_PORT=8980 RESUME="$RESUME" \
bash "$SNAPSHOT/experiments/sigmetrics/run_dhnsw_topology_control.sh"
