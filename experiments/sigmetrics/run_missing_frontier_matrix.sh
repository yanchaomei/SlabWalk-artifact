#!/usr/bin/env bash
# Complete the five-dataset recall-QPS frontier matrix requested for the
# SIGMETRICS evaluation.  This script runs the missing large-dataset pieces
# sequentially on SKV_1 so d-HNSW, SHINE, and SlabWalk do not contend for the
# same RNIC while measuring QPS.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-c1}
EXP="$ROOT/experiments/sigmetrics"
STAMP=${STAMP:-$(date +%Y%m%d_%H%M%S)}
EF_LIST=${EF_LIST:-"48 64 96 128 200"}
THREADS=${THREADS:-10}
BENCHMARK_DURATION=${BENCHMARK_DURATION:-20}
DEEP_MN=${DEEP_MN:-skv-node5}
TEXT_MN=${TEXT_MN:-skv-node2}
SIFT10_MN=${SIFT10_MN:-skv-node2}

DH_OUT=${DH_OUT:-/home/kvgroup/chaomei/dhnsw_frontier_missing_${STAMP}}
BUILD_OUT=${BUILD_OUT:-/home/kvgroup/chaomei/frontier_index_build_missing_${STAMP}}
SW_OUT=${SW_OUT:-/home/kvgroup/chaomei/frontier_sweeps_missing_${STAMP}}

cd "$ROOT"

echo "=== [1/4] d-HNSW missing datasets: DEEP10M/TEXT10M/SIFT10M ==="
OUT="$DH_OUT" \
DATASETS="deep10M text10M sift10M" \
FETCH_SIFT10M=1 \
EF_LIST="$EF_LIST" \
THREADS="$THREADS" \
BENCHMARK_DURATION="$BENCHMARK_DURATION" \
bash "$EXP/run_dhnsw_frontier.sh"

python3 "$EXP/parse_dhnsw_frontier.py" \
  --result-dir "$DH_OUT" \
  --datasets deep10M text10M sift10M \
  --ef-list "$EF_LIST" \
  --duration "$BENCHMARK_DURATION" \
  --threads "$THREADS" \
  --out "$DH_OUT/dhnsw_frontier_ef_sweep.csv"

echo "=== [2/4] Prepare GraphBeyond SIFT10M data ==="
bash "$EXP/prepare_sift10m_bigann.sh"

echo "=== [3/4] Build missing GraphBeyond indexes: TEXT10M/SIFT10M ==="
OUT="$BUILD_OUT" DATASETS="TEXT10M SIFT10M" \
DEEP_MN="$DEEP_MN" TEXT_MN="$TEXT_MN" SIFT10_MN="$SIFT10_MN" \
bash "$EXP/build_frontier_indexes.sh"

echo "=== [4/4] SHINE/SlabWalk missing frontiers: DEEP10M/TEXT10M/SIFT10M ==="
OUT="$SW_OUT" \
DATASETS="DEEP10M TEXT10M SIFT10M" \
THREADS="$THREADS" \
TIMEOUT_S=${TIMEOUT_S:-7200} \
GB_REMOTE_LD_LIBRARY_PATH=${GB_REMOTE_LD_LIBRARY_PATH:-/home/kvgroup/chaomei/libboost_1_83} \
DEEP_MN="$DEEP_MN" TEXT_MN="$TEXT_MN" SIFT10_MN="$SIFT10_MN" \
bash "$EXP/run_frontier_sweeps.sh"

echo "=== DONE missing frontier matrix ==="
echo "d-HNSW:          $DH_OUT"
echo "GraphBeyond idx: $BUILD_OUT"
echo "SHINE/SlabWalk:  $SW_OUT"
