#!/usr/bin/env bash
# Run a rotated, repeated worker-scaling campaign for staged Slab construction.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BUILD_SOURCE_SCRIPT_DIR=${BUILD_SOURCE_SCRIPT_DIR:-$SCRIPT_DIR}
ROOT=${ROOT:-$(cd -- "$BUILD_SOURCE_SCRIPT_DIR/../.." && pwd)}
SCRIPT_PATH=$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")
INNER_RUNNER=${INNER_RUNNER:-$BUILD_SOURCE_SCRIPT_DIR/run_vldb_materialization_policy.sh}
INNER_SUMMARIZER=${INNER_SUMMARIZER:-$BUILD_SOURCE_SCRIPT_DIR/summarize_vldb_materialization_policy.py}
SUMMARIZER=${SUMMARIZER:-$BUILD_SOURCE_SCRIPT_DIR/summarize_vldb_build_pipeline.py}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$BUILD_SOURCE_SCRIPT_DIR/vldb_evidence_bundle.py}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA to the candidate SHA-256}
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
OUT_ROOT=${OUT_ROOT:-$ROOT/results/vldb_build_pipeline_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-build-pipeline-$(date -u +%Y%m%dT%H%M%SZ)}
DATASET=${DATASET:-DEEP1M}
POLICY=${POLICY:-indeg}
BUDGET_BYTES=${BUDGET_BYTES:-536870912}
BUILD_THREADS_LIST=${BUILD_THREADS_LIST:-"1 2 4 8 16 20 32"}
REPEATS=${REPEATS:-7}
WARMUPS=${WARMUPS:-1}
CAMPAIGN_KIND=${CAMPAIGN_KIND:-formal}
QUERY_THREADS=${QUERY_THREADS:-1}
QUERY_COROUTINES=${QUERY_COROUTINES:-4}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-$QUERY_THREADS}
MEMORY_NODE=${MEMORY_NODE:-skv-node5}
PORT_BASE=${PORT_BASE:-1570}
TIMEOUT_S=${TIMEOUT_S:-1800}
CAPTURE_PHASEF=${CAPTURE_PHASEF:-0}
DRY_RUN=${DRY_RUN:-0}
COMPUTE_HOST=$(hostname)

[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "EXPECTED_BINARY_SHA must contain 64 lowercase hex digits" >&2; exit 2;
}
[[ "$DATASET" == "DEEP1M" ]] || {
  echo "The certified worker-scaling protocol currently requires DEEP1M" >&2; exit 2;
}
[[ "$POLICY" == "indeg" ]] || {
  echo "The certified worker-scaling protocol currently requires indeg" >&2; exit 2;
}
[[ "$BUDGET_BYTES" == "536870912" ]] || {
  echo "The certified worker-scaling protocol currently requires 512 MiB" >&2; exit 2;
}
[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "REPEATS must be positive" >&2; exit 2; }
[[ "$WARMUPS" =~ ^[0-9]+$ ]] || { echo "WARMUPS must be non-negative" >&2; exit 2; }
[[ "$CAMPAIGN_KIND" == "formal" || "$CAMPAIGN_KIND" == "smoke" ]] || {
  echo "CAMPAIGN_KIND must be formal or smoke" >&2; exit 2;
}
[[ "$QUERY_THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_THREADS must be positive" >&2; exit 2; }
[[ "$QUERY_COROUTINES" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_COROUTINES must be positive" >&2; exit 2; }
[[ "$QUERY_CONTEXTS" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_CONTEXTS must be positive" >&2; exit 2; }
(( QUERY_CONTEXTS <= QUERY_THREADS )) || { echo "QUERY_CONTEXTS must not exceed QUERY_THREADS" >&2; exit 2; }
[[ "$CAPTURE_PHASEF" == "0" || "$CAPTURE_PHASEF" == "1" ]] || {
  echo "CAPTURE_PHASEF must be 0 or 1" >&2; exit 2;
}

read -r -a build_thread_array <<< "$BUILD_THREADS_LIST"
(( ${#build_thread_array[@]} > 0 )) || { echo "BUILD_THREADS_LIST must not be empty" >&2; exit 2; }
seen_threads=" "
previous_workers=0
for workers in "${build_thread_array[@]}"; do
  [[ "$workers" =~ ^[1-9][0-9]*$ ]] && (( workers <= 32 )) || {
    echo "Builder worker counts must be in 1..32" >&2; exit 2;
  }
  case "$seen_threads" in
    *" $workers "*) echo "Duplicate builder worker count: $workers" >&2; exit 2 ;;
  esac
  (( workers > previous_workers )) || {
    echo "BUILD_THREADS_LIST must be strictly increasing" >&2; exit 2;
  }
  seen_threads="${seen_threads}${workers} "
  previous_workers=$workers
done
[[ "${build_thread_array[0]}" == "1" ]] || {
  echo "BUILD_THREADS_LIST must start with the one-worker reference" >&2; exit 2;
}

order_string=""
thread_count=${#build_thread_array[@]}
if [[ "$CAMPAIGN_KIND" == "formal" ]] && (( REPEATS % thread_count != 0 )); then
  echo "A formal builder campaign must be position-balanced: REPEATS must be a multiple of the worker-count grid size" >&2
  exit 2
fi
for ((outer_repeat = 0; outer_repeat < REPEATS; ++outer_repeat)); do
  [[ -z "$order_string" ]] || order_string="${order_string};"
  order_string="${order_string}r${outer_repeat}:"
  for ((outer_position = 0; outer_position < thread_count; ++outer_position)); do
    index=$(((outer_repeat + outer_position) % thread_count))
    (( outer_position == 0 )) || order_string="${order_string},"
    order_string="${order_string}${build_thread_array[$index]}"
  done
done

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s campaign_kind=%s dataset=%s policy=%s budget_bytes=%s build_threads=%s repeats=%s warmups=%s order=%s\n' \
    "$CAMPAIGN_ID" "$CAMPAIGN_KIND" "$DATASET" "$POLICY" "$BUDGET_BYTES" \
    "$BUILD_THREADS_LIST" "$REPEATS" "$WARMUPS" "$order_string"
  exit 0
fi

for path in "$GB_BIN" "$INNER_RUNNER" "$INNER_SUMMARIZER" "$SUMMARIZER" "$EVIDENCE_TOOL"; do
  [[ -x "$path" || -f "$path" ]] || { echo "Missing required path: $path" >&2; exit 2; }
done
CN_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$CN_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "CN binary SHA mismatch: $CN_SHA" >&2; exit 2;
}
if [[ "${VLDB_BUILD_HARNESS_FROZEN:-0}" != "1" ]]; then
  if [[ -e "$OUT_ROOT" ]]; then
    echo "Refusing existing OUT_ROOT: $OUT_ROOT" >&2
    exit 2
  fi
  mkdir -p "$OUT_ROOT/raw"
  snapshot_json=$(python3 "$EVIDENCE_TOOL" snapshot \
    --out-dir "$OUT_ROOT/harness" \
    --entry runner="$SCRIPT_PATH" \
    --entry inner_runner="$INNER_RUNNER" \
    --entry inner_summarizer="$INNER_SUMMARIZER" \
    --entry summarizer="$SUMMARIZER" \
    --entry evidence_tool="$EVIDENCE_TOOL")
  read -r frozen_runner frozen_inner frozen_inner_summary frozen_summary \
    frozen_tool harness_sha <<< "$(python3 - "$OUT_ROOT/harness" "$snapshot_json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
payload = json.loads(sys.argv[2])
print(
    root / payload["entries"]["runner"]["path"],
    root / payload["entries"]["inner_runner"]["path"],
    root / payload["entries"]["inner_summarizer"]["path"],
    root / payload["entries"]["summarizer"]["path"],
    root / payload["entries"]["evidence_tool"]["path"],
    payload["manifest_sha256"],
)
PY
  )"
  exec env \
    VLDB_BUILD_HARNESS_FROZEN=1 \
    BUILD_SOURCE_SCRIPT_DIR="$BUILD_SOURCE_SCRIPT_DIR" ROOT="$ROOT" \
    OUT_ROOT="$OUT_ROOT" CAMPAIGN_ID="$CAMPAIGN_ID" \
    CAMPAIGN_KIND="$CAMPAIGN_KIND" \
    INNER_RUNNER="$frozen_inner" INNER_SUMMARIZER="$frozen_inner_summary" \
    SUMMARIZER="$frozen_summary" EVIDENCE_TOOL="$frozen_tool" \
    HARNESS_MANIFEST="$OUT_ROOT/harness/harness.json" \
    HARNESS_MANIFEST_SHA256="$harness_sha" \
    bash "$frozen_runner"
fi

HARNESS_MANIFEST=${HARNESS_MANIFEST:?frozen harness manifest is required}
HARNESS_MANIFEST_SHA256=${HARNESS_MANIFEST_SHA256:?frozen harness SHA is required}
verify_harness() {
  python3 "$EVIDENCE_TOOL" verify-harness \
    --manifest "$HARNESS_MANIFEST" \
    --expected-manifest-sha "$HARNESS_MANIFEST_SHA256" >/dev/null
}
verify_harness
[[ -d "$OUT_ROOT/raw" && ! -e "$OUT_ROOT/campaign.json" ]] || {
  echo "Invalid frozen build-pipeline output root" >&2
  exit 2
}

RUNNER_SHA=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
INNER_SHA=$(sha256sum "$INNER_RUNNER" | awk '{print $1}')
INNER_SUMMARIZER_SHA=$(sha256sum "$INNER_SUMMARIZER" | awk '{print $1}')
SUMMARIZER_SHA=$(sha256sum "$SUMMARIZER" | awk '{print $1}')
python3 - "$OUT_ROOT/campaign.json" "$CAMPAIGN_ID" "$EXPECTED_BINARY_SHA" \
  "$RUNNER_SHA" "$INNER_SHA" "$INNER_SUMMARIZER_SHA" "$SUMMARIZER_SHA" \
  "$DATASET" "$POLICY" \
  "$BUDGET_BYTES" "$BUILD_THREADS_LIST" "$REPEATS" "$WARMUPS" \
  "$CAMPAIGN_KIND" \
  "$QUERY_THREADS" "$QUERY_COROUTINES" "$QUERY_CONTEXTS" "$MEMORY_NODE" \
  "$PORT_BASE" "$order_string" "$COMPUTE_HOST" "$HARNESS_MANIFEST" \
  "$HARNESS_MANIFEST_SHA256" <<'PY'
import hashlib, json, pathlib, sys, uuid
from datetime import datetime, timezone

(path, campaign_id, binary_sha, runner_sha, inner_sha, inner_summarizer_sha,
 summarizer_sha,
 dataset, policy, budget, build_threads, repeats, warmups, campaign_kind,
 query_threads, query_coroutines, query_contexts, memory_node, port_base, order,
 compute_host,
 harness_manifest_path, harness_manifest_sha256) = sys.argv[1:]
harness_path = pathlib.Path(harness_manifest_path)
harness_bytes = harness_path.read_bytes()
if hashlib.sha256(harness_bytes).hexdigest() != harness_manifest_sha256:
    raise SystemExit("build-pipeline harness manifest drifted before campaign creation")
harness = json.loads(harness_bytes)
protocol = {
    "binary_sha256": binary_sha,
    "runner_sha256": runner_sha,
    "inner_runner_sha256": inner_sha,
    "inner_summarizer_sha256": inner_summarizer_sha,
    "summarizer_sha256": summarizer_sha,
    "dataset": dataset,
    "policy": policy,
    "budget_bytes": int(budget),
    "build_threads": [int(value) for value in build_threads.split()],
    "repeats": int(repeats),
    "campaign_kind": campaign_kind,
    "warmups_per_worker_before_first_measurement": int(warmups),
    "outer_order": order,
    "query_threads": int(query_threads),
    "query_coroutines": int(query_coroutines),
    "query_contexts": int(query_contexts),
    "memory_node": memory_node,
    "compute_host": compute_host,
    "port_base": int(port_base),
    "staged_build": True,
    "record_layout": "native_packed_variable",
    "harness": {
        "manifest": "harness/harness.json",
        "manifest_sha256": harness_manifest_sha256,
        "entries": harness["entries"],
    },
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
json.dump({
    "campaign_id": campaign_id,
    "campaign_uuid": str(uuid.uuid4()),
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "protocol_fingerprint": fingerprint,
    "protocol": protocol,
}, open(path, "w"), indent=2, sort_keys=True)
PY

CELL_INDEX=$OUT_ROOT/cell_index.csv
printf 'outer_repeat,outer_position,build_threads,child_dir,status\n' > "$CELL_INDEX"
cell_ordinal=0
port_stride=$((WARMUPS + 1))
for ((outer_repeat = 0; outer_repeat < REPEATS; ++outer_repeat)); do
  for ((outer_position = 0; outer_position < thread_count; ++outer_position)); do
    index=$(((outer_repeat + outer_position) % thread_count))
    workers=${build_thread_array[$index]}
    child_rel="raw/r${outer_repeat}_p${outer_position}_t${workers}"
    child_out="$OUT_ROOT/$child_rel"
    child_warmups=0
    (( outer_repeat == 0 )) && child_warmups=$WARMUPS
    port=$((PORT_BASE + cell_ordinal * port_stride))
    (( port + child_warmups <= 65535 )) || {
      echo "Campaign port range exceeds 65535" >&2; exit 2;
    }
    # The inner runner maps STAGED_BUILD=1 to SHINE_LAVD_STAGED_BUILD=1.
    env EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA" GB_BIN="$GB_BIN" \
      GB_BIN_R="$GB_BIN_R" OUT_ROOT="$child_out" \
      CAMPAIGN_ID="${CAMPAIGN_ID}-r${outer_repeat}-p${outer_position}-t${workers}" \
      DATASETS="$DATASET" POLICIES="$POLICY" BUDGET_BYTES="$BUDGET_BYTES" \
      CAMPAIGN_KIND=smoke REPEATS=1 WARMUPS="$child_warmups" THREADS="$QUERY_THREADS" \
      COROUTINES="$QUERY_COROUTINES" QUERY_CONTEXTS="$QUERY_CONTEXTS" \
      BUILD_THREADS="$workers" STAGED_BUILD=1 CAPTURE_PHASEF="$CAPTURE_PHASEF" \
      MEMORY_NODE="$MEMORY_NODE" PORT_BASE="$port" TIMEOUT_S="$TIMEOUT_S" \
      SUMMARIZER="$INNER_SUMMARIZER" EVIDENCE_TOOL="$EVIDENCE_TOOL" \
      "$INNER_RUNNER"
    python3 - "$child_out/campaign.json" "$child_out/runs.csv" "$workers" \
      "$EXPECTED_BINARY_SHA" "$COMPUTE_HOST" <<'PY'
import csv, json, sys

campaign_path, runs_path, requested_s, expected_sha, compute_host = sys.argv[1:]
requested = int(requested_s)
campaign = json.load(open(campaign_path))
protocol = campaign.get("protocol", {})
if int(protocol.get("build_threads", -1)) != requested:
    raise SystemExit(
        "builder worker drift: child protocol did not honor BUILD_THREADS"
    )
if protocol.get("staged_build") is not True:
    raise SystemExit("builder mode drift: child protocol is not staged")
if protocol.get("binary_sha256") != expected_sha:
    raise SystemExit("builder binary drift: child protocol SHA mismatch")
if str(protocol.get("compute_host", "")).strip() != compute_host:
    raise SystemExit("build-pipeline compute host drift: child protocol mismatch")
rows = list(csv.DictReader(open(runs_path)))
if len(rows) != 1:
    raise SystemExit("builder repeat drift: child must expose one measured row")
row = rows[0]
if int(row.get("build_workers", -1)) != requested:
    raise SystemExit(
        "builder worker drift: publication record did not honor BUILD_THREADS"
    )
if int(row.get("rank_workers_recorded", 0)) != 1 or int(
    row.get("rank_workers", -1)
) != requested:
    raise SystemExit(
        "rank worker drift: policy record did not honor BUILD_THREADS"
    )
if row.get("build_mode") != "staged":
    raise SystemExit("builder mode drift: publication record is not staged")
if row.get("binary_sha256") != expected_sha:
    raise SystemExit("builder binary drift: measured row SHA mismatch")
if str(row.get("compute_host", "")).strip() != compute_host:
    raise SystemExit("build-pipeline compute host drift: measured row mismatch")
PY
    python3 "$EVIDENCE_TOOL" verify --root "$child_out" >/dev/null
    (cd "$child_out" && sha256sum -c SHA256SUMS)
    printf '%s,%s,%s,%s,ok\n' "$outer_repeat" "$outer_position" \
      "$workers" "$child_rel" >> "$CELL_INDEX"
    cell_ordinal=$((cell_ordinal + 1))
  done
done

verify_harness
while IFS=, read -r outer_repeat outer_position workers child_rel status; do
  [[ "$outer_repeat" == "outer_repeat" ]] && continue
  [[ "$status" == "ok" ]] || { echo "Non-ok child before summary: $child_rel" >&2; exit 1; }
  python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT/$child_rel" >/dev/null
done < "$CELL_INDEX"
python3 "$SUMMARIZER" --index "$CELL_INDEX" \
  --threads "${build_thread_array[@]}" --repeats "$REPEATS" \
  --expected-sha "$EXPECTED_BINARY_SHA" \
  --expected-compute-host "$COMPUTE_HOST" --out-runs "$OUT_ROOT/runs.csv" \
  --out-summary "$OUT_ROOT/summary.csv" \
  --out-comparison "$OUT_ROOT/comparison.json"

verify_harness
while IFS=, read -r outer_repeat outer_position workers child_rel status; do
  [[ "$outer_repeat" == "outer_repeat" ]] && continue
  python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT/$child_rel" >/dev/null
done < "$CELL_INDEX"
python3 "$EVIDENCE_TOOL" seal --root "$OUT_ROOT" \
  --campaign "$OUT_ROOT/campaign.json" >/dev/null
python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT" >/dev/null
[[ -s "$OUT_ROOT/SHA256SUMS" ]] || { echo "Missing SHA256SUMS" >&2; exit 1; }
[[ -s "$OUT_ROOT/SEALED.json" ]] || { echo "Missing SEALED.json" >&2; exit 1; }

echo "Completed sealed staged builder worker scaling: $OUT_ROOT"
