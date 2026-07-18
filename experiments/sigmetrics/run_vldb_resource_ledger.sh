#!/usr/bin/env bash
# Measure the Slab physical-layout/resource matrix on the SKV cluster.
# Run on skv-node1 from the synced repository root.
set -euo pipefail

SCRIPT_PATH=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
REPO_ROOT=$(cd -- "$(dirname -- "$SCRIPT_PATH")/../.." && pwd)

GB_BIN=${GB_BIN:-$REPO_ROOT/build/shine}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
OUT=${OUT:-$REPO_ROOT/results/vldb_resource_ledger/raw}
LAYOUTS=${LAYOUTS:-"legacy fixed variable"}
MN_COUNTS=${MN_COUNTS:-"1 3 5"}
REPEATS=${REPEATS:-5}
REP_START=${REP_START:-0}
WARMUPS=${WARMUPS:-1}
THREADS=${THREADS:-10}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}
COROUTINES=${COROUTINES:-2}
BUILD_THREADS=${BUILD_THREADS:-20}
BUILD_CPU_BASE=${BUILD_CPU_BASE:-1}
BUILD_CPU_STRIDE=${BUILD_CPU_STRIDE:-2}
TIMEOUT_S=${TIMEOUT_S:-2400}
PORT=${PORT:-1234}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
DRY_RUN=${DRY_RUN:-0}
RESUME=${RESUME:-0}
CAMPAIGN_ID=${CAMPAIGN_ID:-}
REMOTE_RUNNER=/tmp/vldb_resource_ledger_runner.sh
MN_POOL=(skv-node3 skv-node4 skv-node5 skv-node6 skv-node7)
ACTIVE_HOSTS=()
ACTIVE_REMOTE_OUT=""

DATASET=${DATASET:-GIST1M}
case "$DATASET" in
  GIST1M)
    DATA_PATH=$GB_DATA/gist1m
    QUERY_SUFFIX=u10k
    EF_SEARCH=400
    ;;
  *)
    printf 'Unsupported resource-ledger dataset: %s\n' "$DATASET" >&2
    exit 2
    ;;
esac

if [[ "${1:-}" == "--memory-node" ]]; then
  shift
  bin=$1
  out=$2
  lib=$3
  port=$4
  index_region_bytes=$5
  mkdir -p "$out"
  set +e
  env LD_LIBRARY_PATH="$lib" /usr/bin/time -v -o "$out/mn.time" "$bin" \
    --is-server --num-clients 1 --port "$port" \
    --index-region-bytes "$index_region_bytes" \
    > "$out/mn.out" 2> "$out/mn.runtime.err" &
  time_pid=$!
  set -e
  expected=$(realpath "$bin")
  server_pid=""
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

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

for value in "$REPEATS" "$THREADS" "$COROUTINES" "$BUILD_THREADS" "$TIMEOUT_S"; do
  is_positive_integer "$value" || { printf 'Expected positive integer, got %s\n' "$value" >&2; exit 2; }
done
is_positive_integer "$QUERY_CONTEXTS" || { printf 'QUERY_CONTEXTS must be positive\n' >&2; exit 2; }
((QUERY_CONTEXTS <= THREADS)) || { printf 'QUERY_CONTEXTS must not exceed THREADS\n' >&2; exit 2; }
[[ "$WARMUPS" =~ ^[0-9]+$ ]] || { printf 'WARMUPS must be non-negative\n' >&2; exit 2; }
[[ "$REP_START" =~ ^[0-9]+$ ]] || { printf 'REP_START must be non-negative\n' >&2; exit 2; }
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || { printf 'RESUME must be 0 or 1\n' >&2; exit 2; }

if [[ "$DRY_RUN" != "1" ]]; then
  [[ -x "$GB_BIN" ]] || { printf 'CN binary is not executable: %s\n' "$GB_BIN" >&2; exit 2; }
  mkdir -p "$OUT"
  GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')
  SCRIPT_SHA256=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
  CAMPAIGN_ID=$(python3 - "$OUT/campaign.json" "$RESUME" "$CAMPAIGN_ID" \
    "$GB_BIN_SHA256" "$SCRIPT_SHA256" "$DATASET" "$LAYOUTS" "$MN_COUNTS" \
    "$REPEATS" "$REP_START" "$WARMUPS" "$THREADS" "$COROUTINES" \
    "$QUERY_CONTEXTS" \
    "$BUILD_THREADS" "$BUILD_CPU_BASE" "$BUILD_CPU_STRIDE" "$TIMEOUT_S" \
    "$PORT" "$INDEX_REGION_BYTES" "${MN_POOL[*]}" "$QUERY_SUFFIX" "$EF_SEARCH" <<'PY'
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

(path_s, resume_s, requested_id, binary_sha, script_sha, dataset, layouts,
 mn_counts, repeats, rep_start, warmups, threads, coroutines, query_contexts,
 build_threads, build_cpu_base, build_cpu_stride, timeout_s, port, index_region_bytes,
 mn_pool, query_suffix, ef_search) = sys.argv[1:]
path = Path(path_s)
protocol = {
    "binary_sha256": binary_sha,
    "runner_sha256": script_sha,
    "dataset": dataset,
    "layouts": layouts.split(),
    "memory_node_counts": [int(x) for x in mn_counts.split()],
    "memory_node_pool": mn_pool.split(),
    "repeats": int(repeats),
    "repeat_start": int(rep_start),
    "warmups": int(warmups),
    "threads": int(threads),
    "query_contexts": int(query_contexts),
    "coroutines": int(coroutines),
    "build_threads": int(build_threads),
    "build_cpu_base": int(build_cpu_base),
    "build_cpu_stride": int(build_cpu_stride),
    "timeout_seconds": int(timeout_s),
    "tcp_port": int(port),
    "index_region_bytes": int(index_region_bytes),
    "query_suffix": query_suffix,
    "ef_search": int(ef_search),
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
resume = resume_s == "1"
if path.exists():
    if not resume:
        raise SystemExit(f"{path} exists; set RESUME=1 only for this campaign")
    old = json.loads(path.read_text())
    if old.get("protocol") != protocol or old.get("protocol_fingerprint") != fingerprint:
        raise SystemExit("resource-ledger campaign protocol drift")
    if requested_id and requested_id != old.get("campaign_id"):
        raise SystemExit("resource-ledger CAMPAIGN_ID mismatch")
    campaign_id = old["campaign_id"]
else:
    if resume:
        raise SystemExit("RESUME=1 but resource-ledger campaign.json is missing")
    campaign_id = requested_id or f"vldb-resource-{uuid.uuid4()}"
    path.write_text(json.dumps({
        "campaign_id": campaign_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, indent=2, sort_keys=True) + "\n")
print(campaign_id)
PY
  )
  PROTOCOL_FINGERPRINT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol_fingerprint"])' "$OUT/campaign.json")
else
  GB_BIN_SHA256=dry-run
  SCRIPT_SHA256=dry-run
  CAMPAIGN_ID=${CAMPAIGN_ID:-dry-run}
  PROTOCOL_FINGERPRINT=dry-run
fi

hosts_for_count() {
  local count=$1
  if ((count < 1 || count > ${#MN_POOL[@]})); then
    printf 'Unsupported MN count: %s\n' "$count" >&2
    return 2
  fi
  printf '%s\n' "${MN_POOL[@]:0:count}"
}

region_bytes() {
  local layout=$1 count=$2
  case "$layout:$count" in
    legacy:*) printf '%s\n' 9663676416 ;;
    fixed:1) printf '%s\n' 9663676416 ;;
    fixed:3) printf '%s\n' 3221225472 ;;
    fixed:5) printf '%s\n' 2147483648 ;;
    variable:1) printf '%s\n' 6442450944 ;;
    variable:3) printf '%s\n' 2147483648 ;;
    variable:5) printf '%s\n' 1610612736 ;;
    *) printf 'No capacity for layout=%s count=%s\n' "$layout" "$count" >&2; return 2 ;;
  esac
}

layout_env() {
  case "$1" in
    legacy) printf '%s\n' 'SHINE_LAVD_MULTI_BUILDER=1' ;;
    fixed) printf '%s\n' 'SHINE_LAVD_NATIVE_PACKED_WRITE=1' ;;
    variable) printf '%s\n' 'SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1' ;;
    *) printf 'Unsupported layout: %s\n' "$1" >&2; return 2 ;;
  esac
}

verify_remote_pid() {
  local host=$1 remote_out=$2
  ssh -o LogLevel=ERROR "$host" \
    "test -s '$remote_out/server.pid' -a -s '$remote_out/server.exe'; \
     pid=\$(cat '$remote_out/server.pid'); expected=\$(cat '$remote_out/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_hosts() {
  local remote_out=$1
  shift
  local host
  for host in "$@"; do
    if verify_remote_pid "$host" "$remote_out"; then
      ssh -o LogLevel=ERROR "$host" \
        "pid=\$(cat '$remote_out/server.pid'); kill \$pid 2>/dev/null || true" || true
    fi
  done
}

cleanup_active_hosts() {
  if ((${#ACTIVE_HOSTS[@]} > 0)); then
    stop_hosts "$ACTIVE_REMOTE_OUT" "${ACTIVE_HOSTS[@]}"
    ACTIVE_HOSTS=()
    ACTIVE_REMOTE_OUT=""
  fi
}

copy_mn_artifacts() {
  local cell=$1 remote_out=$2
  shift 2
  local ordinal=0 host
  for host in "$@"; do
    ordinal=$((ordinal + 1))
    mkdir -p "$cell/mn$ordinal"
    scp -q "$host:$remote_out/mn.out" "$cell/mn$ordinal/mn.out" || true
    scp -q "$host:$remote_out/mn.err" "$cell/mn$ordinal/mn.err" || true
    scp -q "$host:$remote_out/status" "$cell/mn$ordinal/status" || true
  done
}

validate_cell() {
  local cell=$1 expected_mns=$2 expected_contexts=$3
  python3 - "$cell/cn.json" "$expected_mns" "$expected_contexts" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_mns = int(sys.argv[2])
expected_contexts = int(sys.argv[3])
data = json.loads(path.read_text())
queries = data["queries"]
assert data["meta"]["memory_nodes"] == expected_mns
assert data["query_contexts"] == expected_contexts
assert queries["processed"] == data["num_queries"] > 0
assert 0.0 <= float(queries["recall"]) <= 1.0
assert int(queries["queries_per_sec"]) > 0
assert sum(queries["local_cn_read_bytes_per_mn"]) == queries["rdma_reads_in_bytes"]
assert sum(queries["local_cn_read_wrs_per_mn"]) == queries["rdma_wrs"]
assert sum(queries["local_cn_read_submits_per_mn"]) == queries["rdma_posts"]
PY
  local ordinal
  for ((ordinal = 1; ordinal <= expected_mns; ++ordinal)); do
    [[ -s "$cell/mn$ordinal/mn.err" ]] || { printf 'Missing MN log: %s/mn%s/mn.err\n' "$cell" "$ordinal" >&2; return 1; }
    [[ "$(<"$cell/mn$ordinal/status")" == "0" ]] || { printf 'MN %s failed in %s\n' "$ordinal" "$cell" >&2; return 1; }
  done
}

run_one() {
  local layout=$1 count=$2 kind=$3 rep=$4
  local -a hosts=()
  while IFS= read -r host; do
    hosts+=("$host")
  done < <(hosts_for_count "$count")
  local capacity env_extra tag cell remote_out host ordinal
  capacity=$(region_bytes "$layout" "$count")
  env_extra=$(layout_env "$layout")
  tag="$(printf '%s' "$DATASET" | tr '[:upper:]' '[:lower:]')_${layout}_s${count}_${kind}_r${rep}"
  cell="$OUT/$tag"
  remote_out="/tmp/$tag"

  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'layout=%s mns=%s kind=%s rep=%s capacity=%s hosts=%s env=%s\n' \
      "$layout" "$count" "$kind" "$rep" "$capacity" "${hosts[*]}" "$env_extra"
    return 0
  fi

  if [[ "$RESUME" == "1" && -s "$cell/manifest.txt" &&
        "$(grep -F 'campaign_id=' "$cell/manifest.txt" 2>/dev/null || true)" == "campaign_id=$CAMPAIGN_ID" &&
        "$(grep -F 'protocol_fingerprint=' "$cell/manifest.txt" 2>/dev/null || true)" == "protocol_fingerprint=$PROTOCOL_FINGERPRINT" ]] &&
      validate_cell "$cell" "$count" "$QUERY_CONTEXTS" >/dev/null 2>&1; then
    printf 'SKIP complete %s\n' "$tag"
    return 0
  fi

  [[ ! -e "$cell" ]] || {
    printf 'Refusing incomplete resource-ledger cell: %s\n' "$cell" >&2
    exit 2
  }

  printf '=== %s ===\n' "$tag"
  mkdir -p "$cell"
  ACTIVE_HOSTS=("${hosts[@]}")
  ACTIVE_REMOTE_OUT="$remote_out"
  for host in "${hosts[@]}"; do
    scp -q "$SCRIPT_PATH" "$host:$REMOTE_RUNNER"
    ssh -o LogLevel=ERROR "$host" \
      "rm -rf '$remote_out'; mkdir -p '$remote_out'; nohup bash '$REMOTE_RUNNER' --memory-node '$GB_BIN' '$remote_out' '$GB_LIB' '$PORT' '$INDEX_REGION_BYTES' >'$remote_out/launcher.out' 2>'$remote_out/launcher.err' < /dev/null & echo \$! >'$remote_out/launcher.pid'"
  done
  sleep 4
  for host in "${hosts[@]}"; do
    verify_remote_pid "$host" "$remote_out" || {
      printf 'MN failed to start on %s\n' "$host" >&2
      copy_mn_artifacts "$cell" "$remote_out" "${hosts[@]}"
      stop_hosts "$remote_out" "${hosts[@]}"
      return 1
    }
  done

  {
    printf 'tag=%s\nlayout=%s\nmemory_nodes=%s\nhosts=%s\ncapacity_per_mn=%s\n' \
      "$tag" "$layout" "$count" "${hosts[*]}" "$capacity"
    printf 'binary_sha256=%s\n' "$(sha256sum "$GB_BIN" | awk '{print $1}')"
    printf 'campaign_id=%s\nprotocol_fingerprint=%s\n' \
      "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT"
    printf 'cn_host=%s\nstarted_utc=%s\nlayout_env=%s\nbuild_threads=%s\n' \
      "$(hostname)" "$(date -u +%FT%TZ)" "$env_extra" "$BUILD_THREADS"
    printf 'build_cpu_base=%s\nbuild_cpu_stride=%s\n' \
      "$BUILD_CPU_BASE" "$BUILD_CPU_STRIDE"
    printf 'query_contexts=%s\n' "$QUERY_CONTEXTS"
    printf 'tcp_port=%s\n' "$PORT"
    printf 'index_region_bytes=%s\n' "$INDEX_REGION_BYTES"
  } > "$cell/manifest.txt"

  local -a extra_env=(SHINE_LAVD_RABITQ_B=2 SHINE_CRANE=1 GB_BITMAP_DEDUP=1
                      GB_QUERY_LATENCY=1
                      SHINE_LAVD_BUILD_THREADS="$BUILD_THREADS"
                      SHINE_LAVD_BUILD_CPU_BASE="$BUILD_CPU_BASE"
                      SHINE_LAVD_BUILD_CPU_STRIDE="$BUILD_CPU_STRIDE")
  local -a layout_tokens=()
  read -r -a layout_tokens <<< "$env_extra"
  extra_env+=("${layout_tokens[@]}")
  set +e
  timeout "$TIMEOUT_S" /usr/bin/time -v env "${extra_env[@]}" \
    numactl --preferred=1 "$GB_BIN" --servers "${hosts[@]}" --initiator \
    --port "$PORT" \
    --index-region-bytes "$INDEX_REGION_BYTES" \
    --data-path "$DATA_PATH" --threads "$THREADS" --coroutines "$COROUTINES" \
    --query-contexts "$QUERY_CONTEXTS" \
    --query-suffix "$QUERY_SUFFIX" --load-index --ef-search "$EF_SEARCH" \
    --ef-construction 100 --k 10 --m 16 --lavd 8 --lavd-rerank 200 \
    --lavd-region-bytes "$capacity" --label "$tag" \
    > "$cell/cn.json" 2> "$cell/cn.err"
  local rc=$?
  set -e

  local wait_deadline=$((SECONDS + 30))
  while ((SECONDS < wait_deadline)); do
    local live=0
    for host in "${hosts[@]}"; do
      ssh -o LogLevel=ERROR "$host" "test ! -e '$remote_out/status'" && live=1 || true
    done
    ((live == 0)) && break
    sleep 1
  done
  copy_mn_artifacts "$cell" "$remote_out" "${hosts[@]}"
  stop_hosts "$remote_out" "${hosts[@]}"
  ACTIVE_HOSTS=()
  ACTIVE_REMOTE_OUT=""

  if [[ $rc -ne 0 ]]; then
    printf 'CN failed: %s rc=%s\n' "$tag" "$rc" >&2
    tail -80 "$cell/cn.err" >&2 || true
    return "$rc"
  fi
  validate_cell "$cell" "$count" "$QUERY_CONTEXTS"
}

trap cleanup_active_hosts EXIT INT TERM

for layout in $LAYOUTS; do
  for count in $MN_COUNTS; do
    for ((rep = 0; rep < WARMUPS; ++rep)); do
      run_one "$layout" "$count" warmup "$rep"
    done
    for ((rep = REP_START; rep < REP_START + REPEATS; ++rep)); do
      run_one "$layout" "$count" measure "$rep"
    done
  done
done

printf 'Resource-ledger raw runs written to %s\n' "$OUT"
