#!/bin/bash
# GraphBeyond C1 grid sweep over (coroutines, K).
# Run on skv-node1.

set -uo pipefail

DATASET="${1:-sift10k}"
EF="${2:-50}"
EFC="${3:-40}"
M="${4:-8}"
COROS="${5:-1,2,4,8}"
KS="${6:-1,2,4,8}"

BIN_LOCAL="/home/kvgroup/chaomei/graphbeyond-c1/graphbeyond/build/shine"
BIN_REMOTE="/home/kvgroup/chaomei/graphbeyond-c1-shine"
DATA="/home/kvgroup/chaomei/hnsw-data/${DATASET}/"
OUT_DIR="/home/kvgroup/chaomei/graphbeyond-c1/results/c1_grid_${DATASET}"
mkdir -p "${OUT_DIR}"

MN_HOST="skv-node4"

start_mn() {
  # Note: do NOT match "graphbeyond-c1-shine" via pkill -- the ssh remote shell's
  # cmdline contains that literal string and pkill would self-terminate.
  ssh -o StrictHostKeyChecking=no "${MN_HOST}" "tmux kill-session -t mn 2>/dev/null; pgrep -x shine | xargs -r kill -9 2>/dev/null; pgrep -x graphbeyond-c1-shine | xargs -r kill -9 2>/dev/null; sleep 1; tmux new-session -d -s mn 'numactl --preferred=1 ${BIN_REMOTE} --is-server --num-clients 1 2>&1 | tee /tmp/mn.log'"
  sleep 4
}

stop_mn() {
  ssh -o StrictHostKeyChecking=no "${MN_HOST}" "tmux kill-session -t mn 2>/dev/null; pgrep -x shine | xargs -r kill -9 2>/dev/null; pgrep -x graphbeyond-c1-shine | xargs -r kill -9 2>/dev/null; true"
}

CSV="${OUT_DIR}/grid.csv"
echo "coroutines,K,QPS,query_ms,build_ms,visited_neighborlists,visited_nodes,rdma_reads_in_bytes" > "${CSV}"

IFS=',' read -ra C_ARRAY <<< "${COROS}"
IFS=',' read -ra K_ARRAY <<< "${KS}"

for C in "${C_ARRAY[@]}"; do
  for K in "${K_ARRAY[@]}"; do
    TAG="c${C}_K${K}"
    echo ""
    echo "=== ${TAG} ==="
    STDOUT="${OUT_DIR}/${TAG}.stdout"
    STDERR="${OUT_DIR}/${TAG}.stderr"

    start_mn
    numactl --preferred=1 "${BIN_LOCAL}" \
      --servers "${MN_HOST}" --initiator \
      --threads 1 --coroutines "${C}" \
      --data-path "${DATA}" --query-suffix uniform \
      --ef-search "${EF}" --ef-construction "${EFC}" \
      --m "${M}" --k 10 --no-recall \
      --label "c1_${TAG}" --spec-k "${K}" \
      > "${STDOUT}" 2> "${STDERR}"
    rc=$?
    stop_mn

    if [[ ${rc} -ne 0 ]]; then
      echo "  rc=${rc}; tail stderr:"
      tail -8 "${STDERR}"
      echo "${C},${K},FAIL,FAIL,FAIL,FAIL,FAIL,FAIL" >> "${CSV}"
      continue
    fi

    python3 - "${STDOUT}" "${C}" "${K}" "${CSV}" <<'PY'
import json, sys
path, C, K, csv = sys.argv[1:]
try:
    obj = json.loads(open(path).read())
except Exception as e:
    print(f"  parse failed: {e}")
    open(csv,'a').write(f"{C},{K},PARSE_FAIL,,,,,,\n")
    sys.exit(0)
q = obj.get('queries', {}); t = obj.get('timings', {})
qps = q.get('queries_per_sec'); query_ms = t.get('query_c0', 0)
build_ms = t.get('build_c0', 0)
vnl = q.get('visited_neighborlists'); vn = q.get('visited_nodes')
rb = q.get('rdma_reads_in_bytes')
print(f"  c={C} K={K}  QPS={qps}  query={query_ms:.1f}ms  visited_nl={vnl}  visited_n={vn}  rdma_b={rb}")
open(csv,'a').write(f"{C},{K},{qps},{query_ms:.1f},{build_ms:.1f},{vnl},{vn},{rb}\n")
PY
  done
done

echo ""
echo "=== Grid done. CSV: ${CSV} ==="
column -t -s, "${CSV}"
