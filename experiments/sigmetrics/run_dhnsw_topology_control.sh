#!/usr/bin/env bash
# Compare the released d-HNSW loopback harness with a separated CN/MN topology.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DROOT=${DROOT:-/home/kvgroup/chaomei/d-HNSW}
OUT=${OUT:-/home/kvgroup/chaomei/dhnsw_topology_control_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-dhnsw-topology-$(date -u +%Y%m%dT%H%M%SZ)}
TOPOLOGIES=${TOPOLOGIES:-"loopback remote"}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
THREADS=${THREADS:-10}
EF=${EF:-200}
CLIENT_IP=${CLIENT_IP:-10.0.0.61}
REMOTE_SERVER_HOST=${REMOTE_SERVER_HOST:-skv-node6}
REMOTE_SERVER_IP=${REMOTE_SERVER_IP:-10.0.0.66}
NIC_IDX=${NIC_IDX:-1}
PORT=${PORT:-50220}
RDMA_PORT=${RDMA_PORT:-8960}
TIMEOUT_CLIENT_S=${TIMEOUT_CLIENT_S:-1200}
SERVER_READY_WAIT_S=${SERVER_READY_WAIT_S:-2400}
DHNSW_LD_LIBRARY_PATH=${DHNSW_LD_LIBRARY_PATH:-}
RESUME=${RESUME:-0}
DRY_RUN=${DRY_RUN:-0}

[[ "$TOPOLOGIES" == "loopback remote" ]] || {
  echo "Final topology control requires: loopback remote" >&2; exit 2;
}
[[ "$REPEATS" == "5" ]] || { echo "Final topology control requires five repeats" >&2; exit 2; }
[[ "$WARMUPS" =~ ^[1-9][0-9]*$ ]] || { echo "WARMUPS must be positive" >&2; exit 2; }
[[ "$THREADS" == "10" && "$EF" == "200" ]] || {
  echo "Final topology control requires THREADS=10 and EF=200" >&2; exit 2;
}
[[ "$RESUME" == "0" || "$RESUME" == "1" ]] || { echo "RESUME must be 0 or 1" >&2; exit 2; }

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'campaign=%s topologies=%s repeats=%s threads=%s ef=%s remote=%s/%s\n' \
    "$CAMPAIGN_ID" "$TOPOLOGIES" "$REPEATS" "$THREADS" "$EF" \
    "$REMOTE_SERVER_HOST" "$REMOTE_SERVER_IP"
  exit 0
fi

CLIENT_BIN=$DROOT/build/run_client
SERVER_BIN=$DROOT/build/run_server
BASE=$DROOT/datasets/deep1M/deep1M_base.fvecs
QUERY=$DROOT/datasets/deep1M/deep1M_query.fvecs
GROUNDTRUTH=$DROOT/datasets/deep1M/deep1M_groundtruth.ivecs
REMOTE_SERVER_BIN=${REMOTE_SERVER_BIN:-$SERVER_BIN}
REMOTE_BASE=${REMOTE_BASE:-$BASE}
REMOTE_DHNSW_LD_LIBRARY_PATH=${REMOTE_DHNSW_LD_LIBRARY_PATH:-$DHNSW_LD_LIBRARY_PATH}
for path in "$CLIENT_BIN" "$SERVER_BIN" "$BASE" "$QUERY" "$GROUNDTRUTH"; do
  [[ -s "$path" ]] || { echo "Missing d-HNSW topology input: $path" >&2; exit 2; }
done
CLIENT_SHA=$(sha256sum "$CLIENT_BIN" | awk '{print $1}')
SERVER_SHA=$(sha256sum "$SERVER_BIN" | awk '{print $1}')
BASE_SHA=$(sha256sum "$BASE" | awk '{print $1}')
REMOTE_SERVER_SHA=$(ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
  "sha256sum '$REMOTE_SERVER_BIN' | awk '{print \$1}'")
REMOTE_BASE_SHA=$(ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
  "sha256sum '$REMOTE_BASE' | awk '{print \$1}'")
[[ "$REMOTE_SERVER_SHA" == "$SERVER_SHA" ]] || {
  echo "Remote d-HNSW server binary SHA mismatch" >&2; exit 2;
}
[[ "$REMOTE_BASE_SHA" == "$BASE_SHA" ]] || {
  echo "Remote DEEP1M base SHA mismatch" >&2; exit 2;
}

if [[ -d "$OUT" && -n "$(find "$OUT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$RESUME" != "1" ]]; then
  echo "Refusing non-empty OUT without RESUME=1: $OUT" >&2
  exit 2
fi
mkdir -p "$OUT/raw" "$OUT/raw_sources"

QUERY_MANIFEST=$OUT/query_pool.json
if [[ ! -s "$QUERY_MANIFEST" ]]; then
  python3 "$SCRIPT_DIR/fingerprint_query_pool.py" \
    --query "$QUERY" --groundtruth "$GROUNDTRUTH" --dataset DEEP1M \
    --method d-HNSW --metric l2 --limit 10000 --out "$QUERY_MANIFEST" >/dev/null
fi

MANIFEST=$OUT/campaign.json
python3 - "$MANIFEST" "$RESUME" "$CAMPAIGN_ID" "$CLIENT_SHA" "$SERVER_SHA" \
  "$BASE_SHA" "$REMOTE_SERVER_SHA" "$REMOTE_BASE_SHA" \
  "$REPEATS" "$WARMUPS" "$THREADS" "$EF" "$CLIENT_IP" \
  "$REMOTE_SERVER_HOST" "$REMOTE_SERVER_IP" "$NIC_IDX" "$PORT" "$RDMA_PORT" \
  "$REMOTE_SERVER_BIN" "$REMOTE_BASE" "$REMOTE_DHNSW_LD_LIBRARY_PATH" \
  "$(sha256sum "$SCRIPT_DIR/parse_dhnsw_frontier.py" | awk '{print $1}')" \
  "$(sha256sum "$QUERY_MANIFEST" | awk '{print $1}')" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

(path_s, resume_s, campaign_id, client_sha, server_sha, base_sha,
 remote_server_sha, remote_base_sha, repeats, warmups, threads, ef,
 client_ip, remote_host, remote_ip, nic_idx, port, rdma_port,
 remote_server_bin, remote_base, remote_runtime, parser_sha,
 query_manifest_sha) = sys.argv[1:]
protocol = {
    "client_binary_sha256": client_sha,
    "server_binary_sha256": server_sha,
    "base_sha256": base_sha,
    "remote_server_sha256": remote_server_sha,
    "remote_base_sha256": remote_base_sha,
    "dataset": "DEEP1M",
    "topologies": ["loopback", "remote"],
    "repeats": int(repeats),
    "warmups": int(warmups),
    "threads": int(threads),
    "ef": int(ef),
    "top_k": 10,
    "measurement_mode": "fixed_query_pool",
    "queries_per_run": 10000,
    "client_ip": client_ip,
    "remote_server_host": remote_host,
    "remote_server_ip": remote_ip,
    "remote_server_binary": remote_server_bin,
    "remote_base": remote_base,
    "remote_runtime_library_path": remote_runtime,
    "nic_index": int(nic_idx),
    "grpc_port": int(port),
    "rdma_port": int(rdma_port),
    "parser_sha256": parser_sha,
    "query_manifest_sha256": query_manifest_sha,
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
path = Path(path_s)
if path.exists():
    if resume_s != "1":
        raise SystemExit(f"{path} exists; set RESUME=1 to continue")
    old = json.loads(path.read_text())
    if old.get("campaign_id") != campaign_id or old.get("protocol") != protocol:
        raise SystemExit("d-HNSW topology campaign drift")
else:
    path.write_text(json.dumps({
        "campaign_id": campaign_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_fingerprint": fingerprint,
        "protocol": protocol,
    }, indent=2, sort_keys=True) + "\n")
PY

LOCAL_SERVER_PID=""
LOCAL_SERVER_EXE=""
REMOTE_SERVER_DIR=""

verify_local_server_pid() {
  [[ -n "$LOCAL_SERVER_PID" && -n "$LOCAL_SERVER_EXE" ]] || return 1
  [[ -r "/proc/$LOCAL_SERVER_PID/exe" ]] || return 1
  [[ "$(readlink -f /proc/"$LOCAL_SERVER_PID"/exe 2>/dev/null)" == "$LOCAL_SERVER_EXE" ]]
}

verify_remote_server_pid() {
  [[ -n "$REMOTE_SERVER_DIR" ]] || return 1
  ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
    "test -s '$REMOTE_SERVER_DIR/server.pid' -a -s '$REMOTE_SERVER_DIR/server.exe'; \
     pid=\$(cat '$REMOTE_SERVER_DIR/server.pid'); expected=\$(cat '$REMOTE_SERVER_DIR/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_server() {
  if verify_local_server_pid; then
    kill "$LOCAL_SERVER_PID" 2>/dev/null || true
    wait "$LOCAL_SERVER_PID" 2>/dev/null || true
  elif [[ -n "$LOCAL_SERVER_PID" ]] && kill -0 "$LOCAL_SERVER_PID" 2>/dev/null; then
    echo "Refusing to kill unowned local PID $LOCAL_SERVER_PID" >&2
  fi
  if verify_remote_server_pid; then
    ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
      "pid=\$(cat '$REMOTE_SERVER_DIR/server.pid'); kill \$pid 2>/dev/null || true" || true
  fi
  LOCAL_SERVER_PID=""
  LOCAL_SERVER_EXE=""
  REMOTE_SERVER_DIR=""
}
trap stop_server EXIT INT TERM

wait_local_ready() {
  local log=$1
  for _ in $(seq 1 "$SERVER_READY_WAIT_S"); do
    grep -q "gRPC server listening" "$log" && return 0
    verify_local_server_pid || return 1
    sleep 1
  done
  return 1
}

wait_remote_ready() {
  for _ in $(seq 1 "$SERVER_READY_WAIT_S"); do
    ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
      "grep -q 'gRPC server listening' '$REMOTE_SERVER_DIR/server.log'" 2>/dev/null && return 0
    verify_remote_server_pid || return 1
    sleep 1
  done
  return 1
}

start_server() {
  local topology=$1 out=$2 server_ip=$3 tag=$4
  stop_server
  if [[ "$topology" == "loopback" ]]; then
    LOCAL_SERVER_EXE=$(readlink -f "$SERVER_BIN")
    env LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" numactl --preferred=1 "$SERVER_BIN" \
      --server_ip="$server_ip" --port="$PORT" --rdma_port="$RDMA_PORT" \
      --use_nic_idx="$NIC_IDX" --dataset_path="$BASE" --dim=96 \
      --num_sub_hnsw=160 --meta_hnsw_neighbors=32 --sub_hnsw_neighbors=48 \
      > "$out/server.log" 2>&1 &
    LOCAL_SERVER_PID=$!
    printf 'pid=%s\nexe=%s\n' "$LOCAL_SERVER_PID" "$LOCAL_SERVER_EXE" > "$out/server_process.txt"
    wait_local_ready "$out/server.log" || return 1
  else
    REMOTE_SERVER_DIR="/tmp/${CAMPAIGN_ID//[^[:alnum:]]/_}_${tag}"
    ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
      "rm -rf '$REMOTE_SERVER_DIR'; mkdir -p '$REMOTE_SERVER_DIR'; \
       realpath '$REMOTE_SERVER_BIN' > '$REMOTE_SERVER_DIR/server.exe'; \
       nohup env LD_LIBRARY_PATH='$REMOTE_DHNSW_LD_LIBRARY_PATH' numactl --preferred=1 '$REMOTE_SERVER_BIN' \
         --server_ip='$server_ip' --port='$PORT' --rdma_port='$RDMA_PORT' \
         --use_nic_idx='$NIC_IDX' --dataset_path='$REMOTE_BASE' --dim=96 \
         --num_sub_hnsw=160 --meta_hnsw_neighbors=32 --sub_hnsw_neighbors=48 \
         > '$REMOTE_SERVER_DIR/server.log' 2>&1 < /dev/null & \
       echo \$! > '$REMOTE_SERVER_DIR/server.pid'"
    wait_remote_ready || return 1
    ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
      "printf 'pid='; cat '$REMOTE_SERVER_DIR/server.pid'; printf 'exe='; cat '$REMOTE_SERVER_DIR/server.exe'" \
      > "$out/server_process.txt"
  fi
}

copy_server_evidence() {
  local topology=$1 out=$2
  if [[ "$topology" == "loopback" ]]; then
    if verify_local_server_pid; then
      grep -E '^(VmRSS|VmHWM):' "/proc/$LOCAL_SERVER_PID/status" > "$out/server_rss_after.txt" || true
    fi
  else
    if verify_remote_server_pid; then
      ssh -o LogLevel=ERROR "$REMOTE_SERVER_HOST" \
        "pid=\$(cat '$REMOTE_SERVER_DIR/server.pid'); grep -E '^(VmRSS|VmHWM):' /proc/\$pid/status" \
        > "$out/server_rss_after.txt" || true
    fi
    scp -q "$REMOTE_SERVER_HOST:$REMOTE_SERVER_DIR/server.log" "$out/server.log"
  fi
}

run_complete() {
  local csv_path=$1 topology=$2 repeat=$3
  [[ -s "$csv_path" ]] || return 1
  python3 - "$csv_path" "$topology" "$repeat" "$CLIENT_SHA" <<'PY'
import csv, sys
path, topology, repeat, binary = sys.argv[1:]
rows = list(csv.DictReader(open(path)))
valid = [row for row in rows if (
    row["status"] == "ok" and row["dataset"].lower() == "deep1m"
    and row["binary_sha256"] == binary and int(row["threads"]) == 10
    and int(row["ef"]) == 200 and int(row["top_k"]) == 10
    and row["measurement_mode"] == "fixed_query_pool"
    and int(row["processed_queries"]) == int(row["expected_queries"]) == 10000
    and int(row["failed_queries"]) == 0
)]
raise SystemExit(0 if len(valid) == 1 else 1)
PY
}

run_one() {
  local topology=$1 run_id=$2 run_kind=$3 repeat=$4
  local out="$OUT/raw/$topology/$run_id"
  if [[ "$RESUME" == "1" ]] && run_complete "$out/frontier.csv" "$topology" "$repeat"; then
    echo "SKIP complete topology=$topology $run_id"
    return 0
  fi
  [[ ! -e "$out" ]] || { echo "Refusing incomplete topology output: $out" >&2; exit 2; }
  mkdir -p "$out"
  local server_ip="$CLIENT_IP"
  [[ "$topology" == "remote" ]] && server_ip="$REMOTE_SERVER_IP"
  local tag="${topology}_${run_id}"
  start_server "$topology" "$out" "$server_ip" "$tag"

  rm -f "$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt"
  set +e
  (
    cd "$DROOT/build"
    timeout "$TIMEOUT_CLIENT_S" env LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" \
      numactl --preferred=1 ./run_client \
        --server_address="$server_ip:$PORT" \
        --rdma_server_address="$server_ip:$RDMA_PORT" \
        --use_nic_idx="$NIC_IDX" --dataset=deep1M --benchmark_duration=20 \
        --worker_threads="$THREADS" --ef_override="$EF" --fixed_query_pool=true \
        --log_file="$out/deep1M_ef200_batch.log" \
        > "$out/deep1M_ef200_client.log" 2>&1
  )
  rc=$?
  set -e
  cp -f "$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt" \
    "$out/deep1M_ef200_benchmark_details.txt" 2>/dev/null || true
  copy_server_evidence "$topology" "$out"
  stop_server
  [[ $rc -eq 0 ]] || { tail -80 "$out/deep1M_ef200_client.log" >&2; return "$rc"; }

  python3 "$SCRIPT_DIR/parse_dhnsw_frontier.py" \
    --result-dir "$out" --datasets deep1M --ef-list 200 --duration 20 \
    --threads "$THREADS" --campaign-id "$CAMPAIGN_ID" \
    --binary-sha256 "$CLIENT_SHA" --out "$out/frontier.csv"
  run_complete "$out/frontier.csv" "$topology" "$repeat"
  printf '{"topology":"%s","run_id":"%s","run_kind":"%s","repeat":%s}\n' \
    "$topology" "$run_id" "$run_kind" "$repeat" > "$out/run.json"
}

for topology in $TOPOLOGIES; do
  for ((warmup = 0; warmup < WARMUPS; ++warmup)); do
    run_one "$topology" "warmup${warmup}" warmup "$warmup"
  done
  for ((repeat = 0; repeat < REPEATS; ++repeat)); do
    run_one "$topology" "r${repeat}" measure "$repeat"
  done
done

python3 - "$OUT" "$CAMPAIGN_ID" "$CLIENT_SHA" "$QUERY_MANIFEST" "$REPEATS" <<'PY'
import csv, hashlib, json, math, statistics, sys
from pathlib import Path

root, campaign_id, binary, manifest_path, repeats = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4]), int(sys.argv[5])
manifest = json.loads(manifest_path.read_text())
query = manifest["query"]
groundtruth = manifest["groundtruth"]
manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

rows = []
for topology in ("loopback", "remote"):
    for repeat in range(repeats):
        run_dir = root / "raw" / topology / f"r{repeat}"
        parsed = list(csv.DictReader((run_dir / "frontier.csv").open()))
        if len(parsed) != 1 or parsed[0]["status"] != "ok":
            raise SystemExit(f"invalid parsed topology row: {run_dir}")
        row = parsed[0]
        protocol = {
            "campaign_id": campaign_id,
            "binary_sha256": binary,
            "dataset": "DEEP1M",
            "topology": topology,
            "threads": 10,
            "ef": 200,
            "top_k": 10,
            "measurement_mode": "fixed_query_pool",
            "query_canonical_sha256": query["canonical_sha256"],
            "groundtruth_canonical_sha256": groundtruth["canonical_ids_sha256"],
        }
        fingerprint = hashlib.sha256(json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        source_record = {
            "kind": "dhnsw_topology_raw_source",
            "topology": topology,
            "repeat": repeat,
            "parsed_row": row,
            "frontier_csv_sha256": sha(run_dir / "frontier.csv"),
            "client_log": (run_dir / "deep1M_ef200_client.log").read_text(errors="replace"),
            "benchmark_details": (run_dir / "deep1M_ef200_benchmark_details.txt").read_text(errors="replace"),
            "server_log": (run_dir / "server.log").read_text(errors="replace"),
            "server_process": (run_dir / "server_process.txt").read_text(errors="replace"),
        }
        source_rel = Path("raw_sources") / f"{topology}_r{repeat}.json"
        source = root / source_rel
        source.write_text(json.dumps(source_record, indent=2, sort_keys=True) + "\n")
        rows.append({
            "campaign_id": campaign_id,
            "protocol_fingerprint": fingerprint,
            "binary_sha256": binary,
            "dataset": "DEEP1M",
            "topology": topology,
            "repeat": repeat,
            "threads": 10,
            "ef": 200,
            "top_k": 10,
            "metric": "l2",
            "measurement_mode": "fixed_query_pool",
            "processed_queries": row["processed_queries"],
            "expected_queries": row["expected_queries"],
            "failed_queries": row["failed_queries"],
            "qps": row["qps_recomputed"],
            "recall": row["recall"],
            "latency_us": row["latency_us"],
            "network_us": row["network_us"],
            "compute_us": row["compute_us"],
            "meta_us": row["meta_us"],
            "deserialize_us": row["deserialize_us"],
            "query_canonical_sha256": query["canonical_sha256"],
            "groundtruth_canonical_sha256": groundtruth["canonical_ids_sha256"],
            "query_file_sha256": query["file_sha256"],
            "groundtruth_file_sha256": groundtruth["file_sha256"],
            "query_manifest_sha256": manifest_sha,
            "source": source_rel.as_posix(),
            "source_sha256": sha(source),
        })

expected = {(topology, repeat) for topology in ("loopback", "remote") for repeat in range(repeats)}
actual = {(row["topology"], int(row["repeat"])) for row in rows}
if actual != expected or len(rows) != 2 * repeats:
    raise SystemExit("topology matrix incomplete")
if len({row["binary_sha256"] for row in rows}) != 1:
    raise SystemExit("topology binary drift")
if any(int(row["processed_queries"]) != int(row["expected_queries"]) or int(row["failed_queries"]) != 0 for row in rows):
    raise SystemExit("topology fixed pool incomplete")

with (root / "runs.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)

tcrit = 2.776
summary = []
for topology in ("loopback", "remote"):
    cell = [row for row in rows if row["topology"] == topology]
    record = {"topology": topology, "n": len(cell)}
    for metric in ("qps", "recall", "latency_us", "network_us", "compute_us", "meta_us", "deserialize_us"):
        values = [float(row[metric]) for row in cell]
        record[f"{metric}_mean"] = statistics.mean(values)
        record[f"{metric}_ci95"] = tcrit * statistics.stdev(values) / math.sqrt(len(values))
    summary.append(record)
with (root / "summary.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
    writer.writeheader(); writer.writerows(summary)
PY

echo "d-HNSW topology control written to $OUT"
