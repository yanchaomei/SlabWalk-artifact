#!/usr/bin/env bash
# Run a formal, fixed-pool SHINE cache-ratio control on SIFT1M.
set -euo pipefail

SCRIPT_PATH=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname -- "$SCRIPT_PATH")
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/cache_control_sift1_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-cache-control-$(date -u +%Y%m%dT%H%M%SZ)}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
CONDITIONS=${CONDITIONS:-"off c5 c20 c50"}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
THREADS=${THREADS:-1}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-1}
COROUTINES=${COROUTINES:-8}
EF_SEARCH=${EF_SEARCH:-100}
MEMORY_NODE=${MEMORY_NODE:-skv-node4}
PORT=${PORT:-1310}
TIMEOUT_S=${TIMEOUT_S:-900}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
DRY_RUN=${DRY_RUN:-0}
REMOTE_RUNNER=/tmp/vldb_cache_control_runner_${PORT}.sh
ACTIVE_REMOTE_OUT=""

DATA_PATH=$GB_DATA/sift1m
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
  cat "$out/mn.runtime.err" "$out/mn.time" > "$out/mn.err"
  [[ -n "$server_pid" ]] || rc=125
  printf '%s\n' "$rc" > "$out/status"
  exit "$rc"
fi

[[ "$CONDITIONS" == "off c5 c20 c50" ]] || {
  echo "Formal cache control requires CONDITIONS='off c5 c20 c50'" >&2; exit 2;
}
[[ "$REPEATS" == "5" && "$WARMUPS" == "1" ]] || {
  echo "Formal cache control requires five repeats and one warmup" >&2; exit 2;
}
[[ "$THREADS" == "1" && "$QUERY_CONTEXTS" == "1" && "$COROUTINES" == "8" ]] || {
  echo "Formal cache control requires T=1, query-contexts=1, C=8" >&2; exit 2;
}
[[ "$EF_SEARCH" == "100" ]] || { echo "Formal cache control requires ef=100" >&2; exit 2; }

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s conditions=%s repeats=%s warmups=%s T=%s Q=%s C=%s ef=%s mn=%s port=%s\n' \
    "$CAMPAIGN_ID" "$CONDITIONS" "$REPEATS" "$WARMUPS" "$THREADS" \
    "$QUERY_CONTEXTS" "$COROUTINES" "$EF_SEARCH" "$MEMORY_NODE" "$PORT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing cache-control binary: $GB_BIN" >&2; exit 2; }
GB_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Cache-control binary SHA mismatch: $GB_SHA" >&2; exit 2;
}
for path in "$QUERY_PATH" "$GT_PATH"; do
  [[ -s "$path" ]] || { echo "Missing cache-control input: $path" >&2; exit 2; }
done
REMOTE_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$GB_BIN_R' | cut -d ' ' -f1")
[[ "$REMOTE_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Cache-control MN binary SHA mismatch: $REMOTE_SHA" >&2; exit 2;
}
REMOTE_DUMP_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$INDEX_DUMP' | cut -d ' ' -f1")
[[ "$REMOTE_DUMP_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "Missing SIFT1M index dump" >&2; exit 2; }
if [[ -d "$OUT_ROOT" && -n "$(find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Refusing non-empty OUT_ROOT: $OUT_ROOT" >&2
  exit 2
fi
mkdir -p "$OUT_ROOT/raw" "$OUT_ROOT/query_pools"

python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
  --query "$QUERY_PATH" --groundtruth "$GT_PATH" --dataset SIFT1M \
  --method SHINE --metric l2 --limit 10000 \
  --out "$OUT_ROOT/query_pools/sift1m_shine.json" >/dev/null

RUNNER_SHA=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
SUMMARIZER_SHA=$(sha256sum "$SCRIPT_DIR/summarize_vldb_cache_control.py" | awk '{print $1}')
FINGERPRINT_SHA=$(sha256sum "$SCRIPT_DIR/fingerprint_query_pool.py" | awk '{print $1}')
python3 - "$OUT_ROOT/campaign.json" "$CAMPAIGN_ID" "$GB_SHA" "$RUNNER_SHA" \
  "$SUMMARIZER_SHA" "$FINGERPRINT_SHA" "$MEMORY_NODE" "$PORT" \
  "$INDEX_REGION_BYTES" "$REMOTE_DUMP_SHA" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(path_s, campaign_id, binary_sha, runner_sha, summarizer_sha, fingerprint_sha,
 memory_node, port, region_bytes, dump_sha) = sys.argv[1:]
protocol = {
    "binary_sha256": binary_sha,
    "dataset": "SIFT1M",
    "conditions": ["off", "c5", "c20", "c50"],
    "repeats": 5,
    "warmups": 1,
    "threads": 1,
    "query_contexts": 1,
    "coroutines": 8,
    "ef_search": 100,
    "top_k": 10,
    "query_suffix": "uniform",
    "queries_per_run": 10000,
    "memory_node": memory_node,
    "tcp_port": int(port),
    "index_region_bytes": int(region_bytes),
    "index_dump_sha256": dump_sha,
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
      "pid=\$(cat '$remote_out/server.pid'); kill \"\$pid\" 2>/dev/null || true" || true
  fi
}

cleanup_active() {
  if [[ -n "$ACTIVE_REMOTE_OUT" ]]; then
    stop_remote "$ACTIVE_REMOTE_OUT"
    ACTIVE_REMOTE_OUT=""
  fi
}
trap cleanup_active EXIT INT TERM

condition_flags() {
  case "$1" in
    off) return 0 ;;
    c5) printf '%s\n' '--cache' '--cache-ratio' '5' ;;
    c20) printf '%s\n' '--cache' '--cache-ratio' '20' ;;
    c50) printf '%s\n' '--cache' '--cache-ratio' '50' ;;
    *) echo "Unknown cache condition: $1" >&2; return 2 ;;
  esac
}

validate_cell() {
  local cell=$1 condition=$2 kind=$3 repeat=$4
  PYTHONPATH="$SCRIPT_DIR" python3 - "$OUT_ROOT" "$cell" "$condition" "$kind" \
    "$repeat" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" "$EXPECTED_BINARY_SHA" <<'PY'
import sys
from pathlib import Path
import summarize_vldb_cache_control as summary

root, cell, condition, kind, repeat, campaign, fingerprint, binary = sys.argv[1:]
row, _ = summary.load_cell(
    Path(root), condition, kind, int(repeat), campaign, fingerprint, binary
)
assert Path(root) / row["source_json"] == Path(cell) / "cn.json"
PY
}

run_one() {
  local condition=$1 kind=$2 repeat=$3
  local cell="$OUT_ROOT/raw/$condition/${kind}_r${repeat}"
  local tag="sift1m_cache_${condition}_${kind}_r${repeat}"
  local remote_out="/tmp/${CAMPAIGN_ID}_${tag}"
  [[ ! -e "$cell" ]] || { echo "Refusing existing cache-control cell: $cell" >&2; exit 2; }
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
    echo "Cache-control MN failed to start: $tag" >&2
    return 1
  }

  local -a cache_flags=()
  while IFS= read -r token; do cache_flags+=("$token"); done < <(condition_flags "$condition")
  local -a command=("$GB_BIN" --servers "$MEMORY_NODE" --initiator --port "$PORT"
    --index-region-bytes "$INDEX_REGION_BYTES" --data-path "$DATA_PATH"
    --threads "$THREADS" --coroutines "$COROUTINES" --query-contexts "$QUERY_CONTEXTS"
    --query-suffix "$QUERY_SUFFIX" --load-index --ef-search "$EF_SEARCH"
    --ef-construction 100 --m 16 --k 10 --label "$tag" --spec-k 1 --lavd 0)
  command+=("${cache_flags[@]}")
  python3 - "$cell/manifest.json" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" \
    "$condition" "$kind" "$repeat" "$EXPECTED_BINARY_SHA" "${command[@]}" <<'PY'
import json, sys
from pathlib import Path
path, campaign, fingerprint, condition, kind, repeat, binary, *command = sys.argv[1:]
Path(path).write_text(json.dumps({
    "campaign_id": campaign,
    "protocol_fingerprint": fingerprint,
    "condition": condition,
    "run_kind": kind,
    "repeat": int(repeat),
    "binary_sha256": binary,
    "command": command,
}, indent=2, sort_keys=True) + "\n")
PY

  set +e
  timeout "$TIMEOUT_S" env LD_LIBRARY_PATH="$GB_LIB" GB_QUERY_LATENCY=1 \
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
    echo "Cache-control CN failed: $tag rc=$rc" >&2
    tail -80 "$cell/cn.err" >&2 || true
    return "$rc"
  fi
  validate_cell "$cell" "$condition" "$kind" "$repeat"
  printf 'complete condition=%s kind=%s repeat=%s\n' "$condition" "$kind" "$repeat"
}

for condition in $CONDITIONS; do
  run_one "$condition" warmup 0
done
for ((repeat = 0; repeat < REPEATS; ++repeat)); do
  for condition in $CONDITIONS; do
    run_one "$condition" measure "$repeat"
  done
done

python3 "$SCRIPT_DIR/summarize_vldb_cache_control.py" \
  --campaign "$OUT_ROOT" --out "$OUT_ROOT/summary" \
  --expected-binary-sha "$EXPECTED_BINARY_SHA"
printf 'Formal cache-control campaign written to %s\n' "$OUT_ROOT"
