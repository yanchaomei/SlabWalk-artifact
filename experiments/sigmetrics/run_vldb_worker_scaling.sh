#!/usr/bin/env bash
# Run the final fixed-pool, three-system DEEP1M worker-scaling campaign.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
DHNSW_SOURCE=${DHNSW_SOURCE:-/home/kvgroup/chaomei/d-HNSW}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/worker_scaling_$(date -u +%Y%m%dT%H%M%SZ)}
DROOT=${DROOT:-$OUT_ROOT/dhnsw-source}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-worker-scaling-$(date -u +%Y%m%dT%H%M%SZ)}
EXPECTED_SLABWALK_SHA=${EXPECTED_SLABWALK_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
WORKERS=${WORKERS:-"1 8 16 40"}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
SW_MN=${SW_MN:-skv-node6}
SW_PORT=${SW_PORT:-1290}
DHNSW_PORT=${DHNSW_PORT:-50210}
DHNSW_RDMA_PORT=${DHNSW_RDMA_PORT:-8950}
DHNSW_SERVER_IP=${DHNSW_SERVER_IP:-10.0.0.61}
DHNSW_RDMA_IP=${DHNSW_RDMA_IP:-$DHNSW_SERVER_IP}
BUILD_DHNSW=${BUILD_DHNSW:-1}
DHNSW_LD_LIBRARY_PATH=${DHNSW_LD_LIBRARY_PATH:-}
RESUME=${RESUME:-0}
DRY_RUN=${DRY_RUN:-0}

[[ "$REPEATS" == "5" ]] || { echo "Final worker scaling requires REPEATS=5" >&2; exit 2; }
[[ "$WARMUPS" =~ ^[1-9][0-9]*$ ]] || { echo "WARMUPS must be positive" >&2; exit 2; }
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || { echo "RESUME must be 0 or 1" >&2; exit 2; }
[[ "$BUILD_DHNSW" == "0" || "$BUILD_DHNSW" == "1" ]] || { echo "BUILD_DHNSW must be 0 or 1" >&2; exit 2; }
for workers in $WORKERS; do
  [[ "$workers" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid worker count: $workers" >&2; exit 2; }
done
[[ " $WORKERS " == " 1 8 16 40 " ]] || {
  echo "Final worker matrix must be exactly: 1 8 16 40" >&2; exit 2;
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s workers=%s repeats=%s warmups=%s sw_mn=%s\n' \
    "$CAMPAIGN_ID" "$WORKERS" "$REPEATS" "$WARMUPS" "$SW_MN"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing SlabWalk binary: $GB_BIN" >&2; exit 2; }
GB_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_SHA" == "$EXPECTED_SLABWALK_SHA" ]] || {
  echo "SlabWalk binary SHA mismatch: $GB_SHA" >&2; exit 2;
}
[[ -d "$DHNSW_SOURCE" ]] || { echo "Missing d-HNSW source: $DHNSW_SOURCE" >&2; exit 2; }
if [[ -d "$OUT_ROOT" && -n "$(find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$RESUME" != "1" ]]; then
  echo "Refusing non-empty OUT_ROOT without RESUME=1: $OUT_ROOT" >&2
  exit 2
fi
mkdir -p "$OUT_ROOT/raw" "$OUT_ROOT/query_pools"

prepare_dhnsw_tree() {
  if [[ ! -d "$DROOT/.worker-scaling-source" ]]; then
    [[ "$BUILD_DHNSW" == "1" ]] || {
      echo "BUILD_DHNSW=0 requires a preseeded, patched d-HNSW tree: $DROOT" >&2
      exit 2
    }
    [[ ! -e "$DROOT" ]] || { echo "Refusing pre-existing d-HNSW tree: $DROOT" >&2; exit 2; }
    mkdir -p "$DROOT"
    git -C "$DHNSW_SOURCE" archive HEAD | tar -x -C "$DROOT"
    mkdir -p "$DROOT/.worker-scaling-source" "$DROOT/datasets"
    ln -s "$(readlink -f "$DHNSW_SOURCE/datasets/deep1M")" "$DROOT/datasets/deep1M"
    cmake -S "$DROOT" -B "$DROOT/build" -DCMAKE_BUILD_TYPE=Release
  fi
  DATASETS=deep1M EF_LIST=200 DROOT="$DROOT" PATCH_ONLY=1 \
    bash "$SCRIPT_DIR/run_dhnsw_frontier.sh"
  if [[ "$BUILD_DHNSW" == "1" ]]; then
    cmake --build "$DROOT/build" -j8 --target run_client run_server
  else
    [[ -x "$DROOT/build/run_client" && -x "$DROOT/build/run_server" ]] || {
      echo "Missing prevalidated d-HNSW binaries under $DROOT/build" >&2
      exit 2
    }
  fi
}

fingerprint_query_pools() {
  local sw_query="$GB_DATA/deep1m/queries/query-uniform.fbin"
  local sw_gt="$GB_DATA/deep1m/queries/groundtruth-uniform.bin"
  local dh_query="$DROOT/datasets/deep1M/deep1M_query.fvecs"
  local dh_gt="$DROOT/datasets/deep1M/deep1M_groundtruth.ivecs"
  for method in SHINE SlabWalk; do
    local slug
    slug=$(printf '%s' "$method" | tr '[:upper:]' '[:lower:]')
    python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
      --query "$sw_query" --groundtruth "$sw_gt" --dataset DEEP1M \
      --method "$method" --metric l2 --limit 10000 \
      --out "$OUT_ROOT/query_pools/deep1m_${slug}.json" >/dev/null
  done
  python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
    --query "$dh_query" --groundtruth "$dh_gt" --dataset DEEP1M \
    --method d-HNSW --metric l2 --limit 10000 \
    --out "$OUT_ROOT/query_pools/deep1m_dhnsw.json" >/dev/null
  python3 - "$OUT_ROOT/query_pools" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
records = [json.loads(path.read_text()) for path in sorted(root.glob("deep1m_*.json"))]
queries = {record["query"]["canonical_sha256"] for record in records}
groundtruth = {record["groundtruth"]["canonical_ids_sha256"] for record in records}
if len(records) != 3 or len(queries) != 1 or len(groundtruth) != 1:
    raise SystemExit("cross-format DEEP1M query-pool mismatch")
PY
}

prepare_dhnsw_tree
fingerprint_query_pools
DHNSW_SHA=$(sha256sum "$DROOT/build/run_client" | awk '{print $1}')

MANIFEST="$OUT_ROOT/campaign.json"
python3 - "$MANIFEST" "$RESUME" "$CAMPAIGN_ID" "$GB_SHA" "$DHNSW_SHA" \
  "$WORKERS" "$REPEATS" "$WARMUPS" "$SW_MN" "$SW_PORT" \
  "$DHNSW_PORT" "$DHNSW_RDMA_PORT" "$DHNSW_SERVER_IP" "$DHNSW_RDMA_IP" \
  "$BUILD_DHNSW" "$DHNSW_LD_LIBRARY_PATH" \
  "$(sha256sum "$SCRIPT_DIR/run_frontier_sweeps.sh" | awk '{print $1}')" \
  "$(sha256sum "$SCRIPT_DIR/run_dhnsw_frontier.sh" | awk '{print $1}')" \
  "$(sha256sum "$SCRIPT_DIR/parse_dhnsw_frontier.py" | awk '{print $1}')" \
  "$(sha256sum "$SCRIPT_DIR/assemble_vldb_worker_scaling.py" | awk '{print $1}')" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(path_s, resume_s, campaign_id, gb_sha, dh_sha, workers_s, repeats_s,
 warmups_s, sw_mn, sw_port, dh_port, dh_rdma_port, dh_server_ip,
 dh_rdma_ip, build_dhnsw, dhnsw_ld_library_path, sw_runner_sha,
 dh_runner_sha, dh_parser_sha,
 assembler_sha) = sys.argv[1:]
path = Path(path_s)
protocol = {
    "slabwalk_binary_sha256": gb_sha,
    "dhnsw_client_binary_sha256": dh_sha,
    "dataset": "DEEP1M",
    "workers": [int(value) for value in workers_s.split()],
    "repeats": int(repeats_s),
    "warmups": int(warmups_s),
    "top_k": 10,
    "ef": 200,
    "measurement_mode": "fixed_query_pool",
    "queries_per_run": 10000,
    "graph_preserving_coroutines": 2,
    "graph_preserving_query_contexts": "one_per_worker",
    "slabwalk_memory_node": sw_mn,
    "slabwalk_tcp_port": int(sw_port),
    "dhnsw_tcp_port": int(dh_port),
    "dhnsw_rdma_port": int(dh_rdma_port),
    "dhnsw_server_ip": dh_server_ip,
    "dhnsw_rdma_ip": dh_rdma_ip,
    "dhnsw_topology": "released_harness_colocated_server_client",
    "dhnsw_build_mode": "source" if build_dhnsw == "1" else "prevalidated_binary",
    "dhnsw_runtime_library_path": dhnsw_ld_library_path,
    "sw_runner_sha256": sw_runner_sha,
    "dhnsw_runner_sha256": dh_runner_sha,
    "dhnsw_parser_sha256": dh_parser_sha,
    "assembler_sha256": assembler_sha,
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
if path.exists():
    if resume_s != "1":
        raise SystemExit(f"{path} exists; set RESUME=1 to continue")
    old = json.loads(path.read_text())
    if old.get("campaign_id") != campaign_id or old.get("protocol") != protocol:
        raise SystemExit("worker-scaling campaign drift")
else:
    path.write_text(json.dumps({
        "campaign_id": campaign_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, indent=2, sort_keys=True) + "\n")
PY

sw_complete() {
  local path=$1 workers=$2 run_id=$3 run_kind=$4
  [[ -s "$path" ]] || return 1
  python3 - "$path" "$workers" "$run_id" "$run_kind" "$GB_SHA" <<'PY'
import csv, sys
path, workers, run_id, run_kind, binary = sys.argv[1:]
rows = list(csv.DictReader(open(path)))
valid = [row for row in rows if (
    row["status"] == "ok" and row["dataset"] == "DEEP1M"
    and row["method"] in {"SHINE", "SlabWalk"}
    and row["run_id"] == run_id and row["run_kind"] == run_kind
    and row["binary_sha256"] == binary and int(row["threads"]) == int(workers)
    and int(row["query_contexts"]) == int(workers)
    and int(row["coroutines"]) == 2 and int(row["ef"]) == 200
    and int(row["processed"]) == int(row["expected_queries"]) == 10000
    and int(row["failed_queries"]) == 0
)]
raise SystemExit(0 if len(valid) == 2 and {row["method"] for row in valid} == {"SHINE", "SlabWalk"} else 1)
PY
}

dhnsw_complete() {
  local path=$1 workers=$2
  [[ -s "$path" ]] || return 1
  python3 - "$path" "$workers" "$DHNSW_SHA" <<'PY'
import csv, sys
path, workers, binary = sys.argv[1:]
rows = list(csv.DictReader(open(path)))
valid = [row for row in rows if (
    row["status"] == "ok" and row["dataset"].lower() == "deep1m"
    and row["binary_sha256"] == binary and int(row["threads"]) == int(workers)
    and int(row["ef"]) == 200 and int(row["top_k"]) == 10
    and row["measurement_mode"] == "fixed_query_pool"
    and int(row["processed_queries"]) == int(row["expected_queries"]) == 10000
    and int(row["failed_queries"]) == 0
)]
raise SystemExit(0 if len(valid) == 1 else 1)
PY
}

run_sw() {
  local workers=$1 run_id=$2 run_kind=$3
  local out="$OUT_ROOT/raw/sw/w${workers}/${run_id}"
  if [[ "$RESUME" == "1" ]] && sw_complete \
      "$out/slabwalk_shine_frontier_raw.csv" "$workers" "$run_id" "$run_kind"; then
    echo "SKIP complete SW workers=$workers $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || { echo "Refusing incomplete SW output: $out" >&2; exit 2; }
  OUT="$out" RUN_ID="$run_id" RUN_KIND="$run_kind" CAMPAIGN_ID="$CAMPAIGN_ID" \
    TRACE=0 DATASETS=DEEP1M THREADS="$workers" QUERY_CONTEXTS="$workers" COROS=2 \
    DEEP1_EFS=200 DEEP1_MN="$SW_MN" PORT="$SW_PORT" \
    GB_ROOT="$ROOT" GB_BIN="$GB_BIN" GB_BIN_R="$GB_BIN_R" \
    GB_LOCAL_LD_LIBRARY_PATH="$GB_LIB" GB_REMOTE_LD_LIBRARY_PATH="$GB_LIB" \
    TIMEOUT_S=1800 bash "$SCRIPT_DIR/run_frontier_sweeps.sh"
  sw_complete "$out/slabwalk_shine_frontier_raw.csv" "$workers" "$run_id" "$run_kind"
}

run_dhnsw() {
  local workers=$1 run_id=$2
  local out="$OUT_ROOT/raw/dhnsw/w${workers}/${run_id}"
  if [[ "$RESUME" == "1" ]] && dhnsw_complete "$out/frontier.csv" "$workers"; then
    echo "SKIP complete d-HNSW workers=$workers $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || { echo "Refusing incomplete d-HNSW output: $out" >&2; exit 2; }
  OUT="$out" DROOT="$DROOT" DATASETS=deep1M EF_LIST=200 THREADS="$workers" \
    BUILD_DHNSW=0 PREPARE_DATASETS=0 BENCHMARK_DURATION=20 \
    DHNSW_LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" \
    SERVER_IP="$DHNSW_SERVER_IP" RDMA_IP="$DHNSW_RDMA_IP" \
    PORT="$DHNSW_PORT" RDMA_PORT="$DHNSW_RDMA_PORT" \
    bash "$SCRIPT_DIR/run_dhnsw_frontier.sh"
  python3 "$SCRIPT_DIR/parse_dhnsw_frontier.py" \
    --result-dir "$out" --datasets deep1M --ef-list 200 --duration 20 \
    --threads "$workers" --campaign-id "$CAMPAIGN_ID" \
    --binary-sha256 "$DHNSW_SHA" --out "$out/frontier.csv"
  dhnsw_complete "$out/frontier.csv" "$workers"
}

for workers in $WORKERS; do
  for ((warmup = 0; warmup < WARMUPS; ++warmup)); do
    run_sw "$workers" "warmup${warmup}" warmup
    run_dhnsw "$workers" "warmup${warmup}"
  done
  for ((repeat = 0; repeat < REPEATS; ++repeat)); do
    run_sw "$workers" "r${repeat}" measure
    run_dhnsw "$workers" "r${repeat}"
  done
done

SUMMARY="$OUT_ROOT/summary"
if [[ -e "$SUMMARY" ]]; then
  if [[ "$RESUME" == "1" ]] && PYTHONPATH="$SCRIPT_DIR" python3 - \
      "$SUMMARY" "$EXPECTED_SLABWALK_SHA" <<'PY'
import sys
from pathlib import Path
import validate_vldb_final_evidence as evidence
evidence.validate_worker_scaling(Path(sys.argv[1]), sys.argv[2])
PY
  then
    echo "SKIP validated worker-scaling summary: $SUMMARY"
    exit 0
  fi
  echo "Refusing pre-existing or invalid summary: $SUMMARY" >&2
  exit 2
fi
python3 "$SCRIPT_DIR/assemble_vldb_worker_scaling.py" \
  --raw-root "$OUT_ROOT/raw" --query-pools "$OUT_ROOT/query_pools" \
  --campaign-root "$OUT_ROOT" \
  --out-dir "$SUMMARY" --campaign-id "$CAMPAIGN_ID" \
  --expected-slabwalk-sha "$EXPECTED_SLABWALK_SHA" --repeats "$REPEATS"
echo "Final worker-scaling campaign written to $OUT_ROOT"
