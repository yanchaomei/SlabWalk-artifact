#!/usr/bin/env bash
# Queue worker scaling after the long-running final VLDB evidence campaigns.
set -euo pipefail

WAIT_SESSIONS=(
  vldb-index-build-12h-retry-v3
  vldb-frontier-text-sift-sw-final-v1
  vldb-resource-gist-final-v2
  vldb-dhnsw-text-sift-final-v1
)
POLL_S=${POLL_S:-300}

while true; do
  active=()
  for session in "${WAIT_SESSIONS[@]}"; do
    if tmux has-session -t "$session" 2>/dev/null; then
      active+=("$session")
    fi
  done
  if (( ${#active[@]} == 0 )); then
    break
  fi
  printf '%s waiting for: %s\n' "$(date -Is)" "${active[*]}"
  sleep "$POLL_S"
done

CLOSURE=/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713
SNAPSHOT=$CLOSURE/worker_scaling_snapshot_20260713
OUT=$CLOSURE/evidence/worker_scaling_final_v1_20260713
FINAL_BIN=$CLOSURE/build-final-v5/shine

cd "$CLOSURE"
OUT_ROOT="$OUT" \
CAMPAIGN_ID=vldb-worker-scaling-final-v1-20260713 \
GB_BIN="$FINAL_BIN" GB_BIN_R="$FINAL_BIN" \
GB_DATA=/home/kvgroup/chaomei/hnsw-data \
GB_LIB=/home/kvgroup/chaomei/lib \
DHNSW_SOURCE=/home/kvgroup/chaomei/d-HNSW \
SW_MN=skv-node6 SW_PORT=1290 \
DHNSW_PORT=50210 DHNSW_RDMA_PORT=8950 \
RESUME=1 \
bash "$SNAPSHOT/experiments/sigmetrics/run_vldb_worker_scaling.sh"
