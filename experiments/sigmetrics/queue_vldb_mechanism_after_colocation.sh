#!/usr/bin/env bash
# Run formal materialization-budget and resident-upper controls after co-location.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/mechanism_controls_snapshot_v7_20260715}
OUT=${OUT:-$ROOT/evidence/mechanism_controls_final_v6_20260715}
PREDECESSOR_SESSION=${PREDECESSOR_SESSION:-vldb-colocation-deep1-final-v2}
PREDECESSOR_OUT=${PREDECESSOR_OUT:-$ROOT/evidence/colocation_control_deep1_final_v2_20260714}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_vldb_mechanism_controls.sh
SUMMARIZER=$SNAPSHOT/experiments/sigmetrics/summarize_vldb_mechanism_controls.py
FINGERPRINT=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py
EXPECTED_RUNNER_SHA=${EXPECTED_RUNNER_SHA:-d4cd6470d805bc534df26e59edd965cc84d47f6e4fbf138010d9a241938b183f}
EXPECTED_SUMMARIZER_SHA=${EXPECTED_SUMMARIZER_SHA:-f4a9b580f2e6933ff2a79f914978df2edce033e597e53712ce7f3e4efb3cf456}
EXPECTED_FINGERPRINT_SHA=${EXPECTED_FINGERPRINT_SHA:-80e588f99e34450a6c238d94c7b459027b4d5c02184581fcc30c261305d25eb3}
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

colocation_complete() {
  local campaign=$1
  python3 - "$campaign" "$EXPECTED_BINARY_SHA" <<'PY'
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
expected_sha = sys.argv[2]
campaign = json.loads((root / "campaign.json").read_text())
protocol = campaign.get("protocol", {})
if (
    campaign.get("campaign_id") != "vldb-colocation-deep1-final-v2-20260714"
    or protocol.get("binary_sha256") != expected_sha
    or protocol.get("dataset") != "DEEP1M"
    or protocol.get("degrees") != ["full", "24", "16", "8", "4", "1"]
    or protocol.get("repeats") != 5
    or protocol.get("warmups") != 1
    or protocol.get("threads") != 10
    or protocol.get("query_contexts") != 10
    or protocol.get("coroutines") != 2
    or protocol.get("ef_search") != 200
):
    raise SystemExit(1)
encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
if campaign.get("protocol_fingerprint") != hashlib.sha256(encoded).hexdigest():
    raise SystemExit(1)

summary = root / "summary"
report = json.loads((summary / "validation.json").read_text())
if (
    report.get("measured_runs") != 30
    or report.get("measured_cells") != 6
    or report.get("retained_cells") != 36
):
    raise SystemExit(1)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

for key, name in (
    ("runs_sha256", "runs.csv"),
    ("summary_sha256", "summary.csv"),
    ("provenance_sha256", "provenance.json"),
):
    if report.get(key) != sha(summary / name):
        raise SystemExit(1)
provenance = json.loads((summary / "provenance.json").read_text())
if (
    provenance.get("campaign_id") != campaign.get("campaign_id")
    or provenance.get("protocol_fingerprint") != campaign.get("protocol_fingerprint")
):
    raise SystemExit(1)
with (summary / "runs.csv").open(newline="") as handle:
    runs = list(csv.DictReader(handle))
with (summary / "summary.csv").open(newline="") as handle:
    cells = list(csv.DictReader(handle))
degrees = ("full", "24", "16", "8", "4", "1")
if len(runs) != 30 or Counter(row["degree"] for row in runs) != Counter({key: 5 for key in degrees}):
    raise SystemExit(1)
if len(cells) != 6 or any(int(row["n"]) != 5 for row in cells):
    raise SystemExit(1)
PY
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'out=%s waits_for=%s predecessor=%s snapshot=%s\n' \
    "$OUT" "$PREDECESSOR_SESSION" "$PREDECESSOR_OUT" "$SNAPSHOT"
  exit 0
fi

for file in "$RUNNER" "$SUMMARIZER" "$FINGERPRINT"; do
  [[ -s "$file" ]] || { echo "missing mechanism-control snapshot input: $file" >&2; exit 2; }
done
[[ "$(sha256sum "$RUNNER" | awk '{print $1}')" == "$EXPECTED_RUNNER_SHA" ]] || {
  echo "mechanism-control runner snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$SUMMARIZER" | awk '{print $1}')" == "$EXPECTED_SUMMARIZER_SHA" ]] || {
  echo "mechanism-control summarizer snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$FINGERPRINT" | awk '{print $1}')" == "$EXPECTED_FINGERPRINT_SHA" ]] || {
  echo "mechanism-control fingerprint snapshot drift" >&2; exit 2;
}
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "mechanism-control frozen binary drift" >&2; exit 2;
}
[[ ! -e "$OUT" ]] || {
  echo "refusing existing mechanism-control output: $OUT" >&2; exit 2;
}

LOG=$ROOT/evidence/mechanism_controls_final_v6_20260715.campaign.log
printf 'waiting for co-location control\n' > "$LOG"
while tmux has-session -t "$PREDECESSOR_SESSION" 2>/dev/null; do
  printf '%s active=%s\n' "$(date -Is)" "$PREDECESSOR_SESSION" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

colocation_complete "$PREDECESSOR_OUT" || {
  echo "co-location predecessor did not complete protocol and hash validation" >&2
  exit 2
}

OUT_ROOT="$OUT" \
CAMPAIGN_ID=vldb-mechanism-controls-final-v6-20260715 \
GB_BIN="$GB_BIN" \
GB_BIN_R="$GB_BIN" \
GB_LIB=/home/kvgroup/chaomei/lib \
GB_DATA=/home/kvgroup/chaomei/hnsw-data \
MEMORY_NODE=skv-node5 \
PORT=1316 \
TIMEOUT_S=1200 \
INDEX_REGION_BYTES=4294967296 \
LAVD_REGION_BYTES=17179869184 \
BUDGET_FRACTIONS="f05 f10 f25 f50 f75 full" \
RESIDENT_MODES="remote resident" \
RESIDENT_EFS="50 100 200" \
REPEATS=5 WARMUPS=1 \
EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA" \
bash "$RUNNER" >> "$LOG" 2>&1

for file in \
  "$OUT/campaign.json" \
  "$OUT/summary/validation.json" \
  "$OUT/summary/runs.csv" \
  "$OUT/summary/budget_summary.csv" \
  "$OUT/summary/resident_summary.csv" \
  "$OUT/summary/provenance.json"; do
  [[ -s "$file" ]] || { echo "missing mechanism-control output: $file" >&2; exit 2; }
done
printf 'mechanism controls complete: %s\n' "$OUT" | tee -a "$LOG"
