#!/usr/bin/env bash
# Run the formal SIFT1M cache control after the final resource-ledger campaign.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/cache_after_resource_snapshot_v1_20260714}
RESOURCE_SESSION=${RESOURCE_SESSION:-vldb-resource-gist-final-v3}
RESOURCE_OUT=${RESOURCE_OUT:-$ROOT/evidence/resource_ledger_gist_final_v3_20260714}
OUT=${OUT:-$ROOT/evidence/cache_control_sift1_final_v1_20260714}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_vldb_cache_control.sh
SUMMARIZER=$SNAPSHOT/experiments/sigmetrics/summarize_vldb_cache_control.py
FINGERPRINTER=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'out=%s waits_for=%s resource=%s\n' "$OUT" "$RESOURCE_SESSION" "$RESOURCE_OUT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "missing frozen SlabWalk binary" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | cut -d ' ' -f1)" == "$EXPECTED_SHA" ]] || {
  echo "frozen SlabWalk binary SHA mismatch" >&2; exit 2;
}
[[ -x "$RUNNER" && -s "$SUMMARIZER" && -s "$FINGERPRINTER" ]] || {
  echo "missing cache-control snapshot" >&2; exit 2;
}
[[ ! -e "$OUT" ]] || { echo "refusing existing cache output: $OUT" >&2; exit 2; }

LOG=$ROOT/evidence/cache_control_sift1_final_v1_20260714.campaign.log
printf 'waiting for resource ledger\n' > "$LOG"
while tmux has-session -t "$RESOURCE_SESSION" 2>/dev/null; do
  printf '%s active=%s\n' "$(date -Is)" "$RESOURCE_SESSION" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

for file in runs.csv per_mn.csv summary.csv; do
  [[ -s "$RESOURCE_OUT/summary/$file" ]] || {
    echo "resource predecessor did not complete: $file" >&2
    exit 2
  }
done

env \
  GB_BIN="$GB_BIN" \
  GB_BIN_R="$GB_BIN" \
  GB_LIB="$GB_LIB" \
  GB_DATA="$GB_DATA" \
  OUT_ROOT="$OUT" \
  CAMPAIGN_ID=vldb-cache-control-sift1-final-v1-20260714 \
  CONDITIONS="off c5 c20 c50" \
  REPEATS=5 WARMUPS=1 \
  THREADS=1 QUERY_CONTEXTS=1 COROUTINES=8 EF_SEARCH=100 \
  MEMORY_NODE=skv-node4 PORT=1310 TIMEOUT_S=900 \
  bash "$RUNNER" >> "$LOG" 2>&1

for file in runs.csv summary.csv provenance.json validation.json; do
  [[ -s "$OUT/summary/$file" ]] || {
    echo "missing cache-control summary: $file" >&2
    exit 2
  }
done
sha256sum "$0" "$RUNNER" "$SUMMARIZER" "$FINGERPRINTER" \
  > "$OUT/queue_sources.sha256"
printf 'cache control complete: %s\n' "$OUT" | tee -a "$LOG"
