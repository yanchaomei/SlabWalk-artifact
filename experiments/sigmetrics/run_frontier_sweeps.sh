#!/usr/bin/env bash
# Run matched-thread SlabWalk/SHINE recall-QPS frontier sweeps on the SKV
# cluster.  This script is intended to be executed on skv-node1.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FRONTIER_SOURCE_SCRIPT_DIR=${FRONTIER_SOURCE_SCRIPT_DIR:-$SCRIPT_DIR}
SCRIPT_PATH=$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")
QUERY_POOL_PREPARER=${QUERY_POOL_PREPARER:-$FRONTIER_SOURCE_SCRIPT_DIR/prepare_fixed_query_pool.py}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$FRONTIER_SOURCE_SCRIPT_DIR/vldb_evidence_bundle.py}
FRONTIER_VERIFIER=${FRONTIER_VERIFIER:-$FRONTIER_SOURCE_SCRIPT_DIR/verify_vldb_frontier_sweep.py}
GB_ROOT=${GB_ROOT:-/home/kvgroup/chaomei/graphbeyond-c1/graphbeyond}
GB_GOOD_BIN=${GB_GOOD_BIN:-/home/kvgroup/chaomei/graphbeyond-c1-shine-1m-latclean}
GB_BIN=${GB_BIN:-$GB_GOOD_BIN}
GB_BIN_R=${GB_BIN_R:-$GB_GOOD_BIN}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA to the frozen candidate SHA-256}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LOCAL_LD_LIBRARY_PATH=${GB_LOCAL_LD_LIBRARY_PATH:-/home/kvgroup/chaomei/lib}
GB_REMOTE_LD_LIBRARY_PATH=${GB_REMOTE_LD_LIBRARY_PATH:-/home/kvgroup/chaomei/lib}
OUT=${OUT:-/home/kvgroup/chaomei/frontier_sweeps_$(date +%Y%m%d_%H%M%S)}
RUN_ID=${RUN_ID:-r0}
RUN_KIND=${RUN_KIND:-measure}
CAMPAIGN_ID=${CAMPAIGN_ID:-standalone-$(date -u +%Y%m%dT%H%M%SZ)}
TRACE=${TRACE:-0}
CAMPAIGN_KIND=${CAMPAIGN_KIND:-formal}
ALLOW_MISSING_DATASETS=${ALLOW_MISSING_DATASETS:-0}
METHOD_ORDER_OFFSET=${METHOD_ORDER_OFFSET:-0}
MIN_POINTS=${MIN_POINTS:-5}
REMOTE_IDENTITY_RETRIES=${REMOTE_IDENTITY_RETRIES:-3}
FRONTIER_LIFECYCLE_LOG=${FRONTIER_LIFECYCLE_LOG:-}
# Keep the measured layout independent of the legacy 6 GiB fallback. Each
# 1M capacity is the smallest whole-GiB envelope above its fixed Slab extent.
LAVD_SIFT1_REGION_BYTES=${LAVD_SIFT1_REGION_BYTES:-5368709120}
LAVD_GIST1_REGION_BYTES=${LAVD_GIST1_REGION_BYTES:-9663676416}
LAVD_DEEP1_REGION_BYTES=${LAVD_DEEP1_REGION_BYTES:-4294967296}
LAVD_BIGANN1_REGION_BYTES=${LAVD_BIGANN1_REGION_BYTES:-5368709120}
LAVD_SPACEV1_REGION_BYTES=${LAVD_SPACEV1_REGION_BYTES:-4294967296}
LAVD_TURING1_REGION_BYTES=${LAVD_TURING1_REGION_BYTES:-4294967296}
LAVD_TEXT1_REGION_BYTES=${LAVD_TEXT1_REGION_BYTES:-8589934592}
LAVD_10M_REGION_BYTES=${LAVD_10M_REGION_BYTES:-42949672960}
INDEX_REGION_1M_BYTES=${INDEX_REGION_1M_BYTES:-4294967296}
INDEX_REGION_10M_BYTES=${INDEX_REGION_10M_BYTES:-17179869184}
MN_MEMORY_HEADROOM_BYTES=${MN_MEMORY_HEADROOM_BYTES:-2147483648}
LAVD_PARALLEL_BUILD_ENV="SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2 SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1"

THREADS=${THREADS:-10}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}
DATASETS=${DATASETS:-"SIFT1M GIST1M DEEP10M TEXT10M SIFT10M"}
# C=2 is the highest queue depth that stayed stable across the matched-thread
# frontier probes on the current SKV software stack.  C=4/8 can hang in the
# SlabWalk initialization path on this binary, so the frontier uses the stable
# denominator instead of mixing in older hero points.
COROS=${COROS:-2}
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }
[[ "$QUERY_CONTEXTS" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_CONTEXTS must be positive" >&2; exit 2; }
(( QUERY_CONTEXTS <= THREADS )) || { echo "QUERY_CONTEXTS cannot exceed THREADS" >&2; exit 2; }
TIMEOUT_S=${TIMEOUT_S:-900}
PORT=${PORT:-1234}
export FRONTIER_TCP_PORT="$PORT"
SIFT1_EFS=${SIFT1_EFS:-"48 64 80 100 150 200"}
GIST1_EFS=${GIST1_EFS:-"100 200 300 400 600"}
DEEP_EFS=${DEEP_EFS:-"50 100 150 200 300 500"}
DEEP_M=${DEEP_M:-32}
DEEP_EFC=${DEEP_EFC:-200}
DEEP1_EFS=${DEEP1_EFS:-"30 50 80 100 150 200"}
DEEP1_M=${DEEP1_M:-16}
DEEP1_EFC=${DEEP1_EFC:-100}
BIGANN1_EFS=${BIGANN1_EFS:-"48 64 80 100 150 200"}
SPACEV1_EFS=${SPACEV1_EFS:-"100 200 300 400 600 800"}
TURING1_EFS=${TURING1_EFS:-"200 400 600 900 1200 1600"}
TEXT_M=${TEXT_M:-16}
TEXT_EFC=${TEXT_EFC:-100}
TEXT1_EFS=${TEXT1_EFS:-"100 150 200 300 500 800"}
TEXT1_M=${TEXT1_M:-16}
TEXT1_EFC=${TEXT1_EFC:-100}
SIFT10_M=${SIFT10_M:-16}
SIFT10_EFC=${SIFT10_EFC:-100}
SIFT1_MN=${SIFT1_MN:-skv-node4}
GIST1_MN=${GIST1_MN:-skv-node3}
DEEP_MN=${DEEP_MN:-skv-node5}
DEEP1_MN=${DEEP1_MN:-skv-node6}
BIGANN1_MN=${BIGANN1_MN:-skv-node7}
SPACEV1_MN=${SPACEV1_MN:-skv-node3}
TURING1_MN=${TURING1_MN:-skv-node2}
TEXT_MN=${TEXT_MN:-skv-node2}
TEXT1_MN=${TEXT1_MN:-skv-node4}
SIFT10_MN=${SIFT10_MN:-skv-node2}
ACTIVE_MN=""
ACTIVE_MN_DIR=""
ACTIVE_MN_BIN_PATH=""
ACTIVE_MN_BIN_SHA256=""
ACTIVE_MN_STARTTIME=""
ACTIVE_MN_PID=""
ACTIVE_CN_PID=""
ACTIVE_CN_EXPECTED=""
ACTIVE_CN_SHA256=""
ACTIVE_CN_STARTTIME=""
CURRENT_LIFECYCLE_TAG=""
CURRENT_LIFECYCLE_DATASET=""
CURRENT_LIFECYCLE_METHOD=""
CURRENT_LIFECYCLE_VARIANT=""
CURRENT_LIFECYCLE_EF=""
COMPUTE_HOST=$(hostname)

[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "EXPECTED_BINARY_SHA must contain 64 lowercase hex digits" >&2; exit 2;
}
[[ "$CAMPAIGN_KIND" == "formal" || "$CAMPAIGN_KIND" == "smoke" ]] || {
  echo "CAMPAIGN_KIND must be formal or smoke" >&2; exit 2;
}
[[ "$ALLOW_MISSING_DATASETS" == "0" || "$ALLOW_MISSING_DATASETS" == "1" ]] || {
  echo "ALLOW_MISSING_DATASETS must be 0 or 1" >&2; exit 2;
}
[[ "$METHOD_ORDER_OFFSET" == "0" || "$METHOD_ORDER_OFFSET" == "1" ]] || {
  echo "METHOD_ORDER_OFFSET must be 0 or 1" >&2; exit 2;
}
[[ "$MIN_POINTS" =~ ^[1-9][0-9]*$ ]] || { echo "MIN_POINTS must be positive" >&2; exit 2; }
[[ "$REMOTE_IDENTITY_RETRIES" =~ ^[1-9][0-9]*$ ]] || {
  echo "REMOTE_IDENTITY_RETRIES must be positive" >&2; exit 2;
}
if [[ "$CAMPAIGN_KIND" == "formal" && "$MIN_POINTS" -lt 5 ]]; then
  echo "Refusing fewer than five formal frontier points" >&2
  exit 2
fi
[[ -x "$GB_BIN" ]] || { echo "Missing CN binary: $GB_BIN" >&2; exit 2; }
GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_BIN_SHA256" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "CN binary SHA mismatch: $GB_BIN_SHA256" >&2; exit 2;
}
[[ -f "$EVIDENCE_TOOL" && -f "$FRONTIER_VERIFIER" ]] || {
  echo "Missing frontier evidence tool or verifier" >&2; exit 2;
}

if [[ "${VLDB_FRONTIER_HARNESS_FROZEN:-0}" != "1" ]]; then
  [[ ! -e "$OUT" ]] || { echo "Refusing existing frontier OUT: $OUT" >&2; exit 2; }
  mkdir -p "$OUT"
  snapshot_json=$(python3 "$EVIDENCE_TOOL" snapshot \
    --out-dir "$OUT/harness" \
    --entry runner="$SCRIPT_PATH" \
    --entry query_pool_preparer="$QUERY_POOL_PREPARER" \
    --entry frontier_verifier="$FRONTIER_VERIFIER" \
    --entry evidence_tool="$EVIDENCE_TOOL")
  read -r frozen_runner frozen_preparer frozen_verifier frozen_tool harness_sha <<< "$({
    python3 - "$OUT/harness" "$snapshot_json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
payload = json.loads(sys.argv[2])
print(
    root / payload["entries"]["runner"]["path"],
    root / payload["entries"]["query_pool_preparer"]["path"],
    root / payload["entries"]["frontier_verifier"]["path"],
    root / payload["entries"]["evidence_tool"]["path"],
    payload["manifest_sha256"],
)
PY
  })"
  exec env \
    VLDB_FRONTIER_HARNESS_FROZEN=1 \
    FRONTIER_SOURCE_SCRIPT_DIR="$FRONTIER_SOURCE_SCRIPT_DIR" \
    QUERY_POOL_PREPARER="$frozen_preparer" \
    FRONTIER_VERIFIER="$frozen_verifier" EVIDENCE_TOOL="$frozen_tool" \
    HARNESS_MANIFEST="$OUT/harness/harness.json" \
    HARNESS_MANIFEST_SHA256="$harness_sha" \
    OUT="$OUT" RUN_ID="$RUN_ID" RUN_KIND="$RUN_KIND" \
    CAMPAIGN_ID="$CAMPAIGN_ID" CAMPAIGN_KIND="$CAMPAIGN_KIND" TRACE="$TRACE" \
    DATASETS="$DATASETS" THREADS="$THREADS" QUERY_CONTEXTS="$QUERY_CONTEXTS" \
    COROS="$COROS" TIMEOUT_S="$TIMEOUT_S" PORT="$PORT" \
    GB_ROOT="$GB_ROOT" GB_BIN="$GB_BIN" GB_BIN_R="$GB_BIN_R" GB_DATA="$GB_DATA" \
    GB_LOCAL_LD_LIBRARY_PATH="$GB_LOCAL_LD_LIBRARY_PATH" \
    GB_REMOTE_LD_LIBRARY_PATH="$GB_REMOTE_LD_LIBRARY_PATH" \
    EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA" \
    ALLOW_MISSING_DATASETS="$ALLOW_MISSING_DATASETS" \
    METHOD_ORDER_OFFSET="$METHOD_ORDER_OFFSET" \
    MIN_POINTS="$MIN_POINTS" \
    REMOTE_IDENTITY_RETRIES="$REMOTE_IDENTITY_RETRIES" \
    FRONTIER_LIFECYCLE_LOG="$FRONTIER_LIFECYCLE_LOG" \
    bash "$frozen_runner"
fi

HARNESS_MANIFEST=${HARNESS_MANIFEST:?frozen frontier harness manifest is required}
HARNESS_MANIFEST_SHA256=${HARNESS_MANIFEST_SHA256:?frozen frontier harness SHA is required}
verify_harness() {
  python3 "$EVIDENCE_TOOL" verify-harness \
    --manifest "$HARNESS_MANIFEST" \
    --expected-manifest-sha "$HARNESS_MANIFEST_SHA256" >/dev/null
}
verify_harness
[[ -d "$OUT" && ! -e "$OUT/campaign.json" ]] || {
  echo "Invalid frozen frontier output root" >&2; exit 2;
}

CSV="$OUT/slabwalk_shine_frontier_raw.csv"
INPUT_MANIFEST="$OUT/input_manifest.tsv"
CURRENT_INPUT_SIGNATURE=""
CURRENT_MEMORY_NODE=""
CURRENT_DATA_PATH=""
CURRENT_QUERY_SUFFIX=""
CURRENT_M=""
CURRENT_EFC=""
echo "dataset,method,variant,campaign_id,protocol_fingerprint,binary_sha256,input_signature,compute_host,memory_host,mn_binary_sha256,run_id,run_kind,trace,measurement_mode,threads,query_contexts,coroutines,top_k,metric,ef,m,efc,query_suffix,lavd,index_region_bytes,lavd_region_bytes,env,recall,qps,p50_us,p95_us,p99_us,posts_per_q,bytes_per_q,processed,expected_queries,failed_queries,trace_csv,json,stderr,execution_manifest,status" > "$CSV"
printf 'dataset\trole\thost\tpath\tbytes\tsha256\n' > "$INPUT_MANIFEST"

python3 - "$OUT/campaign.json" "$CAMPAIGN_ID" "$CAMPAIGN_KIND" \
  "$GB_BIN_SHA256" "$DATASETS" "$RUN_ID" "$RUN_KIND" "$TRACE" \
  "$THREADS" "$QUERY_CONTEXTS" "$COROS" "$PORT" "$TIMEOUT_S" \
  "$COMPUTE_HOST" "$METHOD_ORDER_OFFSET" "$MIN_POINTS" "$HARNESS_MANIFEST" \
  "$HARNESS_MANIFEST_SHA256" "$REMOTE_IDENTITY_RETRIES" <<'PY'
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone

(path, campaign_id, campaign_kind, binary_sha, datasets, run_id, run_kind,
 trace, threads, query_contexts, coroutines, port, timeout_s, compute_host,
 method_order_offset, min_points, harness_manifest, harness_sha,
 remote_identity_retries) = sys.argv[1:]
protocol = {
    "binary_sha256": binary_sha,
    "datasets": datasets.split(),
    "run_id": run_id,
    "run_kind": run_kind,
    "trace": trace == "1",
    "measurement_mode": "fixed_query_pool",
    "workers": int(threads),
    "query_contexts": int(query_contexts),
    "coroutines": int(coroutines),
    "top_k": 10,
    "tcp_port": int(port),
    "timeout_s": int(timeout_s),
    "compute_host": compute_host,
    "method_order_offset": int(method_order_offset),
    "minimum_frontier_points": int(min_points),
    "remote_identity_retries": int(remote_identity_retries),
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
with open(path, "w") as handle:
    json.dump(
        {
            "schema_version": 2,
            "campaign_id": campaign_id,
            "campaign_uuid": str(uuid.uuid4()),
            "campaign_kind": campaign_kind,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "protocol_fingerprint": fingerprint,
            "protocol": protocol,
            "harness": {
                "manifest": "harness/harness.json",
                "manifest_sha256": harness_sha,
            },
        },
        handle,
        indent=2,
        sort_keys=True,
    )
    handle.write("\n")
PY

verify_remote_pid() {
  local mn="$1" remote_dir="$2"
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe' \
       -a -s '$remote_dir/server.sha256' -a -s '$remote_dir/server.starttime'; \
     pid=\$(cat '$remote_dir/server.pid'); \
     expected=\$(cat '$remote_dir/server.exe'); \
     expected_sha=\$(cat '$remote_dir/server.sha256'); \
     expected_start=\$(cat '$remote_dir/server.starttime'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     actual_sha=\$(sha256sum /proc/\$pid/exe 2>/dev/null | awk '{print \$1}'); \
     actual_start=\$(awk '{print \$22}' /proc/\$pid/stat 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\" \
       -a \"\$actual_sha\" = \"\$expected_sha\" \
       -a \"\$actual_start\" = \"\$expected_start\"" 2>/dev/null
}

probe_remote_process_instance() {
  local mn="$1" remote_dir="$2"
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "if ! test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe' \
         -a -s '$remote_dir/server.starttime'; then \
       printf mismatch; exit 0; \
     fi; \
     pid=\$(cat '$remote_dir/server.pid'); \
     expected=\$(cat '$remote_dir/server.exe'); \
     expected_start=\$(cat '$remote_dir/server.starttime'); \
     if ! test -e /proc/\$pid; then printf exited; exit 0; fi; \
     state=\$(awk '{print \$3}' /proc/\$pid/stat 2>/dev/null); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     actual_start=\$(awk '{print \$22}' /proc/\$pid/stat 2>/dev/null); \
     if test \"\$state\" = Z; then \
       printf exited; \
     elif test -n \"\$actual\" -a \"\$actual\" = \"\$expected\" \
         -a \"\$actual_start\" = \"\$expected_start\"; then \
       printf same; \
     elif ! test -e /proc/\$pid; then \
       printf exited; \
     else \
       printf mismatch; \
     fi" 2>/dev/null
}

local_pid_starttime() {
  local pid=$1
  awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || true
}

verify_local_cn_pid() {
  local pid=$1 expected_path=$2 expected_sha=$3 expected_start=$4
  [[ "$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)" == "$expected_path" ]] &&
    [[ "$(sha256sum "/proc/$pid/exe" 2>/dev/null | awk '{print $1}')" == "$expected_sha" ]] &&
    [[ "$(local_pid_starttime "$pid")" == "$expected_start" ]]
}

preflight_mn_memory() {
  local mn="$1" index_region_bytes="$2" lavd_region_bytes="$3"
  local available_kib available_bytes required_bytes
  available_kib=$(ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "awk '/^MemAvailable:/ {print \$2; exit}' /proc/meminfo")
  [[ "$available_kib" =~ ^[1-9][0-9]*$ ]] || {
    echo "MN capacity preflight could not read MemAvailable on $mn" >&2
    return 1
  }
  available_bytes=$((available_kib * 1024))
  required_bytes=$((index_region_bytes + lavd_region_bytes + MN_MEMORY_HEADROOM_BYTES))
  if (( available_bytes < required_bytes )); then
    printf 'MN capacity preflight failed on %s: MemAvailable=%s required=%s (index=%s Slab=%s headroom=%s)\n' \
      "$mn" "$available_bytes" "$required_bytes" "$index_region_bytes" \
      "$lavd_region_bytes" "$MN_MEMORY_HEADROOM_BYTES" >&2
    return 1
  fi
}

stop_mn() {
  local mn="$1" remote_dir="$2"
  if verify_remote_pid "$mn" "$remote_dir"; then
    ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
      "pid=\$(cat '$remote_dir/server.pid'); kill \$pid 2>/dev/null || true" 2>/dev/null || true
  fi
}

cleanup_active_mn() {
  if [[ -n "$ACTIVE_CN_PID" && -n "$ACTIVE_CN_EXPECTED" &&
        -n "$ACTIVE_CN_SHA256" && -n "$ACTIVE_CN_STARTTIME" ]] &&
      verify_local_cn_pid "$ACTIVE_CN_PID" "$ACTIVE_CN_EXPECTED" \
        "$ACTIVE_CN_SHA256" "$ACTIVE_CN_STARTTIME"; then
    kill "$ACTIVE_CN_PID" 2>/dev/null || true
  fi
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_MN_DIR" ]]; then
    stop_mn "$ACTIVE_MN" "$ACTIVE_MN_DIR"
    ACTIVE_MN=""
    ACTIVE_MN_DIR=""
    ACTIVE_MN_BIN_PATH=""
    ACTIVE_MN_BIN_SHA256=""
    ACTIVE_MN_STARTTIME=""
    ACTIVE_MN_PID=""
  fi
}

write_lifecycle_event() {
  local event="$1" status="${2:-}"
  [[ -n "$FRONTIER_LIFECYCLE_LOG" ]] || return 0
  python3 - "$FRONTIER_LIFECYCLE_LOG" "$event" "$status" \
    "$CAMPAIGN_ID" "$RUN_ID" "$RUN_KIND" "$COMPUTE_HOST" "$$" "$PPID" \
    "$CURRENT_LIFECYCLE_TAG" "$CURRENT_LIFECYCLE_DATASET" \
    "$CURRENT_LIFECYCLE_METHOD" "$CURRENT_LIFECYCLE_VARIANT" \
    "$CURRENT_LIFECYCLE_EF" "$ACTIVE_CN_PID" "$ACTIVE_MN" \
    "$ACTIVE_MN_PID" <<'PY' || true
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from datetime import datetime, timezone

(log_s, event, status, campaign_id, run_id, run_kind, compute_host, pid,
 ppid, tag, dataset, method, variant, ef, cn_pid, memory_host,
 mn_pid) = sys.argv[1:]
log = Path(log_s)
log.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "schema_version": 1,
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "monotonic_ns": int(time.monotonic() * 1_000_000_000),
    "event": event,
    "status": status,
    "campaign_id": campaign_id,
    "run_id": run_id,
    "run_kind": run_kind,
    "compute_host": compute_host,
    "shell_pid": int(pid),
    "parent_pid": int(ppid),
    "cell": {
        "tag": tag,
        "dataset": dataset,
        "method": method,
        "variant": variant,
        "ef": int(ef) if ef else None,
    },
    "active_processes": {
        "compute_pid": int(cn_pid) if cn_pid else None,
        "memory_host": memory_host or None,
        "memory_pid": int(mn_pid) if mn_pid else None,
    },
}
line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
with log.open("a", encoding="utf-8") as handle:
    handle.write(line)
    handle.flush()
    os.fsync(handle.fileno())
latest = Path(str(log) + ".latest.json")
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=latest.parent, delete=False
) as handle:
    temporary = Path(handle.name)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, latest)
directory_fd = os.open(latest.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

frontier_on_exit() {
  local rc="$1"
  trap - EXIT HUP INT TERM
  write_lifecycle_event "process_exit" "$rc"
  cleanup_active_mn
  exit "$rc"
}

frontier_on_signal() {
  local signal="$1" rc="$2"
  trap - EXIT HUP INT TERM
  write_lifecycle_event "signal_${signal}" "$rc"
  cleanup_active_mn
  exit "$rc"
}

start_mn() {
  local mn="$1" remote_dir="$2" index_region_bytes="$3"
  local identity remote_binary remote_sha
  identity=$(ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "resolved=\$(realpath '$GB_BIN_R'); test -x \"\$resolved\"; \
     digest=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); \
     printf '%s|%s\n' \"\$resolved\" \"\$digest\"")
  IFS='|' read -r remote_binary remote_sha <<< "$identity"
  [[ -n "$remote_binary" && "$remote_sha" == "$EXPECTED_BINARY_SHA" ]] || {
    echo "MN binary SHA mismatch on $mn" >&2
    return 1
  }
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     printf '%s\n' '$remote_binary' > '$remote_dir/server.exe'; \
     printf '%s\n' '$remote_sha' > '$remote_dir/server.sha256'; \
     nohup env LD_LIBRARY_PATH='$GB_REMOTE_LD_LIBRARY_PATH' numactl --preferred=1 '$remote_binary' \
       --is-server --num-clients 1 --port "$PORT" \
       --index-region-bytes '$index_region_bytes' > '$remote_dir/mn.out' 2> '$remote_dir/mn.err' < /dev/null & \
     pid=\$!; echo \$pid > '$remote_dir/server.pid'; \
     awk '{print \$22}' /proc/\$pid/stat > '$remote_dir/server.starttime'" \
    2>/dev/null
  ACTIVE_MN="$mn"
  ACTIVE_MN_DIR="$remote_dir"
  ACTIVE_MN_BIN_PATH="$remote_binary"
  ACTIVE_MN_BIN_SHA256="$remote_sha"
  for _ in $(seq 1 80); do
    if verify_remote_pid "$mn" "$remote_dir"; then
      ACTIVE_MN_STARTTIME=$(ssh -o LogLevel=ERROR "$mn" \
        "cat '$remote_dir/server.starttime'")
      ACTIVE_MN_PID=$(ssh -o LogLevel=ERROR "$mn" \
        "cat '$remote_dir/server.pid'")
      return 0
    fi
    sleep 0.1
  done
  echo "MN failed to expose an owned server PID on $mn" >&2
  return 1
}

query_extension_for_dataset() {
  case "$1" in
    BIGANN1M) printf 'u8bin\n' ;;
    SPACEV1M) printf 'i8bin\n' ;;
    *) printf 'fbin\n' ;;
  esac
}

capture_dataset_inputs() {
  local dataset=$1 mn=$2 data=$3 query_suffix=$4 m=$5 efc=$6 phase=$7
  local query_extension
  query_extension=$(query_extension_for_dataset "$dataset")
  local query_file="${data%/}/queries/query-${query_suffix}.${query_extension}"
  local groundtruth_file="${data%/}/queries/groundtruth-${query_suffix}.bin"
  local dump_file="${data%/}/dump/index_m${m}_efc${efc}_node1_of1.dat"
  if [[ ! -s "$query_file" || ! -s "$groundtruth_file" ]] ||
      ! ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
        "test -s '$dump_file'" 2>/dev/null; then
    if [[ "$ALLOW_MISSING_DATASETS" == "1" ]]; then
      printf 'Allowed missing smoke frontier dataset: %s\n' "$dataset" >&2
      return 3
    fi
    printf 'Refusing missing formal frontier dataset: %s\n' "$dataset" >&2
    return 2
  fi
  local query_bytes query_sha groundtruth_bytes groundtruth_sha
  local dump_bytes dump_sha signature
  query_bytes=$(stat -c%s "$query_file")
  query_sha=$(sha256sum "$query_file" | awk '{print $1}')
  groundtruth_bytes=$(stat -c%s "$groundtruth_file")
  groundtruth_sha=$(sha256sum "$groundtruth_file" | awk '{print $1}')
  read -r dump_bytes dump_sha <<< "$(ssh -o LogLevel=ERROR \
    -o StrictHostKeyChecking=no "$mn" \
    "printf '%s ' \"\$(stat -c%s '$dump_file')\"; sha256sum '$dump_file' | awk '{print \$1}'")"
  signature=$(python3 - "$dataset" "$COMPUTE_HOST" \
    "$query_file" "$query_bytes" "$query_sha" \
    "$groundtruth_file" "$groundtruth_bytes" "$groundtruth_sha" \
    "$mn" "$dump_file" "$dump_bytes" "$dump_sha" <<'PY'
import hashlib
import json
import sys

(dataset, compute_host, query_path, query_bytes, query_sha, gt_path, gt_bytes, gt_sha,
 mn, dump_path, dump_bytes, dump_sha) = sys.argv[1:]
records = [
    {"dataset": dataset, "role": "query", "host": compute_host,
     "path": query_path, "bytes": int(query_bytes), "sha256": query_sha},
    {"dataset": dataset, "role": "groundtruth", "host": compute_host,
     "path": gt_path, "bytes": int(gt_bytes), "sha256": gt_sha},
    {"dataset": dataset, "role": "index_dump", "host": mn,
     "path": dump_path, "bytes": int(dump_bytes), "sha256": dump_sha},
]
print(hashlib.sha256(
    json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
).hexdigest())
PY
  )
  if [[ "$phase" == "pre_run" ]]; then
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$dataset" query "$COMPUTE_HOST" \
      "$query_file" "$query_bytes" "$query_sha" >> "$INPUT_MANIFEST"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$dataset" groundtruth "$COMPUTE_HOST" \
      "$groundtruth_file" "$groundtruth_bytes" "$groundtruth_sha" >> "$INPUT_MANIFEST"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$dataset" index_dump "$mn" \
      "$dump_file" "$dump_bytes" "$dump_sha" >> "$INPUT_MANIFEST"
  fi
  printf '%s\n' "$signature"
}

verify_input_manifest() {
  local dataset=$1 phase=$2 signature rc
  set +e
  signature=$(capture_dataset_inputs "$dataset" "$CURRENT_MEMORY_NODE" \
    "$CURRENT_DATA_PATH" "$CURRENT_QUERY_SUFFIX" "$CURRENT_M" "$CURRENT_EFC" \
    "$phase")
  rc=$?
  set -e
  [[ $rc -eq 0 ]] || return "$rc"
  if [[ "$phase" == "pre_run" ]]; then
    CURRENT_INPUT_SIGNATURE="$signature"
  elif [[ "$phase" == "post_run" ]]; then
    [[ -n "$CURRENT_INPUT_SIGNATURE" && \
        "$signature" == "$CURRENT_INPUT_SIGNATURE" ]] || {
      printf 'Input drift detected for frontier dataset %s\n' "$dataset" >&2
      return 2
    }
  else
    printf 'Unknown input-verification phase: %s\n' "$phase" >&2
    return 2
  fi
}

append_row() {
  local dataset="$1"
  local method="$2"
  local variant="$3"
  local threads="$4"
  local query_contexts="$5"
  local coros="$6"
  local ef="$7"
  local m="$8"
  local efc="$9"
  local qs="${10}"
  local lavd="${11}"
  local region_bytes="${12}"
  local env_name="${13}"
  local trace_csv="${14}"
  local json="${15}"
  local err="${16}"
  local status="${17}"
  local metric="${18}"
  local execution_manifest="${19}"
  local memory_host="${20}"
  local mn_binary_sha256="${21}"
  local index_region_bytes="$INDEX_REGION_1M_BYTES"
  [[ "$dataset" == *10M ]] && index_region_bytes="$INDEX_REGION_10M_BYTES"
  python3 - "$CSV" "$dataset" "$method" "$variant" "$CAMPAIGN_ID" \
    "$GB_BIN_SHA256" "$CURRENT_INPUT_SIGNATURE" "$COMPUTE_HOST" \
    "$memory_host" "$mn_binary_sha256" "$RUN_ID" "$RUN_KIND" "$TRACE" \
    "$threads" "$query_contexts" "$coros" "$ef" "$m" "$efc" "$qs" \
    "$lavd" "$index_region_bytes" "$region_bytes" "$env_name" "$trace_csv" \
    "$json" "$err" "$execution_manifest" "$status" "$metric" <<'PY'
import csv
import hashlib
import json
import os
from pathlib import Path
import sys

(csv_path, dataset, method, variant, campaign_id, binary_sha256,
 input_signature, compute_host, memory_host, mn_binary_sha256, run_id,
 run_kind, trace, threads, query_contexts, coros, ef, m, efc, qs, lavd,
 index_region_bytes, region_bytes, env_name, trace_csv, json_path, err_path,
 execution_manifest, status, metric) = sys.argv[1:]
root = Path(csv_path).resolve().parent


def relative(raw):
    if not raw:
        return ""
    return Path(raw).resolve().relative_to(root).as_posix()


recall = qps = p50_us = p95_us = p99_us = posts_per_q = bytes_per_q = processed = expected = failed = ""
protocol_fingerprint = ""
if status == "ok":
    try:
        obj = json.load(open(json_path))
        if not obj["query_contexts"] == int(query_contexts):
            status = "query_context_mismatch"
        q = obj["queries"]
        processed = q.get("processed") or obj.get("num_queries") or ""
        expected = obj.get("num_queries") or ""
        failed = int(expected) - int(processed)
        if failed != 0:
            status = "incomplete_queries"
        recall = q.get("recall", "")
        qps = q.get("queries_per_sec", "")
        if int(q.get("local_latency_samples", -1)) != int(processed):
            status = "incomplete_latency_samples"
        p50_us = q.get("local_latency_p50_us", "")
        p95_us = q.get("local_latency_p95_us", "")
        p99_us = q.get("local_latency_p99_us", "")
        n = float(processed or obj.get("num_queries") or 1)
        posts_per_q = q.get("rdma_posts", 0) / n
        bytes_per_q = q.get("rdma_reads_in_bytes", 0) / n
        protocol = {
            "binary_sha256": binary_sha256,
            "input_signature": input_signature,
            "compute_host": compute_host,
            "memory_host": memory_host,
            "mn_binary_sha256": mn_binary_sha256,
            "dataset": dataset,
            "method": method,
            "variant": variant,
            "threads": int(threads),
            "query_contexts": int(query_contexts),
            "coroutines": int(coros),
            "top_k": 10,
            "metric": metric,
            "measurement_mode": "fixed_query_pool",
            "latency_mode": "thread_local_steady_clock",
            "tcp_port": int(os.environ["FRONTIER_TCP_PORT"]),
            "expected_queries": int(expected),
            "ef": int(ef),
            "m": int(m),
            "efc": int(efc),
            "query_suffix": qs,
            "lavd": int(lavd),
            "index_region_bytes": int(index_region_bytes),
            "lavd_region_bytes": int(region_bytes),
            "env": env_name,
        }
        protocol_fingerprint = hashlib.sha256(
            json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except Exception:
        status = "parse_error"
row = {
    "dataset": dataset,
    "method": method,
    "variant": variant,
    "campaign_id": campaign_id,
    "protocol_fingerprint": protocol_fingerprint,
    "binary_sha256": binary_sha256,
    "input_signature": input_signature,
    "compute_host": compute_host,
    "memory_host": memory_host,
    "mn_binary_sha256": mn_binary_sha256,
    "run_id": run_id,
    "run_kind": run_kind,
    "trace": trace,
    "measurement_mode": "fixed_query_pool",
    "threads": threads,
    "query_contexts": query_contexts,
    "coroutines": coros,
    "top_k": 10,
    "metric": metric,
    "ef": ef,
    "m": m,
    "efc": efc,
    "query_suffix": qs,
    "lavd": lavd,
    "index_region_bytes": index_region_bytes,
    "lavd_region_bytes": region_bytes,
    "env": env_name,
    "recall": recall,
    "qps": qps,
    "p50_us": p50_us,
    "p95_us": p95_us,
    "p99_us": p99_us,
    "posts_per_q": posts_per_q,
    "bytes_per_q": bytes_per_q,
    "processed": processed,
    "expected_queries": expected,
    "failed_queries": failed,
    "trace_csv": relative(trace_csv),
    "json": relative(json_path),
    "stderr": relative(err_path),
    "execution_manifest": relative(execution_manifest),
    "status": status,
}
with open(csv_path, "a", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(row))
    writer.writerow(row)
PY
}

run_case() {
  local dataset="$1"
  local method="$2"
  local variant="$3"
  local mn="$4"
  local data="$5"
  local qs="$6"
  local m="$7"
  local efc="$8"
  local lavd="$9"
  local env_vars="${10}"
  local ip_flag="${11}"
  local ef="${12}"
  local region_bytes="${13}"
  local tag="${dataset}_${variant}_${RUN_ID}_${RUN_KIND}_T${THREADS}_ef${ef}"
  local remote_mn_dir="/tmp/vldb_frontier_${tag}"
  local json="$OUT/${tag}.json"
  local err="$OUT/${tag}.err"
  local mn_out="$OUT/${tag}.mn.out"
  local mn_err="$OUT/${tag}.mn.err"
  local execution_manifest="$OUT/${tag}.execution.json"
  local trace_csv=""
  local -a trace_env=()
  local -a lavd_region_args=()
  local index_region_bytes="$INDEX_REGION_1M_BYTES"
  CURRENT_LIFECYCLE_TAG="$tag"
  CURRENT_LIFECYCLE_DATASET="$dataset"
  CURRENT_LIFECYCLE_METHOD="$method"
  CURRENT_LIFECYCLE_VARIANT="$variant"
  CURRENT_LIFECYCLE_EF="$ef"
  write_lifecycle_event "case_start" "0"
  [[ "$dataset" == *10M ]] && index_region_bytes="$INDEX_REGION_10M_BYTES"
  if [[ "$TRACE" == "1" ]]; then
    trace_csv="$OUT/${tag}.trace.csv"
    trace_env=(GB_QUERY_TRACE="$trace_csv")
  fi
  if [[ "$lavd" != "0" && "$region_bytes" != "0" ]]; then
    lavd_region_args=(--lavd-region-bytes "$region_bytes")
  fi
  echo "=== $tag @ MN=$mn ==="
  preflight_mn_memory "$mn" "$index_region_bytes" "$region_bytes"
  start_mn "$mn" "$remote_mn_dir" "$index_region_bytes"
  local mn_binary_path="$ACTIVE_MN_BIN_PATH"
  local mn_binary_sha256="$ACTIVE_MN_BIN_SHA256"
  local mn_starttime="$ACTIVE_MN_STARTTIME"
  local mn_pid="$ACTIVE_MN_PID"
  local cn_expected cn_pid cn_starttime cn_verified=0 identity_failed=0 timed_out=0
  local mn_exit_observed=0 mn_state
  local started_at deadline rc state current_path current_start
  local mn_probe="" probe_ok=0 attempt identity_failure_reason=""
  cn_expected=$(realpath "$GB_BIN")
  env LD_LIBRARY_PATH="$GB_LOCAL_LD_LIBRARY_PATH" GB_QUERY_LATENCY=1 \
    FRONTIER_TCP_PORT="$PORT" \
    "${trace_env[@]}" $env_vars numactl --preferred=1 "$GB_BIN" \
    --servers "$mn" --initiator --threads "$THREADS" --coroutines "$COROS" \
    --query-contexts "$QUERY_CONTEXTS" \
    --port "$PORT" \
    --index-region-bytes "$index_region_bytes" \
    --data-path "$data" --query-suffix "$qs" \
    --ef-search "$ef" --ef-construction "$efc" --m "$m" --k 10 \
    --label "$tag" --spec-k 1 --load-index --lavd "$lavd" \
    "${lavd_region_args[@]}" $ip_flag \
    > "$json" 2> "$err" &
  cn_pid=$!
  cn_starttime=$(local_pid_starttime "$cn_pid")
  ACTIVE_CN_PID="$cn_pid"
  ACTIVE_CN_EXPECTED="$cn_expected"
  ACTIVE_CN_SHA256="$GB_BIN_SHA256"
  ACTIVE_CN_STARTTIME="$cn_starttime"
  for _ in $(seq 1 100); do
    if verify_local_cn_pid "$cn_pid" "$cn_expected" "$GB_BIN_SHA256" \
        "$cn_starttime" && verify_remote_pid "$mn" "$remote_mn_dir"; then
      cn_verified=1
      break
    fi
    kill -0 "$cn_pid" 2>/dev/null || break
    sleep 0.05
  done
  if [[ "$cn_verified" != "1" ]]; then
    identity_failed=1
    identity_failure_reason="cn_initial_identity_unverified"
    if [[ "$(local_pid_starttime "$cn_pid")" == "$cn_starttime" ]]; then
      kill "$cn_pid" 2>/dev/null || true
    fi
  fi
  started_at=$SECONDS
  deadline=$((started_at + TIMEOUT_S))
  while [[ "$identity_failed" == "0" ]] && kill -0 "$cn_pid" 2>/dev/null; do
    state=$(awk '{print $3}' "/proc/$cn_pid/stat" 2>/dev/null || true)
    [[ -n "$state" ]] || break
    [[ "$state" != "Z" ]] || break
    current_path=$(readlink -f "/proc/$cn_pid/exe" 2>/dev/null || true)
    current_start=$(local_pid_starttime "$cn_pid")
    if [[ "$current_path" != "$cn_expected" || "$current_start" != "$cn_starttime" ]]; then
      state=$(awk '{print $3}' "/proc/$cn_pid/stat" 2>/dev/null || true)
      [[ -n "$state" && "$state" != "Z" ]] || break
      identity_failed=1
      identity_failure_reason="cn_identity_changed"
      kill "$cn_pid" 2>/dev/null || true
      break
    fi
    if [[ "$mn_exit_observed" == "0" ]]; then
      mn_probe=""
      probe_ok=0
      for attempt in $(seq 1 "$REMOTE_IDENTITY_RETRIES"); do
        if mn_probe=$(probe_remote_process_instance "$mn" "$remote_mn_dir"); then
          if [[ "$mn_probe" == "same" || "$mn_probe" == "exited" ]]; then
            probe_ok=1
            break
          fi
        fi
        sleep 0.05
      done
      case "$mn_probe" in
        same) ;;
        exited) mn_exit_observed=1 ;;
        mismatch)
          identity_failed=1
          identity_failure_reason="mn_mismatch"
          ;;
        *)
          identity_failed=1
          identity_failure_reason="mn_probe_unreachable"
          ;;
      esac
      if [[ "$identity_failed" == "1" ]]; then
        kill "$cn_pid" 2>/dev/null || true
        break
      fi
    fi
    if (( SECONDS >= deadline )); then
      timed_out=1
      kill "$cn_pid" 2>/dev/null || true
      break
    fi
    sleep 1
  done
  set +e
  wait "$cn_pid"
  rc=$?
  set -e
  [[ "$timed_out" == "0" ]] || rc=124
  [[ "$identity_failed" == "0" ]] || rc=125
  write_lifecycle_event "cn_reaped" "$rc"
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
  local mn_identity_verified=1
  mn_probe=""
  probe_ok=0
  write_lifecycle_event "mn_final_probe_start" "$rc"
  for attempt in $(seq 1 "$REMOTE_IDENTITY_RETRIES"); do
    if mn_probe=$(probe_remote_process_instance "$mn" "$remote_mn_dir"); then
      if [[ "$mn_probe" == "same" || "$mn_probe" == "exited" ]]; then
        probe_ok=1
        break
      fi
    fi
    sleep 0.05
  done
  if [[ "$probe_ok" != "1" ]]; then
    mn_identity_verified=0
    [[ -n "$identity_failure_reason" ]] || \
      identity_failure_reason="mn_final_${mn_probe:-probe_unreachable}"
    [[ $rc -ne 0 ]] || rc=126
  fi
  write_lifecycle_event "mn_final_probe_complete" "$rc"
  write_lifecycle_event "mn_copy_start" "$rc"
  if ! ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
      "cat '$remote_mn_dir/mn.out'" > "$mn_out" 2>/dev/null; then
    [[ $rc -ne 0 ]] || rc=126
  fi
  if ! ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
      "cat '$remote_mn_dir/mn.err'" > "$mn_err" 2>/dev/null; then
    [[ $rc -ne 0 ]] || rc=126
  fi
  write_lifecycle_event "mn_copy_complete" "$rc"
  stop_mn "$mn" "$remote_mn_dir"
  ACTIVE_MN=""
  ACTIVE_MN_DIR=""
  ACTIVE_MN_BIN_PATH=""
  ACTIVE_MN_BIN_SHA256=""
  ACTIVE_MN_STARTTIME=""
  ACTIVE_MN_PID=""
  python3 - "$OUT" "$execution_manifest" "$CAMPAIGN_ID" "$tag" "$dataset" \
    "$method" "$variant" "$CURRENT_INPUT_SIGNATURE" "$COMPUTE_HOST" \
    "$cn_pid" "$cn_expected" "$GB_BIN_SHA256" "$cn_starttime" "$mn" \
    "$mn_pid" "$mn_binary_path" "$mn_binary_sha256" "$mn_starttime" \
    "$cn_verified" "$mn_identity_verified" "$mn_exit_observed" "$rc" \
    "$identity_failure_reason" \
    "$json" "$err" "$mn_out" \
    "$mn_err" "$trace_csv" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

(root_s, manifest_s, campaign_id, tag, dataset, method, variant,
 input_signature, compute_host, cn_pid, cn_path, cn_sha, cn_starttime,
 memory_host, mn_pid, mn_path, mn_sha, mn_starttime, cn_verified,
 mn_verified, mn_exit_observed, exit_code, identity_failure_reason,
 *artifact_paths) = sys.argv[1:]
root = Path(root_s).resolve()
manifest = Path(manifest_s).resolve()


def artifact(raw):
    if not raw:
        return None
    path = Path(raw).resolve()
    relative = path.relative_to(root).as_posix()
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


records = [record for record in map(artifact, artifact_paths) if record]
payload = {
    "schema_version": 1,
    "campaign_id": campaign_id,
    "cell": {
        "tag": tag,
        "dataset": dataset,
        "method": method,
        "variant": variant,
        "input_signature": input_signature,
    },
    "compute_process": {
        "host": compute_host,
        "pid": int(cn_pid),
        "executable": cn_path,
        "binary_sha256": cn_sha,
        "proc_starttime": int(cn_starttime),
        "identity_verified": cn_verified == "1",
    },
    "memory_process": {
        "host": memory_host,
        "pid": int(mn_pid),
        "executable": mn_path,
        "binary_sha256": mn_sha,
        "proc_starttime": int(mn_starttime),
        "identity_verified": mn_verified == "1",
        "exit_observed_before_compute_exit": mn_exit_observed == "1",
    },
    "exit_code": int(exit_code),
    "identity_failure_reason": identity_failure_reason,
    "artifacts": records,
}
manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
  write_lifecycle_event "execution_manifest_written" "$rc"
  local metric="l2"
  [[ -n "$ip_flag" ]] && metric="ip"
  write_lifecycle_event "csv_commit_start" "$rc"
  if [[ $rc -eq 0 && -s "$json" ]]; then
    append_row "$dataset" "$method" "$variant" "$THREADS" "$QUERY_CONTEXTS" \
      "$COROS" "$ef" "$m" "$efc" "$qs" "$lavd" "$region_bytes" \
      "${env_vars:-none}" "$trace_csv" "$json" "$err" "ok" "$metric" \
      "$execution_manifest" "$mn" "$mn_binary_sha256"
  else
    echo "  rc=$rc"
    tail -20 "$err" || true
    append_row "$dataset" "$method" "$variant" "$THREADS" "$QUERY_CONTEXTS" \
      "$COROS" "$ef" "$m" "$efc" "$qs" "$lavd" "$region_bytes" \
      "${env_vars:-none}" "$trace_csv" "$json" "$err" "rc_$rc" "$metric" \
      "$execution_manifest" "$mn" "$mn_binary_sha256"
  fi
  write_lifecycle_event "csv_committed" "$rc"
  CURRENT_LIFECYCLE_TAG=""
  CURRENT_LIFECYCLE_DATASET=""
  CURRENT_LIFECYCLE_METHOD=""
  CURRENT_LIFECYCLE_VARIANT=""
  CURRENT_LIFECYCLE_EF=""
}

run_dataset() {
  local dataset="$1" mn="$2" data="$3" qs="$4" m="$5" efc="$6" ip_flag="$7" slab_env="$8" ef_list="$9" region_bytes="${10}"
  local input_rc point_index=0
  CURRENT_MEMORY_NODE="$mn"
  CURRENT_DATA_PATH="$data"
  CURRENT_QUERY_SUFFIX="$qs"
  CURRENT_M="$m"
  CURRENT_EFC="$efc"
  set +e
  verify_input_manifest "$dataset" "pre_run"
  input_rc=$?
  set -e
  if [[ $input_rc -eq 3 && "$CAMPAIGN_KIND" == "smoke" && \
      "$ALLOW_MISSING_DATASETS" == "1" ]]; then
    return 0
  fi
  [[ $input_rc -eq 0 ]] || return "$input_rc"
  for ef in $ef_list; do
    if (( (point_index + METHOD_ORDER_OFFSET) % 2 == 0 )); then
      run_case "$dataset" "SHINE" "shine_path" "$mn" "$data" "$qs" "$m" "$efc" 0 "" "$ip_flag" "$ef" 0
      run_case "$dataset" "SlabWalk" "slabwalk_expansion" "$mn" "$data" "$qs" "$m" "$efc" 8 "$slab_env" "$ip_flag" "$ef" "$region_bytes"
    else
      run_case "$dataset" "SlabWalk" "slabwalk_expansion" "$mn" "$data" "$qs" "$m" "$efc" 8 "$slab_env" "$ip_flag" "$ef" "$region_bytes"
      run_case "$dataset" "SHINE" "shine_path" "$mn" "$data" "$qs" "$m" "$efc" 0 "" "$ip_flag" "$ef" 0
    fi
    point_index=$((point_index + 1))
  done
  verify_input_manifest "$dataset" "post_run"
}

prepare_tti10m_query_pool() {
  local query_dir="$GB_DATA/tti-10m/queries"
  local query_out="$query_dir/query-u10k.fbin"
  local groundtruth_out="$query_dir/groundtruth-u10k.bin"
  [[ -s "$QUERY_POOL_PREPARER" ]] || {
    printf 'Missing fixed-query-pool preparer: %s\n' "$QUERY_POOL_PREPARER" >&2
    return 2
  }
  python3 "$QUERY_POOL_PREPARER" \
    --query "$query_dir/query-uniform.fbin" \
    --groundtruth "$query_dir/groundtruth-uniform.bin" \
    --limit 10000 \
    --query-fbin "$query_out" \
    --groundtruth-bin "$groundtruth_out" \
    --manifest "$OUT/TTI10M_query_pool.json" >/dev/null
}

trap 'frontier_on_exit "$?"' EXIT
trap 'frontier_on_signal HUP 129' HUP
trap 'frontier_on_signal INT 130' INT
trap 'frontier_on_signal TERM 143' TERM

# Datasets that already have memory-node index dumps and can be queried now.
for dataset in $DATASETS; do
  case "$dataset" in
    SIFT1M)
      run_dataset "SIFT1M"  "$SIFT1_MN" "$GB_DATA/sift1m/"  "uniform" 16 100 "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$SIFT1_EFS" "$LAVD_SIFT1_REGION_BYTES"
      ;;
    GIST1M)
      run_dataset "GIST1M"  "$GIST1_MN" "$GB_DATA/gist1m/"  "u10k"    16 100 "" \
        "SHINE_CRANE=1 SHINE_LAVD_RABITQ_B=2 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$GIST1_EFS" "$LAVD_GIST1_REGION_BYTES"
      ;;
    DEEP10M)
      run_dataset "DEEP10M" "$DEEP_MN" "$GB_DATA/deep10m/" "uniform" "$DEEP_M" "$DEEP_EFC" "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2" \
        "$DEEP_EFS" "$LAVD_10M_REGION_BYTES"
      ;;
    DEEP1M)
      run_dataset "DEEP1M" "$DEEP1_MN" "$GB_DATA/deep1m/" "uniform" "$DEEP1_M" "$DEEP1_EFC" "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$DEEP1_EFS" "$LAVD_DEEP1_REGION_BYTES"
      ;;
    BIGANN1M)
      run_dataset "BIGANN1M" "$BIGANN1_MN" "$GB_DATA/bigann1m/" "uniform" 16 100 "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$BIGANN1_EFS" "$LAVD_BIGANN1_REGION_BYTES"
      ;;
    SPACEV1M)
      run_dataset "SPACEV1M" "$SPACEV1_MN" "$GB_DATA/spacev1m/" "uniform" 16 100 "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$SPACEV1_EFS" "$LAVD_SPACEV1_REGION_BYTES"
      ;;
    TURING1M)
      run_dataset "TURING1M" "$TURING1_MN" "$GB_DATA/turing1m/" "uniform" 16 100 "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$TURING1_EFS" "$LAVD_TURING1_REGION_BYTES"
      ;;
    TEXT10M)
      prepare_tti10m_query_pool
      run_dataset "TEXT10M" "$TEXT_MN" "$GB_DATA/tti-10m/" "u10k" "$TEXT_M" "$TEXT_EFC" "--ip-dist" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2" \
        "100 150 200 300 500 800" "$LAVD_10M_REGION_BYTES"
      ;;
    TEXT1M)
      run_dataset "TEXT1M" "$TEXT1_MN" "$GB_DATA/tti1m/" "uniform" "$TEXT1_M" "$TEXT1_EFC" "--ip-dist" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 $LAVD_PARALLEL_BUILD_ENV" \
        "$TEXT1_EFS" "$LAVD_TEXT1_REGION_BYTES"
      ;;
    SIFT10M)
      run_dataset "SIFT10M" "$SIFT10_MN" "$GB_DATA/sift10m/" "uniform" "$SIFT10_M" "$SIFT10_EFC" "" \
        "SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2" \
        "64 96 128 160 200 300" "$LAVD_10M_REGION_BYTES"
      ;;
    *)
      echo "Unknown or not-yet-ready SlabWalk/SHINE dataset: $dataset" >&2
      exit 2
      ;;
  esac
done

verify_harness
python3 "$FRONTIER_VERIFIER" \
  --root "$OUT" \
  --expected-binary-sha "$EXPECTED_BINARY_SHA" \
  --expected-campaign-id "$CAMPAIGN_ID" \
  --expected-run-id "$RUN_ID" \
  --expected-run-kind "$RUN_KIND" \
  --expected-datasets "${DATASETS// /,}" \
  --expected-threads "$THREADS" \
  --expected-query-contexts "$QUERY_CONTEXTS" \
  --expected-coroutines "$COROS" \
  --expected-trace "$TRACE" \
  --min-points "$MIN_POINTS" > "$OUT/semantic_verification.json"
python3 "$EVIDENCE_TOOL" seal --root "$OUT" --campaign "$OUT/campaign.json" >/dev/null
python3 "$EVIDENCE_TOOL" verify --root "$OUT" >/dev/null
[[ -s "$OUT/SEALED.json" && -s "$OUT/SHA256SUMS" ]] || {
  echo "Frontier evidence seal is incomplete" >&2
  exit 2
}
echo "Wrote $CSV"
