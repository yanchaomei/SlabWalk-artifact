#!/usr/bin/env bash
# Recover the TTI10M/SIFT10M SlabWalk+SHINE campaign only if the primary queue
# exits without five complete repetitions. Active jobs are observed, not edited.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/recover_text_sift_frontier_snapshot_v1_20260714}
EVIDENCE=${EVIDENCE:-$ROOT/evidence}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
PRIMARY_QUEUE=${PRIMARY_QUEUE:-vldb-frontier-text-sift-sw-final-v1}
PRIMARY_OUT=${PRIMARY_OUT:-$EVIDENCE/frontier_text_sift_sw_final_v1_20260713}
HEDGE_HOST=${HEDGE_HOST:-skv-node3}
HEDGE_SESSION=${HEDGE_SESSION:-vldb-index-tti10m-parallel-v1}
SIFT_HOST=${SIFT_HOST:-skv-node4}
SIFT_SESSION=${SIFT_SESSION:-vldb-index-sift10m-parallel-v1}
MEMORY_NODE=${MEMORY_NODE:-skv-node2}
OUT_ROOT=${OUT_ROOT:-$EVIDENCE/frontier_text_sift_sw_final_v2_20260714}
QUERY_EVIDENCE=${QUERY_EVIDENCE:-$EVIDENCE/query_pools_text_sift_recovery_v2_20260714}
LOG=${LOG:-$EVIDENCE/frontier_text_sift_sw_recovery_v2_20260714.log}
WAIT_SECONDS=${WAIT_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

RUNNER=$SNAPSHOT/experiments/sigmetrics/run_frontier_repeated.sh
PREPARER=$SNAPSHOT/experiments/sigmetrics/prepare_fixed_query_pool.py
SPOTCHECK=$SNAPSHOT/experiments/sigmetrics/spotcheck_groundtruth.py
FINGERPRINT=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py
TTI_DATA=/home/kvgroup/chaomei/hnsw-data/tti-10m
SIFT_DATA=/home/kvgroup/chaomei/hnsw-data/sift10m

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'primary=%s hedge=%s:%s sift=%s:%s out=%s\n' \
    "$PRIMARY_QUEUE" "$HEDGE_HOST" "$HEDGE_SESSION" \
    "$SIFT_HOST" "$SIFT_SESSION" "$OUT_ROOT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "missing frozen SlabWalk binary" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_SHA" ]] || {
  echo "frozen SlabWalk binary SHA mismatch" >&2; exit 2;
}
for path in "$RUNNER" "$PREPARER" "$SPOTCHECK" "$FINGERPRINT"; do
  [[ -s "$path" ]] || { echo "missing recovery snapshot file: $path" >&2; exit 2; }
done

campaign_complete() {
  local campaign=$1
  python3 - "$campaign" "$EXPECTED_SHA" <<'PY'
import csv
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
expected_sha = sys.argv[2]
manifest_path = root / "campaign.json"
if not manifest_path.is_file():
    raise SystemExit(1)
manifest = json.loads(manifest_path.read_text())
protocol = manifest.get("protocol", {})
if (
    protocol.get("gb_binary_sha256") != expected_sha
    or protocol.get("datasets_sw") != ["TEXT10M", "SIFT10M"]
    or protocol.get("repeats") != 5
    or protocol.get("threads") != 10
    or protocol.get("query_contexts") != 10
    or protocol.get("coroutines") != 2
):
    raise SystemExit(1)
campaign_id = manifest.get("campaign_id")
for repeat in range(1, 6):
    run_id = f"r{repeat}"
    path = root / f"sw_{run_id}" / "slabwalk_shine_frontier_raw.csv"
    if not path.is_file():
        raise SystemExit(1)
    rows = list(csv.DictReader(path.open()))
    counts = Counter()
    for row in rows:
        if (
            row.get("status") != "ok"
            or row.get("run_kind") != "measure"
            or row.get("run_id") != run_id
            or row.get("campaign_id") != campaign_id
            or row.get("binary_sha256") != expected_sha
            or row.get("measurement_mode") != "fixed_query_pool"
            or row.get("trace") != "0"
            or int(row.get("threads", 0)) != 10
            or int(row.get("query_contexts", 0)) != 10
            or int(row.get("top_k", 0)) != 10
            or int(row.get("processed", -1)) != int(row.get("expected_queries", -2))
            or int(row.get("failed_queries", -1)) != 0
        ):
            raise SystemExit(1)
        counts[(row["dataset"], row["method"])] += 1
    expected = {
        (dataset, method)
        for dataset in ("TEXT10M", "SIFT10M")
        for method in ("SHINE", "SlabWalk")
    }
    if set(counts) != expected or any(counts[cell] < 5 for cell in expected):
        raise SystemExit(1)
PY
}

printf 'waiting for primary queue\n' > "$LOG"
while tmux has-session -t "$PRIMARY_QUEUE" 2>/dev/null; do
  sleep "$WAIT_SECONDS"
done
if campaign_complete "$PRIMARY_OUT"; then
  printf 'primary campaign already complete: %s\n' "$PRIMARY_OUT" | tee -a "$LOG"
  exit 0
fi

printf 'primary incomplete; waiting for hedge builders\n' >> "$LOG"
while ssh -o LogLevel=ERROR "$HEDGE_HOST" \
    "tmux has-session -t '$HEDGE_SESSION' 2>/dev/null" \
  || ssh -o LogLevel=ERROR "$SIFT_HOST" \
    "tmux has-session -t '$SIFT_SESSION' 2>/dev/null"; do
  sleep "$WAIT_SECONDS"
done

ssh -o LogLevel=ERROR "$MEMORY_NODE" \
  "test -s '$TTI_DATA/dump/index_m16_efc100_node1_of1.dat' && \
   test -s '$SIFT_DATA/dump/index_m16_efc100_node1_of1.dat'"
[[ ! -e "$OUT_ROOT" ]] || {
  echo "refusing existing recovery output: $OUT_ROOT" >&2; exit 2;
}
mkdir -p "$QUERY_EVIDENCE"

python3 "$PREPARER" \
  --query "$TTI_DATA/queries/query-uniform.fbin" \
  --groundtruth "$TTI_DATA/queries/groundtruth-uniform.bin" \
  --limit 10000 \
  --query-fbin "$TTI_DATA/queries/query-u10k.fbin" \
  --groundtruth-bin "$TTI_DATA/queries/groundtruth-u10k.bin" \
  --manifest "$QUERY_EVIDENCE/tti10m_graph_query_pool.json" \
  >> "$LOG" 2>&1

env OPENBLAS_NUM_THREADS=8 OMP_NUM_THREADS=8 nice -n 10 \
  python3 "$SPOTCHECK" \
    --base "$TTI_DATA/base.fbin" \
    --query "$TTI_DATA/queries/query-u10k.fbin" \
    --groundtruth "$TTI_DATA/queries/groundtruth-u10k.bin" \
    --metric ip --query-indices 0,4999,9999 --top-k 10 \
    --block-rows 100000 --require-exact \
    --out "$QUERY_EVIDENCE/tti_exact_groundtruth_spotcheck.json" \
    >> "$LOG" 2>&1

for method in SHINE SlabWalk; do
  safe_method=$(printf '%s' "$method" | tr '[:upper:]' '[:lower:]')
  python3 "$FINGERPRINT" \
    --query "$TTI_DATA/queries/query-u10k.fbin" \
    --groundtruth "$TTI_DATA/queries/groundtruth-u10k.bin" \
    --limit 10000 --dataset TTI10M --method "$method" --metric ip \
    --out "$QUERY_EVIDENCE/tti10m_$safe_method.json" \
    >> "$LOG" 2>&1
done

sha256sum "$0" "$RUNNER" "$PREPARER" "$SPOTCHECK" "$FINGERPRINT" \
  > "$QUERY_EVIDENCE/recovery_sources.sha256"
env \
  GB_BIN="$GB_BIN" \
  OUT_ROOT="$OUT_ROOT" \
  DATASETS_SW="TEXT10M SIFT10M" \
  DATASETS_DH="text10M sift10M" \
  PHASES=sw THREADS=10 QUERY_CONTEXTS=10 COROS=2 REPEATS=5 \
  SW_PORT=1493 CAMPAIGN_ID=vldb-frontier-text-sift-sw-final-v2-20260714 \
  RESUME=0 \
  bash "$RUNNER" >> "$LOG" 2>&1

campaign_complete "$OUT_ROOT" || {
  echo "incomplete recovery frontier" >&2; exit 2;
}
printf 'recovery campaign complete: %s\n' "$OUT_ROOT" | tee -a "$LOG"
