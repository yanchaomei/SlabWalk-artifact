#!/usr/bin/env bash
# Measure d-HNSW worker scaling on DEEP1M. Intended for skv-node1.
set -euo pipefail

DROOT=${DROOT:-/home/kvgroup/chaomei/d-HNSW}
OUT=${OUT:-/home/kvgroup/chaomei/dhnsw_worker_scaling_fixedbatch_20260711}
WORKERS=${WORKERS:-"1 8 16 40"}
REPEATS=${REPEATS:-5}
EF=${EF:-200}
BENCHMARK_DURATION=${BENCHMARK_DURATION:-30}
SERVER_IP=${SERVER_IP:-10.0.0.61}
RDMA_IP=${RDMA_IP:-10.0.0.61}
NIC_IDX=${NIC_IDX:-1}
PORT=${PORT:-50051}
RDMA_PORT=${RDMA_PORT:-8888}
SERVER_READY_WAIT_S=${SERVER_READY_WAIT_S:-2400}
CLIENT_TIMEOUT_S=${CLIENT_TIMEOUT_S:-300}

mkdir -p "$OUT"

patch_runtime_controls() {
  cd "$DROOT"
  cp -n src/bench/search_client_pipelined_reuse_thread.cc \
    src/bench/search_client_pipelined_reuse_thread.cc.bak.worker-scaling
  python3 - <<'PY'
from pathlib import Path

path = Path("src/bench/search_client_pipelined_reuse_thread.cc")
text = path.read_text()

flag_anchor = 'DEFINE_int32(benchmark_duration, 20, "Duration (in seconds) to run each ef benchmark.");'
flag_block = '''DEFINE_int32(benchmark_duration, 20, "Duration (in seconds) to run each ef benchmark.");
DEFINE_int32(worker_threads, 0, "Override the dataset-config worker count; zero keeps the config value.");
DEFINE_int32(ef_override, 0, "Run one ef value instead of the dataset-config sweep; zero keeps the sweep.");'''
if "DEFINE_int32(worker_threads" not in text:
    if flag_anchor not in text:
        raise SystemExit("benchmark_duration flag anchor not found")
    text = text.replace(flag_anchor, flag_block, 1)

if "DEFINE_int32(worker_start_stagger_ms" not in text:
    text = text.replace(
        'DEFINE_int32(ef_override, 0, "Run one ef value instead of the dataset-config sweep; zero keeps the sweep.");',
        'DEFINE_int32(ef_override, 0, "Run one ef value instead of the dataset-config sweep; zero keeps the sweep.");\n'
        'DEFINE_int32(worker_start_stagger_ms, 0, "Delay between worker starts to avoid control-plane bursts.");',
        1,
    )
if "DEFINE_bool(replicate_query_pool_per_worker" not in text:
    text = text.replace(
        'DEFINE_int32(worker_start_stagger_ms, 0, "Delay between worker starts to avoid control-plane bursts.");',
        'DEFINE_int32(worker_start_stagger_ms, 0, "Delay between worker starts to avoid control-plane bursts.");\n'
        'DEFINE_bool(replicate_query_pool_per_worker, false, "Give each worker the full query pool for fixed-batch scaling.");',
        1,
    )
if "#include <unistd.h>" not in text:
    text = text.replace("#include <pthread.h>", "#include <pthread.h>\n#include <unistd.h>", 1)

load_anchor = '''    dhnsw::load_dataset_config();
    const auto& cfg = dhnsw::GlobalDatasetConfig;'''
load_block = '''    dhnsw::load_dataset_config();
    if (FLAGS_ef_override > 0) {
        dhnsw::GlobalDatasetConfig.ef_search_values = {FLAGS_ef_override};
    }
    const auto& cfg = dhnsw::GlobalDatasetConfig;'''
if "GlobalDatasetConfig.ef_search_values = {FLAGS_ef_override}" not in text:
    if load_anchor not in text:
        raise SystemExit("dataset-config load anchor not found")
    text = text.replace(load_anchor, load_block, 1)

thread_anchor = "    int num_threads = cfg.num_threads;"
thread_block = "    int num_threads = FLAGS_worker_threads > 0 ? FLAGS_worker_threads : cfg.num_threads;"
if thread_block not in text:
    if thread_anchor not in text:
        raise SystemExit("worker-count anchor not found")
    text = text.replace(thread_anchor, thread_block, 1)

stagger_anchor = '''        if (ret != 0) {
            std::cerr << "Error: unable to create thread, " << ret << std::endl;
            exit(-1);
        }
'''
stagger_block = stagger_anchor + '''        if (FLAGS_worker_start_stagger_ms > 0) {
            usleep(static_cast<useconds_t>(FLAGS_worker_start_stagger_ms) * 1000);
        }
'''
if "FLAGS_worker_start_stagger_ms > 0" not in text:
    if stagger_anchor not in text:
        raise SystemExit("worker-start anchor not found")
    text = text.replace(stagger_anchor, stagger_block, 1)

range_anchor = '''        thread_params[i].query_start = i * queries_per_thread;
        thread_params[i].query_end = thread_params[i].query_start + queries_per_thread;'''
range_block = '''        if (FLAGS_replicate_query_pool_per_worker) {
            thread_params[i].query_start = 0;
            thread_params[i].query_end = n_query_data;
        } else {
            thread_params[i].query_start = i * queries_per_thread;
            thread_params[i].query_end = thread_params[i].query_start + queries_per_thread;
        }'''
if "FLAGS_replicate_query_pool_per_worker" not in text.split("// --- Create worker threads ---", 1)[-1]:
    if range_anchor not in text:
        raise SystemExit("query-range anchor not found")
    text = text.replace(range_anchor, range_block, 1)

path.write_text(text)
PY
}

build_client() {
  cd "$DROOT"
  cmake --build build -j8 --target run_client
}

server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_server() {
  if pgrep -x run_server >/dev/null; then
    echo "A run_server process already exists; refusing to disturb it." >&2
    exit 2
  fi

  cd "$DROOT/build"
  numactl --preferred=1 ./run_server \
    --server_ip="$SERVER_IP" --port="$PORT" --rdma_port="$RDMA_PORT" \
    --use_nic_idx="$NIC_IDX" \
    --dataset_path=../datasets/deep1M/deep1M_base.fvecs \
    --dim=96 --num_sub_hnsw=160 --meta_hnsw_neighbors=32 \
    --sub_hnsw_neighbors=48 >"$OUT/deep1M_server.log" 2>&1 &
  server_pid=$!
  printf 'server_pid=%s\n' "$server_pid" | tee "$OUT/run.log"

  for _ in $(seq 1 "$SERVER_READY_WAIT_S"); do
    if grep -q "gRPC server listening" "$OUT/deep1M_server.log"; then
      grep -E 'VmRSS' "/proc/$server_pid/status" >"$OUT/deep1M_server_rss.txt" || true
      return 0
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      tail -80 "$OUT/deep1M_server.log" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "d-HNSW server did not become ready" >&2
  tail -80 "$OUT/deep1M_server.log" >&2 || true
  return 1
}

run_clients() {
  cd "$DROOT/build"
  for workers in $WORKERS; do
    for rep in $(seq 1 "$REPEATS"); do
      stem="deep1M_w${workers}_r${rep}"
      printf 'workers=%s repeat=%s\n' "$workers" "$rep" | tee -a "$OUT/run.log"
      rm -f "$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt"
      timeout "$CLIENT_TIMEOUT_S" numactl --preferred=1 ./run_client \
        --server_address="$SERVER_IP:$PORT" \
        --rdma_server_address="$RDMA_IP:$RDMA_PORT" \
        --use_nic_idx="$NIC_IDX" --dataset=deep1M \
        --worker_threads="$workers" --ef_override="$EF" \
        --worker_start_stagger_ms=100 \
        --replicate_query_pool_per_worker=true \
        --benchmark_duration="$BENCHMARK_DURATION" \
        --physical_cores_per_thread=1 \
        --log_file="$OUT/${stem}_batch.log" \
        >"$OUT/${stem}_client.log" 2>&1
      cp "$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt" \
        "$OUT/${stem}_benchmark_details.txt"
    done
  done
}

patch_runtime_controls
build_client
start_server
run_clients
printf 'complete=1\n' | tee -a "$OUT/run.log"
