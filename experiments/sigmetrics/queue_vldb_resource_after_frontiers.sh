#!/usr/bin/env bash
# Run the final GIST1M resource ledger only after every competing frontier and
# index-build session has released the cluster.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/resource_after_frontiers_snapshot_v1_20260714}
OUT=${OUT:-$ROOT/evidence/resource_ledger_gist_final_v3_20260714}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_vldb_resource_ledger.sh
SUMMARIZER=$SNAPSHOT/experiments/sigmetrics/summarize_vldb_resource_ledger.py
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

SESSIONS=(
  "local:vldb-index-build-12h-retry-v3"
  "local:vldb-frontier-text-sift-sw-final-v1"
  "local:vldb-frontier-text-sift-sw-recovery-v2"
  "skv-node3:vldb-index-tti10m-parallel-v1"
  "skv-node4:vldb-index-sift10m-parallel-v1"
  "skv-node7:vldb-dhnsw-text-sift-final-v3"
  "skv-node7:vldb-frontier-deep10-sw-final-v1"
)

session_exists() {
  local host=$1 session=$2
  if [[ "$host" == "local" ]]; then
    tmux has-session -t "$session" 2>/dev/null
  else
    ssh -o LogLevel=ERROR "$host" \
      "tmux has-session -t '$session' 2>/dev/null"
  fi
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'out=%s sessions=%s\n' "$OUT" "${SESSIONS[*]}"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "missing frozen SlabWalk binary" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_SHA" ]] || {
  echo "frozen SlabWalk binary SHA mismatch" >&2; exit 2;
}
[[ -x "$RUNNER" && -s "$SUMMARIZER" ]] || {
  echo "missing resource-ledger snapshot" >&2; exit 2;
}
[[ ! -e "$OUT" ]] || { echo "refusing existing resource output: $OUT" >&2; exit 2; }

LOG=$ROOT/evidence/resource_ledger_gist_final_v3_20260714.campaign.log
printf 'waiting for isolated cluster\n' > "$LOG"
while true; do
  active=()
  for spec in "${SESSIONS[@]}"; do
    host=${spec%%:*}
    session=${spec#*:}
    if session_exists "$host" "$session"; then
      active+=("$spec")
    fi
  done
  if ((${#active[@]} == 0)); then
    break
  fi
  printf '%s active=%s\n' "$(date -Is)" "${active[*]}" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

mkdir -p "$OUT"
sha256sum "$0" "$RUNNER" "$SUMMARIZER" > "$OUT/queue_sources.sha256"
env \
  GB_BIN="$GB_BIN" \
  OUT="$OUT/raw" \
  LAYOUTS="legacy fixed variable" \
  MN_COUNTS="1 3 5" \
  REPEATS=5 WARMUPS=1 \
  THREADS=10 QUERY_CONTEXTS=10 COROUTINES=2 \
  PORT=1300 TIMEOUT_S=2400 \
  CAMPAIGN_ID=vldb-resource-gist-final-v3-20260714 \
  RESUME=0 \
  bash "$RUNNER" >> "$LOG" 2>&1

python3 "$SUMMARIZER" \
  --raw "$OUT/raw" \
  --out "$OUT/summary" \
  --expected-repeats 5 \
  --expected-layouts legacy,fixed,variable \
  --expected-mn-counts 1,3,5 \
  --require-latency \
  >> "$LOG" 2>&1

for file in runs.csv per_mn.csv summary.csv; do
  [[ -s "$OUT/summary/$file" ]] || {
    echo "missing resource summary: $file" >&2; exit 2;
  }
done
printf 'resource ledger complete: %s\n' "$OUT" | tee -a "$LOG"
