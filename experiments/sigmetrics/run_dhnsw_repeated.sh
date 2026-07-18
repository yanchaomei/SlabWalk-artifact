#!/usr/bin/env bash
# Run a warmup plus repeated fixed-query-pool d-HNSW frontier campaign.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DROOT=${DROOT:-/home/kvgroup/chaomei/d-HNSW}
OUT_ROOT=${OUT_ROOT:-/home/kvgroup/chaomei/dhnsw_repeated_$(date -u +%Y%m%dT%H%M%SZ)}
DATASETS=${DATASETS:-"sift1M gist1M deep1M text1M"}
EF_LIST=${EF_LIST:-"48 64 96 128 200"}
THREADS=${THREADS:-10}
REPEATS=${REPEATS:-5}
BENCHMARK_DURATION=${BENCHMARK_DURATION:-20}
BUILD_DHNSW=${BUILD_DHNSW:-1}
PREPARE_DATASETS=${PREPARE_DATASETS:-1}
DHNSW_LD_LIBRARY_PATH=${DHNSW_LD_LIBRARY_PATH:-}
SERVER_IP=${SERVER_IP:-10.0.0.61}
RDMA_IP=${RDMA_IP:-10.0.0.61}
PORT=${PORT:-50051}
RDMA_PORT=${RDMA_PORT:-8888}
NIC_IDX=${NIC_IDX:-1}
TIMEOUT_SERVER_S=${TIMEOUT_SERVER_S:-7200}
TIMEOUT_CLIENT_S=${TIMEOUT_CLIENT_S:-1200}
SERVER_READY_WAIT_S=${SERVER_READY_WAIT_S:-2400}
WORK=${WORK:-$OUT_ROOT/work}
RESUME=${RESUME:-0}
CAMPAIGN_ID=${CAMPAIGN_ID:-}
RUNNER=${RUNNER:-$SCRIPT_DIR/run_dhnsw_frontier.sh}
PARSER=${PARSER:-$SCRIPT_DIR/parse_dhnsw_frontier.py}
VALIDATOR=${VALIDATOR:-$SCRIPT_DIR/validate_dhnsw_dataset.py}

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "REPEATS must be positive" >&2; exit 2; }
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || { echo "RESUME must be 0 or 1" >&2; exit 2; }
if [[ "$BUILD_DHNSW" == "1" ]]; then
  [[ -s "$DROOT/CMakeLists.txt" ||
     ( -x "$DROOT/build/run_client" && -x "$DROOT/build/run_server" ) ]] || {
    echo "Missing d-HNSW source or binaries under $DROOT" >&2; exit 2;
  }
else
  [[ -x "$DROOT/build/run_client" && -x "$DROOT/build/run_server" ]] || {
    echo "Missing d-HNSW binaries under $DROOT/build" >&2; exit 2;
  }
fi
[[ -s "$RUNNER" && -s "$PARSER" ]] || { echo "Missing runner or parser" >&2; exit 2; }

CLIENT_SHA=$(sha256sum "$DROOT/build/run_client" | awk '{print $1}')
SERVER_SHA=$(sha256sum "$DROOT/build/run_server" | awk '{print $1}')
RUNNER_SHA=$(sha256sum "$RUNNER" | awk '{print $1}')
PARSER_SHA=$(sha256sum "$PARSER" | awk '{print $1}')
VALIDATOR_SHA=missing
if [[ -s "$VALIDATOR" ]]; then
  VALIDATOR_SHA=$(sha256sum "$VALIDATOR" | awk '{print $1}')
fi
RUNTIME_MANIFEST_SHA=system-default
if [[ -n "$DHNSW_LD_LIBRARY_PATH" ]]; then
  [[ -d "$DHNSW_LD_LIBRARY_PATH" ]] || {
    echo "Missing d-HNSW runtime library directory: $DHNSW_LD_LIBRARY_PATH" >&2
    exit 2
  }
  RUNTIME_MANIFEST_SHA=$(
    cd "$DHNSW_LD_LIBRARY_PATH"
    find . -maxdepth 1 -type f -print0 | sort -z |
      xargs -0 sha256sum | sha256sum | awk '{print $1}'
  )
fi
mkdir -p "$OUT_ROOT"
MANIFEST=$OUT_ROOT/campaign.json
CAMPAIGN_ID=$(python3 - "$MANIFEST" "$RESUME" "$CAMPAIGN_ID" \
  "$CLIENT_SHA" "$SERVER_SHA" "$DATASETS" "$EF_LIST" "$THREADS" \
  "$REPEATS" "$SERVER_IP" "$RDMA_IP" "$PORT" "$RDMA_PORT" \
  "$BUILD_DHNSW" "$PREPARE_DATASETS" "$RUNNER" "$RUNNER_SHA" \
  "$PARSER" "$PARSER_SHA" "$VALIDATOR" "$VALIDATOR_SHA" \
  "$DHNSW_LD_LIBRARY_PATH" "$RUNTIME_MANIFEST_SHA" <<'PY'
import hashlib, json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

(path_s, resume_s, requested_id, client_sha, server_sha, datasets, ef_list,
 threads, repeats, server_ip, rdma_ip, port, rdma_port, build, prepare,
 runner_path, runner_sha, parser_path, parser_sha, validator_path,
 validator_sha, runtime_path, runtime_sha) = sys.argv[1:]
path = Path(path_s)
protocol = {
    "client_binary_sha256": client_sha,
    "server_binary_sha256": server_sha,
    "datasets": datasets.split(),
    "ef_values": [int(x) for x in ef_list.replace(",", " ").split()],
    "threads": int(threads),
    "repeats": int(repeats),
    "top_k": 10,
    "measurement_mode": "fixed_query_pool",
    "server_ip": server_ip,
    "rdma_ip": rdma_ip,
    "tcp_port": int(port),
    "rdma_port": int(rdma_port),
    "build_dhnsw": int(build),
    "prepare_datasets": int(prepare),
    "warmup_build_dhnsw": int(build),
    "warmup_prepare_datasets": int(prepare),
    "measured_build_dhnsw": 0,
    "measured_prepare_datasets": 0,
    "runner_path": runner_path,
    "runner_sha256": runner_sha,
    "parser_path": parser_path,
    "parser_sha256": parser_sha,
    "validator_path": validator_path,
    "validator_sha256": validator_sha,
    "runtime_library_path": runtime_path,
    "runtime_manifest_sha256": runtime_sha,
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
        raise SystemExit("campaign protocol drift")
    if requested_id and requested_id != old["campaign_id"]:
        raise SystemExit("CAMPAIGN_ID mismatch")
    campaign_id = old["campaign_id"]
else:
    if resume:
        raise SystemExit("RESUME=1 but campaign manifest is missing")
    campaign_id = requested_id or f"dhnsw-{uuid.uuid4()}"
    path.write_text(json.dumps({
        "campaign_id": campaign_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, indent=2, sort_keys=True) + "\n")
print(campaign_id)
PY
)

run_complete() {
  local csv_path=$1 meta_path=$2 expected_run_id=$3 expected_run_kind=$4
  [[ -s "$csv_path" && -s "$meta_path" ]] || return 1
  python3 - "$csv_path" "$meta_path" "$expected_run_id" "$expected_run_kind" \
    "$DATASETS" "$EF_LIST" "$THREADS" "$CAMPAIGN_ID" "$CLIENT_SHA" <<'PY'
import csv
import json
import sys

(csv_path, meta_path, expected_run_id, expected_run_kind, datasets_s, ef_s,
 threads_s, campaign_id, binary_sha256) = sys.argv[1:]
rows = list(csv.DictReader(open(csv_path)))
meta = json.load(open(meta_path))
datasets = set(datasets_s.split())
efs = {int(value) for value in ef_s.replace(",", " ").split()}
if meta != {
    "run_id": expected_run_id,
    "run_kind": expected_run_kind,
    "rows": len(rows),
}:
    raise SystemExit(1)
cells = set()
for row in rows:
    try:
        valid = (
            row["status"] == "ok"
            and row["campaign_id"] == campaign_id
            and row["binary_sha256"] == binary_sha256
            and row["measurement_mode"] == "fixed_query_pool"
            and int(row["threads"]) == int(threads_s)
            and int(row["top_k"]) == 10
            and int(row["processed_queries"]) == int(row["expected_queries"])
            and int(row["failed_queries"]) == 0
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

run_one() {
  local run_id=$1 run_kind=$2
  local out="$OUT_ROOT/$run_id"
  local build=$BUILD_DHNSW prepare=$PREPARE_DATASETS
  if [[ "$run_kind" == "measure" ]]; then
    build=0
    prepare=0
  fi
  if [[ "$RESUME" == "1" ]] && run_complete \
      "$out/frontier.csv" "$out/run.json" "$run_id" "$run_kind"; then
    echo "SKIP complete $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || {
    echo "Refusing incomplete run directory: $out" >&2
    exit 2
  }
  mkdir -p "$out"
  OUT="$out" WORK="$WORK" DROOT="$DROOT" DATASETS="$DATASETS" \
    EF_LIST="$EF_LIST" THREADS="$THREADS" \
    BENCHMARK_DURATION="$BENCHMARK_DURATION" BUILD_DHNSW="$build" \
    PREPARE_DATASETS="$prepare" \
    VALIDATOR="$VALIDATOR" \
    DHNSW_LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" \
    SERVER_IP="$SERVER_IP" RDMA_IP="$RDMA_IP" PORT="$PORT" \
    RDMA_PORT="$RDMA_PORT" NIC_IDX="$NIC_IDX" \
    TIMEOUT_SERVER_S="$TIMEOUT_SERVER_S" TIMEOUT_CLIENT_S="$TIMEOUT_CLIENT_S" \
    SERVER_READY_WAIT_S="$SERVER_READY_WAIT_S" \
    bash "$RUNNER" > "$out/driver.log" 2>&1
  python3 "$PARSER" --result-dir "$out" --datasets $DATASETS \
    --ef-list "$EF_LIST" --duration "$BENCHMARK_DURATION" --threads "$THREADS" \
    --campaign-id "$CAMPAIGN_ID" --binary-sha256 "$CLIENT_SHA" \
    --out "$out/frontier.csv"
  python3 - "$out/frontier.csv" "$out/run.json" "$run_id" "$run_kind" <<'PY'
import csv, json, sys
csv_path, meta_path, run_id, run_kind = sys.argv[1:]
rows = list(csv.DictReader(open(csv_path)))
if not rows or any(row["status"] != "ok" for row in rows):
    raise SystemExit(f"{run_id}: incomplete parsed frontier")
json.dump({"run_id": run_id, "run_kind": run_kind, "rows": len(rows)},
          open(meta_path, "w"), indent=2, sort_keys=True)
PY
  run_complete "$out/frontier.csv" "$out/run.json" "$run_id" "$run_kind" || {
    echo "$run_id: parsed frontier does not cover the requested dataset-by-ef matrix" >&2
    exit 2
  }
}

run_one warmup warmup
for ((rep = 0; rep < REPEATS; ++rep)); do
  run_one "r$rep" measure
done

python3 - "$OUT_ROOT" "$REPEATS" "$DATASETS" "$EF_LIST" <<'PY'
import csv, math, statistics, sys
from pathlib import Path

root, repeats = Path(sys.argv[1]), int(sys.argv[2])
datasets = sys.argv[3].split()
ef_values = [value for value in sys.argv[4].replace(",", " ").split()]
tcrit = {1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306,9:2.262,10:2.228}
rows = []
for rep in range(repeats):
    path = root / f"r{rep}" / "frontier.csv"
    for row in csv.DictReader(path.open()):
        row["run_id"] = f"r{rep}"
        row["run_kind"] = "measure"
        rows.append(row)
groups = {}
for row in rows:
    groups.setdefault((row["dataset"], row["ef"]), []).append(row)
expected_cells = {(dataset, ef) for dataset in datasets for ef in ef_values}
if set(groups) != expected_cells:
    raise SystemExit(
        f"frontier cell mismatch: expected={sorted(expected_cells)} actual={sorted(groups)}"
    )
expected = repeats
metrics = ("qps_recomputed", "recall", "latency_us", "network_us", "compute_us",
           "meta_us", "deserialize_us", "server_rss_after_gb")
summary = []
for key, cell in sorted(groups.items()):
    if len(cell) != expected or len({r["protocol_fingerprint"] for r in cell}) != 1:
        raise SystemExit(f"repeat/protocol mismatch for {key}")
    record = {"dataset": key[0], "ef": key[1], "n": len(cell)}
    for metric in metrics:
        values = [float(row[metric]) for row in cell]
        mean = statistics.mean(values)
        ci = 0.0 if len(values) < 2 else tcrit.get(len(values)-1, 1.96) * statistics.stdev(values) / math.sqrt(len(values))
        record[f"{metric}_mean"] = mean
        record[f"{metric}_ci95"] = ci
    summary.append(record)
out = root / "summary"
out.mkdir(exist_ok=True)
for path, data in ((out / "runs.csv", rows), (out / "summary.csv", summary)):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0]))
        writer.writeheader(); writer.writerows(data)
PY

echo "Repeated d-HNSW campaign written to $OUT_ROOT"
