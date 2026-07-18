#!/usr/bin/env bash
# Fixed-pool 1/2/3-CN scaling campaign for SHINE, SlabWalk, and d-HNSW.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
CAMPAIGN_KIND=${CAMPAIGN_KIND:-formal}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-multicn-$(date -u +%Y%m%dT%H%M%SZ)}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/$CAMPAIGN_ID}
CN_COUNTS=${CN_COUNTS:-"1 2 3"}
SYSTEMS=${SYSTEMS:-"SHINE SlabWalk d-HNSW"}
DATASETS=${DATASETS:-"SIFT1M DEEP1M GIST1M"}
REPEATS=${REPEATS:-5}
THREADS_PER_CN=${THREADS_PER_CN:-10}
COROUTINES=${COROUTINES:-2}
RESUME=${RESUME:-0}
DRY_RUN=${DRY_RUN:-0}
PREPARE_DHNSW=${PREPARE_DHNSW:-0}

GB_BIN=${GB_BIN:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713/build-final-v5/shine}
GB_REMOTE_BIN=${GB_REMOTE_BIN:-$GB_BIN}
EXPECTED_SLABWALK_SHA=${EXPECTED_SLABWALK_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIBRARY_PATH=${GB_LIBRARY_PATH:-/home/kvgroup/chaomei/lib}
GRAPH_CN_HOSTS=${GRAPH_CN_HOSTS:-"skv-node1 skv-node2 skv-node3"}
GRAPH_MN=${GRAPH_MN:-skv-node4}
GRAPH_PORT_BASE=${GRAPH_PORT_BASE:-14200}
GRAPH_TIMEOUT_S=${GRAPH_TIMEOUT_S:-2400}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}

DHNSW_SOURCE=${DHNSW_SOURCE:-/home/kvgroup/chaomei/d-HNSW}
DHNSW_CLIENT=${DHNSW_CLIENT:-$DHNSW_SOURCE/build/run_client}
DHNSW_SERVER=${DHNSW_SERVER:-$DHNSW_SOURCE/build/run_server}
DHNSW_CLIENT_HOSTS=${DHNSW_CLIENT_HOSTS:-"skv-node1 skv-node2 skv-node3"}
DHNSW_SERVER_HOST=${DHNSW_SERVER_HOST:-skv-node5}
DHNSW_SERVER_IP=${DHNSW_SERVER_IP:-10.0.0.65}
DHNSW_RDMA_IP=${DHNSW_RDMA_IP:-10.0.0.65}
DHNSW_DEPLOY_ROOT=${DHNSW_DEPLOY_ROOT:-/home/kvgroup/chaomei/vldb-multicn-dhnsw}
DHNSW_PORT_BASE=${DHNSW_PORT_BASE:-52200}
DHNSW_RDMA_PORT_BASE=${DHNSW_RDMA_PORT_BASE:-53200}
DHNSW_NIC_IDX=${DHNSW_NIC_IDX:-1}
DHNSW_OMP_CORES_PER_WORKER=${DHNSW_OMP_CORES_PER_WORKER:-4}
DHNSW_SERVER_READY_S=${DHNSW_SERVER_READY_S:-3600}
DHNSW_CLIENT_TIMEOUT_S=${DHNSW_CLIENT_TIMEOUT_S:-1800}
DHNSW_RUNTIME_CACHE_ROOT=${DHNSW_RUNTIME_CACHE_ROOT:-$DHNSW_SOURCE/runtime-bundles}
DHNSW_REMOTE_RUNTIME=$DHNSW_DEPLOY_ROOT/$CAMPAIGN_ID/runtime

RECORDER=$SCRIPT_DIR/record_vldb_multicn_run.py
PARSER=$SCRIPT_DIR/parse_dhnsw_frontier.py
ASSEMBLER=$SCRIPT_DIR/assemble_vldb_multicn_scaling.py
FINGERPRINTER=$SCRIPT_DIR/fingerprint_query_pool.py
RAW_CSV=$OUT_ROOT/runs.csv
MANIFEST=$OUT_ROOT/campaign.json
QUERY_POOLS=$OUT_ROOT/query_pools.json
REMOTE_RUN_ROOT=/home/kvgroup/chaomei/vldb-multicn-runs/$CAMPAIGN_ID

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s kind=%s datasets=%s systems=%s cn_counts=%s repeats=%s threads_per_cn=%s\n' \
    "$CAMPAIGN_ID" "$CAMPAIGN_KIND" "$DATASETS" "$SYSTEMS" \
    "$CN_COUNTS" "$REPEATS" "$THREADS_PER_CN"
  exit 0
fi

[[ "$CAMPAIGN_KIND" == "formal" || "$CAMPAIGN_KIND" == "smoke" ]] || {
  echo "CAMPAIGN_KIND must be formal or smoke" >&2; exit 2;
}
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || {
  echo "RESUME must be 0 or 1" >&2; exit 2;
}
[[ "$PREPARE_DHNSW" == "0" || "$PREPARE_DHNSW" == "1" ]] || {
  echo "PREPARE_DHNSW must be 0 or 1" >&2; exit 2;
}
[[ "$THREADS_PER_CN" =~ ^[1-9][0-9]*$ && "$COROUTINES" =~ ^[1-9][0-9]*$ ]] || {
  echo "thread and coroutine counts must be positive" >&2; exit 2;
}
[[ "$CAMPAIGN_ID" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "CAMPAIGN_ID contains unsafe path characters" >&2; exit 2;
}
if [[ "$CAMPAIGN_KIND" == "formal" ]]; then
  [[ "$DATASETS" == "SIFT1M DEEP1M GIST1M" ]] || {
    echo "formal dataset matrix must be SIFT1M DEEP1M GIST1M" >&2; exit 2;
  }
  [[ "$SYSTEMS" == "SHINE SlabWalk d-HNSW" ]] || {
    echo "formal system matrix must be SHINE SlabWalk d-HNSW" >&2; exit 2;
  }
  [[ "$CN_COUNTS" == "1 2 3" && "$REPEATS" == "5" ]] || {
    echo "formal matrix requires CN_COUNTS=1 2 3 and REPEATS=5" >&2; exit 2;
  }
fi
for value in $CN_COUNTS; do
  [[ "$value" =~ ^[123]$ ]] || { echo "invalid CN count: $value" >&2; exit 2; }
done
for value in $SYSTEMS; do
  [[ "$value" == "SHINE" || "$value" == "SlabWalk" || "$value" == "d-HNSW" ]] || {
    echo "invalid system: $value" >&2; exit 2;
  }
done
for tool in "$RECORDER" "$PARSER" "$ASSEMBLER" "$FINGERPRINTER"; do
  [[ -s "$tool" ]] || { echo "missing campaign tool: $tool" >&2; exit 2; }
done
[[ -x "$GB_BIN" && -x "$DHNSW_CLIENT" && -x "$DHNSW_SERVER" ]] || {
  echo "missing frozen SlabWalk or d-HNSW binary" >&2; exit 2;
}

GB_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
DHNSW_CLIENT_SHA=$(sha256sum "$DHNSW_CLIENT" | awk '{print $1}')
DHNSW_SERVER_SHA=$(sha256sum "$DHNSW_SERVER" | awk '{print $1}')
RUNNER_SHA=$(sha256sum "${BASH_SOURCE[0]}" | awk '{print $1}')
RECORDER_SHA=$(sha256sum "$RECORDER" | awk '{print $1}')
PARSER_SHA=$(sha256sum "$PARSER" | awk '{print $1}')
ASSEMBLER_SHA=$(sha256sum "$ASSEMBLER" | awk '{print $1}')
FINGERPRINTER_SHA=$(sha256sum "$FINGERPRINTER" | awk '{print $1}')
DHNSW_RUNTIME_CACHE=$DHNSW_RUNTIME_CACHE_ROOT/${DHNSW_CLIENT_SHA:0:16}-${DHNSW_SERVER_SHA:0:16}
DHNSW_RUNTIME_MANIFEST_SHA=
[[ "$GB_SHA" == "$EXPECTED_SLABWALK_SHA" ]] || {
  echo "SlabWalk binary SHA mismatch: $GB_SHA" >&2; exit 2;
}

SSH=(ssh -n -o BatchMode=yes -o LogLevel=ERROR -o StrictHostKeyChecking=no)
SCP=(scp -q -o BatchMode=yes -o LogLevel=ERROR -o StrictHostKeyChecking=no)
ACTIVE_HOSTS=()
ACTIVE_DIRS=()

remote() {
  local host=$1
  shift
  "${SSH[@]}" "$host" "$@"
}

prepare_dhnsw_runtime_bundle() {
  local temporary soname source
  mkdir -p "$DHNSW_RUNTIME_CACHE_ROOT"
  if [[ ! -e "$DHNSW_RUNTIME_CACHE" ]]; then
    [[ "$PREPARE_DHNSW" == "1" ]] || {
      echo "missing d-HNSW runtime bundle; rerun with PREPARE_DHNSW=1" >&2
      return 2
    }
    temporary=$(mktemp -d "$DHNSW_RUNTIME_CACHE_ROOT/.bundle.XXXXXX")
    mkdir -p "$temporary/lib"
    (ldd "$DHNSW_CLIENT"; ldd "$DHNSW_SERVER") |
      awk '$2 == "=>" && ($3 ~ "^/usr/local/lib/" || $1 == "libopenblas.so.0") {print $1, $3}' |
      LC_ALL=C sort -u > "$temporary/dependencies.txt"
    [[ -s "$temporary/dependencies.txt" ]] || {
      echo "empty d-HNSW runtime dependency set" >&2
      return 2
    }
    while read -r soname source; do
      [[ "$soname" =~ ^[A-Za-z0-9_.+-]+$ && -f "$source" ]] || {
        echo "unsafe or missing d-HNSW dependency: $soname $source" >&2
        return 2
      }
      cp -L -- "$source" "$temporary/lib/$soname"
    done < "$temporary/dependencies.txt"
    (cd "$temporary/lib" && sha256sum ./* | LC_ALL=C sort) > "$temporary/manifest.sha256"
    mv "$temporary" "$DHNSW_RUNTIME_CACHE"
  fi
  [[ -d "$DHNSW_RUNTIME_CACHE/lib" && -s "$DHNSW_RUNTIME_CACHE/manifest.sha256" ]] || {
    echo "incomplete d-HNSW runtime bundle: $DHNSW_RUNTIME_CACHE" >&2
    return 2
  }
  (cd "$DHNSW_RUNTIME_CACHE/lib" && sha256sum -c ../manifest.sha256 >/dev/null)
  if env LD_LIBRARY_PATH="$DHNSW_RUNTIME_CACHE/lib" ldd "$DHNSW_CLIENT" "$DHNSW_SERVER" |
      grep -q 'not found'; then
    echo "d-HNSW runtime bundle has unresolved dependencies" >&2
    return 2
  fi
  DHNSW_RUNTIME_MANIFEST_SHA=$(sha256sum "$DHNSW_RUNTIME_CACHE/manifest.sha256" | awk '{print $1}')
}

deploy_dhnsw_runtime_bundle() {
  local host
  local -a client_hosts
  read -r -a client_hosts <<< "$DHNSW_CLIENT_HOSTS"
  local -a runtime_hosts=("${client_hosts[@]}" "$DHNSW_SERVER_HOST")
  for host in "${runtime_hosts[@]}"; do
    [[ -n "$host" ]] || continue
    remote "$host" "mkdir -p '$DHNSW_REMOTE_RUNTIME/lib'"
    if [[ "$PREPARE_DHNSW" == "1" ]]; then
      rsync -a "$DHNSW_RUNTIME_CACHE/lib/" "$host:$DHNSW_REMOTE_RUNTIME/lib/"
      rsync -a "$DHNSW_RUNTIME_CACHE/manifest.sha256" \
        "$host:$DHNSW_REMOTE_RUNTIME/manifest.sha256"
    fi
    remote "$host" \
      "test \"\$(sha256sum '$DHNSW_REMOTE_RUNTIME/manifest.sha256' | awk '{print \$1}')\" = '$DHNSW_RUNTIME_MANIFEST_SHA'; \
       cd '$DHNSW_REMOTE_RUNTIME/lib'; sha256sum -c ../manifest.sha256 >/dev/null"
  done
}

verify_remote_pid() {
  local host=$1 directory=$2
  remote "$host" \
    "test -s '$directory/process.pid' -a -s '$directory/process.exe' \
       -a -s '$directory/process.sha256' -a -s '$directory/process.starttime'; \
     pid=\$(cat '$directory/process.pid'); \
     expected=\$(cat '$directory/process.exe'); \
     expected_sha=\$(cat '$directory/process.sha256'); \
     expected_start=\$(cat '$directory/process.starttime'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     actual_sha=\$(sha256sum /proc/\$pid/exe 2>/dev/null | awk '{print \$1}'); \
     actual_start=\$(awk '{print \$22}' /proc/\$pid/stat 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\" \
       -a \"\$actual_sha\" = \"\$expected_sha\" \
       -a \"\$actual_start\" = \"\$expected_start\"" 2>/dev/null
}

stop_remote_pid() {
  local host=$1 directory=$2
  if verify_remote_pid "$host" "$directory"; then
    remote "$host" \
      "pid=\$(cat '$directory/process.pid'); kill -TERM \$pid 2>/dev/null || true; \
       for ignored in \$(seq 1 100); do test -e /proc/\$pid || exit 0; sleep 0.1; done; \
       expected=\$(cat '$directory/process.exe'); \
       expected_start=\$(cat '$directory/process.starttime'); \
       actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
       actual_start=\$(awk '{print \$22}' /proc/\$pid/stat 2>/dev/null); \
       if test \"\$actual\" = \"\$expected\" -a \"\$actual_start\" = \"\$expected_start\"; \
       then kill -KILL \$pid 2>/dev/null || true; fi" || true
  fi
}

cleanup_active() {
  local index
  for ((index=${#ACTIVE_HOSTS[@]}-1; index>=0; index--)); do
    stop_remote_pid "${ACTIVE_HOSTS[$index]}" "${ACTIVE_DIRS[$index]}"
  done
}
trap cleanup_active EXIT INT TERM

prepare_remote_run_dir() {
  local host=$1 directory=$2
  remote "$host" \
    "set -e; if test -e '$directory'; then \
       test '$RESUME' = 1 || { echo 'remote run directory exists: $directory' >&2; exit 2; }; \
       mv '$directory' '${directory}.incomplete.'\$(date -u +%Y%m%dT%H%M%SZ); \
     fi; mkdir -p '$directory'"
}

shell_join() {
  local output="" item quoted
  for item in "$@"; do
    printf -v quoted '%q' "$item"
    output+="$quoted "
  done
  printf '%s' "$output"
}

launch_remote_owned() {
  local host=$1 directory=$2 working_directory=$3 binary=$4 expected_sha=$5
  shift 5
  local command
  command=$(shell_join "$@")
  prepare_remote_run_dir "$host" "$directory"
  remote "$host" \
    "set -e; resolved=\$(realpath '$binary'); test -x \"\$resolved\"; \
     observed=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); \
     test \"\$observed\" = '$expected_sha'; \
     printf '%s\n' \"\$resolved\" > '$directory/process.exe'; \
     printf '%s\n' \"\$observed\" > '$directory/process.sha256'; \
     cd '$working_directory'; \
     nohup $command > '$directory/process.stdout' 2> '$directory/process.stderr' < /dev/null & \
     pid=\$!; printf '%s\n' \$pid > '$directory/process.pid'; \
     for ignored in \$(seq 1 100); do \
       actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null || true); \
       if test \"\$actual\" = \"\$resolved\"; then \
         awk '{print \$22}' /proc/\$pid/stat > '$directory/process.starttime'; exit 0; \
       fi; test -e /proc/\$pid || exit 3; sleep 0.1; \
     done; exit 4"
  verify_remote_pid "$host" "$directory"
  ACTIVE_HOSTS+=("$host")
  ACTIVE_DIRS+=("$directory")
}

schedule_remote_owned() {
  local host=$1 directory=$2 working_directory=$3 binary=$4 expected_sha=$5 start_ms=$6
  shift 6
  local command
  command=$(shell_join "$@")
  prepare_remote_run_dir "$host" "$directory"
  remote "$host" \
    "set -e; resolved=\$(realpath '$binary'); test -x \"\$resolved\"; \
     observed=\$(sha256sum \"\$resolved\" | awk '{print \$1}'); \
     test \"\$observed\" = '$expected_sha'; \
     printf '%s\n' \"\$resolved\" > '$directory/process.exe'; \
     printf '%s\n' \"\$observed\" > '$directory/process.sha256'; \
     cd '$working_directory'; \
     nohup bash -c 'while test \$(date +%s%3N) -lt $start_ms; do sleep 0.02; done; exec $command' \
       > '$directory/process.stdout' 2> '$directory/process.stderr' < /dev/null & \
     pid=\$!; printf '%s\n' \$pid > '$directory/process.pid'; \
     awk '{print \$22}' /proc/\$pid/stat > '$directory/process.starttime'"
  ACTIVE_HOSTS+=("$host")
  ACTIVE_DIRS+=("$directory")
}

wait_remote_identity() {
  local host=$1 directory=$2 timeout_s=$3
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    verify_remote_pid "$host" "$directory" && return 0
    remote "$host" "pid=\$(cat '$directory/process.pid'); test -e /proc/\$pid" 2>/dev/null || return 1
    sleep 0.1
  done
  return 1
}

wait_remote_exit() {
  local host=$1 directory=$2 timeout_s=$3
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    verify_remote_pid "$host" "$directory" || return 0
    sleep 0.25
  done
  echo "timed out waiting for owned process: $host:$directory" >&2
  return 1
}

wait_remote_log() {
  local host=$1 directory=$2 pattern=$3 timeout_s=$4
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    remote "$host" "grep -q '$pattern' '$directory/process.stdout'" 2>/dev/null && return 0
    verify_remote_pid "$host" "$directory" || return 1
    sleep 1
  done
  return 1
}

fetch_remote_file() {
  local host=$1 remote_path=$2 local_path=$3
  mkdir -p "$(dirname "$local_path")"
  "${SCP[@]}" "$host:$remote_path" "$local_path"
}

cell_exists() {
  local dataset=$1 system=$2 cn_count=$3 repeat=$4
  [[ -s "$RAW_CSV" ]] || return 1
  python3 - "$RAW_CSV" "$dataset" "$system" "$cn_count" "$repeat" <<'PY'
import csv, hashlib, sys
from pathlib import Path
path, dataset, system, cn_count, repeat = sys.argv[1:]
rows = list(csv.DictReader(open(path)))
matches = [row for row in rows if (
    row["dataset"] == dataset and row["system"] == system
    and row["cn_count"] == cn_count and row["repeat"] == repeat
)]
if len(matches) != 1:
    raise SystemExit(1)
source = Path(path).resolve().parent / matches[0]["source"]
if not source.is_file():
    raise SystemExit(1)
source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
raise SystemExit(0 if source_sha256 == matches[0]["source_sha256"] else 1)
PY
}

dataset_parameters() {
  case "$1" in
    SIFT1M)
      DATA_SLUG=sift1m; DH_DATASET=sift1M; DH_ALIAS=sift
      DH_QUERY=sift_query.fvecs; DH_GT=sift_groundtruth.ivecs; DH_BASE=sift_base.fvecs
      DIM=128; NUM_SUB=160; EF=100; M=16; EFC=100; EXPECTED_QUERIES=10000
      LAVD_REGION_BYTES=5368709120
      ;;
    DEEP1M)
      DATA_SLUG=deep1m; DH_DATASET=deep1M; DH_ALIAS=deep1M
      DH_QUERY=deep1M_query.fvecs; DH_GT=deep1M_groundtruth.ivecs; DH_BASE=deep1M_base.fvecs
      DIM=96; NUM_SUB=160; EF=200; M=16; EFC=100; EXPECTED_QUERIES=10000
      LAVD_REGION_BYTES=4294967296
      ;;
    GIST1M)
      DATA_SLUG=gist1m; DH_DATASET=gist1M; DH_ALIAS=gist
      DH_QUERY=gist_query.fvecs; DH_GT=gist_groundtruth.ivecs; DH_BASE=gist_base.fvecs
      DIM=960; NUM_SUB=120; EF=300; M=16; EFC=100; EXPECTED_QUERIES=1000
      LAVD_REGION_BYTES=9663676416
      ;;
    *) echo "unsupported dataset: $1" >&2; return 2 ;;
  esac
  GRAPH_DATA=$GB_DATA/$DATA_SLUG
  GRAPH_QUERY=$GRAPH_DATA/queries/query-uniform.fbin
  GRAPH_GT=$GRAPH_DATA/queries/groundtruth-uniform.bin
  DH_DATA_DIR=$DHNSW_SOURCE/datasets/$DH_ALIAS
  DH_QUERY_PATH=$DH_DATA_DIR/$DH_QUERY
  DH_GT_PATH=$DH_DATA_DIR/$DH_GT
  DH_BASE_PATH=$DH_DATA_DIR/$DH_BASE
}

prepare_query_pools() {
  mkdir -p "$OUT_ROOT/query_pool_records"
  local dataset graph_record dh_record
  for dataset in SIFT1M DEEP1M GIST1M; do
    dataset_parameters "$dataset"
    for file in "$GRAPH_QUERY" "$GRAPH_GT" "$DH_QUERY_PATH" "$DH_GT_PATH"; do
      [[ -s "$file" ]] || { echo "missing query-pool input: $file" >&2; return 2; }
    done
    graph_record=$OUT_ROOT/query_pool_records/${dataset}_slabwalk.json
    dh_record=$OUT_ROOT/query_pool_records/${dataset}_dhnsw.json
    python3 "$FINGERPRINTER" --query "$GRAPH_QUERY" --groundtruth "$GRAPH_GT" \
      --dataset "$dataset" --method SlabWalk --metric l2 --limit "$EXPECTED_QUERIES" \
      --out "$graph_record" >/dev/null
    python3 "$FINGERPRINTER" --query "$DH_QUERY_PATH" --groundtruth "$DH_GT_PATH" \
      --dataset "$dataset" --method d-HNSW --metric l2 --limit "$EXPECTED_QUERIES" \
      --out "$dh_record" >/dev/null
  done
  python3 - "$OUT_ROOT/query_pool_records" "$QUERY_POOLS" <<'PY'
import json, sys
from pathlib import Path
root, out = Path(sys.argv[1]), Path(sys.argv[2])
result = {}
for dataset in ("SIFT1M", "DEEP1M", "GIST1M"):
    graph = json.loads((root / f"{dataset}_slabwalk.json").read_text())
    dhnsw = json.loads((root / f"{dataset}_dhnsw.json").read_text())
    if graph["query"]["canonical_sha256"] != dhnsw["query"]["canonical_sha256"]:
        raise SystemExit(f"{dataset}: cross-format query mismatch")
    if graph["groundtruth"]["canonical_ids_sha256"] != dhnsw["groundtruth"]["canonical_ids_sha256"]:
        raise SystemExit(f"{dataset}: cross-format ground-truth mismatch")
    result[dataset] = {
        "query_canonical_sha256": graph["query"]["canonical_sha256"],
        "groundtruth_canonical_sha256": graph["groundtruth"]["canonical_ids_sha256"],
        "expected_queries": graph["query"]["rows"],
        "graph_record": str((root / f"{dataset}_slabwalk.json").resolve()),
        "dhnsw_record": str((root / f"{dataset}_dhnsw.json").resolve()),
    }
out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
PY
}

query_pool_value() {
  python3 - "$QUERY_POOLS" "$1" "$2" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])
PY
}

write_campaign_manifest() {
  python3 - "$MANIFEST" "$CAMPAIGN_ID" "$GB_SHA" "$DHNSW_CLIENT_SHA" \
    "$DHNSW_SERVER_SHA" "$DHNSW_RUNTIME_MANIFEST_SHA" "$QUERY_POOLS" \
    "$RUNNER_SHA" "$RECORDER_SHA" "$PARSER_SHA" "$ASSEMBLER_SHA" "$FINGERPRINTER_SHA" \
    "$THREADS_PER_CN" "$COROUTINES" \
    "$GRAPH_CN_HOSTS" "$GRAPH_MN" "$DHNSW_CLIENT_HOSTS" "$DHNSW_SERVER_HOST" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
(path_s, campaign_id, slab_sha, dh_client_sha, dh_server_sha, dh_runtime_sha, pools_s,
 runner_sha, recorder_sha, parser_sha, assembler_sha, fingerprinter_sha,
 threads, coroutines, graph_clients, graph_mn, dh_clients, dh_server) = sys.argv[1:]
path = Path(path_s)
pools = json.loads(Path(pools_s).read_text())
tool_sha256 = {
    "assembler": assembler_sha,
    "dhnsw_parser": parser_sha,
    "query_fingerprinter": fingerprinter_sha,
    "recorder": recorder_sha,
    "runner": runner_sha,
}
protocol = {
    "datasets": ["SIFT1M", "DEEP1M", "GIST1M"],
    "systems": ["SHINE", "SlabWalk", "d-HNSW"],
    "cn_counts": [1, 2, 3],
    "repeats": 5,
    "threads_per_cn": int(threads),
    "graph_coroutines": int(coroutines),
    "graph_ef": {"SIFT1M": 100, "DEEP1M": 200, "GIST1M": 300},
    "dhnsw_ef": {"SIFT1M": 100, "DEEP1M": 200, "GIST1M": 300},
    "measurement_mode": "one_fixed_logical_query_pool_partitioned_across_cns",
    "graph_latency_scope": {
        "1cn": "all_queries_single_cn",
        "multi_cn": "not_reported_cross_cn_frozen_binary_boundary",
    },
    "dhnsw_throughput_aggregation": "sum_of_concurrent_disjoint_client_shards",
    "dhnsw_required_metrics": "atomic_FRONTIER_THREAD_RESULT_fixed_pool_query_coverage_qps_recall",
    "dhnsw_machine_record_recovery": "exact_complete_sentinel_with_optional_known_Thread_prefix_interleaving",
    "dhnsw_detail_metrics": "best_effort_non_gating_human_readable_per_thread_breakdown",
    "slabwalk_binary_sha256": slab_sha,
    "dhnsw_client_binary_sha256": dh_client_sha,
    "dhnsw_server_binary_sha256": dh_server_sha,
    "dhnsw_runtime_manifest_sha256": dh_runtime_sha,
    "tool_sha256": tool_sha256,
    "query_pools": pools,
    "graph_hosts": {"clients": graph_clients.split(), "memory": graph_mn},
    "dhnsw_hosts": {"clients": dh_clients.split(), "server": dh_server},
}
protocol_fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
record = {
    "kind": "vldb_multicn_campaign",
    "campaign_id": campaign_id,
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "protocol_fingerprint": protocol_fingerprint,
    "datasets": protocol["datasets"],
    "systems": protocol["systems"],
    "cn_counts": protocol["cn_counts"],
    "repeats": protocol["repeats"],
    "expected_queries": {name: pools[name]["expected_queries"] for name in protocol["datasets"]},
    "slabwalk_binary_sha256": slab_sha,
    "dhnsw_binary_sha256": dh_client_sha,
    "dhnsw_runtime_manifest_sha256": dh_runtime_sha,
    "tool_sha256": tool_sha256,
    "protocol": protocol,
}
if path.exists():
    old = json.loads(path.read_text())
    if old.get("campaign_id") != campaign_id or old.get("protocol") != protocol:
        raise SystemExit("multi-CN campaign manifest drift")
else:
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
PY
}

manifest_fingerprint() {
  python3 - "$MANIFEST" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["protocol_fingerprint"])
PY
}

preflight_graph_memory() {
  local required=$((INDEX_REGION_BYTES + $1 + 2147483648))
  local available
  available=$(remote "$GRAPH_MN" "awk '/MemAvailable:/ {print \$2 * 1024}' /proc/meminfo")
  (( available >= required )) || {
    echo "insufficient MN memory: available=$available required=$required" >&2; return 2;
  }
}

run_graph_cell() {
  local dataset=$1 system=$2 cn_count=$3 repeat=$4 protocol_fingerprint=$5
  dataset_parameters "$dataset"
  cell_exists "$dataset" "$system" "$cn_count" "$repeat" && {
    [[ "$RESUME" == "1" ]] || { echo "duplicate completed cell" >&2; return 2; }
    echo "SKIP $dataset $system ${cn_count}CN r$repeat"
    return 0
  }
  preflight_graph_memory "$LAVD_REGION_BYTES"
  local dataset_index system_index port tag mn_dir
  case "$dataset" in SIFT1M) dataset_index=0;; DEEP1M) dataset_index=1;; GIST1M) dataset_index=2;; esac
  [[ "$system" == "SHINE" ]] && system_index=0 || system_index=1
  port=$((GRAPH_PORT_BASE + dataset_index * 100 + system_index * 40 + cn_count * 10 + repeat))
  tag=${dataset}_${system}_${cn_count}cn_r${repeat}
  mn_dir=$REMOTE_RUN_ROOT/graph/$tag/mn
  local -a mn_command=(env LD_LIBRARY_PATH="$GB_LIBRARY_PATH" numactl --preferred=1
    "$GB_REMOTE_BIN" --is-server --num-clients "$cn_count" --port "$port"
    --index-region-bytes "$INDEX_REGION_BYTES")
  launch_remote_owned "$GRAPH_MN" "$mn_dir" /tmp "$GB_REMOTE_BIN" "$GB_SHA" "${mn_command[@]}"
  sleep 2

  read -r -a cn_hosts <<< "$GRAPH_CN_HOSTS"
  local lavd=0
  local -a method_env=(env LD_LIBRARY_PATH="$GB_LIBRARY_PATH" GB_QUERY_LATENCY=1)
  if [[ "$system" == "SlabWalk" ]]; then
    lavd=8
    method_env+=(SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_BUILD_THREADS=20
      SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2
      SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1)
    [[ "$dataset" == "GIST1M" ]] && method_env+=(SHINE_LAVD_RABITQ_B=2)
  fi
  local -a common_args=(--servers "$GRAPH_MN" --port "$port"
    --threads "$THREADS_PER_CN" --query-contexts "$THREADS_PER_CN"
    --coroutines "$COROUTINES" --data-path "$GRAPH_DATA" --query-suffix uniform
    --ef-search "$EF" --ef-construction "$EFC" --m "$M" --k 10 --spec-k 1
    --load-index --lavd "$lavd" --label "$tag" --index-region-bytes "$INDEX_REGION_BYTES")
  [[ "$lavd" != 0 ]] && common_args+=(--lavd-region-bytes "$LAVD_REGION_BYTES")

  local rank host cn_dir
  local -a client_dirs=()
  for ((rank=1; rank<cn_count; rank++)); do
    host=${cn_hosts[$rank]}
    cn_dir=$REMOTE_RUN_ROOT/graph/$tag/c$rank
    client_dirs+=("$cn_dir")
    launch_remote_owned "$host" "$cn_dir" /tmp "$GB_REMOTE_BIN" "$GB_SHA" \
      "${method_env[@]}" numactl --preferred=1 "$GB_REMOTE_BIN" "${common_args[@]}"
  done
  local init_dir=$REMOTE_RUN_ROOT/graph/$tag/c0
  local -a initiator_args=("${method_env[@]}" numactl --preferred=1 "$GB_REMOTE_BIN"
    "${common_args[@]}" --initiator)
  if (( cn_count > 1 )); then
    initiator_args+=(--clients)
    for ((rank=1; rank<cn_count; rank++)); do initiator_args+=("${cn_hosts[$rank]}"); done
  fi
  launch_remote_owned "${cn_hosts[0]}" "$init_dir" /tmp "$GB_REMOTE_BIN" "$GB_SHA" "${initiator_args[@]}"
  wait_remote_exit "${cn_hosts[0]}" "$init_dir" "$GRAPH_TIMEOUT_S" || return 1
  for ((rank=1; rank<cn_count; rank++)); do
    wait_remote_exit "${cn_hosts[$rank]}" "${client_dirs[$((rank-1))]}" 120 || return 1
  done
  wait_remote_exit "$GRAPH_MN" "$mn_dir" 120 || return 1

  local local_dir=$OUT_ROOT/raw/graph/$tag
  mkdir -p "$local_dir"
  fetch_remote_file "${cn_hosts[0]}" "$init_dir/process.stdout" "$local_dir/c0.json"
  fetch_remote_file "${cn_hosts[0]}" "$init_dir/process.stderr" "$local_dir/c0.stderr"
  local -a recorder_clients=()
  for ((rank=1; rank<cn_count; rank++)); do
    fetch_remote_file "${cn_hosts[$rank]}" "${client_dirs[$((rank-1))]}/process.stdout" "$local_dir/c${rank}.stdout"
    fetch_remote_file "${cn_hosts[$rank]}" "${client_dirs[$((rank-1))]}/process.stderr" "$local_dir/c${rank}.stderr"
    recorder_clients+=(--client-log "$local_dir/c${rank}.stderr")
  done
  local query_sha gt_sha source
  query_sha=$(query_pool_value "$dataset" query_canonical_sha256)
  gt_sha=$(query_pool_value "$dataset" groundtruth_canonical_sha256)
  source=$OUT_ROOT/sources/${tag}.json
  python3 "$RECORDER" graph --campaign-id "$CAMPAIGN_ID" \
    --protocol-fingerprint "$protocol_fingerprint" --dataset "$dataset" --system "$system" \
    --cn-count "$cn_count" --repeat "$repeat" --binary-sha256 "$GB_SHA" \
    --query-sha256 "$query_sha" --groundtruth-sha256 "$gt_sha" \
    --expected-queries "$EXPECTED_QUERIES" --initiator-json "$local_dir/c0.json" \
    "${recorder_clients[@]}" --source "$source" --csv "$RAW_CSV"
}

write_fixed_shards() {
  local query=$1 gt=$2 output=$3 cn_count=$4 alias=$5 query_name=$6 gt_name=$7 expected=$8
  python3 - "$query" "$gt" "$output" "$cn_count" "$alias" "$query_name" "$gt_name" "$expected" <<'PY'
import os, struct, sys
from pathlib import Path
query, gt, out, count, alias, query_name, gt_name, expected = sys.argv[1:]
count, expected = int(count), int(expected)
out = Path(out)
def read(path):
    path = Path(path)
    with path.open("rb") as f:
        raw = f.read(4)
        if len(raw) != 4: raise SystemExit(f"truncated fixed-vector file: {path}")
        width = struct.unpack("<i", raw)[0]
    row_bytes = 4 + width * 4
    if path.stat().st_size % row_bytes: raise SystemExit(f"invalid fixed-vector file: {path}")
    rows = path.stat().st_size // row_bytes
    return width, row_bytes, rows
q_width, q_bytes, q_rows = read(query)
g_width, g_bytes, g_rows = read(gt)
if q_rows != expected or g_rows != expected: raise SystemExit("query shard source row mismatch")
qf, gf = open(query, "rb"), open(gt, "rb")
try:
    offset = 0
    for rank in range(count):
        rows = expected // count + (1 if rank < expected % count else 0)
        target = out / f"c{count}" / f"r{rank}" / "datasets" / alias
        target.mkdir(parents=True, exist_ok=True)
        with (target / query_name).open("wb") as qout, (target / gt_name).open("wb") as gout:
            qf.seek(offset * q_bytes); gf.seek(offset * g_bytes)
            qout.write(qf.read(rows * q_bytes)); gout.write(gf.read(rows * g_bytes))
        offset += rows
    if offset != expected: raise SystemExit("query shard coverage mismatch")
finally:
    qf.close(); gf.close()
PY
}

prepare_dhnsw_dataset() {
  local dataset=$1
  dataset_parameters "$dataset"
  local shard_root=$OUT_ROOT/dhnsw_shards/$dataset
  local cn_count rank host rank_root source_rank
  for cn_count in $CN_COUNTS; do
    write_fixed_shards "$DH_QUERY_PATH" "$DH_GT_PATH" "$shard_root" "$cn_count" \
      "$DH_ALIAS" "$DH_QUERY" "$DH_GT" "$EXPECTED_QUERIES"
    for ((rank=0; rank<cn_count; rank++)); do
      read -r -a hosts <<< "$DHNSW_CLIENT_HOSTS"
      host=${hosts[$rank]}
      rank_root=$DHNSW_DEPLOY_ROOT/$CAMPAIGN_ID/$dataset/c${cn_count}/r${rank}
      source_rank=$shard_root/c${cn_count}/r${rank}
      remote "$host" "mkdir -p '$rank_root/build' '$rank_root/benchs/pipeline/test' '$rank_root/datasets/$DH_ALIAS'"
      if [[ "$PREPARE_DHNSW" == "1" ]]; then
        rsync -a "$DHNSW_CLIENT" "$host:$rank_root/build/run_client"
        rsync -a "$source_rank/datasets/$DH_ALIAS/" "$host:$rank_root/datasets/$DH_ALIAS/"
      fi
      remote "$host" \
        "set -e; test -x '$rank_root/build/run_client'; \
         test \"\$(sha256sum '$rank_root/build/run_client' | awk '{print \$1}')\" = '$DHNSW_CLIENT_SHA'; \
         test -z \"\$(LD_LIBRARY_PATH='$DHNSW_REMOTE_RUNTIME/lib' ldd '$rank_root/build/run_client' | awk '/not found/')\"; \
         test -s '$rank_root/datasets/$DH_ALIAS/$DH_QUERY' -a -s '$rank_root/datasets/$DH_ALIAS/$DH_GT'"
    done
  done
  local server_root=$DHNSW_DEPLOY_ROOT/$CAMPAIGN_ID/server/$dataset
  remote "$DHNSW_SERVER_HOST" "mkdir -p '$server_root/build' '$server_root/data'"
  if [[ "$PREPARE_DHNSW" == "1" ]]; then
    rsync -a "$DHNSW_SERVER" "$DHNSW_SERVER_HOST:$server_root/build/run_server"
    rsync -aL --partial "$DH_BASE_PATH" "$DHNSW_SERVER_HOST:$server_root/data/$DH_BASE"
  fi
  remote "$DHNSW_SERVER_HOST" \
    "set -e; test -x '$server_root/build/run_server'; \
     test \"\$(sha256sum '$server_root/build/run_server' | awk '{print \$1}')\" = '$DHNSW_SERVER_SHA'; \
     test -z \"\$(LD_LIBRARY_PATH='$DHNSW_REMOTE_RUNTIME/lib' ldd '$server_root/build/run_server' | awk '/not found/')\"; \
     test -s '$server_root/data/$DH_BASE'"
}

run_dhnsw_dataset() {
  local dataset=$1 protocol_fingerprint=$2
  dataset_parameters "$dataset"
  prepare_dhnsw_dataset "$dataset"
  local dataset_index
  case "$dataset" in SIFT1M) dataset_index=0;; DEEP1M) dataset_index=1;; GIST1M) dataset_index=2;; esac
  local port=$((DHNSW_PORT_BASE + dataset_index))
  local rdma_port=$((DHNSW_RDMA_PORT_BASE + dataset_index))
  local server_root=$DHNSW_DEPLOY_ROOT/$CAMPAIGN_ID/server/$dataset
  local server_dir=$REMOTE_RUN_ROOT/dhnsw/$dataset/server
  local -a server_command=(env LD_LIBRARY_PATH="$DHNSW_REMOTE_RUNTIME/lib" numactl --preferred=1
    "$server_root/build/run_server" --server_ip="$DHNSW_SERVER_IP" --port="$port"
    --rdma_port="$rdma_port" --use_nic_idx="$DHNSW_NIC_IDX"
    --dataset_path="$server_root/data/$DH_BASE" --dim="$DIM" --num_sub_hnsw="$NUM_SUB"
    --meta_hnsw_neighbors=32 --sub_hnsw_neighbors=48)
  launch_remote_owned "$DHNSW_SERVER_HOST" "$server_dir" "$server_root/build" \
    "$server_root/build/run_server" "$DHNSW_SERVER_SHA" "${server_command[@]}"
  wait_remote_log "$DHNSW_SERVER_HOST" "$server_dir" "gRPC server listening" "$DHNSW_SERVER_READY_S" || {
    echo "d-HNSW server did not become ready for $dataset" >&2; return 1;
  }

  local cn_count repeat rank host rank_root run_dir start_ms tag local_dir
  read -r -a hosts <<< "$DHNSW_CLIENT_HOSTS"
  for cn_count in $CN_COUNTS; do
    for ((repeat=0; repeat<REPEATS; repeat++)); do
      cell_exists "$dataset" d-HNSW "$cn_count" "$repeat" && {
        [[ "$RESUME" == "1" ]] || { echo "duplicate completed d-HNSW cell" >&2; return 2; }
        echo "SKIP $dataset d-HNSW ${cn_count}CN r$repeat"
        continue
      }
      tag=${dataset}_d-HNSW_${cn_count}cn_r${repeat}
      start_ms=$(( $(date +%s%3N) + 12000 ))
      local -a run_dirs=()
      for ((rank=0; rank<cn_count; rank++)); do
        host=${hosts[$rank]}
        rank_root=$DHNSW_DEPLOY_ROOT/$CAMPAIGN_ID/$dataset/c${cn_count}/r${rank}
        run_dir=$REMOTE_RUN_ROOT/dhnsw/$tag/c$rank
        run_dirs+=("$run_dir")
        schedule_remote_owned "$host" "$run_dir" "$rank_root/build" \
          "$rank_root/build/run_client" "$DHNSW_CLIENT_SHA" "$((start_ms + rank * 250))" \
          env LD_LIBRARY_PATH="$DHNSW_REMOTE_RUNTIME/lib" numactl --preferred=1 "$rank_root/build/run_client" \
          --server_address="$DHNSW_SERVER_IP:$port" --rdma_server_address="$DHNSW_RDMA_IP:$rdma_port" \
          --use_nic_idx="$DHNSW_NIC_IDX" --dataset="$DH_DATASET" --worker_threads="$THREADS_PER_CN" \
          --ef_override="$EF" --fixed_query_pool=true --benchmark_duration=20 \
          --physical_cores_per_thread="$DHNSW_OMP_CORES_PER_WORKER" \
          --use_physical_cores_only=true --worker_start_stagger_ms=20 \
          --log_file="$run_dir/batches.log"
      done
      for ((rank=0; rank<cn_count; rank++)); do
        wait_remote_identity "${hosts[$rank]}" "${run_dirs[$rank]}" 30 || {
          echo "d-HNSW client identity failed: $tag c$rank" >&2; return 1;
        }
      done
      for ((rank=0; rank<cn_count; rank++)); do
        wait_remote_exit "${hosts[$rank]}" "${run_dirs[$rank]}" "$DHNSW_CLIENT_TIMEOUT_S" || return 1
      done
      local_dir=$OUT_ROOT/raw/dhnsw/$tag
      mkdir -p "$local_dir"
      local -a client_logs=()
      for ((rank=0; rank<cn_count; rank++)); do
        fetch_remote_file "${hosts[$rank]}" "${run_dirs[$rank]}/process.stdout" "$local_dir/c${rank}.log"
        fetch_remote_file "${hosts[$rank]}" "${run_dirs[$rank]}/process.stderr" "$local_dir/c${rank}.stderr"
        client_logs+=(--client-log "$local_dir/c${rank}.log")
      done
      local query_sha gt_sha source
      query_sha=$(query_pool_value "$dataset" query_canonical_sha256)
      gt_sha=$(query_pool_value "$dataset" groundtruth_canonical_sha256)
      source=$OUT_ROOT/sources/${tag}.json
      python3 "$RECORDER" dhnsw --campaign-id "$CAMPAIGN_ID" \
        --protocol-fingerprint "$protocol_fingerprint" --dataset "$dataset" \
        --cn-count "$cn_count" --repeat "$repeat" --binary-sha256 "$DHNSW_CLIENT_SHA" \
        --query-sha256 "$query_sha" --groundtruth-sha256 "$gt_sha" \
        --expected-queries "$EXPECTED_QUERIES" "${client_logs[@]}" \
        --threads "$THREADS_PER_CN" --ef "$EF" --source "$source" --csv "$RAW_CSV"
    done
  done
  stop_remote_pid "$DHNSW_SERVER_HOST" "$server_dir"
}

if [[ -e "$OUT_ROOT" && "$RESUME" != "1" ]]; then
  echo "refusing existing OUT_ROOT without RESUME=1: $OUT_ROOT" >&2
  exit 2
fi
mkdir -p "$OUT_ROOT/sources"
prepare_dhnsw_runtime_bundle
prepare_query_pools
write_campaign_manifest
PROTOCOL_FINGERPRINT=$(manifest_fingerprint)
if [[ " $SYSTEMS " == *" d-HNSW "* ]]; then
  deploy_dhnsw_runtime_bundle
fi

for dataset in $DATASETS; do
  for system in $SYSTEMS; do
    if [[ "$system" == "d-HNSW" ]]; then
      run_dhnsw_dataset "$dataset" "$PROTOCOL_FINGERPRINT"
      continue
    fi
    for cn_count in $CN_COUNTS; do
      for ((repeat=0; repeat<REPEATS; repeat++)); do
        run_graph_cell "$dataset" "$system" "$cn_count" "$repeat" "$PROTOCOL_FINGERPRINT"
      done
    done
  done
done

if [[ "$CAMPAIGN_KIND" == "formal" ]]; then
  python3 "$ASSEMBLER" --manifest "$MANIFEST" --raw "$RAW_CSV" --out "$OUT_ROOT/summary"
else
  echo "smoke campaign complete; formal assembler intentionally not invoked"
fi

trap - EXIT INT TERM
cleanup_active
