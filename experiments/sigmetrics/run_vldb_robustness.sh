#!/usr/bin/env bash
# Run one-factor-at-a-time SlabWalk robustness controls on skv-node1.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$REPO_ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
MN=${MN:-skv-node6}
OUT=${OUT:-$REPO_ROOT/results/vldb_robustness/raw_$(date -u +%Y%m%dT%H%M%SZ)}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-robustness-$(date -u +%Y%m%dT%H%M%SZ)}
REPEATS=${REPEATS:-5}
WARMUPS=${WARMUPS:-1}
TIMEOUT_S=${TIMEOUT_S:-1200}
PORT=${PORT:-1234}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
DRY_RUN=${DRY_RUN:-0}
RESUME=${RESUME:-0}

DATASET=DEEP1M
DATA_PATH=$GB_DATA/deep1m
M=16
EFC=100
BASE_EF=200
BASE_THREADS=10
BASE_COROUTINES=2
BASE_K=10
REGION_BYTES=4294967296
LAYOUT_ENV="SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_NATIVE_PACKED_WRITE=1 SHINE_LAVD_VARBLOCK=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2"
ACTIVE_MN=""
ACTIVE_REMOTE_DIR=""

is_nonnegative_integer() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_positive_integer() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
is_positive_integer "$REPEATS" || { echo "REPEATS must be positive" >&2; exit 2; }
is_nonnegative_integer "$WARMUPS" || { echo "WARMUPS must be non-negative" >&2; exit 2; }
is_positive_integer "$TIMEOUT_S" || { echo "TIMEOUT_S must be positive" >&2; exit 2; }

if [[ -d "$OUT" && -n "$(find "$OUT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" && "$RESUME" != "1" ]]; then
  echo "Refusing non-empty OUT without RESUME=1: $OUT" >&2
  exit 2
fi
mkdir -p "$OUT"

RAW=$OUT/robustness_raw.csv
MATRIX=$OUT/matrix.csv
CAMPAIGN=$OUT/campaign.json
GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')

if [[ "$RESUME" != "1" ]]; then
  echo "campaign_id,protocol_fingerprint,binary_sha256,dataset,factor,value,run_kind,repeat,threads,query_contexts,coroutines,top_k,ef,query_suffix,latency_enabled,metric,json,stderr,status" > "$RAW"
  echo "factor,value,threads,query_contexts,coroutines,top_k,ef,query_suffix,latency_enabled" > "$MATRIX"
  cat >> "$MATRIX" <<EOF
workers,1,1,1,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
workers,8,8,8,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
workers,16,16,16,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
workers,40,40,40,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
coroutines,1,$BASE_THREADS,$BASE_THREADS,1,$BASE_K,$BASE_EF,uniform,1
coroutines,2,$BASE_THREADS,$BASE_THREADS,2,$BASE_K,$BASE_EF,uniform,1
coroutines,4,$BASE_THREADS,$BASE_THREADS,4,$BASE_K,$BASE_EF,uniform,1
coroutines,8,$BASE_THREADS,$BASE_THREADS,8,$BASE_K,$BASE_EF,uniform,1
coroutines,16,$BASE_THREADS,$BASE_THREADS,16,$BASE_K,$BASE_EF,uniform,1
top_k,1,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,1,$BASE_EF,uniform,1
top_k,10,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,10,$BASE_EF,uniform,1
top_k,50,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,50,$BASE_EF,uniform,1
top_k,100,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,100,$BASE_EF,uniform,1
query_distribution,uniform,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
query_distribution,zipf1.0,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,$BASE_K,$BASE_EF,a1.0-n10000,1
latency_instrumentation,off,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,0
latency_instrumentation,on,$BASE_THREADS,$BASE_THREADS,$BASE_COROUTINES,$BASE_K,$BASE_EF,uniform,1
EOF
  python3 - "$CAMPAIGN" "$CAMPAIGN_ID" "$GB_BIN_SHA256" "$REPEATS" "$WARMUPS" "$PORT" "$INDEX_REGION_BYTES" <<'PY'
import json, sys
from datetime import datetime, timezone
path, campaign_id, binary_sha256, repeats, warmups, tcp_port, index_region_bytes = sys.argv[1:]
json.dump({
    "campaign_id": campaign_id,
    "binary_sha256": binary_sha256,
    "dataset": "DEEP1M",
    "measurement_mode": "fixed_query_pool",
    "latency_mode": "thread_local_steady_clock",
    "design": "one_factor_at_a_time",
    "query_context_policy": "one_context_per_worker",
    "warmups": int(warmups),
    "repeats": int(repeats),
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "tcp_port": int(tcp_port),
    "index_region_bytes": int(index_region_bytes),
}, open(path, "w"), indent=2, sort_keys=True)
PY
else
  [[ -s "$RAW" && -s "$MATRIX" && -s "$CAMPAIGN" ]] || {
    echo "Incomplete resume state in $OUT" >&2; exit 2;
  }
  old_campaign=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["campaign_id"])' "$CAMPAIGN")
  old_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["binary_sha256"])' "$CAMPAIGN")
  [[ "$old_campaign" == "$CAMPAIGN_ID" && "$old_sha" == "$GB_BIN_SHA256" ]] || {
    echo "Resume campaign or binary mismatch" >&2; exit 2;
  }
fi

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

cleanup_active_mn() {
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_REMOTE_DIR" ]]; then
    stop_mn "$ACTIVE_MN" "$ACTIVE_REMOTE_DIR"
    ACTIVE_MN=""
    ACTIVE_REMOTE_DIR=""
  fi
}

start_mn() {
  local host=$1 remote_dir=$2
  ssh -o LogLevel=ERROR "$host" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     realpath '$GB_BIN_R' > '$remote_dir/server.exe'; \
     nohup env LD_LIBRARY_PATH='$GB_LIB' numactl --preferred=1 '$GB_BIN_R' \
       --is-server --num-clients 1 --port "$PORT" \
       --index-region-bytes '$INDEX_REGION_BYTES' > '$remote_dir/mn.out' \
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

protocol_fingerprint() {
  local factor=$1 value=$2 threads=$3 query_contexts=$4 coroutines=$5 top_k=$6 ef=$7 query_suffix=$8 latency_enabled=$9
  python3 - "$GB_BIN_SHA256" "$DATASET" "$factor" "$value" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix" "$latency_enabled" "$M" "$EFC" "$REGION_BYTES" "$INDEX_REGION_BYTES" "$PORT" "$LAYOUT_ENV" <<'PY'
import hashlib, json, sys
binary, dataset, factor, value, threads, query_contexts, coroutines, top_k, ef, query_suffix, latency_enabled, m, efc, capacity, index_region_bytes, tcp_port, env = sys.argv[1:]
protocol = {
    "binary_sha256": binary, "dataset": dataset, "factor": factor,
    "value": value, "threads": int(threads), "query_contexts": int(query_contexts),
    "coroutines": int(coroutines),
    "top_k": int(top_k), "ef": int(ef), "query_suffix": query_suffix,
    "m": int(m), "efc": int(efc), "lavd_bits": 8,
    "lavd_region_bytes": int(capacity), "layout_env": env,
    "index_region_bytes": int(index_region_bytes),
    "latency_enabled": int(latency_enabled),
    "tcp_port": int(tcp_port),
    "measurement_mode": "fixed_query_pool",
    "latency_mode": "thread_local_steady_clock",
}
print(hashlib.sha256(json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
PY
}

append_row() {
  python3 - "$RAW" "$@" <<'PY'
import csv, sys
path = sys.argv[1]
fields = [
    "campaign_id", "protocol_fingerprint", "binary_sha256", "dataset",
    "factor", "value", "run_kind", "repeat", "threads", "query_contexts", "coroutines",
    "top_k", "ef", "query_suffix", "latency_enabled", "metric", "json", "stderr", "status",
]
with open(path, "a", newline="") as handle:
    csv.DictWriter(handle, fieldnames=fields).writerow(dict(zip(fields, sys.argv[2:])))
PY
}

already_recorded() {
  local factor=$1 value=$2 kind=$3 rep=$4
  python3 - "$RAW" "$factor" "$value" "$kind" "$rep" <<'PY'
import csv, sys
path, factor, value, kind, rep = sys.argv[1:]
rows = list(csv.DictReader(open(path)))
raise SystemExit(0 if any(
    r["factor"] == factor and r["value"] == value and
    r["run_kind"] == kind and r["repeat"] == rep and r["status"] == "ok"
    for r in rows
) else 1)
PY
}

run_one() {
  local factor=$1 value=$2 threads=$3 query_contexts=$4 coroutines=$5 top_k=$6 ef=$7 query_suffix=$8 latency_enabled=$9 kind=${10} rep=${11}
  if [[ "$RESUME" == "1" ]] && already_recorded "$factor" "$value" "$kind" "$rep"; then
    echo "SKIP recorded $factor=$value $kind r$rep"
    return 0
  fi
  local safe_value tag remote_dir json_path err_path status rc fingerprint
  safe_value=$(printf '%s' "$value" | tr -c '[:alnum:].-' '_')
  tag="deep1m_${factor}_${safe_value}_${kind}_r${rep}"
  remote_dir="/tmp/${CAMPAIGN_ID//[^[:alnum:]]/_}_${tag}"
  json_path="$OUT/$tag.json"
  err_path="$OUT/$tag.err"
  fingerprint=$(protocol_fingerprint "$factor" "$value" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix" "$latency_enabled")

  if [[ "$DRY_RUN" == "1" ]]; then
    printf 'DRY_RUN %s T=%s QCTX=%s C=%s k=%s ef=%s q=%s\n' "$tag" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix"
    return 0
  fi

  start_mn "$MN" "$remote_dir"
  set +e
  timeout "$TIMEOUT_S" env LD_LIBRARY_PATH="$GB_LIB" GB_QUERY_LATENCY="$latency_enabled" \
    $LAYOUT_ENV numactl --preferred=1 "$GB_BIN" \
      --servers "$MN" --initiator --threads "$threads" --coroutines "$coroutines" \
      --query-contexts "$query_contexts" \
      --port "$PORT" \
      --index-region-bytes "$INDEX_REGION_BYTES" \
      --data-path "$DATA_PATH/" --query-suffix "$query_suffix" \
      --ef-search "$ef" --ef-construction "$EFC" --m "$M" --k "$top_k" \
      --label "$tag" --spec-k 1 --load-index --lavd 8 \
      --lavd-region-bytes "$REGION_BYTES" > "$json_path" 2> "$err_path"
  rc=$?
  set -e
  stop_mn "$MN" "$remote_dir"
  scp -q "$MN:$remote_dir/mn.err" "$OUT/$tag.mn.err" 2>/dev/null || true
  ACTIVE_MN=""
  ACTIVE_REMOTE_DIR=""

  status="rc_$rc"
  if [[ $rc -eq 0 && -s "$json_path" ]]; then
    if python3 - "$json_path" "$latency_enabled" "$query_contexts" <<'PY'
import json, math, sys
obj = json.load(open(sys.argv[1]))
q = obj["queries"]
n = int(obj["num_queries"])
assert obj["query_contexts"] == int(sys.argv[3])
assert int(q["processed"]) == n > 0
if int(sys.argv[2]):
    assert int(q["local_latency_samples"]) == n
    p = [float(q[k]) for k in ("local_latency_p50_us", "local_latency_p95_us", "local_latency_p99_us")]
    assert all(math.isfinite(x) and x >= 0 for x in p) and p == sorted(p)
else:
    assert "local_latency_samples" not in q
assert float(q["queries_per_sec"]) > 0
assert 0 <= float(q["recall"]) <= 1
PY
    then
      status=ok
    else
      status=invalid_json_metrics
    fi
  fi
  append_row "$CAMPAIGN_ID" "$fingerprint" "$GB_BIN_SHA256" "$DATASET" \
    "$factor" "$value" "$kind" "$rep" "$threads" "$query_contexts" "$coroutines" \
    "$top_k" "$ef" "$query_suffix" "$latency_enabled" l2 "$json_path" "$err_path" "$status"
  [[ "$status" == "ok" ]] || {
    tail -40 "$err_path" >&2 || true
    return 1
  }
}

run_cell() {
  local factor=$1 value=$2 threads=$3 query_contexts=$4 coroutines=$5 top_k=$6 ef=$7 query_suffix=$8 latency_enabled=$9
  local rep
  for ((rep = 0; rep < WARMUPS; ++rep)); do
    run_one "$factor" "$value" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix" "$latency_enabled" warmup "$rep"
  done
  for ((rep = 0; rep < REPEATS; ++rep)); do
    run_one "$factor" "$value" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix" "$latency_enabled" measure "$rep"
  done
}

trap cleanup_active_mn EXIT INT TERM

if [[ "$DRY_RUN" != "1" ]]; then
  [[ -x "$GB_BIN" ]] || { echo "Missing binary: $GB_BIN" >&2; exit 2; }
  ssh -o LogLevel=ERROR "$MN" \
    "test -s '$DATA_PATH/dump/index_m${M}_efc${EFC}_node1_of1.dat' && \
     test -s '$DATA_PATH/queries/query-uniform.fbin' && \
     test -s '$DATA_PATH/queries/groundtruth-uniform.bin' && \
     test -s '$DATA_PATH/queries/query-a1.0-n10000.fbin' && \
     test -s '$DATA_PATH/queries/groundtruth-a1.0-n10000.bin'" || {
    echo "DEEP1M index or robustness query files are missing on $MN" >&2
    exit 2
  }
fi

while IFS=, read -r factor value threads query_contexts coroutines top_k ef query_suffix latency_enabled <&3; do
  [[ "$factor" == "factor" ]] && continue
  run_cell "$factor" "$value" "$threads" "$query_contexts" "$coroutines" "$top_k" "$ef" "$query_suffix" "$latency_enabled"
done 3< "$MATRIX"

if [[ "$DRY_RUN" != "1" ]]; then
  python3 "$SCRIPT_DIR/summarize_vldb_robustness.py" \
    --raw "$RAW" --matrix "$MATRIX" --out-dir "$OUT/summary" --repeats "$REPEATS"
fi
echo "Wrote robustness campaign to $OUT"
