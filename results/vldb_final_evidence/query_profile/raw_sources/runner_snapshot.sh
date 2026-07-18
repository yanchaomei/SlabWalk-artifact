#!/usr/bin/env bash
# Capture query-only CPU profiles after the Slab/placement setup phase.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$REPO_ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
MN_SIFT1M=${MN_SIFT1M:-skv-node6}
MN_DEEP1M=${MN_DEEP1M:-skv-node6}
MN_DEEP10M=${MN_DEEP10M:-skv-node5}
OUT=${OUT:-$REPO_ROOT/results/vldb_query_profile_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-query-profile-$(date -u +%Y%m%dT%H%M%SZ)}
DATASETS=${DATASETS:-"DEEP1M DEEP10M"}
METHODS=${METHODS:-"shine slabwalk"}
THREADS=${THREADS:-10}
COROUTINES=${COROUTINES:-2}
EF=${EF:-200}
TOP_K=${TOP_K:-10}
TILE=${TILE:-20}
PROFILE_S=${PROFILE_S:-20}
TIMEOUT_S=${TIMEOUT_S:-1200}
PERF_FREQ=${PERF_FREQ:-199}
PERF_CMD=${PERF_CMD:-perf}
PERF_REPORT_CMD=${PERF_REPORT_CMD:-$PERF_CMD}
PERF_DATA_FIXUP_CMD=${PERF_DATA_FIXUP_CMD:-}
CAPTURE_PERF=${CAPTURE_PERF:-1}
COMPUTE_RECALL=${COMPUTE_RECALL:-0}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-0}
PORT=${PORT:-1234}
INDEX_REGION_1M_BYTES=${INDEX_REGION_1M_BYTES:-4294967296}
INDEX_REGION_10M_BYTES=${INDEX_REGION_10M_BYTES:-17179869184}
LAVD_DEEP10_REGION_BYTES=${LAVD_DEEP10_REGION_BYTES:-42949672960}
DRY_RUN=${DRY_RUN:-0}
ACTIVE_MN=""
ACTIVE_REMOTE_DIR=""
ACTIVE_CN_PID=""

[[ "$CAPTURE_PERF" == "0" || "$CAPTURE_PERF" == "1" ]] || { echo "CAPTURE_PERF must be 0 or 1" >&2; exit 2; }
[[ "$COMPUTE_RECALL" == "0" || "$COMPUTE_RECALL" == "1" ]] || { echo "COMPUTE_RECALL must be 0 or 1" >&2; exit 2; }
[[ "$QUERY_CONTEXTS" =~ ^[0-9]+$ ]] || { echo "QUERY_CONTEXTS must be non-negative" >&2; exit 2; }
(( QUERY_CONTEXTS == 0 || QUERY_CONTEXTS <= THREADS )) || { echo "QUERY_CONTEXTS must not exceed THREADS" >&2; exit 2; }
context_args=()
if (( QUERY_CONTEXTS > 0 )); then
  context_args=(--query-contexts "$QUERY_CONTEXTS")
fi
mkdir -p "$OUT"
read -r -a perf_cmd <<< "$PERF_CMD"
(( ${#perf_cmd[@]} > 0 )) || { echo "PERF_CMD must not be empty" >&2; exit 2; }
read -r -a perf_report_cmd <<< "$PERF_REPORT_CMD"
(( ${#perf_report_cmd[@]} > 0 )) || { echo "PERF_REPORT_CMD must not be empty" >&2; exit 2; }
perf_data_fixup_cmd=()
if [[ -n "$PERF_DATA_FIXUP_CMD" ]]; then
  read -r -a perf_data_fixup_cmd <<< "$PERF_DATA_FIXUP_CMD"
fi
GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')
python3 - "$OUT/campaign.json" "$CAMPAIGN_ID" "$GB_BIN_SHA256" \
  "$DATASETS" "$METHODS" "$THREADS" "$COROUTINES" "$EF" "$TOP_K" \
  "$TILE" "$PROFILE_S" "$PERF_FREQ" "$PERF_CMD" "$PERF_REPORT_CMD" \
  "$PERF_DATA_FIXUP_CMD" "$PORT" "$(hostname)" "$INDEX_REGION_1M_BYTES" \
  "$INDEX_REGION_10M_BYTES" "$LAVD_DEEP10_REGION_BYTES" \
  "$CAPTURE_PERF" "$COMPUTE_RECALL" "$QUERY_CONTEXTS" \
  "$MN_SIFT1M" "$MN_DEEP1M" "$MN_DEEP10M" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone

(path, campaign_id, binary_sha256, datasets, methods, threads, coroutines,
 ef, top_k, tile, profile_s, perf_freq, perf_command, perf_report_command,
 perf_data_fixup_command, port, host, index_region_1m, index_region_10m,
 lavd_deep10_region, capture_perf, compute_recall, query_contexts,
 mn_sift1m, mn_deep1m, mn_deep10m) = sys.argv[1:]
protocol = {
    "binary_sha256": binary_sha256,
    "datasets": datasets.split(),
    "methods": methods.split(),
    "threads": int(threads),
    "coroutines": int(coroutines),
    "ef": int(ef),
    "top_k": int(top_k),
    "query_tile": int(tile),
    "profile_seconds": int(profile_s),
    "perf_frequency": int(perf_freq),
    "perf_command": perf_command,
    "perf_report_command": perf_report_command,
    "perf_data_fixup_command": perf_data_fixup_command,
    "capture_perf": bool(int(capture_perf)),
    "compute_recall": bool(int(compute_recall)),
    "query_contexts_requested": int(query_contexts),
    "tcp_port": int(port),
    "index_region_bytes_by_scale": {
        "1M": int(index_region_1m),
        "10M": int(index_region_10m),
    },
    "lavd_region_bytes_by_dataset": {
        "SIFT1M": 4294967296,
        "DEEP1M": 4294967296,
        "DEEP10M": int(lavd_deep10_region),
    },
    "memory_nodes_by_dataset": {
        "SIFT1M": mn_sift1m,
        "DEEP1M": mn_deep1m,
        "DEEP10M": mn_deep10m,
    },
    "compute_host": host,
    "profile_scope": "query-only-after-phase-marker",
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
with open(path, "w") as handle:
    json.dump({
        "campaign_id": campaign_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

dataset_spec() {
  case "$1" in
    SIFT1M) printf '%s|%s|%s|%s|%s|%s\n' "$MN_SIFT1M" "$GB_DATA/sift1m" 16 100 4294967296 "$INDEX_REGION_1M_BYTES" ;;
    DEEP1M) printf '%s|%s|%s|%s|%s|%s\n' "$MN_DEEP1M" "$GB_DATA/deep1m" 16 100 4294967296 "$INDEX_REGION_1M_BYTES" ;;
    DEEP10M) printf '%s|%s|%s|%s|%s|%s\n' "$MN_DEEP10M" "$GB_DATA/deep10m" 32 200 "$LAVD_DEEP10_REGION_BYTES" "$INDEX_REGION_10M_BYTES" ;;
    *) echo "Unsupported profile dataset: $1" >&2; return 2 ;;
  esac
}

verify_remote_pid() {
  local host=$1 remote_dir=$2
  ssh -o LogLevel=ERROR "$host" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe'; \
     pid=\$(cat '$remote_dir/server.pid'); expected=\$(cat '$remote_dir/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_mn() {
  local host=$1 remote_dir=$2
  if verify_remote_pid "$host" "$remote_dir"; then
    ssh -o LogLevel=ERROR "$host" \
      "pid=\$(cat '$remote_dir/server.pid'); kill \$pid 2>/dev/null || true" || true
  fi
}

cleanup() {
  if [[ -n "$ACTIVE_CN_PID" ]] && kill -0 "$ACTIVE_CN_PID" 2>/dev/null; then
    kill "$ACTIVE_CN_PID" 2>/dev/null || true
  fi
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_REMOTE_DIR" ]]; then
    stop_mn "$ACTIVE_MN" "$ACTIVE_REMOTE_DIR"
  fi
  ACTIVE_CN_PID=""
  ACTIVE_MN=""
  ACTIVE_REMOTE_DIR=""
}

start_mn() {
  local host=$1 remote_dir=$2 index_region_bytes=$3
  ssh -o LogLevel=ERROR "$host" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     realpath '$GB_BIN_R' > '$remote_dir/server.exe'; \
     nohup env LD_LIBRARY_PATH='$GB_LIB' numactl --preferred=1 '$GB_BIN_R' \
       --is-server --num-clients 1 --port "$PORT" \
       --index-region-bytes '$index_region_bytes' > '$remote_dir/mn.out' \
       2> '$remote_dir/mn.err' < /dev/null & \
     echo \$! > '$remote_dir/server.pid'"
  ACTIVE_MN=$host
  ACTIVE_REMOTE_DIR=$remote_dir
  for _ in $(seq 1 100); do
    verify_remote_pid "$host" "$remote_dir" && return 0
    sleep 0.1
  done
  echo "Memory-node process failed ownership verification on $host" >&2
  return 1
}

ensure_tiled_query() {
  local data=$1
  python3 - "$data/queries/query-uniform.fbin" "$data/queries/query-profile${TILE}x.fbin" "$TILE" <<'PY'
import os, struct, sys
source, target, tile = sys.argv[1], sys.argv[2], int(sys.argv[3])
raw = open(source, "rb").read()
if len(raw) < 8:
    raise SystemExit(f"short fbin: {source}")
n, d = struct.unpack_from("<II", raw)
payload = raw[8:]
if len(payload) != n * d * 4:
    raise SystemExit(f"fbin shape mismatch: {source}")
expected = 8 + n * tile * d * 4
if os.path.exists(target) and os.path.getsize(target) == expected:
    raise SystemExit(0)
tmp = target + ".tmp"
with open(tmp, "wb") as handle:
    handle.write(struct.pack("<II", n * tile, d))
    for _ in range(tile):
        handle.write(payload)
os.replace(tmp, target)
PY
}

verify_local_cn_pid() {
  local pid=$1 expected=$2
  local actual
  actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
  [[ -n "$actual" && "$actual" == "$expected" ]]
}

run_profile() {
  local dataset=$1 method=$2
  local mn data m efc region index_region_bytes
  IFS='|' read -r mn data m efc region index_region_bytes <<< "$(dataset_spec "$dataset")"
  local suffix="profile${TILE}x"
  if [[ "$COMPUTE_RECALL" == "1" ]]; then
    suffix=uniform
  fi
  local tag="${dataset}_${method}_T${THREADS}_C${COROUTINES}_ef${EF}"
  local remote_dir="/tmp/${CAMPAIGN_ID//[^[:alnum:]]/_}_${tag}"
  local stdout="$OUT/$tag.json" stderr="$OUT/$tag.err"
  local perf_data="$OUT/$tag.perf.data" report="$OUT/$tag.perf.txt"
  local -a method_args=(--lavd 0)
  local -a recall_args=(--no-recall)
  if [[ "$COMPUTE_RECALL" == "1" ]]; then
    recall_args=()
  fi
  local method_env=""
  if [[ "$method" == "slabwalk" ]]; then
    method_args=(--lavd 8 --lavd-region-bytes "$region")
    method_env="SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2"
  elif [[ "$method" != "shine" ]]; then
    echo "Unsupported profile method: $method" >&2
    return 2
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY_RUN $tag MN=$mn data=$data suffix=$suffix"
    return 0
  fi
  if [[ "$COMPUTE_RECALL" == "0" ]]; then
    ensure_tiled_query "$data"
  fi
  ssh -o LogLevel=ERROR "$mn" \
    "test -s '$data/dump/index_m${m}_efc${efc}_node1_of1.dat'" || {
    echo "Missing $dataset index on $mn" >&2; return 2;
  }
  start_mn "$mn" "$remote_dir" "$index_region_bytes"

  env LD_LIBRARY_PATH="$GB_LIB" $method_env numactl --preferred=1 "$GB_BIN" \
    --servers "$mn" --initiator --threads "$THREADS" --coroutines "$COROUTINES" \
    "${context_args[@]}" \
    --port "$PORT" \
    --index-region-bytes "$index_region_bytes" \
    --data-path "$data/" --query-suffix "$suffix" --ef-search "$EF" \
    --ef-construction "$efc" --m "$m" --k "$TOP_K" --label "$tag" \
    --spec-k 1 --load-index "${recall_args[@]}" "${method_args[@]}" \
    > "$stdout" 2> "$stderr" &
  local cn_pid=$!
  ACTIVE_CN_PID=$cn_pid
  local expected
  expected=$(realpath "$GB_BIN")
  for _ in $(seq 1 100); do
    verify_local_cn_pid "$cn_pid" "$expected" && break
    kill -0 "$cn_pid" 2>/dev/null || break
    sleep 0.05
  done
  verify_local_cn_pid "$cn_pid" "$expected" || {
    echo "Compute-node PID ownership check failed for $tag" >&2; return 1;
  }

  ( sleep "$TIMEOUT_S"; kill "$cn_pid" 2>/dev/null || true ) &
  local watchdog=$!
  local query_started=0
  for _ in $(seq 1 $((TIMEOUT_S * 10))); do
    if grep -Fq '**QUERY**: running worker threads' "$stderr" 2>/dev/null; then
      query_started=1
      break
    fi
    kill -0 "$cn_pid" 2>/dev/null || break
    sleep 0.1
  done
  if [[ $query_started -ne 1 ]]; then
    kill "$watchdog" 2>/dev/null || true
    echo "Query phase marker not observed for $tag" >&2
    tail -40 "$stderr" >&2 || true
    return 1
  fi

  local perf_rc=0
  if [[ "$CAPTURE_PERF" == "1" ]]; then
    set +e
    "${perf_cmd[@]}" record -F "$PERF_FREQ" -e cycles:u -g --call-graph dwarf,8192 \
      --inherit -p "$cn_pid" -o "$perf_data" -- sleep "$PROFILE_S"
    perf_rc=$?
    set -e
    printf '%s\n' "$perf_rc" > "$OUT/$tag.perf.record.status"
    if [[ -s "$perf_data" && ${#perf_data_fixup_cmd[@]} -gt 0 ]]; then
      "${perf_data_fixup_cmd[@]}" "$(id -u):$(id -g)" "$perf_data"
    fi
  fi
  set +e
  wait "$cn_pid"
  local cn_rc=$?
  set -e
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  stop_mn "$mn" "$remote_dir"
  scp -q "$mn:$remote_dir/mn.err" "$OUT/$tag.mn.err" 2>/dev/null || true
  ACTIVE_CN_PID=""
  ACTIVE_MN=""
  ACTIVE_REMOTE_DIR=""

  [[ $cn_rc -eq 0 && -s "$stdout" ]] || {
    echo "$tag failed: perf=$perf_rc compute=$cn_rc" >&2
    return 1
  }
  if [[ "$CAPTURE_PERF" == "1" && ! -s "$perf_data" ]]; then
    echo "$tag failed to produce perf data" >&2
    return 1
  fi
  python3 - "$stdout" "$TILE" "$COMPUTE_RECALL" <<'PY'
import json, sys
obj = json.load(open(sys.argv[1]))
compute_recall = int(sys.argv[3])
expected = 10000 if compute_recall else 10000 * int(sys.argv[2])
assert int(obj["num_queries"]) == expected
assert int(obj["queries"]["processed"]) == expected
if compute_recall:
    recall = float(obj["queries"]["recall"])
    assert 0.0 < recall <= 1.0
PY
  if [[ "$CAPTURE_PERF" == "1" ]]; then
    set +e
    "${perf_report_cmd[@]}" report -i "$perf_data" --stdio --no-children \
      --sort=comm,dso,symbol -g none --percent-limit 0.2 > "$report"
    local report_rc=$?
    set -e
    [[ $report_rc -eq 0 && -s "$report" ]] || {
      echo "$tag produced unreadable perf data: record=$perf_rc report=$report_rc" >&2
      return 1
    }
    sha256sum "$perf_data" > "$perf_data.sha256"
    echo "PROFILE_OK $tag"
  else
    echo "RUN_OK $tag"
  fi
}

trap cleanup EXIT INT TERM
for dataset in $DATASETS; do
  for method in $METHODS; do
    run_profile "$dataset" "$method"
  done
done
echo "Wrote query-only profiles to $OUT"
