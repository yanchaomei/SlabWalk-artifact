#!/usr/bin/env bash
# Matched-physical-byte comparison of Slab materialization policies.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MATERIALIZATION_SOURCE_SCRIPT_DIR=${MATERIALIZATION_SOURCE_SCRIPT_DIR:-$SCRIPT_DIR}
ROOT=${ROOT:-$(cd -- "$MATERIALIZATION_SOURCE_SCRIPT_DIR/../.." && pwd)}
SCRIPT_PATH=$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")
SUMMARIZER=${SUMMARIZER:-$MATERIALIZATION_SOURCE_SCRIPT_DIR/summarize_vldb_materialization_policy.py}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$MATERIALIZATION_SOURCE_SCRIPT_DIR/vldb_evidence_bundle.py}
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA to the frozen candidate SHA-256}
OUT_ROOT=${OUT_ROOT:-$ROOT/results/vldb_materialization_policy_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-materialization-policy-$(date -u +%Y%m%dT%H%M%SZ)}
DATASETS=${DATASETS:-"DEEP1M SIFT1M GIST1M"}
POLICIES=${POLICIES:-"benefit indeg hop"}
BUDGET_BYTES=${BUDGET_BYTES:-"536870912 1073741824 2147483648"}
REPEATS=${REPEATS:-6}
WARMUPS=${WARMUPS:-1}
CAMPAIGN_KIND=${CAMPAIGN_KIND:-formal}
THREADS=${THREADS:-16}
COROUTINES=${COROUTINES:-4}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}
BUILD_THREADS=${BUILD_THREADS:-20}
EF_OVERRIDE=${EF_OVERRIDE:-0}
MEMORY_NODE=${MEMORY_NODE:-skv-node5}
PORT_BASE=${PORT_BASE:-1490}
TIMEOUT_S=${TIMEOUT_S:-1800}
STAGED_BUILD=${STAGED_BUILD:-0}
CAPTURE_PHASEF=${CAPTURE_PHASEF:-0}
DRY_RUN=${DRY_RUN:-0}
ACTIVE_REMOTE_DIR=""
ACTIVE_CN_PID=""
ACTIVE_CN_EXPECTED=""
ACTIVE_CN_SHA256=""
ACTIVE_CN_STARTTIME=""
ACTIVE_MN_BIN_PATH=""
ACTIVE_MN_BIN_SHA256=""
ACTIVE_MN_STARTTIME=""
RUNS_CSV=$OUT_ROOT/runs.csv
COMPUTE_HOST=$(hostname)

[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "EXPECTED_BINARY_SHA must contain 64 lowercase hex digits" >&2; exit 2;
}
[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "REPEATS must be positive" >&2; exit 2; }
[[ "$WARMUPS" =~ ^[0-9]+$ ]] || { echo "WARMUPS must be non-negative" >&2; exit 2; }
[[ "$CAMPAIGN_KIND" == "formal" || "$CAMPAIGN_KIND" == "smoke" ]] || {
  echo "CAMPAIGN_KIND must be formal or smoke" >&2; exit 2;
}
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }
[[ "$COROUTINES" =~ ^[1-9][0-9]*$ ]] || { echo "COROUTINES must be positive" >&2; exit 2; }
[[ "$QUERY_CONTEXTS" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_CONTEXTS must be positive" >&2; exit 2; }
(( QUERY_CONTEXTS <= THREADS )) || { echo "QUERY_CONTEXTS must not exceed THREADS" >&2; exit 2; }
[[ "$BUILD_THREADS" =~ ^[1-9][0-9]*$ ]] && (( BUILD_THREADS <= 32 )) || {
  echo "BUILD_THREADS must be in 1..32" >&2; exit 2;
}
[[ "$STAGED_BUILD" == "0" || "$STAGED_BUILD" == "1" ]] || {
  echo "STAGED_BUILD must be 0 or 1" >&2; exit 2;
}
[[ "$CAPTURE_PHASEF" == "0" || "$CAPTURE_PHASEF" == "1" ]] || {
  echo "CAPTURE_PHASEF must be 0 or 1" >&2; exit 2;
}
[[ -x "$SUMMARIZER" || -f "$SUMMARIZER" ]] || { echo "Missing summarizer: $SUMMARIZER" >&2; exit 2; }

read -r -a policy_array <<< "$POLICIES"
read -r -a budget_array <<< "$BUDGET_BYTES"
read -r -a dataset_array <<< "$DATASETS"
(( ${#policy_array[@]} > 0 && ${#budget_array[@]} > 0 && ${#dataset_array[@]} > 0 )) || {
  echo "DATASETS, POLICIES, and BUDGET_BYTES must not be empty" >&2; exit 2;
}
seen_policies=" "
for policy in "${policy_array[@]}"; do
  [[ "$policy" == "benefit" || "$policy" == "indeg" || "$policy" == "hop" ]] || {
    echo "Unsupported materialization policy: $policy" >&2; exit 2;
  }
  case "$seen_policies" in
    *" $policy "*) echo "Duplicate policy: $policy" >&2; exit 2 ;;
  esac
  seen_policies="${seen_policies}${policy} "
done
if [[ "$CAMPAIGN_KIND" == "formal" ]] && (( REPEATS % ${#policy_array[@]} != 0 )); then
  echo "A formal materialization campaign must be position-balanced: REPEATS must be a multiple of the policy count" >&2
  exit 2
fi
for budget in "${budget_array[@]}"; do
  [[ "$budget" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid positive byte budget: $budget" >&2; exit 2; }
done

dataset_spec() {
  case "$1" in
    DEEP1M) printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$GB_DATA/deep1m" 16 100 200 4294967296 4294967296 uniform query-uniform.fbin groundtruth-uniform.bin 10000 ;;
    SIFT1M) printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$GB_DATA/sift1m" 16 100 200 4294967296 4294967296 uniform query-uniform.fbin groundtruth-uniform.bin 10000 ;;
    GIST1M) printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$GB_DATA/gist1m" 16 100 400 8589934592 8589934592 u10k query-u10k.fbin groundtruth-u10k.bin 10000 ;;
    *) echo "Unsupported materialization dataset: $1" >&2; return 2 ;;
  esac
}

for dataset in "${dataset_array[@]}"; do dataset_spec "$dataset" >/dev/null; done
total_cells=$((${#dataset_array[@]} * ${#budget_array[@]} * ${#policy_array[@]} * (WARMUPS + REPEATS)))
(( PORT_BASE >= 1024 && PORT_BASE + total_cells - 1 <= 65535 )) || {
  echo "Campaign port range is outside 1024..65535" >&2; exit 2;
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s campaign_kind=%s datasets=%s policies=%s budgets=%s repeats=%s warmups=%s threads=%s coroutines=%s contexts=%s memory_node=%s staged_build=%s build_threads=%s\n' \
    "$CAMPAIGN_ID" "$CAMPAIGN_KIND" "$DATASETS" "$POLICIES" "$BUDGET_BYTES" "$REPEATS" \
    "$WARMUPS" "$THREADS" "$COROUTINES" "$QUERY_CONTEXTS" "$MEMORY_NODE" \
    "$STAGED_BUILD" "$BUILD_THREADS"
  for dataset in "${dataset_array[@]}"; do
    IFS='|' read -r _ _ _ _ _ _ query_suffix query_name groundtruth_name expected_queries \
      <<< "$(dataset_spec "$dataset")"
    printf 'dataset_spec=%s:%s:%s:%s:%s\n' "$dataset" "$query_suffix" \
      "$query_name" "$groundtruth_name" "$expected_queries"
  done
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing CN binary: $GB_BIN" >&2; exit 2; }
CN_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$CN_SHA" == "$EXPECTED_BINARY_SHA" ]] || { echo "CN binary SHA mismatch: $CN_SHA" >&2; exit 2; }
MN_SHA=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" "sha256sum '$GB_BIN_R' | cut -d ' ' -f1")
[[ "$MN_SHA" == "$EXPECTED_BINARY_SHA" ]] || { echo "MN binary SHA mismatch: $MN_SHA" >&2; exit 2; }

if [[ "${VLDB_MATERIALIZATION_HARNESS_FROZEN:-0}" != "1" ]]; then
  if [[ -e "$OUT_ROOT" ]]; then
    echo "Refusing existing OUT_ROOT: $OUT_ROOT" >&2
    exit 2
  fi
  mkdir -p "$OUT_ROOT/raw"
  snapshot_json=$(python3 "$EVIDENCE_TOOL" snapshot \
    --out-dir "$OUT_ROOT/harness" \
    --entry runner="$SCRIPT_PATH" \
    --entry summarizer="$SUMMARIZER" \
    --entry evidence_tool="$EVIDENCE_TOOL")
  read -r frozen_runner frozen_summarizer frozen_tool harness_sha <<< "$(
    python3 - "$OUT_ROOT/harness" "$snapshot_json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
payload = json.loads(sys.argv[2])
print(
    root / payload["entries"]["runner"]["path"],
    root / payload["entries"]["summarizer"]["path"],
    root / payload["entries"]["evidence_tool"]["path"],
    payload["manifest_sha256"],
)
PY
  )"
  exec env \
    VLDB_MATERIALIZATION_HARNESS_FROZEN=1 \
    MATERIALIZATION_SOURCE_SCRIPT_DIR="$MATERIALIZATION_SOURCE_SCRIPT_DIR" ROOT="$ROOT" \
    OUT_ROOT="$OUT_ROOT" CAMPAIGN_ID="$CAMPAIGN_ID" \
    CAMPAIGN_KIND="$CAMPAIGN_KIND" \
    SUMMARIZER="$frozen_summarizer" EVIDENCE_TOOL="$frozen_tool" \
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
  echo "Invalid frozen materialization output root" >&2
  exit 2
}

RUNNER_SHA=$(sha256sum "$SCRIPT_PATH" | awk '{print $1}')
SUMMARIZER_SHA=$(sha256sum "$SUMMARIZER" | awk '{print $1}')
INPUT_MANIFEST_TSV=$OUT_ROOT/input_manifest.tsv
for dataset in "${dataset_array[@]}"; do
  IFS='|' read -r data m efc _ _ _ _ query_name groundtruth_name _ \
    <<< "$(dataset_spec "$dataset")"
  query_file=$data/queries/$query_name
  groundtruth_file=$data/queries/$groundtruth_name
  dump_file=$data/dump/index_m${m}_efc${efc}_node1_of1.dat
  [[ -s "$query_file" && -s "$groundtruth_file" ]] || {
    echo "Missing fixed query pool for $dataset" >&2; exit 2;
  }
  ssh -o LogLevel=ERROR "$MEMORY_NODE" "test -s '$dump_file'" || {
    echo "Missing authoritative dump for $dataset on $MEMORY_NODE" >&2; exit 2;
  }
  printf '%s\tquery\t%s\t%s\t%s\n' "$dataset" "$query_file" \
    "$(stat -c%s "$query_file")" "$(sha256sum "$query_file" | awk '{print $1}')" \
    >> "$INPUT_MANIFEST_TSV"
  printf '%s\tgroundtruth\t%s\t%s\t%s\n' "$dataset" "$groundtruth_file" \
    "$(stat -c%s "$groundtruth_file")" "$(sha256sum "$groundtruth_file" | awk '{print $1}')" \
    >> "$INPUT_MANIFEST_TSV"
  read -r dump_size dump_sha <<< "$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "printf '%s ' \"\$(stat -c%s '$dump_file')\"; sha256sum '$dump_file' | cut -d ' ' -f1")"
  printf '%s\tindex_dump\t%s\t%s\t%s\n' "$dataset" "$dump_file" "$dump_size" "$dump_sha" \
    >> "$INPUT_MANIFEST_TSV"
done
python3 - "$OUT_ROOT/campaign.json" "$CAMPAIGN_ID" "$CN_SHA" "$MN_SHA" \
  "$RUNNER_SHA" "$SUMMARIZER_SHA" "$DATASETS" "$POLICIES" "$BUDGET_BYTES" \
  "$REPEATS" "$WARMUPS" "$CAMPAIGN_KIND" "$THREADS" "$COROUTINES" "$QUERY_CONTEXTS" \
  "$MEMORY_NODE" "$PORT_BASE" "$STAGED_BUILD" "$CAPTURE_PHASEF" \
  "$BUILD_THREADS" "$COMPUTE_HOST" \
  "$INPUT_MANIFEST_TSV" "$ROOT" "$HARNESS_MANIFEST" \
  "$HARNESS_MANIFEST_SHA256" <<'PY'
import hashlib, json, pathlib, subprocess, sys, uuid
from datetime import datetime, timezone

(path, campaign_id, cn_sha, mn_sha, runner_sha, summarizer_sha, datasets,
 policies, budgets, repeats, warmups, campaign_kind, threads, coroutines,
 contexts, memory_node, port_base, staged_build, capture_phasef, build_threads,
 compute_host,
 input_manifest_path, repo_root, harness_manifest_path,
 harness_manifest_sha256) = sys.argv[1:]
harness_path = pathlib.Path(harness_manifest_path)
harness_bytes = harness_path.read_bytes()
if hashlib.sha256(harness_bytes).hexdigest() != harness_manifest_sha256:
    raise SystemExit("materialization harness manifest drifted before campaign creation")
harness = json.loads(harness_bytes)
inputs = []
with open(input_manifest_path) as handle:
    for line in handle:
        dataset, role, artifact_path, size, sha256 = line.rstrip("\n").split("\t")
        inputs.append({
            "dataset": dataset,
            "role": role,
            "path": artifact_path,
            "bytes": int(size),
            "sha256": sha256,
        })
input_signatures = {}
for dataset in datasets.split():
    dataset_inputs = sorted(
        (record for record in inputs if record["dataset"] == dataset),
        key=lambda record: record["role"],
    )
    input_signatures[dataset] = hashlib.sha256(
        json.dumps(dataset_inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
source_root = pathlib.Path(repo_root)
source_scope = [
    "graphbeyond/CMakeLists.txt",
    "graphbeyond/src",
    "graphbeyond/rdma-library/CMakeLists.txt",
    "graphbeyond/rdma-library/FindIBVerbs.cmake",
    "graphbeyond/rdma-library/library",
    "graphbeyond/thirdparty",
]
source_files = []
for relative in source_scope:
    source_path = source_root / relative
    if source_path.is_file():
        source_files.append(source_path)
    elif source_path.is_dir():
        source_files.extend(path for path in source_path.rglob("*") if path.is_file())
source_hasher = hashlib.sha256()
for source_path in sorted(
    source_files, key=lambda path: path.relative_to(source_root).as_posix()
):
    relative = source_path.relative_to(source_root).as_posix().encode()
    payload = source_path.read_bytes()
    source_hasher.update(len(relative).to_bytes(8, "little"))
    source_hasher.update(relative)
    source_hasher.update(len(payload).to_bytes(8, "little"))
    source_hasher.update(payload)
try:
    git_head = subprocess.check_output(
        ["git", "-C", repo_root, "rev-parse", "HEAD"], text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    git_dirty = bool(
        subprocess.check_output(
            ["git", "-C", repo_root, "status", "--porcelain", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    )
    git_available = True
except (FileNotFoundError, subprocess.CalledProcessError):
    git_available, git_head, git_dirty = False, None, None
protocol = {
    "binary_sha256": cn_sha,
    "memory_node_binary_sha256": mn_sha,
    "runner_sha256": runner_sha,
    "summarizer_sha256": summarizer_sha,
    "datasets": datasets.split(),
    "policies": policies.split(),
    "budget_bytes": [int(value) for value in budgets.split()],
    "repeats": int(repeats),
    "campaign_kind": campaign_kind,
    "warmups": int(warmups),
    "workers": int(threads),
    "coroutines": int(coroutines),
    "query_contexts": int(contexts),
    "query_pool": "complete_uniform_with_exact_groundtruth",
    "record_layout": "native_packed_variable",
    "budget_scope": "aggregate_high_water_across_mns",
    "order": "cyclic_policy_rotation_per_repeat",
    "memory_node": memory_node,
    "port_base": int(port_base),
    "staged_build": bool(int(staged_build)),
    "capture_phasef": bool(int(capture_phasef)),
    "build_threads": int(build_threads),
    "compute_host": compute_host,
    "inputs": inputs,
    "input_signatures": input_signatures,
    "harness": {
        "manifest": "harness/harness.json",
        "manifest_sha256": harness_manifest_sha256,
        "entries": harness["entries"],
    },
    "source": {
        "tree_sha256": source_hasher.hexdigest(),
        "tree_scope": source_scope,
        "file_count": len(source_files),
        "git_available": git_available,
        "git_head": git_head,
        "git_dirty": git_dirty,
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
PROTOCOL_FINGERPRINT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol_fingerprint"])' "$OUT_ROOT/campaign.json")
SOURCE_TREE_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol"]["source"]["tree_sha256"])' "$OUT_ROOT/campaign.json")

dataset_input_signature() {
  python3 - "$OUT_ROOT/campaign.json" "$1" <<'PY'
import json, sys
campaign = json.load(open(sys.argv[1]))
signature = campaign["protocol"]["input_signatures"].get(sys.argv[2], "")
if len(signature) != 64:
    raise SystemExit("missing dataset input signature")
print(signature)
PY
}

verify_input_manifest() {
  local selected_dataset=${1:-all} phase=${2:-campaign} output=${3:-}
  local row_dataset role path expected_size expected_sha current_size current_sha
  verify_harness
  while IFS=$'\t' read -r row_dataset role path expected_size expected_sha; do
    if [[ "$selected_dataset" != "all" && "$row_dataset" != "$selected_dataset" ]]; then
      continue
    fi
    if [[ "$role" == "index_dump" ]]; then
      read -r current_size current_sha <<< "$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
        "printf '%s ' \"\$(stat -c%s '$path')\"; sha256sum '$path' | cut -d ' ' -f1")"
    else
      current_size=$(stat -c%s "$path")
      current_sha=$(sha256sum "$path" | awk '{print $1}')
    fi
    [[ "$current_size" == "$expected_size" && "$current_sha" == "$expected_sha" ]] || {
      echo "Input drift detected for $row_dataset $role during $phase: $path" >&2
      return 1
    }
    if [[ -n "$output" ]]; then
      printf '%s\t%s\t%s\t%s\t%s\n' "$phase" "$row_dataset" "$role" \
        "$current_size" "$current_sha" >> "$output"
    fi
  done < "$INPUT_MANIFEST_TSV"
}

verify_remote_pid() {
  local remote_dir=$1
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
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

stop_remote() {
  local remote_dir=$1
  local identity pid expected_starttime current_starttime
  identity=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.starttime'; \
     printf '%s|%s\n' \"\$(cat '$remote_dir/server.pid')\" \
       \"\$(cat '$remote_dir/server.starttime')\"") || {
    echo "Memory-node ownership record is missing: $remote_dir" >&2
    return 1
  }
  IFS='|' read -r pid expected_starttime <<< "$identity"
  if verify_remote_pid "$remote_dir"; then
    ssh -o LogLevel=ERROR "$MEMORY_NODE" \
      "current=\$(awk '{print \$22}' /proc/$pid/stat 2>/dev/null); \
       test -n \"\$current\" -a \"\$current\" = '$expected_starttime'; \
       kill $pid" || {
      echo "Memory-node termination request failed: $remote_dir" >&2
      return 1
    }
  else
    current_starttime=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
      "awk '{print \$22}' /proc/$pid/stat 2>/dev/null || true")
    if [[ -z "$current_starttime" || "$current_starttime" != "$expected_starttime" ]]; then
      return 0
    fi
    echo "Memory-node identity changed before shutdown: $remote_dir" >&2
    return 1
  fi
  for _ in $(seq 1 200); do
    current_starttime=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
      "awk '{print \$22}' /proc/$pid/stat 2>/dev/null || true")
    if [[ -z "$current_starttime" || "$current_starttime" != "$expected_starttime" ]]; then
      return 0
    fi
    sleep 0.05
  done
  echo "Memory-node process did not terminate: $remote_dir" >&2
  return 1
}

local_pid_starttime() {
  local pid=$1
  python3 - "/proc/$pid/stat" <<'PY'
import pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_text()
fields = raw[raw.rfind(")") + 2:].split()
if len(fields) <= 19:
    raise SystemExit("short /proc stat")
print(fields[19])
PY
}

verify_local_cn_pid() {
  local pid=$1 expected=$2 expected_sha=$3 expected_starttime=$4
  local actual actual_sha actual_starttime
  actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
  actual_sha=$(sha256sum "/proc/$pid/exe" 2>/dev/null | awk '{print $1}')
  actual_starttime=$(local_pid_starttime "$pid" 2>/dev/null || true)
  [[ -n "$actual" && "$actual" == "$expected" \
     && -n "$actual_sha" && "$actual_sha" == "$expected_sha" \
     && -n "$actual_starttime" && "$actual_starttime" == "$expected_starttime" ]]
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
  if [[ -n "$ACTIVE_REMOTE_DIR" ]]; then
    stop_remote "$ACTIVE_REMOTE_DIR" || cleanup_rc=1
  fi
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
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
trap on_exit EXIT INT TERM

start_remote() {
  local remote_dir=$1 index_region_bytes=$2 port=$3
  local identity remote_binary remote_sha
  identity=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "resolved=\$(realpath '$GB_BIN_R'); test -x \"\$resolved\"; \
     digest=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); \
     printf '%s|%s\n' \"\$resolved\" \"\$digest\"")
  IFS='|' read -r remote_binary remote_sha <<< "$identity"
  [[ -n "$remote_binary" && "$remote_sha" == "$EXPECTED_BINARY_SHA" ]] || {
    echo "Memory-node binary SHA drift on $MEMORY_NODE" >&2
    return 1
  }
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     printf '%s\n' '$remote_binary' > '$remote_dir/server.exe'; \
     printf '%s\n' '$remote_sha' > '$remote_dir/server.sha256'; \
     nohup env LD_LIBRARY_PATH='$GB_LIB' numactl --preferred=1 '$remote_binary' \
       --is-server --num-clients 1 --port '$port' \
       --index-region-bytes '$index_region_bytes' > '$remote_dir/mn.out' \
       2> '$remote_dir/mn.err' < /dev/null & \
     pid=\$!; echo \$pid > '$remote_dir/server.pid'; \
     awk '{print \$22}' /proc/\$pid/stat > '$remote_dir/server.starttime'"
  ACTIVE_REMOTE_DIR=$remote_dir
  ACTIVE_MN_BIN_PATH=$remote_binary
  ACTIVE_MN_BIN_SHA256=$remote_sha
  for _ in $(seq 1 100); do
    if verify_remote_pid "$remote_dir"; then
      ACTIVE_MN_STARTTIME=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
        "cat '$remote_dir/server.starttime'")
      return 0
    fi
    sleep 0.1
  done
  echo "Memory-node ownership verification failed: $remote_dir" >&2
  return 1
}

run_cell() {
  local dataset=$1 budget=$2 policy=$3 repeat=$4 position=$5 kind=$6 port=$7
  local data m efc default_ef lavd_region index_region query_suffix query_name
  local groundtruth_name expected_queries ef query_file groundtruth_file
  IFS='|' read -r data m efc default_ef lavd_region index_region query_suffix \
    query_name groundtruth_name expected_queries <<< "$(dataset_spec "$dataset")"
  query_file=$data/queries/$query_name
  groundtruth_file=$data/queries/$groundtruth_name
  ef=$default_ef
  if (( EF_OVERRIDE > 0 )); then ef=$EF_OVERRIDE; fi
  (( budget <= lavd_region )) || {
    echo "Byte budget $budget exceeds LAVD region $lavd_region for $dataset" >&2; return 2;
  }
  local cell_dir="$OUT_ROOT/raw/$dataset/b$budget/$policy/${kind}${repeat}"
  local remote_dir="/tmp/${CAMPAIGN_ID//[^[:alnum:]]/_}_${dataset}_b${budget}_${policy}_${kind}${repeat}"
  local result="$cell_dir/result.json" stderr="$cell_dir/run.err"
  local phasef="$cell_dir/phasef.csv" phasef_sha="" input_signature
  local input_checks="$cell_dir/input_checks.tsv"
  input_signature=$(dataset_input_signature "$dataset")
  local -a trace_env=()
  if [[ "$CAPTURE_PHASEF" == "1" && "$kind" == "r" ]]; then
    trace_env=("GB_PHASEF_LOG=$phasef")
  fi
  mkdir -p "$cell_dir"
  [[ -s "$query_file" && -s "$groundtruth_file" ]] || {
    echo "Missing fixed query pool for $dataset" >&2; return 2;
  }
  ssh -o LogLevel=ERROR "$MEMORY_NODE" \
    "test -s '$data/dump/index_m${m}_efc${efc}_node1_of1.dat'" || {
      echo "Missing authoritative dump for $dataset on $MEMORY_NODE" >&2; return 2;
    }
  : > "$input_checks"
  verify_input_manifest "$dataset" "pre_run" "$input_checks"
  start_remote "$remote_dir" "$index_region" "$port"

  env -u SHINE_LAVD_BUDGET "${trace_env[@]}" \
    LD_LIBRARY_PATH="$GB_LIB" SHINE_CRANE=1 GB_BITMAP_DEDUP=1 \
    GB_QUERY_LATENCY=1 \
    SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 \
    SHINE_LAVD_STAGED_BUILD="$STAGED_BUILD" \
    SHINE_LAVD_BUILD_THREADS="$BUILD_THREADS" SHINE_LAVD_BUILD_CPU_BASE=1 \
    SHINE_LAVD_BUILD_CPU_STRIDE=2 \
    SHINE_LAVD_BUDGET_BYTES="$budget" SHINE_LAVD_HOTSET="$policy" \
    numactl --preferred=1 "$GB_BIN" --servers "$MEMORY_NODE" --initiator \
    --threads "$THREADS" --coroutines "$COROUTINES" \
    --query-contexts "$QUERY_CONTEXTS" --port "$port" \
    --index-region-bytes "$index_region" --data-path "$data/" \
    --query-suffix "$query_suffix" --ef-search "$ef" --ef-construction "$efc" \
    --m "$m" --k 10 --label "${dataset}_${policy}_b${budget}_r${repeat}" \
    --spec-k 1 --load-index --lavd 8 --lavd-region-bytes "$lavd_region" \
    > "$result" 2> "$stderr" &
  ACTIVE_CN_PID=$!
  local cn_pid=$ACTIVE_CN_PID expected_path cn_starttime=""
  expected_path=$(realpath "$GB_BIN")
  for _ in $(seq 1 100); do
    cn_starttime=$(local_pid_starttime "$cn_pid" 2>/dev/null || true)
    [[ -n "$cn_starttime" ]] && \
      verify_local_cn_pid "$cn_pid" "$expected_path" "$CN_SHA" \
        "$cn_starttime" && break
    kill -0 "$cn_pid" 2>/dev/null || break
    sleep 0.05
  done
  [[ -n "$cn_starttime" ]] && \
    verify_local_cn_pid "$cn_pid" "$expected_path" "$CN_SHA" \
      "$cn_starttime" || {
    echo "Compute-node ownership verification failed for $cell_dir" >&2; return 1;
  }
  ACTIVE_CN_EXPECTED=$expected_path
  ACTIVE_CN_SHA256=$CN_SHA
  ACTIVE_CN_STARTTIME=$cn_starttime

  python3 - "$TIMEOUT_S" "$cn_pid" "$cn_starttime" "$expected_path" \
    "$CN_SHA" <<'PY' &
import hashlib, os, signal, sys, time
time.sleep(float(sys.argv[1]))
pid = int(sys.argv[2])
try:
    raw = open(f"/proc/{pid}/stat").read()
    starttime = raw[raw.rfind(")") + 2:].split()[19]
    executable = os.path.realpath(f"/proc/{pid}/exe")
    hasher = hashlib.sha256()
    with open(f"/proc/{pid}/exe", "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    if (starttime == sys.argv[3] and executable == sys.argv[4]
            and hasher.hexdigest() == sys.argv[5]):
        os.kill(pid, signal.SIGTERM)
except (ProcessLookupError, FileNotFoundError):
    pass
PY
  local watchdog=$!
  set +e
  wait "$cn_pid"
  local rc=$?
  set -e
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  ACTIVE_CN_PID=""
  ACTIVE_CN_EXPECTED=""
  ACTIVE_CN_SHA256=""
  ACTIVE_CN_STARTTIME=""
  local mn_bin_path=$ACTIVE_MN_BIN_PATH mn_bin_sha=$ACTIVE_MN_BIN_SHA256
  local mn_starttime=$ACTIVE_MN_STARTTIME
  stop_remote "$remote_dir"
  ACTIVE_REMOTE_DIR=""
  ACTIVE_MN_BIN_PATH=""
  ACTIVE_MN_BIN_SHA256=""
  ACTIVE_MN_STARTTIME=""
  scp -q "$MEMORY_NODE:$remote_dir/mn.err" "$cell_dir/mn.err"
  scp -q "$MEMORY_NODE:$remote_dir/mn.out" "$cell_dir/mn.out"
  [[ -f "$cell_dir/mn.err" && -f "$cell_dir/mn.out" ]] || {
    echo "Missing mandatory memory-node logs for $cell_dir" >&2; return 1;
  }
  verify_input_manifest "$dataset" "post_run" "$input_checks"
  [[ "$rc" == "0" ]] || { echo "Cell failed with status $rc: $cell_dir" >&2; return "$rc"; }

  local processed
  processed=$(python3 -c 'import json,sys; x=json.load(open(sys.argv[1])); print(x["queries"]["processed"])' "$result")
  [[ "$processed" == "$expected_queries" ]] || {
    echo "Unexpected query count for $dataset: $processed != $expected_queries" >&2; return 2;
  }
  if [[ "$CAPTURE_PHASEF" == "1" && "$kind" == "r" ]]; then
    [[ -s "$phasef" ]] || { echo "Missing Phase-F trace for $cell_dir" >&2; return 2; }
    LC_ALL=C sort -t, -k1,1n -k2,2n "$phasef" > "$cell_dir/phasef.sorted.csv"
    phasef_sha=$(sha256sum "$cell_dir/phasef.sorted.csv" | awk '{print $1}')
  fi
  python3 - "$cell_dir/campaign.json" "$CAMPAIGN_ID" "$PROTOCOL_FINGERPRINT" \
    "$dataset" "$budget" "$policy" "$repeat" "$position" "$kind" "$port" \
    "$phasef_sha" "$input_signature" "$expected_path" "$CN_SHA" \
    "$COMPUTE_HOST" \
    "$MEMORY_NODE" "$mn_bin_path" "$mn_bin_sha" "$result" "$stderr" \
    "$cell_dir/mn.out" "$cell_dir/mn.err" "$input_checks" \
    "$OUT_ROOT/campaign.json" "$cn_starttime" "$mn_starttime" <<'PY'
import hashlib, json, sys
from pathlib import Path
(path, campaign, fingerprint, dataset, budget, policy, repeat, position,
 kind, port, phasef_sha256, input_signature, cn_path, cn_sha, compute_host,
 mn_host,
 mn_path, mn_sha, result_path, stderr_path, mn_out_path, mn_err_path,
 input_checks_path, parent_campaign_path, cn_starttime,
 mn_starttime) = sys.argv[1:]
parent_campaign = json.load(open(parent_campaign_path))
artifacts = {}
for name, artifact_path in {
    "compute_stdout": result_path,
    "compute_stderr": stderr_path,
    "memory_node_stdout": mn_out_path,
    "memory_node_stderr": mn_err_path,
}.items():
    artifact = Path(artifact_path)
    artifacts[name] = {
        "path": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
checks = {}
for line in Path(input_checks_path).read_text().splitlines():
    phase, row_dataset, role, size, digest = line.split("\t")
    checks.setdefault(phase, {})[role] = {
        "dataset": row_dataset,
        "bytes": int(size),
        "sha256": digest,
    }
if set(checks) != {"pre_run", "post_run"} or checks["pre_run"] != checks["post_run"]:
    raise SystemExit("materialization cell input verification is incomplete or drifted")
json.dump({
    "schema_version": 2,
    "campaign_id": campaign,
    "campaign_uuid": parent_campaign["campaign_uuid"],
    "protocol_fingerprint": fingerprint,
    "dataset": dataset,
    "budget_bytes": int(budget),
    "policy": policy,
    "repeat": int(repeat),
    "position": int(position),
    "kind": kind,
    "tcp_port": int(port),
    "phasef_sha256": phasef_sha256 or None,
    "input_signature": input_signature,
    "executables": {
        "compute_node": {
            "host": compute_host,
            "pid_exe_path": cn_path,
            "sha256": cn_sha,
            "pid_starttime": cn_starttime,
        },
        "memory_node": {
            "host": mn_host,
            "pid_exe_path": mn_path,
            "sha256": mn_sha,
            "pid_starttime": mn_starttime,
        },
    },
    "input_verification": checks,
    "artifacts": artifacts,
}, open(path, "w"), indent=2, sort_keys=True)
PY
  if [[ "$kind" == "r" ]]; then
    verify_harness
    VLDB_EVIDENCE_BUNDLE_MODULE="$EVIDENCE_TOOL" \
    python3 "$SUMMARIZER" cell --result "$result" --stderr "$stderr" \
      --dataset "$dataset" --repeat "$repeat" --position "$position" \
      --policy "$policy" --budget "$budget" --binary-sha "$CN_SHA" \
      --input-signature "$input_signature" \
      --source-tree-sha "$SOURCE_TREE_SHA" \
      --compute-host "$COMPUTE_HOST" \
      --out "$RUNS_CSV"
  fi
}

cell_index=0
for dataset in "${dataset_array[@]}"; do
  for budget in "${budget_array[@]}"; do
    for ((warmup = 0; warmup < WARMUPS; ++warmup)); do
      for position in "${!policy_array[@]}"; do
        policy=${policy_array[$position]}
        run_cell "$dataset" "$budget" "$policy" "$warmup" "$position" w \
          "$((PORT_BASE + cell_index))"
        ((cell_index += 1))
      done
    done
    for ((repeat = 0; repeat < REPEATS; ++repeat)); do
      rotation=$((repeat % ${#policy_array[@]}))
      for position in "${!policy_array[@]}"; do
        policy=${policy_array[$(((position + rotation) % ${#policy_array[@]}))]}
        run_cell "$dataset" "$budget" "$policy" "$repeat" "$position" r \
          "$((PORT_BASE + cell_index))"
        ((cell_index += 1))
      done
    done
  done
done

verify_harness
VLDB_EVIDENCE_BUNDLE_MODULE="$EVIDENCE_TOOL" \
python3 "$SUMMARIZER" summary --runs "$RUNS_CSV" \
  --campaign "$OUT_ROOT/campaign.json" --out "$OUT_ROOT/summary.csv"
verify_input_manifest "all" "campaign_close"
python3 - "$OUT_ROOT" "$total_cells" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
expected_cells = int(sys.argv[2])
campaign = json.loads((root / "campaign.json").read_text())
compute_host = str(campaign.get("protocol", {}).get("compute_host", "")).strip()
if not compute_host:
    raise SystemExit("materialization compute host drift: missing campaign host")
records = sorted((root / "raw").rglob("campaign.json"))
if len(records) != expected_cells:
    raise SystemExit(
        f"materialization cell matrix incomplete: expected {expected_cells}, got {len(records)}"
    )
for record_path in records:
    record = json.loads(record_path.read_text())
    if record.get("campaign_id") != campaign.get("campaign_id"):
        raise SystemExit(f"cell campaign id drift: {record_path}")
    if record.get("campaign_uuid") != campaign.get("campaign_uuid"):
        raise SystemExit(f"cell campaign UUID drift: {record_path}")
    if record.get("protocol_fingerprint") != campaign.get("protocol_fingerprint"):
        raise SystemExit(f"cell protocol fingerprint drift: {record_path}")
    observed_host = str(
        record.get("executables", {}).get("compute_node", {}).get("host", "")
    ).strip()
    if observed_host != compute_host:
        raise SystemExit(f"materialization compute host drift: {record_path}")
    checks = record.get("input_verification", {})
    if set(checks) != {"pre_run", "post_run"} or checks["pre_run"] != checks["post_run"]:
        raise SystemExit(f"cell input verification drift: {record_path}")
    for artifact in record.get("artifacts", {}).values():
        path = (record_path.parent / artifact["path"]).resolve()
        try:
            path.relative_to(record_path.parent.resolve())
        except ValueError as error:
            raise SystemExit(f"cell artifact escapes cell directory: {path}") from error
        if not path.is_file():
            raise SystemExit(f"missing cell artifact: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != artifact["sha256"]:
            raise SystemExit(f"cell artifact drift: {path}")
    phasef_sha = record.get("phasef_sha256")
    if phasef_sha:
        phasef = record_path.parent / "phasef.sorted.csv"
        if not phasef.is_file() or hashlib.sha256(phasef.read_bytes()).hexdigest() != phasef_sha:
            raise SystemExit(f"Phase-F artifact drift: {phasef}")
PY
verify_harness
python3 "$EVIDENCE_TOOL" seal --root "$OUT_ROOT" \
  --campaign "$OUT_ROOT/campaign.json" >/dev/null
python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT" >/dev/null
[[ -s "$OUT_ROOT/SHA256SUMS" ]] || { echo "Missing SHA256SUMS" >&2; exit 1; }
[[ -s "$OUT_ROOT/SEALED.json" ]] || { echo "Missing SEALED.json" >&2; exit 1; }
printf 'Sealed materialization-policy campaign written to %s\n' "$OUT_ROOT"
