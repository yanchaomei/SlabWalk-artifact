#!/usr/bin/env bash
# Run key GB_QUERY_TRACE points for the SIGMETRICS evaluation.
# Execute on an SKV compute node after building graphbeyond/build/shine.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
EXP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

source "$EXP_DIR/lib/env.sh"

MODE=${1:-quick}
STAMP=${GB_TRACE_STAMP:-$(date +%Y%m%d_%H%M%S)}
OUT=${GB_TRACE_OUT:-"$REPO_ROOT/results/sigmetrics_trace_$STAMP"}
mkdir -p "$OUT"

THREADS_MATCHED=${GB_TRACE_THREADS_MATCHED:-10}
THREADS_LAT=${GB_TRACE_THREADS_LAT:-1}
COROS_TRACE=${GB_TRACE_COROS:-1}
NQ_SUFFIX=${GB_TRACE_QUERY_SUFFIX:-uniform}
TIMEOUT_SEC=${GB_TRACE_TIMEOUT_SEC:-900}

run_case() {
  local name=$1
  local data_dir=$2
  local threads=$3
  local ef=$4
  local m=$5
  local efc=$6
  local lavd=$7
  local extra_env=$8

  if [[ ! -d "$data_dir" ]]; then
    echo "skip $name: missing data dir $data_dir" | tee -a "$OUT/run.log"
    return 0
  fi

  local trace="$OUT/${name}.trace.csv"
  local log="$OUT/${name}.json"
  local err="$OUT/${name}.err"
  rm -f "$trace"

  echo "run $name" | tee -a "$OUT/run.log"
  gb_smn
  env $extra_env GB_QUERY_TRACE="$trace" timeout "$TIMEOUT_SEC" numactl --preferred=1 "$GB_BIN" \
    --servers "$GB_MN" --initiator \
    --threads "$threads" --coroutines "$COROS_TRACE" \
    --data-path "$data_dir" --query-suffix "$NQ_SUFFIX" \
    --ef-search "$ef" --ef-construction "$efc" --m "$m" --k 10 \
    --spec-k 1 --load-index --lavd "$lavd" --label "$name" \
    >"$log" 2>"$err"
  gb_xmn
}

trap gb_xmn EXIT

run_sift1m() {
  run_case "sift1m_baseline_10t_ef48" "$GB_DATA/sift1m" "$THREADS_MATCHED" 48 16 100 0 ""
  run_case "sift1m_slabwalk_10t_ef48" "$GB_DATA/sift1m" "$THREADS_MATCHED" 48 16 100 8 "SHINE_CRANE=1 GB_BITMAP_DEDUP=1"
  run_case "sift1m_baseline_1t_ef48" "$GB_DATA/sift1m" "$THREADS_LAT" 48 16 100 0 ""
  run_case "sift1m_slabwalk_1t_ef48" "$GB_DATA/sift1m" "$THREADS_LAT" 48 16 100 8 "SHINE_CRANE=1 GB_BITMAP_DEDUP=1"
}

run_deep1m() {
  run_case "deep1m_baseline_10t_ef80" "$GB_DATA/deep1m" "$THREADS_MATCHED" 80 16 100 0 ""
  run_case "deep1m_slabwalk_10t_ef80" "$GB_DATA/deep1m" "$THREADS_MATCHED" 80 16 100 8 "SHINE_CRANE=1 GB_BITMAP_DEDUP=1"
  run_case "deep1m_baseline_1t_ef80" "$GB_DATA/deep1m" "$THREADS_LAT" 80 16 100 0 ""
  run_case "deep1m_slabwalk_1t_ef80" "$GB_DATA/deep1m" "$THREADS_LAT" 80 16 100 8 "SHINE_CRANE=1 GB_BITMAP_DEDUP=1"
}

if [[ "$MODE" == "quick" || "$MODE" == "sift1m" ]]; then
  run_sift1m
elif [[ "$MODE" == "deep1m" ]]; then
  run_deep1m
elif [[ "$MODE" == "full" ]]; then
  run_sift1m
  run_deep1m
  run_case "gist1m_baseline_10t_ef400" "$GB_DATA/gist1m" "$THREADS_MATCHED" 400 16 100 0 ""
  run_case "gist1m_slabwalk_10t_ef400" "$GB_DATA/gist1m" "$THREADS_MATCHED" 400 16 100 8 "SHINE_CRANE=1 SHINE_LAVD_RABITQ_B=2 GB_BITMAP_DEDUP=1"
  run_case "deep10m_slabwalk_40t_ef80" "$GB_DATA/deep10m" 40 80 32 200 8 "SHINE_CRANE=1 SHINE_LAVD_RABITQ_B=2 GB_BITMAP_DEDUP=1"
else
  echo "unknown mode: $MODE (expected quick, sift1m, deep1m, or full)" >&2
  exit 2
fi

traces=("$OUT"/*.trace.csv)
if [[ -e "${traces[0]}" ]]; then
  python3 "$SCRIPT_DIR/trace_summary.py" "${traces[@]}" \
    --json-out "$OUT/trace_summary.json" \
    --md-out "$OUT/trace_summary.md"
fi

echo "done: $OUT"
