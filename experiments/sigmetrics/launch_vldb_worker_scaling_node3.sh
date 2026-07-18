#!/usr/bin/env bash
# Node3-side launch profile for the uncontended final worker campaign.
set -euo pipefail

CLOSURE=/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713
SNAPSHOT=$CLOSURE/worker_scaling_node3_snapshot_20260713
OUT=$CLOSURE/evidence/worker_scaling_final_node3_v1_20260713
FINAL_BIN=$CLOSURE/build-final-v5/shine

cd "$CLOSURE"
OUT_ROOT="$OUT" \
CAMPAIGN_ID=vldb-worker-scaling-final-node3-v1-20260713 \
GB_BIN="$FINAL_BIN" GB_BIN_R="$FINAL_BIN" \
GB_DATA=/home/kvgroup/chaomei/hnsw-data \
GB_LIB=/home/kvgroup/chaomei/lib \
DHNSW_SOURCE=/home/kvgroup/chaomei/dhnsw-pilot-20260713 \
BUILD_DHNSW=0 \
DHNSW_LD_LIBRARY_PATH=/home/kvgroup/chaomei/dhnsw-pilot-20260713/runtime-lib \
SW_MN=skv-node5 SW_PORT=1390 \
DHNSW_SERVER_IP=10.0.0.63 DHNSW_RDMA_IP=10.0.0.63 \
DHNSW_PORT=50310 DHNSW_RDMA_PORT=8970 \
RESUME=1 \
bash "$SNAPSHOT/experiments/sigmetrics/run_vldb_worker_scaling.sh"
