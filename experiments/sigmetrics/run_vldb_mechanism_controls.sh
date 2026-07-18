#!/usr/bin/env bash
# Formal five-repeat controls for degree-bounded materialization and resident descent.
set -euo pipefail

SCRIPT_PATH=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname -- "$SCRIPT_PATH")
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/mechanism_controls_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-mechanism-controls-$(date -u +%Y%m%dT%H%M%SZ)}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
BUDGET_FRACTIONS=${BUDGET_FRACTIONS:-"f05 f10 f25 f50 f75 full"}
RESIDENT_MODES=${RESIDENT_MODES:-"remote resident"}
RESIDENT_EFS=${RESIDENT_EFS:-"50 100 200"}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
MEMORY_NODE=${MEMORY_NODE:-skv-node5}
PORT=${PORT:-1316}
TIMEOUT_S=${TIMEOUT_S:-1200}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
LAVD_REGION_BYTES=${LAVD_REGION_BYTES:-17179869184}
DRY_RUN=${DRY_RUN:-0}
REMOTE_RUNNER=/tmp/vldb_mechanism_controls_runner_${PORT}.sh
ACTIVE_REMOTE_OUT=""

GIST_PATH=$GB_DATA/gist200k
SIFT_PATH=$GB_DATA/sift1m
GIST_QUERY=$GIST_PATH/queries/query-uniform.fbin
GIST_GT=$GIST_PATH/queries/groundtruth-uniform.bin
SIFT_QUERY=$SIFT_PATH/queries/query-uniform.fbin
SIFT_GT=$SIFT_PATH/queries/groundtruth-uniform.bin
GIST_DUMP=$GIST_PATH/dump/index_m32_efc200_node1_of1.dat
SIFT_DUMP=$SIFT_PATH/dump/index_m16_efc100_node1_of1.dat

if [[ "${1:-}" == "--memory-node" ]]; then
  shift
  bin=$1
  out=$2
  lib=$3
  port=$4
  index_region_bytes=$5
  lavd_region_bytes=$6
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
    --lavd-region-bytes "$lavd_region_bytes" \
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

[[ "$BUDGET_FRACTIONS" == "f05 f10 f25 f50 f75 full" ]] || {
  echo "Formal mechanism control requires the complete budget matrix" >&2; exit 2;
}
[[ "$RESIDENT_MODES" == "remote resident" && "$RESIDENT_EFS" == "50 100 200" ]] || {
  echo "Formal mechanism control requires remote/resident at ef=50/100/200" >&2; exit 2;
}
[[ "$REPEATS" == "5" && "$WARMUPS" == "1" ]] || {
  echo "Formal mechanism control requires five repeats and one warmup" >&2; exit 2;
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s budget=%s resident=%s ef=%s repeats=%s warmups=%s mn=%s port=%s\n' \
    "$CAMPAIGN_ID" "$BUDGET_FRACTIONS" "$RESIDENT_MODES" "$RESIDENT_EFS" \
    "$REPEATS" "$WARMUPS" "$MEMORY_NODE" "$PORT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing mechanism-control binary: $GB_BIN" >&2; exit 2; }
GB_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Mechanism-control binary SHA mismatch: $GB_SHA" >&2; exit 2;
}
for path in "$GIST_QUERY" "$GIST_GT" "$SIFT_QUERY" "$SIFT_GT"; do
  [[ -s "$path" ]] || { echo "Missing mechanism-control input: $path" >&2; exit 2; }
done
REMOTE_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$GB_BIN_R' | cut -d ' ' -f1")
[[ "$REMOTE_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Mechanism-control MN binary SHA mismatch: $REMOTE_SHA" >&2; exit 2;
}
GIST_DUMP_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$GIST_DUMP' | cut -d ' ' -f1")
SIFT_DUMP_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$SIFT_DUMP' | cut -d ' ' -f1")
[[ "$GIST_DUMP_SHA" =~ ^[0-9a-f]{64}$ && "$SIFT_DUMP_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "Missing mechanism-control index dump" >&2; exit 2;
}
GIST_QUERY_SHA=$(sha256sum "$GIST_QUERY" | awk '{print $1}')
GIST_GT_SHA=$(sha256sum "$GIST_GT" | awk '{print $1}')
SIFT_QUERY_SHA=$(sha256sum "$SIFT_QUERY" | awk '{print $1}')
SIFT_GT_SHA=$(sha256sum "$SIFT_GT" | awk '{print $1}')
for sha in "$GIST_QUERY_SHA" "$GIST_GT_SHA" "$SIFT_QUERY_SHA" "$SIFT_GT_SHA"; do
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Missing mechanism-control query input" >&2; exit 2;
  }
done
if [[ -d "$OUT_ROOT" && -n "$(find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Refusing non-empty OUT_ROOT: $OUT_ROOT" >&2
  exit 2
fi
mkdir -p "$OUT_ROOT/raw/budget" "$OUT_ROOT/raw/resident" "$OUT_ROOT/query_pools"

python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
  --query "$GIST_QUERY" --groundtruth "$GIST_GT" --dataset GIST200K \
  --method SlabWalk --metric l2 --limit 1000 \
  --out "$OUT_ROOT/query_pools/gist200k_slabwalk.json" >/dev/null
python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
  --query "$SIFT_QUERY" --groundtruth "$SIFT_GT" --dataset SIFT1M \
  --method SlabWalk --metric l2 --limit 10000 \
  --out "$OUT_ROOT/query_pools/sift1m_slabwalk.json" >/dev/null

RUNNER_SHA=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
SUMMARIZER_SHA=$(sha256sum "$SCRIPT_DIR/summarize_vldb_mechanism_controls.py" | awk '{print $1}')
FINGERPRINT_SHA=$(sha256sum "$SCRIPT_DIR/fingerprint_query_pool.py" | awk '{print $1}')
python3 - "$OUT_ROOT/campaign.json" "$CAMPAIGN_ID" "$GB_SHA" "$RUNNER_SHA" \
  "$SUMMARIZER_SHA" "$FINGERPRINT_SHA" "$MEMORY_NODE" "$PORT" \
  "$INDEX_REGION_BYTES" "$LAVD_REGION_BYTES" "$GIST_DUMP_SHA" "$SIFT_DUMP_SHA" \
  "$GIST_QUERY_SHA" "$GIST_GT_SHA" "$SIFT_QUERY_SHA" "$SIFT_GT_SHA" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(path_s, campaign_id, binary_sha, runner_sha, summarizer_sha, fingerprint_sha,
 memory_node, port, index_region_bytes, lavd_region_bytes, gist_dump_sha,
 sift_dump_sha, gist_query_sha, gist_gt_sha, sift_query_sha,
 sift_gt_sha) = sys.argv[1:]
protocol = {
    "binary_sha256": binary_sha,
    "budget": {
        "dataset": "GIST200K", "fractions": ["f05", "f10", "f25", "f50", "f75", "full"],
        "fraction_values": {"f05": 0.05, "f10": 0.10, "f25": 0.25,
                            "f50": 0.50, "f75": 0.75, "full": 1.0},
        "threads": 16, "query_contexts": 16, "coroutines": 8,
        "ef_search": 100, "ef_construction": 200, "m": 32,
        "queries_per_run": 1000, "hotset": "indeg",
    },
    "resident": {
        "dataset": "SIFT1M", "modes": ["remote", "resident"],
        "ef_values": [50, 100, 200], "threads": 1, "query_contexts": 1,
        "coroutines": 8, "ef_construction": 100, "m": 16,
        "queries_per_run": 10000,
    },
    "repeats": 5, "warmups": 1, "top_k": 10,
    "query_suffix": "uniform", "scoring_code": "sq8",
    "record_layout": "packed_variable", "memory_node": memory_node,
    "tcp_port": int(port), "index_region_bytes": int(index_region_bytes),
    "lavd_region_bytes": int(lavd_region_bytes),
    "gist_index_dump_sha256": gist_dump_sha,
    "sift_index_dump_sha256": sift_dump_sha,
    "gist_query_sha256": gist_query_sha,
    "gist_groundtruth_sha256": gist_gt_sha,
    "sift_query_sha256": sift_query_sha,
    "sift_groundtruth_sha256": sift_gt_sha,
    "runner_sha256": runner_sha, "summarizer_sha256": summarizer_sha,
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
  local control=$1 query_path groundtruth_path dump_path
  local expected_dump expected_query expected_groundtruth remote_hashes
  if [[ "$control" == "budget" ]]; then
    query_path=$GIST_QUERY
    groundtruth_path=$GIST_GT
    dump_path=$GIST_DUMP
    expected_dump=$GIST_DUMP_SHA
    expected_query=$GIST_QUERY_SHA
    expected_groundtruth=$GIST_GT_SHA
  else
    query_path=$SIFT_QUERY
    groundtruth_path=$SIFT_GT
    dump_path=$SIFT_DUMP
    expected_dump=$SIFT_DUMP_SHA
    expected_query=$SIFT_QUERY_SHA
    expected_groundtruth=$SIFT_GT_SHA
  fi

  OBS_CN_BINARY_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
  OBS_QUERY_SHA=$(sha256sum "$query_path" | awk '{print $1}')
  OBS_GROUNDTRUTH_SHA=$(sha256sum "$groundtruth_path" | awk '{print $1}')
  remote_hashes=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "sha256sum '$GB_BIN_R' '$dump_path' | awk '{print \$1}'")
  OBS_MN_BINARY_SHA=$(printf '%s\n' "$remote_hashes" | sed -n '1p')
  OBS_INDEX_DUMP_SHA=$(printf '%s\n' "$remote_hashes" | sed -n '2p')
  if [[ "$OBS_CN_BINARY_SHA" != "$EXPECTED_BINARY_SHA" ||
        "$OBS_MN_BINARY_SHA" != "$EXPECTED_BINARY_SHA" ||
        "$OBS_INDEX_DUMP_SHA" != "$expected_dump" ||
        "$OBS_QUERY_SHA" != "$expected_query" ||
        "$OBS_GROUNDTRUTH_SHA" != "$expected_groundtruth" ]]; then
    echo "Mechanism-control immutable input drift before $control cell" >&2
    return 2
  fi
}

fraction_value() {
  case "$1" in
    f05) printf '0.05\n' ;;
    f10) printf '0.1\n' ;;
    f25) printf '0.25\n' ;;
    f50) printf '0.5\n' ;;
    f75) printf '0.75\n' ;;
    full) printf '1.0\n' ;;
    *) return 2 ;;
  esac
}

validate_cell() {
  local control=$1 key=$2 ef=$3 kind=$4 repeat=$5
  PYTHONPATH="$SCRIPT_DIR" python3 - "$OUT_ROOT" "$control" "$key" "$ef" \
    "$kind" "$repeat" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" \
    "$EXPECTED_BINARY_SHA" <<'PY'
import sys
from pathlib import Path
import summarize_vldb_mechanism_controls as summary

root, control, key, ef, kind, repeat, campaign, fingerprint, binary = sys.argv[1:]
summary.load_cell(
    Path(root), control, key, int(ef), kind, int(repeat), campaign,
    fingerprint, binary,
)
PY
}

run_one() {
  local control=$1 key=$2 ef=$3 kind=$4 repeat=$5
  local cell tag dataset data_path threads contexts coroutines efc m expected_queries crane
  local -a env_args command
  if [[ "$control" == "budget" ]]; then
    dataset=gist200k
    data_path=$GIST_PATH
    threads=16
    contexts=16
    coroutines=8
    ef=100
    efc=200
    m=32
    expected_queries=1000
    crane=1
    cell="$OUT_ROOT/raw/budget/$key/${kind}_r${repeat}"
    tag="gist200k_budget_${key}_${kind}_r${repeat}"
    env_args=(SHINE_LAVD_HOTSET=indeg SHINE_LAVD_NATIVE_PACKED_WRITE=1
      SHINE_LAVD_VARBLOCK=1 SHINE_CRANE=1 GB_BITMAP_DEDUP=1
      GB_QUERY_LATENCY=1)
    if [[ "$key" != "full" ]]; then
      env_args+=(SHINE_LAVD_BUDGET="$(fraction_value "$key")")
    fi
  else
    dataset=sift1m
    data_path=$SIFT_PATH
    threads=1
    contexts=1
    coroutines=8
    efc=100
    m=16
    expected_queries=10000
    [[ "$key" == "resident" ]] && crane=1 || crane=0
    cell="$OUT_ROOT/raw/resident/$key/ef${ef}/${kind}_r${repeat}"
    tag="sift1m_resident_${key}_ef${ef}_${kind}_r${repeat}"
    env_args=(SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1
      SHINE_CRANE="$crane" GB_BITMAP_DEDUP=1 GB_QUERY_LATENCY=1)
  fi

  verify_immutable_inputs "$control"
  local remote_out="/tmp/${CAMPAIGN_ID}_${tag}"
  [[ ! -e "$cell" ]] || { echo "Refusing existing mechanism cell: $cell" >&2; exit 2; }
  mkdir -p "$cell/mn"
  ACTIVE_REMOTE_OUT="$remote_out"
  scp -q "$SCRIPT_PATH" "$MEMORY_NODE:$REMOTE_RUNNER"
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "rm -rf '$remote_out'; mkdir -p '$remote_out'; \
     nohup bash '$REMOTE_RUNNER' --memory-node '$GB_BIN_R' '$remote_out' '$GB_LIB' \
       '$PORT' '$INDEX_REGION_BYTES' '$LAVD_REGION_BYTES' >'$remote_out/launcher.out' \
       2>'$remote_out/launcher.err' < /dev/null &"
  sleep 4
  verify_remote_pid "$remote_out" || {
    echo "Mechanism-control MN failed to start: $tag" >&2
    return 1
  }

  command=("$GB_BIN" --servers "$MEMORY_NODE" --initiator --port "$PORT"
    --index-region-bytes "$INDEX_REGION_BYTES" --lavd-region-bytes "$LAVD_REGION_BYTES"
    --data-path "$data_path" --threads "$threads" --coroutines "$coroutines"
    --query-contexts "$contexts" --query-suffix uniform --load-index
    --ef-search "$ef" --ef-construction "$efc" --m "$m" --k 10
    --label "$tag" --spec-k 1 --lavd 8)
  python3 - "$cell/manifest.json" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" \
    "$control" "$key" "$ef" "$kind" "$repeat" "$EXPECTED_BINARY_SHA" \
    "$OBS_CN_BINARY_SHA" "$OBS_MN_BINARY_SHA" "$OBS_INDEX_DUMP_SHA" \
    "$OBS_QUERY_SHA" "$OBS_GROUNDTRUTH_SHA" \
    "$(printf '%s\034' "${env_args[@]}")" "${command[@]}" <<'PY'
import json, sys
from pathlib import Path
(path, campaign, fingerprint, control, key, ef, kind, repeat, binary,
 cn_binary, mn_binary, index_dump, query, groundtruth, env_blob,
 *command) = sys.argv[1:]
environment = {}
for item in env_blob.rstrip("\x1c").split("\x1c"):
    name, value = item.split("=", 1)
    environment[name] = value
Path(path).write_text(json.dumps({
    "campaign_id": campaign, "protocol_fingerprint": fingerprint,
    "control": control, "key": key, "ef": int(ef), "run_kind": kind,
    "repeat": int(repeat), "binary_sha256": binary,
    "observed_inputs": {
        "cn_binary": cn_binary, "mn_binary": mn_binary,
        "index_dump": index_dump, "query": query,
        "groundtruth": groundtruth,
    },
    "environment": environment, "command": command,
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
    echo "Mechanism-control CN failed: $tag rc=$rc" >&2
    tail -80 "$cell/cn.err" >&2 || true
    return "$rc"
  fi
  if ! grep -Fq '[LAVD][native] packed addressing restored from descriptor:' "$cell/cn.err"; then
    echo "Mechanism-control descriptor readback did not pass: $tag" >&2
    return 1
  fi
  if ! grep -Fq 'LAVD_PHYSICAL_ACCOUNTING ' "$cell/cn.err"; then
    echo "Mechanism-control physical accounting is missing: $tag" >&2
    return 1
  fi
  validate_cell "$control" "$key" "$ef" "$kind" "$repeat"
  printf 'complete control=%s key=%s ef=%s kind=%s repeat=%s queries=%s\n' \
    "$control" "$key" "$ef" "$kind" "$repeat" "$expected_queries"
}

for key in $BUDGET_FRACTIONS; do
  run_one budget "$key" 100 warmup 0
done
for mode in $RESIDENT_MODES; do
  for ef in $RESIDENT_EFS; do
    run_one resident "$mode" "$ef" warmup 0
  done
done
for ((repeat = 0; repeat < REPEATS; ++repeat)); do
  for key in $BUDGET_FRACTIONS; do
    run_one budget "$key" 100 measure "$repeat"
  done
  for mode in $RESIDENT_MODES; do
    for ef in $RESIDENT_EFS; do
      run_one resident "$mode" "$ef" measure "$repeat"
    done
  done
done

python3 "$SCRIPT_DIR/summarize_vldb_mechanism_controls.py" \
  --campaign "$OUT_ROOT" --out "$OUT_ROOT/summary" \
  --expected-binary-sha "$EXPECTED_BINARY_SHA"
printf 'Formal mechanism-control campaign written to %s\n' "$OUT_ROOT"
