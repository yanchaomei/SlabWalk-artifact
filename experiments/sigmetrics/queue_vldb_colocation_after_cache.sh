#!/usr/bin/env bash
# Run the formal DEEP1M co-location control after the CPU profile releases the cluster.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/colocation_after_profile_snapshot_v2_20260714}
OUT=${OUT:-$ROOT/evidence/colocation_control_deep1_final_v2_20260714}
PROFILE_SESSION=${PROFILE_SESSION:-vldb-profile-sift1-frozen-v2}
PROFILE_HOST=${PROFILE_HOST:-skv-node7}
PROFILE_OUT=${PROFILE_OUT:-$ROOT/evidence/query_profile_sift1_frozen_v2_20260714}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_vldb_colocation_control.sh
SUMMARIZER=$SNAPSHOT/experiments/sigmetrics/summarize_vldb_colocation_control.py
FINGERPRINT=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py
EXPECTED_RUNNER_SHA=${EXPECTED_RUNNER_SHA:-10e5af742ab1c259f8965420bcedd51c40950de5d52fe0040e89c6289e867d55}
EXPECTED_SUMMARIZER_SHA=${EXPECTED_SUMMARIZER_SHA:-450a08c5ced26b1645f29a366e247d700521eddb562af0c92b9b1158f6630cb2}
EXPECTED_FINGERPRINT_SHA=${EXPECTED_FINGERPRINT_SHA:-80e588f99e34450a6c238d94c7b459027b4d5c02184581fcc30c261305d25eb3}
EXPECTED_PROFILE_RUNNER_SHA=${EXPECTED_PROFILE_RUNNER_SHA:-3d20f0968654a3ad27fa1b4624c5425e1844258a1a75cc905232ac78e68bcf6e}
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

profile_complete() {
  local campaign=$1
  ssh -o LogLevel=ERROR "$PROFILE_HOST" \
    python3 - "$campaign" "$EXPECTED_BINARY_SHA" "$EXPECTED_PROFILE_RUNNER_SHA" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
expected_binary_sha = sys.argv[2]
expected_runner_sha = sys.argv[3]
manifest = json.loads((root / "campaign.json").read_text())
protocol = manifest.get("protocol", {})
if (
    manifest.get("campaign_id") != "vldb-query-profile-sift1-frozen-v2-20260714"
    or protocol.get("binary_sha256") != expected_binary_sha
    or protocol.get("datasets") != ["SIFT1M"]
    or protocol.get("methods") != ["shine"]
    or protocol.get("threads") != 1
    or protocol.get("query_contexts_requested") != 1
    or protocol.get("coroutines") != 8
    or protocol.get("ef") != 100
    or protocol.get("capture_perf") is not True
    or protocol.get("compute_recall") is not False
):
    raise SystemExit(1)
encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
if manifest.get("protocol_fingerprint") != hashlib.sha256(encoded).hexdigest():
    raise SystemExit(1)
tag = "SIFT1M_shine_T1_C8_ef100"
data_path = root / f"{tag}.json"
perf_path = root / f"{tag}.perf.data"
required = [
    data_path,
    perf_path,
    root / f"{tag}.perf.data.sha256",
    root / f"{tag}.perf.record.status",
    root / f"{tag}.perf.txt",
    root / f"{tag}.mn.err",
    root / "runner_snapshot.sh",
    root / "profile_sources.sha256",
]
if any(not path.is_file() or path.stat().st_size == 0 for path in required):
    raise SystemExit(1)
if (root / f"{tag}.perf.record.status").read_text().strip() != "0":
    raise SystemExit(1)
if hashlib.sha256((root / "runner_snapshot.sh").read_bytes()).hexdigest() != expected_runner_sha:
    raise SystemExit(1)
if (root / f"{tag}.perf.data.sha256").read_text().split()[0] != hashlib.sha256(perf_path.read_bytes()).hexdigest():
    raise SystemExit(1)
source_hashes = {line.split()[0] for line in (root / "profile_sources.sha256").read_text().splitlines() if line.strip()}
if not {expected_runner_sha, expected_binary_sha}.issubset(source_hashes):
    raise SystemExit(1)
data = json.loads(data_path.read_text())
if (
    data.get("num_queries") != 200000
    or data.get("query_contexts") != 1
    or data.get("meta", {}).get("dataset") != "sift1m"
    or data.get("meta", {}).get("compute_threads") != 1
    or data.get("meta", {}).get("coroutines_per_thread") != 8
    or data.get("hnsw_parameters", {}).get("ef_search") != 100
    or data.get("queries", {}).get("processed") != 200000
    or data.get("queries", {}).get("queries_per_sec", 0) <= 0
):
    raise SystemExit(1)
PY
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'out=%s waits_for=%s profile=%s:%s snapshot=%s\n' \
    "$OUT" "$PROFILE_SESSION" "$PROFILE_HOST" "$PROFILE_OUT" "$SNAPSHOT"
  exit 0
fi

for file in "$RUNNER" "$SUMMARIZER" "$FINGERPRINT"; do
  [[ -s "$file" ]] || { echo "missing co-location snapshot input: $file" >&2; exit 2; }
done
[[ "$(sha256sum "$RUNNER" | awk '{print $1}')" == "$EXPECTED_RUNNER_SHA" ]] || {
  echo "co-location runner snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$SUMMARIZER" | awk '{print $1}')" == "$EXPECTED_SUMMARIZER_SHA" ]] || {
  echo "co-location summarizer snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$FINGERPRINT" | awk '{print $1}')" == "$EXPECTED_FINGERPRINT_SHA" ]] || {
  echo "co-location fingerprint snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "co-location frozen binary drift" >&2; exit 2;
}
[[ ! -e "$OUT" ]] || { echo "refusing existing co-location output: $OUT" >&2; exit 2; }

LOG=$ROOT/evidence/colocation_control_deep1_final_v2_20260714.campaign.log
printf 'waiting for query profile\n' > "$LOG"
while tmux has-session -t "$PROFILE_SESSION" 2>/dev/null; do
  printf '%s active=%s\n' "$(date -Is)" "$PROFILE_SESSION" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

profile_complete "$PROFILE_OUT" || {
  echo "profile predecessor did not complete protocol and hash validation" >&2
  exit 2
}

OUT_ROOT="$OUT" \
CAMPAIGN_ID=vldb-colocation-deep1-final-v2-20260714 \
GB_BIN="$GB_BIN" \
GB_BIN_R="$GB_BIN" \
GB_LIB=/home/kvgroup/chaomei/lib \
GB_DATA=/home/kvgroup/chaomei/hnsw-data \
MEMORY_NODE=skv-node5 \
PORT=1314 \
TIMEOUT_S=1200 \
DEGREES="full 24 16 8 4 1" \
REPEATS=5 WARMUPS=1 \
THREADS=10 QUERY_CONTEXTS=10 COROUTINES=2 EF_SEARCH=200 \
EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA" \
bash "$RUNNER" >> "$LOG" 2>&1

for file in \
  "$OUT/campaign.json" \
  "$OUT/summary/validation.json" \
  "$OUT/summary/runs.csv" \
  "$OUT/summary/summary.csv" \
  "$OUT/summary/provenance.json"; do
  [[ -s "$file" ]] || { echo "missing co-location output: $file" >&2; exit 2; }
done
printf 'co-location control complete: %s\n' "$OUT" | tee -a "$LOG"
