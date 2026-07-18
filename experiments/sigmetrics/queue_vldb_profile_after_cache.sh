#!/usr/bin/env bash
# Capture a frozen-binary SIFT1M baseline profile after the formal cache control.
set -euo pipefail

ROOT=${ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
CACHE_SESSION=${CACHE_SESSION:-vldb-cache-control-sift1-final-v2}
CACHE_OUT=${CACHE_OUT:-$ROOT/evidence/cache_control_sift1_final_v2_20260714}
PROFILE_HOST=${PROFILE_HOST:-skv-node7}
REMOTE_RUNNER=${REMOTE_RUNNER:-$ROOT/profile_frozen_sift_v2_20260714/experiments/sigmetrics/run_vldb_query_profile.sh}
OUT=${OUT:-$ROOT/evidence/query_profile_sift1_frozen_v2_20260714}
GB_BIN=${GB_BIN:-$ROOT/build-final-v5/shine}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
EXPECTED_BINARY_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
EXPECTED_RUNNER_SHA=3d20f0968654a3ad27fa1b4624c5425e1844258a1a75cc905232ac78e68bcf6e
WAIT_SECONDS=${WAIT_SECONDS:-60}
QUIET_SECONDS=${QUIET_SECONDS:-60}
DRY_RUN=${DRY_RUN:-0}
LOG=${LOG:-$ROOT/evidence/query_profile_sift1_frozen_v2_20260714.campaign.log}

cache_complete() {
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
    campaign.get("campaign_id") != "vldb-cache-control-sift1-final-v2-20260714"
    or protocol.get("binary_sha256") != expected_sha
    or protocol.get("dataset") != "SIFT1M"
    or protocol.get("conditions") != ["off", "c5", "c20", "c50"]
    or protocol.get("repeats") != 5
    or protocol.get("warmups") != 1
    or protocol.get("threads") != 1
    or protocol.get("query_contexts") != 1
    or protocol.get("coroutines") != 8
    or protocol.get("ef_search") != 100
):
    raise SystemExit(1)
encoded = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
if campaign.get("protocol_fingerprint") != hashlib.sha256(encoded).hexdigest():
    raise SystemExit(1)

summary = root / "summary"
report = json.loads((summary / "validation.json").read_text())
if (
    report.get("measured_runs") != 20
    or report.get("measured_cells") != 4
    or report.get("retained_cells") != 24
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
counts = Counter(row["condition"] for row in runs)
if len(runs) != 20 or counts != Counter({key: 5 for key in ("off", "c5", "c20", "c50")}):
    raise SystemExit(1)
if len(cells) != 4 or any(int(row["n"]) != 5 for row in cells):
    raise SystemExit(1)
PY
}

profile_output_complete() {
  local campaign=$1
  ssh -o LogLevel=ERROR "$PROFILE_HOST" \
    python3 - "$campaign" "$EXPECTED_BINARY_SHA" "$EXPECTED_RUNNER_SHA" <<'PY'
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
    or protocol.get("memory_nodes_by_dataset", {}).get("SIFT1M") != "skv-node4"
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
recorded_perf_sha = (root / f"{tag}.perf.data.sha256").read_text().split()[0]
if recorded_perf_sha != hashlib.sha256(perf_path.read_bytes()).hexdigest():
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
    or data.get("meta", {}).get("query_suffix") != "profile20x"
    or data.get("hnsw_parameters", {}).get("ef_search") != 100
    or data.get("queries", {}).get("processed") != 200000
    or data.get("queries", {}).get("queries_per_sec", 0) <= 0
):
    raise SystemExit(1)
PY
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'host=%s out=%s waits_for=%s cache=%s\n' \
    "$PROFILE_HOST" "$OUT" "$CACHE_SESSION" "$CACHE_OUT"
  exit 0
fi

printf 'waiting for formal cache control\n' > "$LOG"
while tmux has-session -t "$CACHE_SESSION" 2>/dev/null; do
  printf '%s active=%s\n' "$(date -Is)" "$CACHE_SESSION" >> "$LOG"
  sleep "$WAIT_SECONDS"
done
sleep "$QUIET_SECONDS"

cache_complete "$CACHE_OUT" || {
  echo "cache predecessor did not complete protocol and hash validation" >&2
  exit 2
}

remote_runner_sha=$(ssh -o LogLevel=ERROR "$PROFILE_HOST" \
  "sha256sum '$REMOTE_RUNNER' | cut -d ' ' -f1")
[[ "$remote_runner_sha" == "$EXPECTED_RUNNER_SHA" ]] || {
  echo "profile runner SHA mismatch" >&2
  exit 2
}
remote_binary_sha=$(ssh -o LogLevel=ERROR "$PROFILE_HOST" \
  "sha256sum '$GB_BIN' | cut -d ' ' -f1")
[[ "$remote_binary_sha" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "frozen profile binary SHA mismatch" >&2
  exit 2
}
ssh -o LogLevel=ERROR "$PROFILE_HOST" "test ! -e '$OUT'" || {
  echo "refusing existing profile output: $OUT" >&2
  exit 2
}

ssh -o LogLevel=ERROR "$PROFILE_HOST" \
  "env GB_BIN='$GB_BIN' GB_BIN_R='$GB_BIN' GB_LIB='$GB_LIB' GB_DATA='$GB_DATA' \
  OUT='$OUT' CAMPAIGN_ID='vldb-query-profile-sift1-frozen-v2-20260714' \
  DATASETS=SIFT1M METHODS=shine THREADS=1 QUERY_CONTEXTS=1 COROUTINES=8 \
  EF=100 TOP_K=10 TILE=20 PROFILE_S=20 TIMEOUT_S=1200 PERF_FREQ=199 \
  PERF_CMD='sudo -n perf' PERF_REPORT_CMD=perf \
  PERF_DATA_FIXUP_CMD='sudo -n chown' MN_SIFT1M=skv-node4 PORT=1312 \
  bash '$REMOTE_RUNNER'" >> "$LOG" 2>&1

ssh -o LogLevel=ERROR "$PROFILE_HOST" \
  "cp '$REMOTE_RUNNER' '$OUT/runner_snapshot.sh'"
ssh -o LogLevel=ERROR "$PROFILE_HOST" \
  "sha256sum '$OUT/runner_snapshot.sh' '$GB_BIN' > '$OUT/profile_sources.sha256'"

for file in \
  campaign.json \
  runner_snapshot.sh \
  profile_sources.sha256 \
  SIFT1M_shine_T1_C8_ef100.json \
  SIFT1M_shine_T1_C8_ef100.mn.err \
  SIFT1M_shine_T1_C8_ef100.perf.data \
  SIFT1M_shine_T1_C8_ef100.perf.data.sha256 \
  SIFT1M_shine_T1_C8_ef100.perf.record.status \
  SIFT1M_shine_T1_C8_ef100.perf.txt; do
  ssh -o LogLevel=ERROR "$PROFILE_HOST" "test -s '$OUT/$file'" || {
    echo "missing profile artifact: $file" >&2
    exit 2
  }
done
profile_output_complete "$OUT" || {
  echo "profile output did not complete protocol and hash validation" >&2
  exit 2
}

printf 'query profile complete: %s:%s\n' "$PROFILE_HOST" "$OUT" | tee -a "$LOG"
