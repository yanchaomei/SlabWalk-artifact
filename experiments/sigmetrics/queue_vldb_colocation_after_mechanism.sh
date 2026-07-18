#!/usr/bin/env bash
# Run the hardened DEEP1M co-location control after mechanism controls finish.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$ROOT/colocation_snapshot_v4_20260715}
OUT=${OUT:-$ROOT/evidence/colocation_control_deep1_final_v4_20260715}
PREDECESSOR_SESSION=${PREDECESSOR_SESSION:-vldb-mechanism-controls-final-v6}
PREDECESSOR_OUT=${PREDECESSOR_OUT:-$ROOT/evidence/mechanism_controls_final_v6_20260715}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_vldb_colocation_control.sh
SUMMARIZER=$SNAPSHOT/experiments/sigmetrics/summarize_vldb_colocation_control.py
FINGERPRINT=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py
EXPECTED_RUNNER_SHA=${EXPECTED_RUNNER_SHA:-b1d18539b1d73b1cff17d9aa3333eecbf32ca92ffdb2d373a3c78388fce007a4}
EXPECTED_SUMMARIZER_SHA=${EXPECTED_SUMMARIZER_SHA:-cdcd91484f3358d4896145321ab8c66e250095295dca9d0cad95979967ea33e5}
EXPECTED_FINGERPRINT_SHA=${EXPECTED_FINGERPRINT_SHA:-80e588f99e34450a6c238d94c7b459027b4d5c02184581fcc30c261305d25eb3}
EXPECTED_MECHANISM_RUNNER_SHA=${EXPECTED_MECHANISM_RUNNER_SHA:-d4cd6470d805bc534df26e59edd965cc84d47f6e4fbf138010d9a241938b183f}
EXPECTED_MECHANISM_SUMMARIZER_SHA=${EXPECTED_MECHANISM_SUMMARIZER_SHA:-f4a9b580f2e6933ff2a79f914978df2edce033e597e53712ce7f3e4efb3cf456}
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}

mechanism_complete() {
  local campaign=$1
  python3 - "$campaign" "$EXPECTED_BINARY_SHA" \
    "$EXPECTED_MECHANISM_RUNNER_SHA" "$EXPECTED_MECHANISM_SUMMARIZER_SHA" \
    "$EXPECTED_FINGERPRINT_SHA" <<'PY'
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
expected_binary, expected_runner, expected_summarizer, expected_fingerprint = sys.argv[2:]
if (root / "campaign_failure.json").exists():
    raise SystemExit(1)
campaign = json.loads((root / "campaign.json").read_text())
protocol = campaign.get("protocol", {})
budget = protocol.get("budget", {})
resident = protocol.get("resident", {})
if (
    campaign.get("campaign_id") != "vldb-mechanism-controls-final-v6-20260715"
    or protocol.get("binary_sha256") != expected_binary
    or protocol.get("runner_sha256") != expected_runner
    or protocol.get("summarizer_sha256") != expected_summarizer
    or protocol.get("fingerprint_tool_sha256") != expected_fingerprint
    or protocol.get("repeats") != 5
    or protocol.get("warmups") != 1
    or budget.get("dataset") != "GIST200K"
    or budget.get("fractions") != ["f05", "f10", "f25", "f50", "f75", "full"]
    or resident.get("dataset") != "SIFT1M"
    or resident.get("modes") != ["remote", "resident"]
    or resident.get("ef_values") != [50, 100, 200]
):
    raise SystemExit(1)
for key in (
    "gist_index_dump_sha256", "sift_index_dump_sha256",
    "gist_query_sha256", "gist_groundtruth_sha256",
    "sift_query_sha256", "sift_groundtruth_sha256",
):
    if not re.fullmatch(r"[0-9a-f]{64}", str(protocol.get(key, ""))):
        raise SystemExit(1)
encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
fingerprint = hashlib.sha256(encoded).hexdigest()
if campaign.get("protocol_fingerprint") != fingerprint:
    raise SystemExit(1)

summary = root / "summary"
names = (
    "validation.json", "runs.csv", "budget_summary.csv",
    "resident_summary.csv", "provenance.json",
)
if any(not (summary / name).is_file() or (summary / name).stat().st_size == 0 for name in names):
    raise SystemExit(1)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

report = json.loads((summary / "validation.json").read_text())
if (
    report.get("measured_runs") != 60
    or report.get("measured_cells") != 12
    or report.get("retained_cells") != 72
    or report.get("retained_source_files") != 360
    or report.get("runs_sha256") != sha(summary / "runs.csv")
    or report.get("budget_summary_sha256") != sha(summary / "budget_summary.csv")
    or report.get("resident_summary_sha256") != sha(summary / "resident_summary.csv")
    or report.get("provenance_sha256") != sha(summary / "provenance.json")
):
    raise SystemExit(1)
provenance = json.loads((summary / "provenance.json").read_text())
if (
    provenance.get("campaign_id") != campaign.get("campaign_id")
    or provenance.get("protocol_fingerprint") != fingerprint
    or len(provenance.get("retained_source_files", [])) != 360
):
    raise SystemExit(1)
with (summary / "runs.csv").open(newline="") as handle:
    runs = list(csv.DictReader(handle))
counts = Counter((row["control"], row["key"], int(row["ef"])) for row in runs)
expected_cells = {
    **{("budget", key, 100): 5 for key in ("f05", "f10", "f25", "f50", "f75", "full")},
    **{("resident", mode, ef): 5 for mode in ("remote", "resident") for ef in (50, 100, 200)},
}
if len(runs) != 60 or counts != Counter(expected_cells):
    raise SystemExit(1)
for name, expected_rows in (("budget_summary.csv", 6), ("resident_summary.csv", 6)):
    with (summary / name).open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != expected_rows or any(int(row["n"]) != 5 for row in rows):
        raise SystemExit(1)
PY
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'out=%s waits_for=%s predecessor=%s snapshot=%s\n' \
    "$OUT" "$PREDECESSOR_SESSION" "$PREDECESSOR_OUT" "$SNAPSHOT"
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

LOG=$ROOT/evidence/colocation_control_deep1_final_v4_20260715.campaign.log
printf 'waiting for mechanism controls\n' > "$LOG"
while tmux has-session -t "$PREDECESSOR_SESSION" 2>/dev/null; do
  printf '%s active=%s\n' "$(date -Is)" "$PREDECESSOR_SESSION" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

mechanism_complete "$PREDECESSOR_OUT" || {
  echo "mechanism predecessor did not complete protocol and hash validation" >&2
  exit 2
}

OUT_ROOT="$OUT" \
CAMPAIGN_ID=vldb-colocation-deep1-final-v4-20260715 \
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
