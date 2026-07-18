#!/usr/bin/env bash
# Formal control for separating expansion-sized retrieval from compact-code quality.
set -euo pipefail

SCRIPT_PATH=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname -- "$SCRIPT_PATH")
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/colocation_control_deep1_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-colocation-control-$(date -u +%Y%m%dT%H%M%SZ)}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
DEGREES=${DEGREES:-"full 24 16 8 4 1"}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
THREADS=${THREADS:-10}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-10}
COROUTINES=${COROUTINES:-2}
EF_SEARCH=${EF_SEARCH:-200}
MEMORY_NODE=${MEMORY_NODE:-skv-node5}
PORT=${PORT:-1314}
TIMEOUT_S=${TIMEOUT_S:-1200}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
LAVD_REGION_BYTES=${LAVD_REGION_BYTES:-6442450944}
DRY_RUN=${DRY_RUN:-0}
REMOTE_RUNNER=/tmp/vldb_colocation_control_runner_${PORT}.sh
ACTIVE_REMOTE_OUT=""

DATA_PATH=$GB_DATA/deep1m
QUERY_SUFFIX=uniform
QUERY_PATH=$DATA_PATH/queries/query-uniform.fbin
GT_PATH=$DATA_PATH/queries/groundtruth-uniform.bin
INDEX_DUMP=$DATA_PATH/dump/index_m16_efc100_node1_of1.dat

if [[ "${1:-}" == "--memory-node" ]]; then
  shift
  bin=$1
  out=$2
  lib=$3
  port=$4
  index_region_bytes=$5
  mkdir -p "$out"
  server_pid=""
  terminate_owned_server() {
    if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
      kill "$server_pid" 2>/dev/null || true
    fi
  }
  trap terminate_owned_server INT TERM
  set +e
  env LD_LIBRARY_PATH="$lib" /usr/bin/time -v -o "$out/mn.time" "$bin" \
    --is-server --num-clients 1 --port "$port" \
    --index-region-bytes "$index_region_bytes" \
    > "$out/mn.out" 2> "$out/mn.runtime.err" &
  time_pid=$!
  set -e
  expected=$(realpath "$bin")
  for _ in $(seq 1 100); do
    if [[ -r "/proc/$time_pid/task/$time_pid/children" ]]; then
      for child in $(<"/proc/$time_pid/task/$time_pid/children"); do
        actual=$(readlink -f "/proc/$child/exe" 2>/dev/null || true)
        if [[ "$actual" == "$expected" ]]; then
          server_pid=$child
          break 2
        fi
      done
    fi
    kill -0 "$time_pid" 2>/dev/null || break
    sleep 0.1
  done
  if [[ -n "$server_pid" ]]; then
    printf '%s\n' "$server_pid" > "$out/server.pid"
    printf '%s\n' "$expected" > "$out/server.exe"
  else
    printf 'failed to resolve owned memory-node PID\n' >> "$out/mn.runtime.err"
  fi
  set +e
  wait "$time_pid"
  rc=$?
  set -e
  if [[ "$rc" == "143" && -n "$server_pid" && -s "$out/owned-stop.pid" ]] &&
     [[ "$(cat "$out/owned-stop.pid")" == "$server_pid" ]]; then
    printf 'normalized owned SIGTERM status 143 to 0 for pid=%s\n' \
      "$server_pid" >> "$out/mn.runtime.err"
    rc=0
  fi
  cat "$out/mn.runtime.err" "$out/mn.time" > "$out/mn.err"
  [[ -n "$server_pid" ]] || rc=125
  printf '%s\n' "$rc" > "$out/status"
  exit "$rc"
fi

[[ "$DEGREES" == "full 24 16 8 4 1" ]] || {
  echo "Formal co-location control requires DEGREES='full 24 16 8 4 1'" >&2; exit 2;
}
[[ "$REPEATS" == "5" && "$WARMUPS" == "1" ]] || {
  echo "Formal co-location control requires five repeats and one warmup" >&2; exit 2;
}
[[ "$THREADS" == "10" && "$QUERY_CONTEXTS" == "10" && "$COROUTINES" == "2" ]] || {
  echo "Formal co-location control requires T=10, query-contexts=10, C=2" >&2; exit 2;
}
[[ "$EF_SEARCH" == "200" ]] || {
  echo "Formal co-location control requires ef=200" >&2; exit 2;
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s degrees=%s repeats=%s warmups=%s T=%s Q=%s C=%s ef=%s mn=%s port=%s\n' \
    "$CAMPAIGN_ID" "$DEGREES" "$REPEATS" "$WARMUPS" "$THREADS" \
    "$QUERY_CONTEXTS" "$COROUTINES" "$EF_SEARCH" "$MEMORY_NODE" "$PORT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing co-location binary: $GB_BIN" >&2; exit 2; }
GB_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Co-location binary SHA mismatch: $GB_SHA" >&2; exit 2;
}
for path in "$QUERY_PATH" "$GT_PATH"; do
  [[ -s "$path" ]] || { echo "Missing co-location input: $path" >&2; exit 2; }
done
REMOTE_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$GB_BIN_R' | cut -d ' ' -f1")
[[ "$REMOTE_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Co-location MN binary SHA mismatch: $REMOTE_SHA" >&2; exit 2;
}
REMOTE_DUMP_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$INDEX_DUMP' | cut -d ' ' -f1")
[[ "$REMOTE_DUMP_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "Missing DEEP1M index dump" >&2; exit 2; }
QUERY_SHA=$(sha256sum "$QUERY_PATH" | awk '{print $1}')
GT_SHA=$(sha256sum "$GT_PATH" | awk '{print $1}')
if [[ -d "$OUT_ROOT" && -n "$(find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Refusing non-empty OUT_ROOT: $OUT_ROOT" >&2
  exit 2
fi
mkdir -p "$OUT_ROOT/raw" "$OUT_ROOT/query_pools"

python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
  --query "$QUERY_PATH" --groundtruth "$GT_PATH" --dataset DEEP1M \
  --method SlabWalk --metric l2 --limit 10000 \
  --out "$OUT_ROOT/query_pools/deep1m_slabwalk.json" >/dev/null

RUNNER_SHA=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
SUMMARIZER_SHA=$(sha256sum "$SCRIPT_DIR/summarize_vldb_colocation_control.py" | awk '{print $1}')
FINGERPRINT_SHA=$(sha256sum "$SCRIPT_DIR/fingerprint_query_pool.py" | awk '{print $1}')
python3 - "$OUT_ROOT/campaign.json" "$CAMPAIGN_ID" "$GB_SHA" "$RUNNER_SHA" \
  "$SUMMARIZER_SHA" "$FINGERPRINT_SHA" "$MEMORY_NODE" "$PORT" \
  "$INDEX_REGION_BYTES" "$LAVD_REGION_BYTES" "$REMOTE_DUMP_SHA" \
  "$QUERY_SHA" "$GT_SHA" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(path_s, campaign_id, binary_sha, runner_sha, summarizer_sha, fingerprint_sha,
 memory_node, port, index_region_bytes, lavd_region_bytes, dump_sha,
 query_sha, groundtruth_sha) = sys.argv[1:]
protocol = {
    "binary_sha256": binary_sha,
    "dataset": "DEEP1M",
    "degrees": ["full", "24", "16", "8", "4", "1"],
    "inline_codes": {"full": 32, "24": 24, "16": 16, "8": 8, "4": 4, "1": 1},
    "m_max0": 32,
    "code": "sq8",
    "repeats": 5,
    "warmups": 1,
    "threads": 10,
    "query_contexts": 10,
    "coroutines": 2,
    "ef_search": 200,
    "top_k": 10,
    "query_suffix": "uniform",
    "queries_per_run": 10000,
    "memory_node": memory_node,
    "tcp_port": int(port),
    "index_region_bytes": int(index_region_bytes),
    "lavd_region_bytes": int(lavd_region_bytes),
    "index_dump_sha256": dump_sha,
    "query_sha256": query_sha,
    "groundtruth_sha256": groundtruth_sha,
    "runner_sha256": runner_sha,
    "summarizer_sha256": summarizer_sha,
    "fingerprint_tool_sha256": fingerprint_sha,
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
Path(path_s).write_text(json.dumps({
    "campaign_id": campaign_id,
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "protocol_fingerprint": fingerprint,
    "protocol": protocol,
}, indent=2, sort_keys=True) + "\n")
PY
PROTOCOL_FINGERPRINT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol_fingerprint"])' "$OUT_ROOT/campaign.json")

verify_remote_pid() {
  local remote_out=$1
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "test -s '$remote_out/server.pid' -a -s '$remote_out/server.exe'; \
     pid=\$(cat '$remote_out/server.pid'); expected=\$(cat '$remote_out/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_remote() {
  local remote_out=$1
  if verify_remote_pid "$remote_out"; then
    ssh -o LogLevel=ERROR "$MEMORY_NODE" \
      "pid=\$(cat '$remote_out/server.pid'); \
       printf '%s\\n' \"\$pid\" > '$remote_out/owned-stop.pid'; \
       kill \"\$pid\" 2>/dev/null || true" || true
  fi
}

cleanup_active() {
  if [[ -n "$ACTIVE_REMOTE_OUT" ]]; then
    stop_remote "$ACTIVE_REMOTE_OUT"
    ACTIVE_REMOTE_OUT=""
  fi
}
trap cleanup_active EXIT INT TERM

OBS_CN_BINARY_SHA=""
OBS_MN_BINARY_SHA=""
OBS_INDEX_DUMP_SHA=""
OBS_QUERY_SHA=""
OBS_GROUNDTRUTH_SHA=""

verify_immutable_inputs() {
  local remote_hashes
  OBS_CN_BINARY_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
  OBS_QUERY_SHA=$(sha256sum "$QUERY_PATH" | awk '{print $1}')
  OBS_GROUNDTRUTH_SHA=$(sha256sum "$GT_PATH" | awk '{print $1}')
  remote_hashes=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "sha256sum '$GB_BIN_R' '$INDEX_DUMP' | awk '{print \$1}'")
  OBS_MN_BINARY_SHA=$(printf '%s\n' "$remote_hashes" | sed -n '1p')
  OBS_INDEX_DUMP_SHA=$(printf '%s\n' "$remote_hashes" | sed -n '2p')
  if [[ "$OBS_CN_BINARY_SHA" != "$EXPECTED_BINARY_SHA" ||
        "$OBS_MN_BINARY_SHA" != "$EXPECTED_BINARY_SHA" ||
        "$OBS_INDEX_DUMP_SHA" != "$REMOTE_DUMP_SHA" ||
        "$OBS_QUERY_SHA" != "$QUERY_SHA" ||
        "$OBS_GROUNDTRUTH_SHA" != "$GT_SHA" ]]; then
    echo "Co-location immutable input drift before cell" >&2
    return 2
  fi
}

validate_cell() {
  local cell=$1 degree=$2 kind=$3 repeat=$4
  PYTHONPATH="$SCRIPT_DIR" python3 - "$OUT_ROOT" "$cell" "$degree" "$kind" \
    "$repeat" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" "$EXPECTED_BINARY_SHA" <<'PY'
import sys
from pathlib import Path
import summarize_vldb_colocation_control as summary

root, cell, degree, kind, repeat, campaign, fingerprint, binary = sys.argv[1:]
row, _ = summary.load_cell(
    Path(root), degree, kind, int(repeat), campaign, fingerprint, binary
)
assert Path(root) / row["source_json"] == Path(cell) / "cn.json"
PY
}

run_one() {
  local degree=$1 kind=$2 repeat=$3
  local slug=$degree
  local cell="$OUT_ROOT/raw/$slug/${kind}_r${repeat}"
  local tag="deep1m_coloc_${slug}_${kind}_r${repeat}"
  local remote_out="/tmp/${CAMPAIGN_ID}_${tag}"
  verify_immutable_inputs
  [[ ! -e "$cell" ]] || { echo "Refusing existing co-location cell: $cell" >&2; exit 2; }
  mkdir -p "$cell/mn"
  ACTIVE_REMOTE_OUT="$remote_out"
  scp -q "$SCRIPT_PATH" "$MEMORY_NODE:$REMOTE_RUNNER"
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "rm -rf '$remote_out'; mkdir -p '$remote_out'; \
     nohup bash '$REMOTE_RUNNER' --memory-node '$GB_BIN_R' '$remote_out' '$GB_LIB' \
       '$PORT' '$INDEX_REGION_BYTES' >'$remote_out/launcher.out' \
       2>'$remote_out/launcher.err' < /dev/null &"
  sleep 4
  verify_remote_pid "$remote_out" || {
    echo "Co-location MN failed to start: $tag" >&2
    return 1
  }

  local -a env_args=(SHINE_CRANE=1 GB_BITMAP_DEDUP=1
    SHINE_LAVD_HOT_COLD_BATCH=1 SHINE_LAVD_SELFTEST=1
    SHINE_LAVD_COLOC_SELFTEST=1 GB_QUERY_LATENCY=1)
  if [[ "$degree" != "full" ]]; then
    env_args+=(SHINE_LAVD_COLOC_DEGREE="$degree")
  fi
  local -a command=("$GB_BIN" --servers "$MEMORY_NODE" --initiator --port "$PORT"
    --index-region-bytes "$INDEX_REGION_BYTES" --lavd-region-bytes "$LAVD_REGION_BYTES"
    --data-path "$DATA_PATH" --threads "$THREADS" --coroutines "$COROUTINES"
    --query-contexts "$QUERY_CONTEXTS" --query-suffix "$QUERY_SUFFIX" --load-index
    --ef-search "$EF_SEARCH" --ef-construction 100 --m 16 --k 10 --label "$tag"
    --spec-k 1 --lavd 8)
  python3 - "$cell/manifest.json" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" \
    "$degree" "$kind" "$repeat" "$EXPECTED_BINARY_SHA" \
    "$OBS_CN_BINARY_SHA" "$OBS_MN_BINARY_SHA" "$OBS_INDEX_DUMP_SHA" \
    "$OBS_QUERY_SHA" "$OBS_GROUNDTRUTH_SHA" \
    "$(printf '%s\034' "${env_args[@]}")" "${command[@]}" <<'PY'
import json, sys
from pathlib import Path
(path, campaign, fingerprint, degree, kind, repeat, binary,
 cn_binary, mn_binary, index_dump, query, groundtruth, env_blob,
 *command) = sys.argv[1:]
environment = {}
for item in env_blob.rstrip("\x1c").split("\x1c"):
    key, value = item.split("=", 1)
    environment[key] = value
Path(path).write_text(json.dumps({
    "campaign_id": campaign,
    "protocol_fingerprint": fingerprint,
    "degree": degree,
    "run_kind": kind,
    "repeat": int(repeat),
    "binary_sha256": binary,
    "observed_inputs": {
        "cn_binary": cn_binary,
        "mn_binary": mn_binary,
        "index_dump": index_dump,
        "query": query,
        "groundtruth": groundtruth,
    },
    "environment": environment,
    "command": command,
}, indent=2, sort_keys=True) + "\n")
PY

  set +e
  timeout "$TIMEOUT_S" env LD_LIBRARY_PATH="$GB_LIB" "${env_args[@]}" \
    numactl --preferred=1 "${command[@]}" > "$cell/cn.json" 2> "$cell/cn.err"
  rc=$?
  set -e
  stop_remote "$remote_out"
  ACTIVE_REMOTE_OUT=""
  for _ in $(seq 1 30); do
    ssh -o LogLevel=ERROR "$MEMORY_NODE" "test -e '$remote_out/status'" && break
    sleep 1
  done
  scp -q "$MEMORY_NODE:$remote_out/mn.err" "$cell/mn/mn.err" || true
  scp -q "$MEMORY_NODE:$remote_out/status" "$cell/mn/status" || true
  if [[ $rc -ne 0 ]]; then
    echo "Co-location CN failed: $tag rc=$rc" >&2
    tail -80 "$cell/cn.err" >&2 || true
    return "$rc"
  fi
  if ! grep -Eq '\[LAVD\]\[selftest\] checked=64 fails=0([[:space:]]|$)' "$cell/cn.err"; then
    echo "Co-location layout selftest did not pass: $tag" >&2
    grep -E '\[LAVD\]\[selftest\]' "$cell/cn.err" >&2 || true
    return 1
  fi
  validate_cell "$cell" "$degree" "$kind" "$repeat"
  printf 'complete degree=%s kind=%s repeat=%s\n' "$degree" "$kind" "$repeat"
}

for degree in $DEGREES; do
  run_one "$degree" warmup 0
done
for ((repeat = 0; repeat < REPEATS; ++repeat)); do
  for degree in $DEGREES; do
    run_one "$degree" measure "$repeat"
  done
done

python3 "$SCRIPT_DIR/summarize_vldb_colocation_control.py" \
  --campaign "$OUT_ROOT" --out "$OUT_ROOT/summary" \
  --expected-binary-sha "$EXPECTED_BINARY_SHA"
printf 'Formal co-location campaign written to %s\n' "$OUT_ROOT"
