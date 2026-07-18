#!/usr/bin/env bash
# Capture query-only CPU profiles after the Slab/placement setup phase.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
QUERY_PROFILE_SOURCE_SCRIPT_DIR=${QUERY_PROFILE_SOURCE_SCRIPT_DIR:-$SCRIPT_DIR}
QUERY_PROFILE_SCRIPT_PATH=$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")
REPO_ROOT=${REPO_ROOT:-$(cd -- "$QUERY_PROFILE_SOURCE_SCRIPT_DIR/../.." && pwd)}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$QUERY_PROFILE_SOURCE_SCRIPT_DIR/vldb_evidence_bundle.py}
GB_BIN=${GB_BIN:-$REPO_ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
MN_SIFT1M=${MN_SIFT1M:-skv-node5}
MN_GIST1M=${MN_GIST1M:-skv-node3}
MN_DEEP1M=${MN_DEEP1M:-skv-node5}
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
LAVD_GIST1_REGION_BYTES=${LAVD_GIST1_REGION_BYTES:-9663676416}
DRY_RUN=${DRY_RUN:-0}

# Execute the campaign from content-addressed copies. This prevents a long run
# from silently changing protocol when a developer edits a live runner.
if [[ "${VLDB_QUERY_PROFILE_HARNESS_FROZEN:-0}" != "1" ]]; then
  mkdir -p "$OUT"
  snapshot_json=$(python3 "$EVIDENCE_TOOL" snapshot \
    --out-dir "$OUT/harness" \
    --entry runner="$QUERY_PROFILE_SCRIPT_PATH" \
    --entry evidence_tool="$EVIDENCE_TOOL")
  read -r frozen_runner frozen_tool harness_sha <<< "$(python3 - "$OUT/harness" "$snapshot_json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
payload = json.loads(sys.argv[2])
print(
    root / payload["entries"]["runner"]["path"],
    root / payload["entries"]["evidence_tool"]["path"],
    payload["manifest_sha256"],
)
PY
  )"
  exec env \
    VLDB_QUERY_PROFILE_HARNESS_FROZEN=1 \
    QUERY_PROFILE_SOURCE_SCRIPT_DIR="$QUERY_PROFILE_SOURCE_SCRIPT_DIR" \
    REPO_ROOT="$REPO_ROOT" \
    OUT="$OUT" \
    CAMPAIGN_ID="$CAMPAIGN_ID" \
    EVIDENCE_TOOL="$frozen_tool" \
    HARNESS_MANIFEST="$OUT/harness/harness.json" \
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
ACTIVE_MN=""
ACTIVE_REMOTE_DIR=""
ACTIVE_CN_PID=""
ACTIVE_CN_EXPECTED=""
ACTIVE_CN_SHA256=""
ACTIVE_CN_STARTTIME=""
ACTIVE_MN_BIN_PATH=""
ACTIVE_MN_BIN_SHA256=""
ACTIVE_MN_STARTTIME=""

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
GB_BIN_RESOLVED=$(realpath "$GB_BIN")
COMPUTE_HOST=$(hostname)
[[ ! -e "$OUT/campaign.json" ]] || {
  echo "Refusing to overwrite existing query-profile campaign" >&2
  exit 2
}
python3 - "$OUT/campaign.json" "$CAMPAIGN_ID" "$GB_BIN_SHA256" \
  "$DATASETS" "$METHODS" "$THREADS" "$COROUTINES" "$EF" "$TOP_K" \
  "$TILE" "$PROFILE_S" "$PERF_FREQ" "$PERF_CMD" "$PERF_REPORT_CMD" \
  "$PERF_DATA_FIXUP_CMD" "$PORT" "$COMPUTE_HOST" "$INDEX_REGION_1M_BYTES" \
  "$INDEX_REGION_10M_BYTES" "$LAVD_DEEP10_REGION_BYTES" \
  "$LAVD_GIST1_REGION_BYTES" \
  "$CAPTURE_PERF" "$COMPUTE_RECALL" "$QUERY_CONTEXTS" \
  "$MN_SIFT1M" "$MN_GIST1M" "$MN_DEEP1M" "$MN_DEEP10M" \
  "$HARNESS_MANIFEST" "$HARNESS_MANIFEST_SHA256" <<'PY'
import hashlib, json, pathlib, sys, uuid
from datetime import datetime, timezone

(path, campaign_id, binary_sha256, datasets, methods, threads, coroutines,
 ef, top_k, tile, profile_s, perf_freq, perf_command, perf_report_command,
 perf_data_fixup_command, port, host, index_region_1m, index_region_10m,
 lavd_deep10_region, lavd_gist1_region, capture_perf, compute_recall,
 query_contexts, mn_sift1m, mn_gist1m, mn_deep1m, mn_deep10m, harness_manifest_path,
 harness_manifest_sha256) = sys.argv[1:]
harness_path = pathlib.Path(harness_manifest_path)
harness_bytes = harness_path.read_bytes()
if hashlib.sha256(harness_bytes).hexdigest() != harness_manifest_sha256:
    raise SystemExit("query-profile harness manifest drifted before campaign creation")
harness = json.loads(harness_bytes)
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
        "GIST1M": int(lavd_gist1_region),
        "DEEP1M": 4294967296,
        "DEEP10M": int(lavd_deep10_region),
    },
    "memory_nodes_by_dataset": {
        "SIFT1M": mn_sift1m,
        "GIST1M": mn_gist1m,
        "DEEP1M": mn_deep1m,
        "DEEP10M": mn_deep10m,
    },
    "compute_host": host,
    "profile_scope": "query-only-after-phase-marker",
    "harness": {
        "manifest": "harness/harness.json",
        "manifest_sha256": harness_manifest_sha256,
        "entries": harness["entries"],
    },
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
with open(path, "w") as handle:
    json.dump({
        "campaign_id": campaign_id,
        "campaign_uuid": str(uuid.uuid4()),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

dataset_spec() {
  case "$1" in
    SIFT1M) printf '%s|%s|%s|%s|%s|%s|%s\n' "$MN_SIFT1M" "$GB_DATA/sift1m" 16 100 4294967296 "$INDEX_REGION_1M_BYTES" uniform ;;
    GIST1M) printf '%s|%s|%s|%s|%s|%s|%s\n' "$MN_GIST1M" "$GB_DATA/gist1m" 16 100 "$LAVD_GIST1_REGION_BYTES" "$INDEX_REGION_1M_BYTES" u10k ;;
    DEEP1M) printf '%s|%s|%s|%s|%s|%s|%s\n' "$MN_DEEP1M" "$GB_DATA/deep1m" 16 100 4294967296 "$INDEX_REGION_1M_BYTES" uniform ;;
    DEEP10M) printf '%s|%s|%s|%s|%s|%s|%s\n' "$MN_DEEP10M" "$GB_DATA/deep10m" 32 200 "$LAVD_DEEP10_REGION_BYTES" "$INDEX_REGION_10M_BYTES" uniform ;;
    *) echo "Unsupported profile dataset: $1" >&2; return 2 ;;
  esac
}

verify_remote_pid() {
  local host=$1 remote_dir=$2
  ssh -o LogLevel=ERROR "$host" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe' \
       -a -s '$remote_dir/server.sha256' -a -s '$remote_dir/server.starttime'; \
     pid=\$(cat '$remote_dir/server.pid'); expected=\$(cat '$remote_dir/server.exe'); \
     expected_sha=\$(cat '$remote_dir/server.sha256'); \
     expected_starttime=\$(cat '$remote_dir/server.starttime'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     actual_sha=\$(sha256sum /proc/\$pid/exe 2>/dev/null | awk '{print \$1}'); \
     actual_starttime=\$(awk '{print \$22}' /proc/\$pid/stat 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\" \
       -a -n \"\$actual_sha\" -a \"\$actual_sha\" = \"\$expected_sha\" \
       -a -n \"\$actual_starttime\" \
       -a \"\$actual_starttime\" = \"\$expected_starttime\"" \
    2>/dev/null
}

stop_mn() {
  local host=$1 remote_dir=$2
  local identity pid expected_starttime current_starttime
  identity=$(ssh -o LogLevel=ERROR "$host" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.starttime'; \
     printf '%s|%s\n' \"\$(cat '$remote_dir/server.pid')\" \
       \"\$(cat '$remote_dir/server.starttime')\"") || {
    echo "Memory-node ownership record is missing on $host" >&2
    return 1
  }
  IFS='|' read -r pid expected_starttime <<< "$identity"
  if verify_remote_pid "$host" "$remote_dir"; then
    ssh -o LogLevel=ERROR "$host" \
      "current=\$(awk '{print \$22}' /proc/$pid/stat 2>/dev/null); \
       test -n \"\$current\" -a \"\$current\" = '$expected_starttime'; \
       kill $pid" || {
      echo "Memory-node termination request failed on $host" >&2
      return 1
    }
  else
    current_starttime=$(ssh -o LogLevel=ERROR "$host" \
      "awk '{print \$22}' /proc/$pid/stat 2>/dev/null || true")
    if [[ -z "$current_starttime" || "$current_starttime" != "$expected_starttime" ]]; then
      return 0
    fi
    echo "Memory-node PID identity changed before shutdown on $host" >&2
    return 1
  fi
  for _ in $(seq 1 200); do
    current_starttime=$(ssh -o LogLevel=ERROR "$host" \
      "awk '{print \$22}' /proc/$pid/stat 2>/dev/null || true")
    if [[ -z "$current_starttime" || "$current_starttime" != "$expected_starttime" ]]; then
      return 0
    fi
    sleep 0.05
  done
  echo "Memory-node process did not terminate on $host (pid=$pid)" >&2
  return 1
}

cleanup() {
  local cleanup_rc=0
  if [[ -n "$ACTIVE_CN_PID" ]] && kill -0 "$ACTIVE_CN_PID" 2>/dev/null; then
    if verify_local_cn_pid "$ACTIVE_CN_PID" "$ACTIVE_CN_EXPECTED" \
      "$ACTIVE_CN_SHA256" "$ACTIVE_CN_STARTTIME"; then
      kill "$ACTIVE_CN_PID" 2>/dev/null || cleanup_rc=1
    else
      echo "Refusing to signal an unowned compute-node PID" >&2
      cleanup_rc=1
    fi
  fi
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_REMOTE_DIR" ]]; then
    stop_mn "$ACTIVE_MN" "$ACTIVE_REMOTE_DIR" || cleanup_rc=1
  fi
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
  ACTIVE_MN=""
  ACTIVE_REMOTE_DIR=""
  ACTIVE_MN_BIN_PATH=""
  ACTIVE_MN_BIN_SHA256=""
  ACTIVE_MN_STARTTIME=""
  return "$cleanup_rc"
}

on_exit() {
  local rc=$?
  trap - EXIT INT TERM
  cleanup || rc=1
  exit "$rc"
}

start_mn() {
  local host=$1 remote_dir=$2 index_region_bytes=$3
  local identity remote_binary remote_sha
  identity=$(ssh -o LogLevel=ERROR "$host" \
    "resolved=\$(realpath '$GB_BIN_R'); test -x \"\$resolved\"; \
     digest=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); \
     printf '%s|%s\\n' \"\$resolved\" \"\$digest\"")
  IFS='|' read -r remote_binary remote_sha <<< "$identity"
  [[ -n "$remote_binary" && "$remote_sha" == "$GB_BIN_SHA256" ]] || {
    echo "Memory-node binary SHA does not match the CN binary on $host" >&2
    return 1
  }
  ssh -o LogLevel=ERROR "$host" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     printf '%s\\n' '$remote_binary' > '$remote_dir/server.exe'; \
     printf '%s\\n' '$remote_sha' > '$remote_dir/server.sha256'; \
     nohup env LD_LIBRARY_PATH='$GB_LIB' numactl --preferred=1 '$remote_binary' \
       --is-server --num-clients 1 --port "$PORT" \
       --index-region-bytes '$index_region_bytes' > '$remote_dir/mn.out' \
       2> '$remote_dir/mn.err' < /dev/null & \
     pid=\$!; echo \$pid > '$remote_dir/server.pid'; \
     awk '{print \$22}' /proc/\$pid/stat > '$remote_dir/server.starttime'"
  ACTIVE_MN=$host
  ACTIVE_REMOTE_DIR=$remote_dir
  ACTIVE_MN_BIN_PATH=$remote_binary
  ACTIVE_MN_BIN_SHA256=$remote_sha
  for _ in $(seq 1 100); do
    if verify_remote_pid "$host" "$remote_dir"; then
      ACTIVE_MN_STARTTIME=$(ssh -o LogLevel=ERROR "$host" \
        "cat '$remote_dir/server.starttime'")
      return 0
    fi
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

local_pid_starttime() {
  local pid=$1
  python3 - "/proc/$pid/stat" <<'PY'
import pathlib
import sys

raw = pathlib.Path(sys.argv[1]).read_text()
right_paren = raw.rfind(")")
if right_paren < 0:
    raise SystemExit("malformed /proc stat")
fields_after_comm = raw[right_paren + 2:].split()
if len(fields_after_comm) <= 19:
    raise SystemExit("short /proc stat")
print(fields_after_comm[19])
PY
}

verify_local_cn_pid() {
  local pid=$1 expected=$2 expected_sha=$3 expected_starttime=$4
  local actual actual_sha actual_starttime
  actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
  actual_sha=$(sha256sum "/proc/$pid/exe" 2>/dev/null | awk '{print $1}')
  actual_starttime=$(local_pid_starttime "$pid" 2>/dev/null || true)
  [[ -n "$actual" && "$actual" == "$expected" &&
     -n "$actual_sha" && "$actual_sha" == "$expected_sha" &&
     -n "$actual_starttime" && "$actual_starttime" == "$expected_starttime" ]]
}

verify_cell_inputs() {
  local phase=$1 mn=$2 index_path=$3 expected_index_sha=$4
  local query_path=$5 expected_query_sha=$6 ground_truth_path=$7
  local expected_ground_truth_sha=$8 output=$9
  local actual_query_sha actual_ground_truth_sha="" actual_index_sha
  verify_harness
  actual_query_sha=$(sha256sum "$query_path" | awk '{print $1}')
  [[ "$actual_query_sha" == "$expected_query_sha" ]] || {
    echo "$phase query input drift: $query_path" >&2
    return 1
  }
  if [[ -n "$ground_truth_path" ]]; then
    actual_ground_truth_sha=$(sha256sum "$ground_truth_path" | awk '{print $1}')
    [[ "$actual_ground_truth_sha" == "$expected_ground_truth_sha" ]] || {
      echo "$phase ground-truth input drift: $ground_truth_path" >&2
      return 1
    }
  fi
  actual_index_sha=$(ssh -o LogLevel=ERROR "$mn" \
    "test -s '$index_path'; sha256sum '$index_path' | awk '{print \$1}'")
  [[ "$actual_index_sha" == "$expected_index_sha" ]] || {
    echo "$phase remote-index input drift: $mn:$index_path" >&2
    return 1
  }
  printf '%s\t%s\t%s\t%s\n' "$phase" "$actual_query_sha" \
    "${actual_ground_truth_sha:--}" "$actual_index_sha" >> "$output"
}

run_profile() {
  local dataset=$1 method=$2
  local mn data m efc region index_region_bytes recall_suffix
  IFS='|' read -r mn data m efc region index_region_bytes recall_suffix <<< "$(dataset_spec "$dataset")"
  local suffix="profile${TILE}x"
  if [[ "$COMPUTE_RECALL" == "1" ]]; then
    suffix=$recall_suffix
  fi
  local tag="${dataset}_${method}_T${THREADS}_C${COROUTINES}_ef${EF}"
  local remote_dir="/tmp/${CAMPAIGN_ID//[^[:alnum:]]/_}_${tag}"
  local stdout="$OUT/$tag.json" stderr="$OUT/$tag.err"
  local provenance="$OUT/$tag.provenance.json"
  local input_checks="$OUT/$tag.input_checks.tsv"
  local perf_data="$OUT/$tag.perf.data" report="$OUT/$tag.perf.txt"
  local -a method_args=(--lavd 0)
  local -a recall_args=(--no-recall)
  if [[ "$COMPUTE_RECALL" == "1" ]]; then
    recall_args=()
  fi
  local method_env=""
  if [[ "$method" == "slabwalk" ]]; then
    method_args=(--lavd 8 --lavd-region-bytes "$region")
    if [[ "$dataset" == "DEEP10M" ]]; then
      method_env="SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2"
    elif [[ "$dataset" == "GIST1M" ]]; then
      method_env="SHINE_CRANE=1 SHINE_LAVD_RABITQ_B=2 GB_BITMAP_DEDUP=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2 SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1"
    else
      method_env="SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2 SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1"
    fi
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
  local index_path="$data/dump/index_m${m}_efc${efc}_node1_of1.dat"
  local index_sha
  index_sha=$(ssh -o LogLevel=ERROR "$mn" \
    "test -s '$index_path'; sha256sum '$index_path' | awk '{print \$1}'") || {
      echo "Missing or unreadable $dataset index on $mn" >&2; return 2;
    }
  [[ "$index_sha" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Invalid index SHA for $dataset on $mn" >&2; return 2;
  }
  local query_path ground_truth_path="" query_sha ground_truth_sha=""
  query_path=$(python3 - "$data/queries" "query-$suffix" <<'PY'
import pathlib, sys
root, stem = pathlib.Path(sys.argv[1]), sys.argv[2]
matches = [path for path in root.iterdir() if path.is_file() and path.stem == stem]
if len(matches) != 1:
    raise SystemExit(f"expected one {stem} file, found {len(matches)}")
print(matches[0])
PY
  )
  query_sha=$(sha256sum "$query_path" | awk '{print $1}')
  if [[ "$COMPUTE_RECALL" == "1" ]]; then
    ground_truth_path=$(python3 - "$data/queries" "groundtruth-$suffix" <<'PY'
import pathlib, sys
root, stem = pathlib.Path(sys.argv[1]), sys.argv[2]
matches = [path for path in root.iterdir() if path.is_file() and path.stem == stem]
if len(matches) != 1:
    raise SystemExit(f"expected one {stem} file, found {len(matches)}")
print(matches[0])
PY
    )
    ground_truth_sha=$(sha256sum "$ground_truth_path" | awk '{print $1}')
  fi
  : > "$input_checks"
  verify_cell_inputs "pre_run" "$mn" "$index_path" "$index_sha" \
    "$query_path" "$query_sha" "$ground_truth_path" "$ground_truth_sha" \
    "$input_checks"
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
  local expected cn_starttime
  expected=$(realpath "$GB_BIN")
  for _ in $(seq 1 100); do
    cn_starttime=$(local_pid_starttime "$cn_pid" 2>/dev/null || true)
    [[ -n "$cn_starttime" ]] && \
      verify_local_cn_pid "$cn_pid" "$expected" "$GB_BIN_SHA256" \
        "$cn_starttime" && break
    kill -0 "$cn_pid" 2>/dev/null || break
    sleep 0.05
  done
  [[ -n "$cn_starttime" ]] && \
    verify_local_cn_pid "$cn_pid" "$expected" "$GB_BIN_SHA256" \
      "$cn_starttime" || {
    echo "Compute-node PID ownership check failed for $tag" >&2; return 1;
  }
  ACTIVE_CN_EXPECTED=$expected
  ACTIVE_CN_SHA256=$GB_BIN_SHA256
  ACTIVE_CN_STARTTIME=$cn_starttime

  # One process avoids orphaning a sleep that keeps a nested SSH channel open.
  python3 - "$TIMEOUT_S" "$cn_pid" "$cn_starttime" "$expected" \
    "$GB_BIN_SHA256" <<'PY' &
import hashlib
import os
import signal
import sys
import time

time.sleep(float(sys.argv[1]))
pid = int(sys.argv[2])
try:
    raw = open(f"/proc/{pid}/stat").read()
    fields = raw[raw.rfind(")") + 2:].split()
    starttime = fields[19]
    executable = os.path.realpath(f"/proc/{pid}/exe")
    hasher = hashlib.sha256()
    with open(f"/proc/{pid}/exe", "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if starttime == sys.argv[3] and executable == sys.argv[4] and digest == sys.argv[5]:
        os.kill(pid, signal.SIGTERM)
except ProcessLookupError:
    pass
except FileNotFoundError:
    pass
PY
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
    wait "$watchdog" 2>/dev/null || true
    echo "Query phase marker not observed for $tag" >&2
    tail -40 "$stderr" >&2 || true
    return 1
  fi
  verify_local_cn_pid "$cn_pid" "$expected" "$GB_BIN_SHA256" \
    "$cn_starttime" || {
    echo "Compute-node identity drifted before profiling $tag" >&2
    return 1
  }

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
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  stop_mn "$mn" "$remote_dir"
  scp -q "$mn:$remote_dir/mn.out" "$OUT/$tag.mn.out"
  scp -q "$mn:$remote_dir/mn.err" "$OUT/$tag.mn.err"
  verify_cell_inputs "post_run" "$mn" "$index_path" "$index_sha" \
    "$query_path" "$query_sha" "$ground_truth_path" "$ground_truth_sha" \
    "$input_checks"

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
  python3 - "$provenance" "$dataset" "$method" "$GB_BIN" \
    "$GB_BIN_RESOLVED" "$GB_BIN_SHA256" "$COMPUTE_HOST" "$mn" "$GB_BIN_R" \
    "$ACTIVE_MN_BIN_PATH" "$ACTIVE_MN_BIN_SHA256" "$query_path" \
    "$query_sha" "$ground_truth_path" "$ground_truth_sha" "$index_path" \
    "$index_sha" "$stdout" "$stderr" "$OUT/$tag.mn.out" \
    "$OUT/$tag.mn.err" "$input_checks" "$OUT/campaign.json" \
    "$cn_starttime" "$ACTIVE_MN_STARTTIME" <<'PY'
import hashlib
import json
import pathlib
import sys

(output, dataset, method, cn_configured, cn_pid_exe, cn_sha, cn_host, mn_host,
 mn_configured, mn_pid_exe, mn_sha, query_path, query_sha,
 ground_truth_path, ground_truth_sha, index_path, index_sha, cn_stdout,
 cn_stderr, mn_stdout, mn_stderr, input_checks_path, campaign_path,
 cn_starttime, mn_starttime) = sys.argv[1:]
campaign = json.load(open(campaign_path))
ground_truth = None
if ground_truth_path:
    ground_truth = {"path": ground_truth_path, "sha256": ground_truth_sha}
inputs = {
    "query": {"path": query_path, "sha256": query_sha},
    "ground_truth": ground_truth,
    "index": [{"host": mn_host, "path": index_path, "sha256": index_sha}],
}
input_signature = hashlib.sha256(
    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
artifacts = {}
for key, raw_path in {
    "compute_stdout": cn_stdout,
    "compute_stderr": cn_stderr,
    "memory_node_stdout": mn_stdout,
    "memory_node_stderr": mn_stderr,
}.items():
    path = pathlib.Path(raw_path)
    if not path.is_file():
        raise SystemExit(f"missing mandatory run artifact: {path}")
    artifacts[key] = {
        "path": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
checks = {}
for line in pathlib.Path(input_checks_path).read_text().splitlines():
    phase, observed_query, observed_ground_truth, observed_index = line.split("\t")
    if phase in checks:
        raise SystemExit(f"duplicate input verification phase: {phase}")
    checks[phase] = {
        "query_sha256": observed_query,
        "ground_truth_sha256": None if observed_ground_truth == "-" else observed_ground_truth,
        "index_sha256": observed_index,
    }
if set(checks) != {"pre_run", "post_run"}:
    raise SystemExit("missing pre/post input verification")
record = {
    "schema_version": 1,
    "dataset": dataset,
    "method": method,
    "executables": {
        "compute_node": {
            "host": cn_host,
            "configured_path": cn_configured,
            "pid_exe_path": cn_pid_exe,
            "sha256": cn_sha,
            "pid_starttime": cn_starttime,
        },
        "memory_nodes": [{
            "host": mn_host,
            "configured_path": mn_configured,
            "pid_exe_path": mn_pid_exe,
            "sha256": mn_sha,
            "pid_starttime": mn_starttime,
        }],
    },
    "campaign": {
        "campaign_id": campaign["campaign_id"],
        "campaign_uuid": campaign["campaign_uuid"],
        "protocol_fingerprint": campaign["protocol_fingerprint"],
    },
    "inputs": inputs,
    "input_signature": input_signature,
    "input_verification": checks,
    "artifacts": artifacts,
}
with open(output, "w") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  ACTIVE_MN=""
  ACTIVE_REMOTE_DIR=""
  ACTIVE_MN_BIN_PATH=""
  ACTIVE_MN_BIN_SHA256=""
  ACTIVE_MN_STARTTIME=""
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

trap on_exit EXIT INT TERM
for dataset in $DATASETS; do
  for method in $METHODS; do
    run_profile "$dataset" "$method"
  done
done
verify_harness
python3 - "$OUT" "$DRY_RUN" "$DATASETS" "$METHODS" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
dry_run = bool(int(sys.argv[2]))
expected_runs = 0 if dry_run else len(sys.argv[3].split()) * len(sys.argv[4].split())
campaign = json.loads((root / "campaign.json").read_text())
expected_compute_host = str(
    campaign.get("protocol", {}).get("compute_host", "")
).strip()
if not expected_compute_host:
    raise SystemExit("query-profile campaign is missing compute host")
records = sorted(root.glob("*.provenance.json"))
if len(records) != expected_runs:
    raise SystemExit(
        f"query-profile provenance matrix incomplete: expected {expected_runs}, got {len(records)}"
    )
for record_path in records:
    record = json.loads(record_path.read_text())
    compute_host = str(
        record.get("executables", {}).get("compute_node", {}).get("host", "")
    ).strip()
    if compute_host != expected_compute_host:
        raise SystemExit(f"query-profile compute host drift: {record_path}")
    binding = record.get("campaign", {})
    for field in ("campaign_id", "campaign_uuid", "protocol_fingerprint"):
        if binding.get(field) != campaign.get(field):
            raise SystemExit(f"query-profile campaign binding drift: {record_path}:{field}")
    inputs = record.get("inputs", {})
    checks = record.get("input_verification", {})
    if set(checks) != {"pre_run", "post_run"}:
        raise SystemExit(f"query-profile input checks incomplete: {record_path}")
    expected = {
        "query_sha256": inputs["query"]["sha256"],
        "ground_truth_sha256": (
            inputs["ground_truth"]["sha256"] if inputs.get("ground_truth") else None
        ),
        "index_sha256": inputs["index"][0]["sha256"],
    }
    if checks["pre_run"] != expected or checks["post_run"] != expected:
        raise SystemExit(f"query-profile pre/post input drift: {record_path}")
    for artifact in record.get("artifacts", {}).values():
        path = (root / artifact["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise SystemExit(f"query-profile artifact escapes bundle: {path}") from error
        if not path.is_file():
            raise SystemExit(f"query-profile artifact missing: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact["sha256"]:
            raise SystemExit(f"query-profile artifact drift: {path}")
PY
python3 "$EVIDENCE_TOOL" seal --root "$OUT" \
  --campaign "$OUT/campaign.json" >/dev/null
python3 "$EVIDENCE_TOOL" verify --root "$OUT" >/dev/null
[[ -s "$OUT/SEALED.json" ]] || { echo "Missing SEALED.json" >&2; exit 1; }
echo "Wrote sealed query-only profiles to $OUT"
