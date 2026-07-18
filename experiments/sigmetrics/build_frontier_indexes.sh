#!/usr/bin/env bash
# Build missing GraphBeyond/SHINE index dumps for frontier datasets.
# Intended to execute on skv-node1.  Keep this separate from the recall-QPS
# sweep so a long store-index run cannot contaminate measured query QPS.
set -euo pipefail

GB_BIN=${GB_BIN:-/home/kvgroup/chaomei/graphbeyond-c1-shine-1m-latclean}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LOCAL_LD_LIBRARY_PATH=${GB_LOCAL_LD_LIBRARY_PATH:-/home/kvgroup/chaomei/lib}
GB_REMOTE_LD_LIBRARY_PATH=${GB_REMOTE_LD_LIBRARY_PATH:-/home/kvgroup/chaomei/lib}
OUT=${OUT:-/home/kvgroup/chaomei/frontier_index_build_$(date +%Y%m%d_%H%M%S)}
DATASETS=${DATASETS:-"DEEP10M TEXT10M SIFT10M"}
THREADS=${THREADS:-16}
COROS=${COROS:-2}
TIMEOUT_S=${TIMEOUT_S:-43200}
PORT=${PORT:-1234}
INDEX_REGION_1M_BYTES=${INDEX_REGION_1M_BYTES:-4294967296}
INDEX_REGION_10M_BYTES=${INDEX_REGION_10M_BYTES:-17179869184}
DEEP_M=${DEEP_M:-32}
DEEP_EFC=${DEEP_EFC:-200}
DEEP1_M=${DEEP1_M:-16}
DEEP1_EFC=${DEEP1_EFC:-100}
TEXT_M=${TEXT_M:-16}
TEXT_EFC=${TEXT_EFC:-100}
TEXT1_M=${TEXT1_M:-16}
TEXT1_EFC=${TEXT1_EFC:-100}
SIFT10_M=${SIFT10_M:-16}
SIFT10_EFC=${SIFT10_EFC:-100}
DEEP_MN=${DEEP_MN:-skv-node5}
DEEP1_MN=${DEEP1_MN:-skv-node5}
TEXT_MN=${TEXT_MN:-skv-node2}
TEXT1_MN=${TEXT1_MN:-skv-node4}
SIFT10_MN=${SIFT10_MN:-skv-node2}
ACTIVE_MN=""
ACTIVE_MN_DIR=""

[[ -x "$GB_BIN" ]] || { echo "CN binary is not executable: $GB_BIN" >&2; exit 2; }
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }
[[ "$COROS" =~ ^[1-9][0-9]*$ ]] || { echo "COROS must be positive" >&2; exit 2; }
[[ "$PORT" =~ ^[1-9][0-9]*$ ]] && ((PORT <= 65535)) || {
  echo "PORT must be in 1..65535" >&2; exit 2;
}

mkdir -p "$OUT"
CSV="$OUT/build_index_status.csv"
echo "dataset,mn,tcp_port,index_region_bytes,data_path,m,efc,query_suffix,ip_flag,index_path,status,stderr" > "$CSV"
GB_BIN_SHA256=$(sha256sum "$GB_BIN" | awk '{print $1}')
python3 - "$OUT/campaign.json" "$GB_BIN_SHA256" "$DATASETS" "$THREADS" \
  "$COROS" "$PORT" "$INDEX_REGION_1M_BYTES" "$INDEX_REGION_10M_BYTES" \
  "$TIMEOUT_S" <<'PY'
import json, sys
from datetime import datetime, timezone

(path, binary_sha256, datasets, threads, coroutines, port,
 index_region_1m, index_region_10m, timeout_s) = sys.argv[1:]
with open(path, "w") as handle:
    json.dump({
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "binary_sha256": binary_sha256,
        "datasets": datasets.split(),
        "threads": int(threads),
        "coroutines": int(coroutines),
        "tcp_port": int(port),
        "timeout_seconds": int(timeout_s),
        "index_region_bytes_by_scale": {
            "1M": int(index_region_1m),
            "10M": int(index_region_10m),
        },
        "measurement_scope": "authoritative-hnsw-build-and-store",
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

verify_remote_pid() {
  local mn="$1" remote_dir="$2"
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe'; \
     pid=\$(cat '$remote_dir/server.pid'); expected=\$(cat '$remote_dir/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_mn() {
  local mn="$1" remote_dir="$2"
  if verify_remote_pid "$mn" "$remote_dir"; then
    ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
      "pid=\$(cat '$remote_dir/server.pid'); kill \$pid 2>/dev/null || true" 2>/dev/null || true
  fi
}

cleanup_active_mn() {
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_MN_DIR" ]]; then
    stop_mn "$ACTIVE_MN" "$ACTIVE_MN_DIR"
  fi
}

start_mn() {
  local mn="$1" remote_dir="$2" index_region_bytes="$3"
  ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     realpath '$GB_BIN_R' > '$remote_dir/server.exe'; \
     nohup env LD_LIBRARY_PATH='$GB_REMOTE_LD_LIBRARY_PATH' numactl --preferred=1 '$GB_BIN_R' \
       --is-server --num-clients 1 --port "$PORT" \
       --index-region-bytes '$index_region_bytes' > '$remote_dir/mn.out' 2> '$remote_dir/mn.err' < /dev/null & \
     echo \$! > '$remote_dir/server.pid'" \
    2>/dev/null
  ACTIVE_MN="$mn"
  ACTIVE_MN_DIR="$remote_dir"
  for _ in $(seq 1 80); do
    verify_remote_pid "$mn" "$remote_dir" && return 0
    sleep 0.1
  done
  return 1
}

append_row() {
  local dataset="$1" mn="$2" index_region_bytes="$3" data="$4" m="$5" efc="$6" qs="$7" ip_flag="$8" index_path="$9" status="${10}" err="${11}"
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$dataset" "$mn" "$PORT" "$index_region_bytes" "$data" "$m" "$efc" "$qs" "${ip_flag:-none}" "$index_path" "$status" "$err" >> "$CSV"
}

run_build() {
  local dataset="$1" mn="$2" data="$3" qs="$4" m="$5" efc="$6" ef="$7" ip_flag="$8" index_region_bytes="$9"
  local tag="${dataset}_build_m${m}_efc${efc}"
  local json="$OUT/${tag}.json"
  local err="$OUT/${tag}.err"
  local index_path="${data%/}/dump/index_m${m}_efc${efc}_node1_of1.dat"
  local remote_mn_dir="/tmp/vldb_index_build_${tag}"

  if ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" "test -f '$index_path'" 2>/dev/null; then
    echo "SKIP $dataset: index exists at $index_path"
    append_row "$dataset" "$mn" "$index_region_bytes" "$data" "$m" "$efc" "$qs" "$ip_flag" "$index_path" "exists" "$err"
    return 0
  fi

  echo "=== BUILD $dataset on $mn ==="
  start_mn "$mn" "$remote_mn_dir" "$index_region_bytes"
  set +e
  timeout "$TIMEOUT_S" env LD_LIBRARY_PATH="$GB_LOCAL_LD_LIBRARY_PATH" \
    numactl --preferred=1 "$GB_BIN" \
    --servers "$mn" --initiator --threads "$THREADS" --coroutines "$COROS" \
    --port "$PORT" \
    --index-region-bytes "$index_region_bytes" \
    --data-path "$data" --query-suffix "$qs" \
    --ef-search "$ef" --ef-construction "$efc" --m "$m" --k 10 \
    --label "$tag" --spec-k 1 --store-index --lavd 0 $ip_flag \
    > "$json" 2> "$err"
  rc=$?
  set -e
  stop_mn "$mn" "$remote_mn_dir"
  ACTIVE_MN=""
  ACTIVE_MN_DIR=""

  if [[ $rc -eq 0 ]] && ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no "$mn" "test -f '$index_path'" 2>/dev/null; then
    append_row "$dataset" "$mn" "$index_region_bytes" "$data" "$m" "$efc" "$qs" "$ip_flag" "$index_path" "ok" "$err"
  else
    echo "BUILD $dataset failed rc=$rc"
    tail -60 "$err" || true
    append_row "$dataset" "$mn" "$index_region_bytes" "$data" "$m" "$efc" "$qs" "$ip_flag" "$index_path" "rc_$rc" "$err"
    return "$rc"
  fi
}

trap cleanup_active_mn EXIT INT TERM

for dataset in $DATASETS; do
  case "$dataset" in
    DEEP10M)
      run_build "DEEP10M" "$DEEP_MN" "$GB_DATA/deep10m/" "uniform" "$DEEP_M" "$DEEP_EFC" 300 "" "$INDEX_REGION_10M_BYTES"
      ;;
    DEEP1M)
      run_build "DEEP1M" "$DEEP1_MN" "$GB_DATA/deep1m/" "uniform" "$DEEP1_M" "$DEEP1_EFC" 300 "" "$INDEX_REGION_1M_BYTES"
      ;;
    TEXT10M)
      run_build "TEXT10M" "$TEXT_MN" "$GB_DATA/tti-10m/" "uniform" "$TEXT_M" "$TEXT_EFC" 300 "--ip-dist" "$INDEX_REGION_10M_BYTES"
      ;;
    TEXT1M)
      run_build "TEXT1M" "$TEXT1_MN" "$GB_DATA/tti1m/" "uniform" "$TEXT1_M" "$TEXT1_EFC" 300 "--ip-dist" "$INDEX_REGION_1M_BYTES"
      ;;
    SIFT10M)
      if [[ ! -s "$GB_DATA/sift10m/base.fbin" || ! -s "$GB_DATA/sift10m/queries/query-uniform.fbin" ]]; then
        echo "SIFT10M data is not prepared under $GB_DATA/sift10m. Run experiments/sigmetrics/prepare_sift10m_bigann.sh first." >&2
        exit 2
      fi
      run_build "SIFT10M" "$SIFT10_MN" "$GB_DATA/sift10m/" "uniform" "$SIFT10_M" "$SIFT10_EFC" 300 "" "$INDEX_REGION_10M_BYTES"
      ;;
    *)
      echo "Unknown index-build dataset: $dataset" >&2
      exit 2
      ;;
  esac
done

echo "Wrote $CSV"
