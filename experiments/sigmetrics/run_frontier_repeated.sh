#!/usr/bin/env bash
# Run warmup plus repeated 10M three-system frontier campaigns.
# Execute on skv-node1 after GraphBeyond index dumps are present.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$ROOT/build/shine}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA to the frozen candidate SHA-256}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$SCRIPT_DIR/vldb_evidence_bundle.py}
FRONTIER_VERIFIER=${FRONTIER_VERIFIER:-$SCRIPT_DIR/verify_vldb_frontier_sweep.py}
OUT_ROOT=${OUT_ROOT:-$ROOT/evidence/frontier_10m_repeated}
DATASETS_SW=${DATASETS_SW:-"DEEP10M TEXT10M SIFT10M"}
DATASETS_DH=${DATASETS_DH:-"deep10M text10M sift10M"}
EXPECTED_DATASETS=${EXPECTED_DATASETS:-${DATASETS_SW// /,}}
REPEATS=${REPEATS:-5}
PHASES=${PHASES:-"sw dhnsw"}
THREADS=${THREADS:-10}
QUERY_CONTEXTS=${QUERY_CONTEXTS:-$THREADS}
COROS=${COROS:-2}
EF_LIST=${EF_LIST:-"48 64 96 128 200"}
BENCHMARK_DURATION=${BENCHMARK_DURATION:-20}
SW_PORT=${SW_PORT:-1260}
DHNSW_PORT=${DHNSW_PORT:-50165}
DHNSW_RDMA_PORT=${DHNSW_RDMA_PORT:-8915}
RESUME=${RESUME:-0}
CAMPAIGN_ID=${CAMPAIGN_ID:-}
DHNSW_SOURCE=${DHNSW_SOURCE:-/home/kvgroup/chaomei/d-HNSW}
DHNSW_ROOT=${DHNSW_ROOT:-$OUT_ROOT/dhnsw-source}
FRONTIER_LIFECYCLE_ROOT=${FRONTIER_LIFECYCLE_ROOT:-}

if [[ -n "$FRONTIER_LIFECYCLE_ROOT" ]]; then
  mkdir -p "$FRONTIER_LIFECYCLE_ROOT"
fi

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "REPEATS must be positive" >&2; exit 2; }
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }
[[ "$QUERY_CONTEXTS" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_CONTEXTS must be positive" >&2; exit 2; }
(( QUERY_CONTEXTS <= THREADS )) || { echo "QUERY_CONTEXTS cannot exceed THREADS" >&2; exit 2; }
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || { echo "RESUME must be 0 or 1" >&2; exit 2; }
[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid EXPECTED_BINARY_SHA" >&2; exit 2; }
[[ -f "$EVIDENCE_TOOL" && -f "$FRONTIER_VERIFIER" ]] || { echo "missing evidence verifier" >&2; exit 2; }
GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$GB_BIN_SHA256" == "$EXPECTED_BINARY_SHA" ]] || { echo "candidate binary SHA mismatch" >&2; exit 2; }
mkdir -p "$OUT_ROOT"
CAMPAIGN_MANIFEST="$OUT_ROOT/campaign.json"
manifest_result=$(python3 - "$CAMPAIGN_MANIFEST" "$RESUME" "$CAMPAIGN_ID" \
  "$GB_BIN_SHA256" "$DATASETS_SW" "$DATASETS_DH" "$REPEATS" "$THREADS" \
  "$COROS" "$QUERY_CONTEXTS" "$EF_LIST" "$BENCHMARK_DURATION" "$SW_PORT" "$DHNSW_PORT" \
  "$DHNSW_RDMA_PORT" <<'PY'
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
resume = sys.argv[2] == "1"
requested_id = sys.argv[3]
protocol = {
    "gb_binary_sha256": sys.argv[4],
    "datasets_sw": sys.argv[5].split(),
    "datasets_dh": sys.argv[6].split(),
    "repeats": int(sys.argv[7]),
    "threads": int(sys.argv[8]),
    "coroutines": int(sys.argv[9]),
    "query_contexts": int(sys.argv[10]),
    "dhnsw_ef_list": [int(value) for value in sys.argv[11].replace(",", " ").split()],
    "measurement_mode": "fixed_query_pool",
    "configured_timeout_s": float(sys.argv[12]),
    "sw_tcp_port": int(sys.argv[13]),
    "dhnsw_tcp_port": int(sys.argv[14]),
    "dhnsw_rdma_port": int(sys.argv[15]),
    "top_k": 10,
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
if path.exists():
    if not resume:
        raise SystemExit(f"{path} already exists; set RESUME=1 only to continue this campaign")
    existing = json.loads(path.read_text())
    if existing.get("protocol_fingerprint") != fingerprint or existing.get("protocol") != protocol:
        raise SystemExit("campaign protocol differs from the existing manifest")
    campaign_id = existing["campaign_id"]
    if requested_id and requested_id != campaign_id:
        raise SystemExit("requested CAMPAIGN_ID differs from the existing manifest")
else:
    if resume:
        raise SystemExit(f"RESUME=1 requested but {path} does not exist")
    campaign_id = requested_id or f"vldb-frontier-{uuid.uuid4()}"
    path.write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "campaign_uuid": str(uuid.uuid4()),
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "protocol_fingerprint": fingerprint,
                "protocol": protocol,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
print(campaign_id)
PY
)
CAMPAIGN_ID=$manifest_result
if [[ -e "$OUT_ROOT/SEALED.json" || -e "$OUT_ROOT/SHA256SUMS" ]]; then
  [[ "$RESUME" == "1" ]] || { echo "refusing an already sealed campaign" >&2; exit 2; }
  python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT" >/dev/null
  echo "Repeated frontier already sealed at $OUT_ROOT"
  exit 0
fi

contains_phase() {
  [[ " $PHASES " == *" $1 "* ]]
}

sw_run_complete() {
  local root=$1 expected_kind=$2 expected_run_id=$3
  [[ -s "$root/SEALED.json" && -s "$root/SHA256SUMS" ]] || return 1
  if ! python3 "$EVIDENCE_TOOL" verify --root "$root" >/dev/null; then
    echo "child frontier seal verification failed: $root" >&2
    return 1
  fi
  python3 "$FRONTIER_VERIFIER" \
    --root "$root" \
    --expected-binary-sha "$EXPECTED_BINARY_SHA" \
    --expected-campaign-id "$CAMPAIGN_ID" \
    --expected-run-id "$expected_run_id" \
    --expected-run-kind "$expected_kind" \
    --expected-datasets "${DATASETS_SW// /,}" \
    --expected-threads "$THREADS" \
    --expected-query-contexts "$QUERY_CONTEXTS" \
    --expected-coroutines "$COROS" \
    --expected-trace 0 \
    --min-points 5 >/dev/null
}

write_sw_completion_marker() {
  local marker="$OUT_ROOT/SW_FRONTIER_COMPLETE.json"
  python3 - "$marker" "$OUT_ROOT" "$CAMPAIGN_ID" "$EXPECTED_BINARY_SHA" \
    "$DATASETS_SW" "$REPEATS" "$THREADS" "$QUERY_CONTEXTS" "$COROS" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

(marker_s, root_s, campaign_id, binary_sha, datasets_s, repeats_s, threads_s,
 query_contexts_s, coroutines_s) = sys.argv[1:]
marker = Path(marker_s)
root = Path(root_s)
repeats = int(repeats_s)
children = []
for repeat in range(1, repeats + 1):
    child = root / f"sw_r{repeat}"
    manifest = child / "SHA256SUMS"
    seal = child / "SEALED.json"
    semantic = child / "semantic_verification.json"
    if not all(path.is_file() for path in (manifest, seal, semantic)):
        raise SystemExit(f"incomplete measured child: {child}")
    children.append(
        {
            "run_id": f"r{repeat}",
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "seal_sha256": hashlib.sha256(seal.read_bytes()).hexdigest(),
            "semantic_verification_sha256": hashlib.sha256(
                semantic.read_bytes()
            ).hexdigest(),
        }
    )
payload = {
    "schema_version": 1,
    "kind": "vldb_sw_frontier_complete_v1",
    "campaign_id": campaign_id,
    "binary_sha256": binary_sha,
    "protocol": {
        "datasets": datasets_s.split(),
        "repeats": repeats,
        "workers": int(threads_s),
        "query_contexts": int(query_contexts_s),
        "coroutines": int(coroutines_s),
        "top_k": 10,
    },
    "children": children,
}
marker.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=marker.parent, delete=False
) as handle:
    temporary = Path(handle.name)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, marker)
PY
}

dhnsw_run_complete() {
  local root=$1 expected_kind=$2 expected_run_id=$3
  local csv="$root/frontier.csv" meta="$root/run.json"
  [[ -s "$csv" && -s "$meta" && -s "$root/SEALED.json" && \
      -s "$root/SHA256SUMS" ]] || return 1
  if ! python3 "$EVIDENCE_TOOL" verify --root "$root" >/dev/null; then
    echo "child frontier seal verification failed: $root" >&2
    return 1
  fi
  python3 - "$csv" "$meta" "$expected_kind" "$expected_run_id" \
    "$DATASETS_DH" "$EF_LIST" "$THREADS" "$CAMPAIGN_ID" <<'PY'
import csv
import json
import sys

(csv_path, meta_path, expected_kind, expected_run_id, datasets_s, ef_s,
 threads_s, campaign_id) = sys.argv[1:]
rows = list(csv.DictReader(open(csv_path)))
meta = json.load(open(meta_path))
datasets = set(datasets_s.split())
efs = {int(value) for value in ef_s.replace(",", " ").split()}
if meta != {"run_id": expected_run_id, "run_kind": expected_kind, "rows": len(rows)}:
    raise SystemExit(1)
cells = set()
for row in rows:
    try:
        valid = (
            row["status"] == "ok"
            and row["campaign_id"] == campaign_id
            and row["measurement_mode"] == "fixed_query_pool"
            and int(row["threads"]) == int(threads_s)
            and int(row["top_k"]) == 10
            and int(row["processed_queries"]) == int(row["expected_queries"])
            and int(row["failed_queries"]) == 0
            and bool(row["binary_sha256"])
            and bool(row["protocol_fingerprint"])
            and row["dataset"] in datasets
            and int(row["ef"]) in efs
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise SystemExit(1)
    cells.add((row["dataset"], int(row["ef"])))
expected = {(dataset, ef) for dataset in datasets for ef in efs}
if cells != expected or len(rows) != len(expected):
    raise SystemExit(1)
PY
}

run_sw() {
  local run_id=$1 run_kind=$2 method_order_offset=$3
  local out="$OUT_ROOT/sw_$run_id"
  local lifecycle_log=""
  if [[ -n "$FRONTIER_LIFECYCLE_ROOT" ]]; then
    lifecycle_log="$FRONTIER_LIFECYCLE_ROOT/sw_${run_id}.jsonl"
  fi
  if [[ "$RESUME" == "1" ]] && sw_run_complete \
      "$out" "$run_kind" "$run_id"; then
    echo "SKIP complete SW $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || {
    echo "Refusing incomplete SW run directory: $out" >&2
    exit 2
  }
  OUT="$out" RUN_ID="$run_id" RUN_KIND="$run_kind" CAMPAIGN_ID="$CAMPAIGN_ID" TRACE=0 \
    FRONTIER_LIFECYCLE_LOG="$lifecycle_log" \
    DATASETS="$DATASETS_SW" THREADS="$THREADS" QUERY_CONTEXTS="$QUERY_CONTEXTS" COROS="$COROS" \
    GB_ROOT="$ROOT" GB_BIN="$GB_BIN" GB_BIN_R="$GB_BIN" \
    GB_LOCAL_LD_LIBRARY_PATH=/home/kvgroup/chaomei/lib \
    GB_REMOTE_LD_LIBRARY_PATH=/home/kvgroup/chaomei/lib \
    TIMEOUT_S=7200 PORT="$SW_PORT" \
    EXPECTED_BINARY_SHA="$EXPECTED_BINARY_SHA" \
    METHOD_ORDER_OFFSET="$method_order_offset" \
    bash "$SCRIPT_DIR/run_frontier_sweeps.sh"
  sw_run_complete "$out" "$run_kind" "$run_id" || {
    echo "child frontier seal verification failed: $out" >&2
    exit 2
  }
}

prepare_dhnsw_tree() {
  if [[ ! -d "$DHNSW_ROOT/.git-archive-source" ]]; then
    [[ ! -e "$DHNSW_ROOT" ]] || { echo "Refusing non-campaign d-HNSW tree: $DHNSW_ROOT" >&2; exit 2; }
    mkdir -p "$DHNSW_ROOT"
    git -C "$DHNSW_SOURCE" archive HEAD | tar -x -C "$DHNSW_ROOT"
    mkdir -p "$DHNSW_ROOT/.git-archive-source" "$DHNSW_ROOT/datasets"
    if [[ -d "$DHNSW_SOURCE/datasets/sift10M" ]]; then
      ln -s "$DHNSW_SOURCE/datasets/sift10M" "$DHNSW_ROOT/datasets/sift10M"
    fi
    cmake -S "$DHNSW_ROOT" -B "$DHNSW_ROOT/build" -DCMAKE_BUILD_TYPE=Release
  fi
}

run_dhnsw() {
  local run_id=$1 run_kind=$2
  local out="$OUT_ROOT/dhnsw_$run_id"
  local build=0 prepare=0
  if [[ "$run_kind" == "warmup" ]]; then
    build=1
    prepare=1
  fi
  if [[ "$RESUME" == "1" ]] && dhnsw_run_complete \
      "$out" "$run_kind" "$run_id"; then
    echo "SKIP complete d-HNSW $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || {
    echo "Refusing incomplete d-HNSW run directory: $out" >&2
    exit 2
  }
  OUT="$out" WORK="$OUT_ROOT/dhnsw-work" DROOT="$DHNSW_ROOT" \
    DATASETS="$DATASETS_DH" FETCH_SIFT10M=0 THREADS="$THREADS" \
    EF_LIST="$EF_LIST" BENCHMARK_DURATION="$BENCHMARK_DURATION" \
    BUILD_DHNSW="$build" PREPARE_DATASETS="$prepare" \
    PORT="$DHNSW_PORT" RDMA_PORT="$DHNSW_RDMA_PORT" \
    bash "$SCRIPT_DIR/run_dhnsw_frontier.sh"
  local dhnsw_sha
  dhnsw_sha=$(sha256sum "$DHNSW_ROOT/build/run_client" | awk '{print $1}')
  python3 "$SCRIPT_DIR/parse_dhnsw_frontier.py" \
    --result-dir "$out" --datasets $DATASETS_DH --ef-list "$EF_LIST" \
    --duration "$BENCHMARK_DURATION" --threads "$THREADS" \
    --campaign-id "$CAMPAIGN_ID" --binary-sha256 "$dhnsw_sha" \
    --out "$out/frontier.csv"
  python3 - "$out/frontier.csv" "$out/run.json" "$run_id" "$run_kind" <<'PY'
import csv
import json
import sys

csv_path, meta_path, run_id, run_kind = sys.argv[1:]
rows = list(csv.DictReader(open(csv_path)))
if not rows or any(row["status"] != "ok" for row in rows):
    raise SystemExit(f"{run_id}: incomplete parsed frontier")
json.dump(
    {"run_id": run_id, "run_kind": run_kind, "rows": len(rows)},
    open(meta_path, "w"),
    indent=2,
    sort_keys=True,
)
PY
  local dhnsw_server_sha
  dhnsw_server_sha=$(sha256sum "$DHNSW_ROOT/build/run_server" | awk '{print $1}')
  python3 - "$out/campaign.json" "$CAMPAIGN_ID" "$run_id" "$run_kind" \
    "$DATASETS_DH" "$EF_LIST" "$THREADS" "$BENCHMARK_DURATION" \
    "$dhnsw_sha" "$dhnsw_server_sha" <<'PY'
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone

(path, parent_campaign_id, run_id, run_kind, datasets, ef_list, threads,
 duration, client_sha, server_sha) = sys.argv[1:]
protocol = {
    "parent_campaign_id": parent_campaign_id,
    "run_id": run_id,
    "run_kind": run_kind,
    "datasets": datasets.split(),
    "ef_list": [int(value) for value in ef_list.replace(",", " ").split()],
    "threads": int(threads),
    "top_k": 10,
    "measurement_mode": "fixed_query_pool",
    "configured_timeout_s": float(duration),
    "client_binary_sha256": client_sha,
    "server_binary_sha256": server_sha,
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
payload = {
    "schema_version": 1,
    "campaign_id": f"{parent_campaign_id}:dhnsw:{run_id}",
    "campaign_uuid": str(uuid.uuid4()),
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "protocol_fingerprint": fingerprint,
    "protocol": protocol,
}
with open(path, "w") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  python3 "$EVIDENCE_TOOL" seal --root "$out" --campaign "$out/campaign.json" >/dev/null
  python3 "$EVIDENCE_TOOL" verify --root "$out" >/dev/null
  dhnsw_run_complete "$out" "$run_kind" "$run_id" || {
    echo "child frontier seal verification failed: $out" >&2
    exit 2
  }
}

if contains_phase sw; then
  run_sw warmup warmup 0
  for ((rep = 1; rep <= REPEATS; ++rep)); do
    method_order_offset=$((rep % 2))
    run_sw "r$rep" measure "$method_order_offset"
  done
  for ((rep = 1; rep <= REPEATS; ++rep)); do
    sw_run_complete "$OUT_ROOT/sw_r$rep" measure "r$rep" || {
      echo "refusing SW completion marker after failed child r$rep" >&2
      exit 2
    }
  done
  write_sw_completion_marker
fi

if contains_phase dhnsw; then
  prepare_dhnsw_tree
  run_dhnsw warmup warmup
  for ((rep = 1; rep <= REPEATS; ++rep)); do
    run_dhnsw "r$rep" measure
  done
fi

args=()
ready=1
for ((rep = 1; rep <= REPEATS; ++rep)); do
  sw_csv="$OUT_ROOT/sw_r$rep/slabwalk_shine_frontier_raw.csv"
  dh_csv="$OUT_ROOT/dhnsw_r$rep/frontier.csv"
  if ! sw_run_complete "$OUT_ROOT/sw_r$rep" measure "r$rep" || \
      ! dhnsw_run_complete "$OUT_ROOT/dhnsw_r$rep" measure "r$rep"; then
    ready=0
  fi
  args+=(--sw "$sw_csv")
  args+=(--dhnsw "$dh_csv")
done
if [[ "$ready" == "1" ]]; then
  python3 "$SCRIPT_DIR/aggregate_frontier_repeats.py" \
    "${args[@]}" --expected-repeats "$REPEATS" --min-points 5 \
    --expected-datasets "$EXPECTED_DATASETS" \
    --expected-query-contexts "$QUERY_CONTEXTS" \
    --out-dir "$OUT_ROOT/summary"
  python3 "$EVIDENCE_TOOL" seal --root "$OUT_ROOT" \
    --campaign "$CAMPAIGN_MANIFEST" >/dev/null
  python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT" >/dev/null
else
  echo "Partial phase complete; summary waits for both systems' five repeats."
fi

echo "Repeated frontier written to $OUT_ROOT"
