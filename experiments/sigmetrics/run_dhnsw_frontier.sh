#!/usr/bin/env bash
# Run d-HNSW recall-QPS frontier sweeps using the same dataset names as the
# d-HNSW SIGMETRICS paper.  Intended to execute on skv-node1.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DROOT=${DROOT:-/home/kvgroup/chaomei/d-HNSW}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
WORK=${WORK:-/tmp/dhnsw_frontier_20260709}
OUT=${OUT:-/home/kvgroup/chaomei/dhnsw_frontier_20260709}
EF_LIST=${EF_LIST:-"48 64 96 128 200"}
BENCHMARK_DURATION=${BENCHMARK_DURATION:-20}
THREADS=${THREADS:-10}
DATASETS=${DATASETS:-"sift1M gist1M deep1M text1M"}
FETCH_SIFT10M=${FETCH_SIFT10M:-0}
PATCH_ONLY=${PATCH_ONLY:-0}
PREPARE_DATASETS=${PREPARE_DATASETS:-1}
BUILD_DHNSW=${BUILD_DHNSW:-1}
BIGANN_URL_BASE=${BIGANN_URL_BASE:-ftp://ftp.irisa.fr/local/texmex/corpus}
SERVER_IP=${SERVER_IP:-10.0.0.61}
RDMA_IP=${RDMA_IP:-10.0.0.61}
NIC_IDX=${NIC_IDX:-1}
PORT=${PORT:-50051}
RDMA_PORT=${RDMA_PORT:-8888}
TIMEOUT_SERVER_S=${TIMEOUT_SERVER_S:-7200}
TIMEOUT_CLIENT_S=${TIMEOUT_CLIENT_S:-1200}
SERVER_READY_WAIT_S=${SERVER_READY_WAIT_S:-2400}
DHNSW_LD_LIBRARY_PATH=${DHNSW_LD_LIBRARY_PATH:-}
VALIDATOR=${VALIDATOR:-$SCRIPT_DIR/validate_dhnsw_dataset.py}
QUERY_POOL_PREPARER=${QUERY_POOL_PREPARER:-$SCRIPT_DIR/prepare_fixed_query_pool.py}
ACTIVE_SERVER_PID=""
ACTIVE_SERVER_WATCHDOG_PID=""

mkdir -p "$WORK" "$OUT"

patch_dhnsw() {
  cd "$DROOT"
  cp -n src/dhnsw/data_config.hh src/dhnsw/data_config.hh.bak.frontier
  cp -n src/bench/search_client_pipelined_reuse_thread.cc src/bench/search_client_pipelined_reuse_thread.cc.bak.frontier
  cp -n src/bench/search_server.cc src/bench/search_server.cc.bak.frontier
  python3 - "$EF_LIST" <<'PY'
import re
import sys
from pathlib import Path

ef_values = ", ".join(x for x in sys.argv[1].replace(",", " ").split() if x)
cfg = Path("src/dhnsw/data_config.hh")
text = cfg.read_text()
new_map = f'''static std::map<std::string, DatasetConfig> config_map = {{
    {{ "sift1M", {{
        "../datasets/sift/sift_query.fvecs",
        "../datasets/sift/sift_groundtruth.ivecs",
        128, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "bigann1M", {{
        "../datasets/bigann1M/bigann1M_query.fvecs",
        "../datasets/bigann1M/bigann1M_groundtruth.ivecs",
        128, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "spacev1M", {{
        "../datasets/spacev1M/spacev1M_query.fvecs",
        "../datasets/spacev1M/spacev1M_groundtruth.ivecs",
        100, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "turing1M", {{
        "../datasets/turing1M/turing1M_query.fvecs",
        "../datasets/turing1M/turing1M_groundtruth.ivecs",
        100, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "gist1M", {{
        "../datasets/gist/gist_query.fvecs",
        "../datasets/gist/gist_groundtruth.ivecs",
        960, 120, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "deep10M", {{
        "../datasets/deep100M/deep10M_query.fvecs",
        "../datasets/deep100M/deep10M_groundtruth.ivecs",
        96, 200, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "deep1M", {{
        "../datasets/deep1M/deep1M_query.fvecs",
        "../datasets/deep1M/deep1M_groundtruth.ivecs",
        96, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "text10M", {{
        "../datasets/text10M/query-u10k.fvecs",
        "../datasets/text10M/groundtruth-u10k.ivecs",
        200, 300, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "text1M", {{
        "../datasets/text1M/text1M_query.fvecs",
        "../datasets/text1M/text1M_groundtruth.ivecs",
        200, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "tti1M", {{
        "../datasets/text1M/text1M_query.fvecs",
        "../datasets/text1M/text1M_groundtruth.ivecs",
        200, 160, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "tti10M", {{
        "../datasets/text10M/query-u10k.fvecs",
        "../datasets/text10M/groundtruth-u10k.ivecs",
        200, 300, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "sift10M", {{
        "../datasets/sift10M/bigann_query.fvecs",
        "../datasets/sift10M/gnd/idx_10M.ivecs",
        128, 200, 32, 48,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
    {{ "sift100M", {{
        "../datasets/sift10M/bigann_query.bvecs",
        "../datasets/sift10M/gnd/idx_100M.ivecs",
        128, 250, 48, 72,
        {{{ef_values}}}, 1, 0, 1, 10, 10, 5000
    }}}},
  }};'''
start = text.find("static std::map<std::string, DatasetConfig> config_map = {")
if start < 0:
    raise SystemExit("could not find data_config config_map start")
end = text.find("\n  };", start)
if end < 0:
    raise SystemExit("could not find data_config config_map end")
end += len("\n  };")
text = text[:start] + new_map + text[end:]
if f"{{{ef_values}}}" not in text or "1, 0, 1, 10, 10, 5000" not in text:
    raise SystemExit("data_config patch verification failed")
cfg.write_text(text)

client = Path("src/bench/search_client_pipelined_reuse_thread.cc")
text = client.read_text()
text = text.replace("int top_k = 1;", "int top_k = dhnsw::GlobalDatasetConfig.top_k;")
if "#include <cstdio>" not in text:
    text = text.replace("#include <chrono>", "#include <chrono>\n#include <cstdio>")
if "pthread_barrier_t frontier_benchmark_barrier;" not in text:
    text = text.replace(
        "int dim_ground_truth;",
        "int dim_ground_truth;\npthread_barrier_t frontier_benchmark_barrier;",
    )
duration_flag = 'DEFINE_int32(benchmark_duration, 20, "Duration (in seconds) to run each ef benchmark.");'
fixed_pool_flag = '''DEFINE_int32(benchmark_duration, 20, "Duration (in seconds) to run each ef benchmark.");
DEFINE_bool(fixed_query_pool, true, "Process each query exactly once instead of looping by duration.");'''
if fixed_pool_flag not in text:
    if duration_flag not in text:
        raise SystemExit("client benchmark-duration flag did not match")
    text = text.replace(duration_flag, fixed_pool_flag)
if "DEFINE_int32(worker_threads" not in text:
    text = text.replace(
        fixed_pool_flag,
        fixed_pool_flag + '''
DEFINE_int32(worker_threads, 0, "Override the dataset-config worker count; zero keeps the config value.");
DEFINE_int32(ef_override, 0, "Run one ef value instead of the dataset-config sweep; zero keeps the sweep.");''',
    )
config_load_marker = '''    dhnsw::load_dataset_config();
    const auto& cfg = dhnsw::GlobalDatasetConfig;'''
config_override_block = '''    dhnsw::load_dataset_config();
    if (FLAGS_ef_override > 0) {
        dhnsw::GlobalDatasetConfig.ef_search_values = {FLAGS_ef_override};
    }
    const auto& cfg = dhnsw::GlobalDatasetConfig;'''
if config_load_marker in text:
    text = text.replace(config_load_marker, config_override_block)
elif config_override_block not in text:
    raise SystemExit("client dataset-config override block did not match")
text = text.replace(
    "    int num_threads = cfg.num_threads;",
    "    int num_threads = FLAGS_worker_threads > 0 ? FLAGS_worker_threads : cfg.num_threads;",
)
shape_marker = """    // Sample 1/3 of the query data (and corresponding ground truth)
    n_query_data = 0;
"""
shape_block = """    // Infer query-to-ground-truth alignment from the loaded shapes.  The
    // released datasets use either one query row per GT row or two query rows
    // per GT row.  A dataset-name special case can silently corrupt recall.
    const int query_rows = static_cast<int>(query_data_tmp.size()) / dim_query_data;
    const int ground_truth_rows = static_cast<int>(ground_truth_tmp.size()) / dim_ground_truth;
    int query_rows_per_ground_truth = 0;
    if (query_rows == ground_truth_rows) {
        query_rows_per_ground_truth = 1;
    } else if (query_rows == 2 * ground_truth_rows) {
        query_rows_per_ground_truth = 2;
    } else {
        std::cerr << "QUERY_GT_SHAPE unsupported query_rows=" << query_rows
                  << " ground_truth_rows=" << ground_truth_rows << std::endl;
        return 2;
    }
    std::cout << "QUERY_GT_SHAPE query_rows=" << query_rows
              << " ground_truth_rows=" << ground_truth_rows
              << " query_rows_per_ground_truth=" << query_rows_per_ground_truth
              << std::endl;

    // Sample 1/3 of the query data (and corresponding ground truth)
    n_query_data = 0;
"""
if shape_marker in text:
    text = text.replace(shape_marker, shape_block)
elif shape_block not in text:
    raise SystemExit("client query/ground-truth shape block did not match")
old_sampling = '''            ground_truth.insert(ground_truth.end(),
                                ground_truth_tmp.begin() + i * dim_ground_truth,
                                ground_truth_tmp.begin() + (i + 1) * dim_ground_truth);
            n_query_data++;
'''
path_sampling = '''            int gt_i = i;
            // The d-HNSW TEXT10M release stores query rows at twice the
            // ground-truth row rate: query[2*j] corresponds to groundtruth[j].
            // Without this alignment, the benchmark reports near-zero recall
            // even though exact brute-force matches the ground truth.
            if (query_data_path.find("text10M") != std::string::npos) {
                gt_i = i / 2;
            }
            if (gt_i < (int)ground_truth_tmp.size() / dim_ground_truth) {
                ground_truth.insert(ground_truth.end(),
                                    ground_truth_tmp.begin() + gt_i * dim_ground_truth,
                                    ground_truth_tmp.begin() + (gt_i + 1) * dim_ground_truth);
            } else {
                ground_truth.insert(ground_truth.end(), dim_ground_truth, -1);
            }
            n_query_data++;
'''
new_sampling = '''            int gt_i = i;
            gt_i /= query_rows_per_ground_truth;
            ground_truth.insert(ground_truth.end(),
                                ground_truth_tmp.begin() + gt_i * dim_ground_truth,
                                ground_truth_tmp.begin() + (gt_i + 1) * dim_ground_truth);
            n_query_data++;
'''
if old_sampling in text:
    text = text.replace(old_sampling, new_sampling)
elif path_sampling in text:
    text = text.replace(path_sampling, new_sampling)
elif new_sampling not in text:
    raise SystemExit("client query/ground-truth alignment block did not match")
pool_marker = '''    n_query_data = original_n_query_data * cfg.num_reps;
    
    int queries_per_thread = n_query_data / num_threads;'''
pool_block = '''    n_query_data = original_n_query_data * cfg.num_reps;
    std::cout << "FRONTIER_QUERY_POOL total_queries=" << n_query_data
              << " threads=" << num_threads
              << " top_k=" << cfg.top_k
              << " fixed=" << FLAGS_fixed_query_pool << std::endl;
    
    int queries_per_thread = n_query_data / num_threads;'''
if pool_marker in text:
    text = text.replace(pool_marker, pool_block)
elif pool_block not in text:
    raise SystemExit("client frontier query-pool record did not match")
thread_range_marker = '''            thread_params[i].query_start = i * queries_per_thread;
            thread_params[i].query_end = thread_params[i].query_start + queries_per_thread;'''
thread_range_block = '''            thread_params[i].query_start = i * queries_per_thread;
            thread_params[i].query_end = (i + 1 == num_threads)
                ? n_query_data
                : thread_params[i].query_start + queries_per_thread;'''
if thread_range_marker in text:
    text = text.replace(thread_range_marker, thread_range_block)
elif thread_range_block not in text:
    simple_thread_range = '''        thread_params[i].query_start = i * queries_per_thread;
        thread_params[i].query_end = thread_params[i].query_start + queries_per_thread;'''
    simple_thread_block = '''        thread_params[i].query_start = i * queries_per_thread;
        thread_params[i].query_end = (i + 1 == num_threads)
            ? n_query_data
            : thread_params[i].query_start + queries_per_thread;'''
    if simple_thread_range in text:
        text = text.replace(simple_thread_range, simple_thread_block)
    elif simple_thread_block not in text:
        raise SystemExit("client query-range block did not match")
thread_vector_marker = '''    std::vector<pthread_t> threads(num_threads);
    std::vector<thread_param_t> thread_params(num_threads);'''
thread_vector_block = '''    std::vector<pthread_t> threads(num_threads);
    std::vector<thread_param_t> thread_params(num_threads);
    if (pthread_barrier_init(&frontier_benchmark_barrier, nullptr, num_threads) != 0) {
        std::cerr << "failed to initialize frontier benchmark barrier" << std::endl;
        return 2;
    }'''
if thread_vector_marker in text:
    text = text.replace(thread_vector_marker, thread_vector_block)
elif thread_vector_block not in text:
    raise SystemExit("client benchmark-barrier initialization did not match")
bench_start_marker = '''        auto bench_start = high_resolution_clock::now();
        int query_index = 0;'''
bench_start_block = '''        pthread_barrier_wait(&frontier_benchmark_barrier);
        auto bench_start = high_resolution_clock::now();
        int query_index = 0;'''
if bench_start_marker in text:
    text = text.replace(bench_start_marker, bench_start_block)
elif bench_start_block not in text:
    raise SystemExit("client benchmark-barrier wait did not match")
join_marker = '''    std::vector<int> ef_values;
    std::vector<float> avg_recalls;'''
join_block = '''    pthread_barrier_destroy(&frontier_benchmark_barrier);

    std::vector<int> ef_values;
    std::vector<float> avg_recalls;'''
if join_marker in text:
    text = text.replace(join_marker, join_block)
elif join_block not in text:
    raise SystemExit("client benchmark-barrier destroy did not match")
timed_loop = '''        while (duration_cast<seconds>(high_resolution_clock::now() - bench_start).count() < (long)duration_sec) {
            int current_batch_size = std::min(batch_size, n_query_data_thread - (query_index % n_query_data_thread));
            if (current_batch_size <= 0) {
                current_batch_size = batch_size;
            }
            const float* batch_query_data_ptr = query_data_ptr + ((query_index % n_query_data_thread) * dim_query_data);'''
fixed_loop = '''        while (FLAGS_fixed_query_pool
                   ? query_index < n_query_data_thread
                   : duration_cast<seconds>(high_resolution_clock::now() - bench_start).count() < (long)duration_sec) {
            int query_offset = FLAGS_fixed_query_pool
                ? query_index
                : (query_index % n_query_data_thread);
            int current_batch_size = std::min(batch_size, n_query_data_thread - query_offset);
            if (current_batch_size <= 0) {
                current_batch_size = batch_size;
                query_offset = 0;
            }
            const float* batch_query_data_ptr = query_data_ptr + query_offset * dim_query_data;'''
if timed_loop in text:
    text = text.replace(timed_loop, fixed_loop)
elif fixed_loop not in text:
    raise SystemExit("client timed query loop did not match")
elapsed_marker = '''        // --- Calculate recall@k after processing all batches ---
        int total_correct = 0;'''
elapsed_block = '''        auto bench_end = high_resolution_clock::now();
        double elapsed_seconds = duration_cast<duration<double>>(bench_end - bench_start).count();

        // --- Calculate recall@k after processing all batches ---
        int total_correct = 0;'''
if elapsed_marker in text:
    text = text.replace(elapsed_marker, elapsed_block)
elif elapsed_block not in text:
    legacy_elapsed_marker = '''        // --- Calculate recall after processing all batches ---
        int total_correct = 0;'''
    legacy_elapsed_block = '''        auto bench_end = high_resolution_clock::now();
        double elapsed_seconds = duration_cast<duration<double>>(bench_end - bench_start).count();

        // --- Calculate recall after processing all batches ---
        int total_correct = 0;'''
    if legacy_elapsed_marker in text:
        text = text.replace(legacy_elapsed_marker, legacy_elapsed_block)
    elif legacy_elapsed_block not in text:
        raise SystemExit("client elapsed-time block did not match")
bad_throughput = '        double throughput = queries_executed / (avg_total_latency * 1e-6);'
wall_throughput = '        double throughput = elapsed_seconds > 0 ? queries_executed / elapsed_seconds : 0.0;'
if bad_throughput in text:
    text = text.replace(bad_throughput, wall_throughput)
elif wall_throughput not in text:
    raise SystemExit("client throughput calculation did not match")
result_marker = '''        std::cout << "  Throughput: " << throughput << " QPS" << std::endl;
        // Save results into thread parameters'''
cout_result_block = '''        std::cout << "  Throughput: " << throughput << " QPS" << std::endl;
        std::cout << "FRONTIER_THREAD_RESULT ef=" << ef
                  << " thread=" << thread_id
                  << " queries=" << queries_executed
                  << " elapsed_s=" << elapsed_seconds
                  << " recall=" << recall
                  << std::endl;
        // Save results into thread parameters'''
result_block = '''        std::cout << "  Throughput: " << throughput << " QPS" << std::endl;
        std::printf("FRONTIER_THREAD_RESULT ef=%d thread=%d queries=%d elapsed_s=%.9f recall=%.9f\\n",
                    ef, thread_id, queries_executed, elapsed_seconds, recall);
        std::fflush(stdout);
        // Save results into thread parameters'''
if result_marker in text:
    text = text.replace(result_marker, result_block)
elif cout_result_block in text:
    text = text.replace(cout_result_block, result_block)
elif result_block not in text:
    raise SystemExit("client frontier thread-result record did not match")
old_map = '''            // --- Determine original indices using local mapping ---
            auto mapping = local_hnsw.get_local_mapping();
            for (int i = 0; i < current_batch_size; i++) {
                int pos = i * top_k;
                if (batch_sub_hnsw_tags[pos] >= 0 && batch_sub_hnsw_tags[pos] < mapping.size() &&
                    batch_labels[pos] >= 0 && batch_labels[pos] < mapping[batch_sub_hnsw_tags[pos]].size()) {
                    batch_original_index[pos] = mapping[batch_sub_hnsw_tags[pos]][batch_labels[pos]];
                } else {
                    batch_original_index[pos] = -1;
                }
            }
            
            // --- Accumulate results for recall (store each retrieved and corresponding ground truth) ---
            for (int i = 0; i < current_batch_size; i++) {
                int pos = i * top_k;
                int gt = *(ground_truth_ptr + (((query_index + i) % n_query_data_thread) * dim_ground_truth));
                int retrieved = batch_original_index[pos];
                all_ground_truth.push_back(gt);
                all_retrieved.push_back(retrieved);
            }
'''
new_map = '''            // --- Determine original indices using local mapping ---
            auto mapping = local_hnsw.get_local_mapping();
            for (int i = 0; i < current_batch_size; i++) {
                for (int j = 0; j < top_k; j++) {
                    int pos = i * top_k + j;
                    if (batch_sub_hnsw_tags[pos] >= 0 && batch_sub_hnsw_tags[pos] < mapping.size() &&
                        batch_labels[pos] >= 0 && batch_labels[pos] < mapping[batch_sub_hnsw_tags[pos]].size()) {
                        batch_original_index[pos] = mapping[batch_sub_hnsw_tags[pos]][batch_labels[pos]];
                    } else {
                        batch_original_index[pos] = -1;
                    }
                }
            }

            // --- Accumulate top-k sets for recall@k ---
            for (int i = 0; i < current_batch_size; i++) {
                int gt_base = ((query_index + i) % n_query_data_thread) * dim_ground_truth;
                int limit = std::min(top_k, dim_ground_truth);
                for (int j = 0; j < limit; j++) {
                    all_ground_truth.push_back(ground_truth_ptr[gt_base + j]);
                }
                for (int j = 0; j < top_k; j++) {
                    all_retrieved.push_back(batch_original_index[i * top_k + j]);
                }
            }
'''
if old_map in text:
    text = text.replace(old_map, new_map)
elif new_map not in text:
    raise SystemExit("client mapping/recall block did not match")
old_recall = '''        // --- Calculate recall after processing all batches ---
        int total_correct = 0;
        for (size_t i = 0; i < all_retrieved.size(); i++) {
            if (all_retrieved[i] == all_ground_truth[i]) {
                total_correct++;
            }
        }
        float recall = (all_retrieved.size() > 0) ? static_cast<float>(total_correct) / all_retrieved.size() : 0.0f;
'''
new_recall = '''        // --- Calculate recall@k after processing all batches ---
        int total_correct = 0;
        int total_possible = 0;
        size_t gt_stride = static_cast<size_t>(std::min(top_k, dim_ground_truth));
        size_t ret_stride = static_cast<size_t>(top_k);
        size_t groups = std::min(all_retrieved.size() / ret_stride, all_ground_truth.size() / gt_stride);
        for (size_t q = 0; q < groups; q++) {
            std::unordered_set<int> gt_set;
            std::unordered_set<int> matched;
            for (size_t j = 0; j < gt_stride; j++) {
                gt_set.insert(all_ground_truth[q * gt_stride + j]);
            }
            for (size_t j = 0; j < ret_stride; j++) {
                int retrieved = all_retrieved[q * ret_stride + j];
                if (gt_set.count(retrieved) && matched.insert(retrieved).second) {
                    total_correct++;
                }
            }
            total_possible += static_cast<int>(gt_stride);
        }
        float recall = (total_possible > 0) ? static_cast<float>(total_correct) / total_possible : 0.0f;
'''
if old_recall in text:
    text = text.replace(old_recall, new_recall)
elif new_recall not in text:
    raise SystemExit("client final recall block did not match")
client.write_text(text)

server = Path("src/bench/search_server.cc")
text = server.read_text()
if "DEFINE_bool(ip_dist" not in text:
    text = text.replace(
        'DEFINE_int32(dim, 128, "Vector dimension.");',
        'DEFINE_int32(dim, 128, "Vector dimension.");\nDEFINE_bool(ip_dist, false, "Use inner-product distance for IP datasets.");',
    )
old_server_ctor = "DistributedHnsw dhnsw(dim, num_sub_hnsw, meta_hnsw_neighbors, sub_hnsw_neighbors, num_meta);"
new_server_ctor = '''distance_type metric = FLAGS_ip_dist ? Angular : Euclidean;
    DistributedHnsw dhnsw(dim, num_sub_hnsw, meta_hnsw_neighbors, sub_hnsw_neighbors, num_meta, metric);'''
if old_server_ctor in text:
    text = text.replace(old_server_ctor, new_server_ctor)
elif new_server_ctor not in text:
    raise SystemExit("server metric constructor patch did not match")
server.write_text(text)

client = Path("src/bench/search_client_pipelined_reuse_thread.cc")
text = client.read_text()
text = text.replace(
    'distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text10M") != std::string::npos) ? Angular : Euclidean;',
    'distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text") != std::string::npos || dhnsw::GlobalDatasetConfig.query_data_path.find("tti") != std::string::npos) ? Angular : Euclidean;',
)
text = text.replace(
    'distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text") != std::string::npos) ? Angular : Euclidean;',
    'distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text") != std::string::npos || dhnsw::GlobalDatasetConfig.query_data_path.find("tti") != std::string::npos) ? Angular : Euclidean;',
)
old_client_ctor = "LocalHnsw local_hnsw(dim, num_sub_hnsw, meta_hnsw_neighbors, sub_hnsw_neighbors, dhnsw_client);"
text_client_ctor = '''distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text") != std::string::npos) ? Angular : Euclidean;
    LocalHnsw local_hnsw(dim, num_sub_hnsw, meta_hnsw_neighbors, sub_hnsw_neighbors, dhnsw_client, metric);'''
new_client_ctor = '''distance_type metric = (dhnsw::GlobalDatasetConfig.query_data_path.find("text") != std::string::npos || dhnsw::GlobalDatasetConfig.query_data_path.find("tti") != std::string::npos) ? Angular : Euclidean;
    LocalHnsw local_hnsw(dim, num_sub_hnsw, meta_hnsw_neighbors, sub_hnsw_neighbors, dhnsw_client, metric);'''
if old_client_ctor in text:
    text = text.replace(old_client_ctor, new_client_ctor)
elif text_client_ctor in text:
    text = text.replace(text_client_ctor, new_client_ctor)
elif new_client_ctor not in text:
    raise SystemExit("client metric constructor patch did not match")
client.write_text(text)
PY
}

build_dhnsw() {
  if [[ ! -s "$DROOT/build/CMakeCache.txt" ]]; then
    cmake -S "$DROOT" -B "$DROOT/build" -DCMAKE_BUILD_TYPE=Release
  fi
  cmake --build "$DROOT/build" -j"$(nproc)"
}

convert_fbin_to_fvecs() {
  local src="$1" dst="$2"
  [[ -s "$dst" ]] && return 0
  mkdir -p "$(dirname "$dst")"
  python3 "$DROOT/tools/convert.py" fbin_streaming "$src" "$dst"
}

convert_ibin_to_ivecs() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  python3 - "$src" "$dst" <<'PY'
import os
import struct
import sys

import numpy as np

src, dst = sys.argv[1], sys.argv[2]
with open(src, "rb") as f:
    header = f.read(8)
    if len(header) != 8:
        raise SystemExit(f"truncated ibin header: {src}")
    n, dim_header = struct.unpack("<II", header)
    if n == 0 or dim_header == 0:
        raise SystemExit(f"invalid ibin shape for {src}: n={n}, dim={dim_header}")
    expected_ids = n * dim_header
    payload_bytes = os.path.getsize(src) - 8
    if payload_bytes not in (expected_ids * 4, expected_ids * 8):
        raise SystemExit(
            f"unsupported ibin payload for {src}: n={n}, dim={dim_header}, "
            f"bytes={payload_bytes}"
        )
    data = np.fromfile(f, dtype="<i4", count=expected_ids).reshape(n, dim_header)
tmp = dst + f".tmp.{os.getpid()}"
try:
    with open(tmp, "wb") as g:
        for row in data:
            g.write(struct.pack("<i", dim_header))
            row.astype("<i4", copy=False).tofile(g)
        g.flush()
        os.fsync(g.fileno())
    os.replace(tmp, dst)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
layout = "ids-only" if payload_bytes == expected_ids * 4 else "ids+distances"
print(f"converted ibin {src} -> ivecs {dst} ({n} x {dim_header}, {layout})", flush=True)
PY
}

convert_bvecs_to_fvecs() {
  local src="$1" dst="$2" maxn="${3:-0}"
  [[ -s "$dst" ]] && return 0
  mkdir -p "$(dirname "$dst")"
  python3 - "$src" "$dst" "$maxn" <<'PY'
import struct
import sys
from pathlib import Path

import numpy as np

src, dst, maxn = sys.argv[1], sys.argv[2], int(sys.argv[3])
count = 0
with open(src, "rb") as fin, open(dst, "wb") as fout:
    while maxn <= 0 or count < maxn:
        raw = fin.read(4)
        if not raw:
            break
        if len(raw) != 4:
            raise SystemExit(f"truncated bvec header at vector {count}")
        dim = struct.unpack("<i", raw)[0]
        vec = fin.read(dim)
        if len(vec) != dim:
            raise SystemExit(f"truncated bvec payload at vector {count}")
        arr = np.frombuffer(vec, dtype=np.uint8).astype(np.float32)
        fout.write(struct.pack("<i", dim))
        fout.write(arr.tobytes())
        count += 1
print(f"converted {count} bvecs -> {dst}")
PY
}

convert_u8bin_to_fvecs() {
  local src="$1" dst="$2"
  [[ -s "$dst" ]] && return 0
  mkdir -p "$(dirname "$dst")"
  python3 - "$src" "$dst" uint8 <<'PY'
import os
import struct
import sys

import numpy as np

src, dst, dtype_name = sys.argv[1:]
dtype = np.uint8 if dtype_name == "uint8" else np.int8
with open(src, "rb") as f:
    header = f.read(8)
    if len(header) != 8:
        raise SystemExit(f"truncated bin header: {src}")
    n, dim = struct.unpack("<II", header)
    expected = 8 + n * dim
    actual = os.path.getsize(src)
    if actual != expected:
        raise SystemExit(f"unexpected bin size for {src}: got {actual}, expected {expected}")
    data = np.fromfile(f, dtype=dtype, count=n * dim).reshape(n, dim)
with open(dst, "wb") as g:
    for row in data:
        g.write(struct.pack("<i", dim))
        row.astype(np.float32).tofile(g)
print(f"converted {dtype_name}bin {src} -> fvecs {dst} ({n} x {dim})", flush=True)
PY
}

convert_i8bin_to_fvecs() {
  local src="$1" dst="$2"
  [[ -s "$dst" ]] && return 0
  mkdir -p "$(dirname "$dst")"
  python3 - "$src" "$dst" int8 <<'PY'
import os
import struct
import sys

import numpy as np

src, dst, dtype_name = sys.argv[1:]
dtype = np.uint8 if dtype_name == "uint8" else np.int8
with open(src, "rb") as f:
    header = f.read(8)
    if len(header) != 8:
        raise SystemExit(f"truncated bin header: {src}")
    n, dim = struct.unpack("<II", header)
    expected = 8 + n * dim
    actual = os.path.getsize(src)
    if actual != expected:
        raise SystemExit(f"unexpected bin size for {src}: got {actual}, expected {expected}")
    data = np.fromfile(f, dtype=dtype, count=n * dim).reshape(n, dim)
with open(dst, "wb") as g:
    for row in data:
        g.write(struct.pack("<i", dim))
        row.astype(np.float32).tofile(g)
print(f"converted {dtype_name}bin {src} -> fvecs {dst} ({n} x {dim})", flush=True)
PY
}

validate_fixed_vec_file() {
  local path="$1" expected_rows="$2" expected_dim="$3"
  python3 - "$path" "$expected_rows" "$expected_dim" <<'PY'
import os
import sys

import numpy as np

path, rows, dim = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
expected_bytes = rows * (dim + 1) * 4
actual_bytes = os.path.getsize(path)
if actual_bytes != expected_bytes:
    raise SystemExit(
        f"invalid fixed-vector file size: {path}: got {actual_bytes}, expected {expected_bytes}"
    )
words = np.memmap(path, dtype="<i4", mode="r", shape=(rows, dim + 1))
bad = np.flatnonzero(words[:, 0] != dim)
if bad.size:
    raise SystemExit(f"invalid row dimension in {path} at row {int(bad[0])}")
print(f"validated fixed-vector shape {path}: {rows} x {dim}", flush=True)
PY
}

stream_bigann_base10m() {
  local dst="$1"
  [[ -s "$dst" ]] && return 0
  mkdir -p "$(dirname "$dst")"
  python3 - "$BIGANN_URL_BASE/bigann_base.bvecs.gz" "$dst" <<'PY'
import gzip
import shutil
import struct
import subprocess
import sys

import numpy as np

url, dst = sys.argv[1], sys.argv[2]
need = 10_000_000
curl = subprocess.Popen(["curl", "-L", "--fail", url], stdout=subprocess.PIPE)
assert curl.stdout is not None
count = 0
try:
    with gzip.GzipFile(fileobj=curl.stdout) as gz, open(dst, "wb") as fout:
        while count < need:
            raw = gz.read(4)
            if not raw:
                break
            if len(raw) != 4:
                raise SystemExit(f"truncated bvec header at vector {count}")
            dim = struct.unpack("<i", raw)[0]
            vec = gz.read(dim)
            if len(vec) != dim:
                raise SystemExit(f"truncated bvec payload at vector {count}")
            arr = np.frombuffer(vec, dtype=np.uint8).astype(np.float32)
            fout.write(struct.pack("<i", dim))
            fout.write(arr.tobytes())
            count += 1
            if count % 1_000_000 == 0:
                print(f"streamed {count}/{need}", flush=True)
finally:
    if curl.poll() is None:
        curl.terminate()
        try:
            curl.wait(timeout=5)
        except subprocess.TimeoutExpired:
            curl.kill()
if count != need:
    raise SystemExit(f"expected {need} vectors, got {count}")
print(f"wrote {dst}")
PY
}

fetch_sift10m_if_requested() {
  local dir="$DROOT/datasets/sift10M"
  mkdir -p "$dir/gnd"
  if [[ -f "$dir/bigann_base.fvecs" && -f "$dir/bigann_query.fvecs" && -f "$dir/gnd/idx_10M.ivecs" ]]; then
    return 0
  fi
  if [[ "$FETCH_SIFT10M" != "1" ]]; then
    return 1
  fi

  echo "Fetching/preparing SIFT10M (BIGANN first 10M) under $dir"
  if [[ ! -f "$dir/bigann_query.bvecs" ]]; then
    curl -L --fail "$BIGANN_URL_BASE/bigann_query.bvecs.gz" | gzip -dc > "$dir/bigann_query.bvecs"
  fi
  if [[ ! -f "$dir/gnd/idx_10M.ivecs" ]]; then
    tmp_tar="$WORK/bigann_gnd.tar.gz"
    curl -L --fail "$BIGANN_URL_BASE/bigann_gnd.tar.gz" -o "$tmp_tar"
    tar -xzf "$tmp_tar" -C "$dir"
  fi
  convert_bvecs_to_fvecs "$dir/bigann_query.bvecs" "$dir/bigann_query.fvecs" 0
  stream_bigann_base10m "$dir/bigann_base.fvecs"
  [[ -f "$dir/gnd/idx_10M.ivecs" ]]
}

prepare_datasets() {
  if [[ " $DATASETS " == *" sift1M "* && ! -f "$DROOT/datasets/sift/sift_base.fvecs" ]]; then
    mkdir -p "$WORK/sift" "$DROOT/datasets"
    convert_fbin_to_fvecs "$GB_DATA/sift1m/base.fbin" "$WORK/sift/sift_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/sift1m/queries/query-uniform.fbin" "$WORK/sift/sift_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/sift1m/queries/groundtruth-uniform.bin" "$WORK/sift/sift_groundtruth.ivecs"
    rm -rf "$DROOT/datasets/sift"
    ln -sfn "$WORK/sift" "$DROOT/datasets/sift"
  fi

  if [[ " $DATASETS " == *" bigann1M "* ]]; then
    mkdir -p "$WORK/bigann1M"
    convert_u8bin_to_fvecs "$GB_DATA/bigann1m/base.u8bin" "$WORK/bigann1M/bigann1M_base.fvecs"
    convert_u8bin_to_fvecs "$GB_DATA/bigann1m/queries/query-uniform.u8bin" "$WORK/bigann1M/bigann1M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/bigann1m/queries/groundtruth-uniform.bin" "$WORK/bigann1M/bigann1M_groundtruth.ivecs"
    ln -sfn "$WORK/bigann1M" "$DROOT/datasets/bigann1M"
  fi

  if [[ " $DATASETS " == *" spacev1M "* ]]; then
    mkdir -p "$WORK/spacev1M"
    convert_i8bin_to_fvecs "$GB_DATA/spacev1m/base.i8bin" "$WORK/spacev1M/spacev1M_base.fvecs"
    convert_i8bin_to_fvecs "$GB_DATA/spacev1m/queries/query-uniform.i8bin" "$WORK/spacev1M/spacev1M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/spacev1m/queries/groundtruth-uniform.bin" "$WORK/spacev1M/spacev1M_groundtruth.ivecs"
    ln -sfn "$WORK/spacev1M" "$DROOT/datasets/spacev1M"
  fi

  if [[ " $DATASETS " == *" turing1M "* ]]; then
    mkdir -p "$WORK/turing1M"
    convert_fbin_to_fvecs "$GB_DATA/turing1m/base.fbin" "$WORK/turing1M/turing1M_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/turing1m/queries/query-uniform.fbin" "$WORK/turing1M/turing1M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/turing1m/queries/groundtruth-uniform.bin" "$WORK/turing1M/turing1M_groundtruth.ivecs"
    ln -sfn "$WORK/turing1M" "$DROOT/datasets/turing1M"
  fi

  if [[ " $DATASETS " == *" gist1M "* && ! -f "$DROOT/datasets/gist/gist_base.fvecs" ]]; then
    mkdir -p "$WORK/gist" "$DROOT/datasets"
    convert_fbin_to_fvecs "$GB_DATA/gist1m/base.fbin" "$WORK/gist/gist_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/gist1m/queries/query-uniform.fbin" "$WORK/gist/gist_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/gist1m/queries/groundtruth-uniform.bin" "$WORK/gist/gist_groundtruth.ivecs"
    rm -rf "$DROOT/datasets/gist"
    ln -sfn "$WORK/gist" "$DROOT/datasets/gist"
  fi

  if [[ " $DATASETS " == *" deep10M "* ]]; then
    mkdir -p "$WORK/deep100M"
    convert_fbin_to_fvecs "$GB_DATA/deep10m/base.fbin" "$WORK/deep100M/deep10M_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/deep10m/queries/query-uniform.fbin" "$WORK/deep100M/deep10M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/deep10m/queries/groundtruth-uniform.bin" "$WORK/deep100M/deep10M_groundtruth.ivecs"
    ln -sfn "$WORK/deep100M" "$DROOT/datasets/deep100M"
  fi

  if [[ " $DATASETS " == *" deep1M "* ]]; then
    mkdir -p "$WORK/deep1M"
    convert_fbin_to_fvecs "$GB_DATA/deep1m/base.fbin" "$WORK/deep1M/deep1M_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/deep1m/queries/query-uniform.fbin" "$WORK/deep1M/deep1M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/deep1m/queries/groundtruth-uniform.bin" "$WORK/deep1M/deep1M_groundtruth.ivecs"
    ln -sfn "$WORK/deep1M" "$DROOT/datasets/deep1M"
  fi

  if [[ " $DATASETS " == *" text10M "* || " $DATASETS " == *" tti10M "* ]]; then
    mkdir -p "$WORK/text10M"
    convert_fbin_to_fvecs "$GB_DATA/tti-10m/base.fbin" "$WORK/text10M/text10M_base.fvecs"
    [[ -s "$QUERY_POOL_PREPARER" ]] || {
      printf 'Missing fixed-query-pool preparer: %s\n' "$QUERY_POOL_PREPARER" >&2
      return 2
    }
    python3 "$QUERY_POOL_PREPARER" \
      --query "$GB_DATA/tti-10m/queries/query-uniform.fbin" \
      --groundtruth "$GB_DATA/tti-10m/queries/groundtruth-uniform.bin" \
      --limit 10000 \
      --query-fvecs "$WORK/text10M/query-u10k.fvecs" \
      --groundtruth-ivecs "$WORK/text10M/groundtruth-u10k.ivecs" \
      --manifest "$WORK/text10M/query-pool-u10k.json"
    ln -sfn "$WORK/text10M" "$DROOT/datasets/text10M"
  fi

  if [[ " $DATASETS " == *" text1M "* || " $DATASETS " == *" tti1M "* ]]; then
    mkdir -p "$WORK/text1M"
    convert_fbin_to_fvecs "$GB_DATA/tti1m/base.fbin" "$WORK/text1M/text1M_base.fvecs"
    convert_fbin_to_fvecs "$GB_DATA/tti1m/queries/query-uniform.fbin" "$WORK/text1M/text1M_query.fvecs"
    convert_ibin_to_ivecs "$GB_DATA/tti1m/queries/groundtruth-uniform.bin" "$WORK/text1M/text1M_groundtruth.ivecs"
    ln -sfn "$WORK/text1M" "$DROOT/datasets/text1M"
  fi

  if [[ " $DATASETS " == *" sift10M "* ]]; then
    if ! fetch_sift10m_if_requested; then
      printf 'SIFT10M missing: no BIGANN/SIFT10M base/query/GT under %s/datasets/sift10M. Set FETCH_SIFT10M=1 to fetch and stream-convert the first 10M BIGANN vectors; do not substitute BIGANN1M.\n' "$DROOT" > "$OUT/SIFT10M.SKIPPED"
    else
      validate_fixed_vec_file "$DROOT/datasets/sift10M/bigann_base.fvecs" 10000000 128
      validate_fixed_vec_file "$DROOT/datasets/sift10M/bigann_query.fvecs" 10000 128
      validate_fixed_vec_file "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" 10000 100
    fi
  fi
}

validate_dataset() {
  local name=$1 base=$2 query=$3 groundtruth=$4
  python3 "$VALIDATOR" --base "$base" --query "$query" --groundtruth "$groundtruth" \
    --expected-queries 10000 --min-k 10 \
    --out "$OUT/${name}_dataset_validation.json"
}

validate_prepared_datasets() {
  [[ -s "$VALIDATOR" ]] || {
    printf 'Missing d-HNSW dataset validator: %s\n' "$VALIDATOR" >&2
    return 2
  }
  local dataset
  for dataset in $DATASETS; do
    case "$dataset" in
      sift1M) validate_dataset sift1M "$DROOT/datasets/sift/sift_base.fvecs" "$DROOT/datasets/sift/sift_query.fvecs" "$DROOT/datasets/sift/sift_groundtruth.ivecs" ;;
      bigann1M) validate_dataset bigann1M "$DROOT/datasets/bigann1M/bigann1M_base.fvecs" "$DROOT/datasets/bigann1M/bigann1M_query.fvecs" "$DROOT/datasets/bigann1M/bigann1M_groundtruth.ivecs" ;;
      spacev1M) validate_dataset spacev1M "$DROOT/datasets/spacev1M/spacev1M_base.fvecs" "$DROOT/datasets/spacev1M/spacev1M_query.fvecs" "$DROOT/datasets/spacev1M/spacev1M_groundtruth.ivecs" ;;
      turing1M) validate_dataset turing1M "$DROOT/datasets/turing1M/turing1M_base.fvecs" "$DROOT/datasets/turing1M/turing1M_query.fvecs" "$DROOT/datasets/turing1M/turing1M_groundtruth.ivecs" ;;
      gist1M) validate_dataset gist1M "$DROOT/datasets/gist/gist_base.fvecs" "$DROOT/datasets/gist/gist_query.fvecs" "$DROOT/datasets/gist/gist_groundtruth.ivecs" ;;
      deep1M) validate_dataset deep1M "$DROOT/datasets/deep1M/deep1M_base.fvecs" "$DROOT/datasets/deep1M/deep1M_query.fvecs" "$DROOT/datasets/deep1M/deep1M_groundtruth.ivecs" ;;
      text1M|tti1M) validate_dataset "$dataset" "$DROOT/datasets/text1M/text1M_base.fvecs" "$DROOT/datasets/text1M/text1M_query.fvecs" "$DROOT/datasets/text1M/text1M_groundtruth.ivecs" ;;
      deep10M) validate_dataset deep10M "$DROOT/datasets/deep100M/deep10M_base.fvecs" "$DROOT/datasets/deep100M/deep10M_query.fvecs" "$DROOT/datasets/deep100M/deep10M_groundtruth.ivecs" ;;
      text10M|tti10M) validate_dataset "$dataset" "$DROOT/datasets/text10M/text10M_base.fvecs" "$DROOT/datasets/text10M/query-u10k.fvecs" "$DROOT/datasets/text10M/groundtruth-u10k.ivecs" ;;
      sift10M)
        if [[ -s "$DROOT/datasets/sift10M/bigann_base.fvecs" &&
              -s "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" ]]; then
          validate_dataset sift10M "$DROOT/datasets/sift10M/bigann_base.fvecs" "$DROOT/datasets/sift10M/bigann_query.fvecs" "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs"
        fi
        ;;
      *) printf 'No validation mapping for dataset: %s\n' "$dataset" >&2; return 2 ;;
    esac
  done
}

verify_owned_server_pid() {
  local pid="$1" expected server_exe
  [[ -n "$pid" && -r "/proc/$pid/exe" ]] || return 1
  expected=$(realpath "$DROOT/build/run_server")
  server_exe=$(readlink -f "/proc/$pid/exe")
  [[ "$server_exe" == "$expected" ]]
}

stop_server() {
  if [[ -n "$ACTIVE_SERVER_WATCHDOG_PID" ]]; then
    kill "$ACTIVE_SERVER_WATCHDOG_PID" 2>/dev/null || true
    wait "$ACTIVE_SERVER_WATCHDOG_PID" 2>/dev/null || true
    ACTIVE_SERVER_WATCHDOG_PID=""
  fi
  if [[ -n "$ACTIVE_SERVER_PID" ]]; then
    if verify_owned_server_pid "$ACTIVE_SERVER_PID"; then
      kill "$ACTIVE_SERVER_PID" 2>/dev/null || true
      wait "$ACTIVE_SERVER_PID" 2>/dev/null || true
    elif kill -0 "$ACTIVE_SERVER_PID" 2>/dev/null; then
      echo "refusing to kill unowned PID $ACTIVE_SERVER_PID" >&2
    else
      wait "$ACTIVE_SERVER_PID" 2>/dev/null || true
    fi
    ACTIVE_SERVER_PID=""
  fi
  sleep 2
}

trap stop_server EXIT INT TERM

run_one() {
  local dataset="$1" base_path="$2" dim="$3" num_sub="$4" meta="$5" sub="$6" ip_dist="${7:-0}"
  echo "=== RUN $dataset ===" | tee -a "$OUT/run.log"
  cd "$DROOT/build"
  stop_server
  local start_ts ready_ts
  start_ts=$(date +%s)
  local server_metric_flag=""
  if [[ "$ip_dist" == "1" ]]; then
    server_metric_flag="--ip_dist=true"
  fi
  env LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" numactl --preferred=1 ./run_server \
    --server_ip="$SERVER_IP" --port="$PORT" --rdma_port="$RDMA_PORT" --use_nic_idx="$NIC_IDX" \
    --dataset_path="$base_path" --dim="$dim" --num_sub_hnsw="$num_sub" \
    --meta_hnsw_neighbors="$meta" --sub_hnsw_neighbors="$sub" $server_metric_flag \
    > "$OUT/${dataset}_server.log" 2>&1 &
  local server_pid=$!
  ACTIVE_SERVER_PID=$server_pid
  local server_exe=""
  for _ in $(seq 1 50); do
    if verify_owned_server_pid "$server_pid"; then
      server_exe=$(readlink -f "/proc/$server_pid/exe")
      break
    fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 0.1
  done
  if [[ -z "$server_exe" ]]; then
    echo "server PID did not resolve to the owned run_server binary" | tee -a "$OUT/run.log"
    stop_server
    return 1
  fi
  python3 - "$server_pid" "$server_exe" "$TIMEOUT_SERVER_S" "$OUT/run.log" <<'PY' &
import os
import signal
import sys
import time

server_pid = int(sys.argv[1])
expected_exe = os.path.realpath(sys.argv[2])
timeout_s = int(sys.argv[3])
log_path = sys.argv[4]
time.sleep(timeout_s)
try:
    current_exe = os.path.realpath(os.readlink(f"/proc/{server_pid}/exe"))
except (FileNotFoundError, PermissionError, ProcessLookupError):
    raise SystemExit(0)
if current_exe != expected_exe:
    raise SystemExit(0)
with open(log_path, "a", encoding="utf-8") as handle:
    handle.write(f"server timeout after {timeout_s}s: pid={server_pid}\n")
os.kill(server_pid, signal.SIGTERM)
PY
  ACTIVE_SERVER_WATCHDOG_PID=$!
  echo "server_pid=$server_pid" | tee -a "$OUT/run.log"
  printf 'server_pid=%s\nserver_exe=%s\n' "$server_pid" "$server_exe" \
    > "$OUT/${dataset}_server_process.txt"
  for _ in $(seq 1 "$SERVER_READY_WAIT_S"); do
    grep -q "gRPC server listening" "$OUT/${dataset}_server.log" && break
    if ! kill -0 "$server_pid" 2>/dev/null; then
      echo "server exited during build" | tee -a "$OUT/run.log"
      tail -80 "$OUT/${dataset}_server.log" || true
      stop_server
      return 1
    fi
    sleep 1
  done
  if ! grep -q "gRPC server listening" "$OUT/${dataset}_server.log"; then
    echo "server did not become ready within ${SERVER_READY_WAIT_S}s" | tee -a "$OUT/run.log"
    tail -80 "$OUT/${dataset}_server.log" || true
    stop_server
    return 1
  fi
  ready_ts=$(date +%s)
  echo "server_ready_s=$((ready_ts - start_ts))" | tee "$OUT/${dataset}_server_ready_seconds.txt" | tee -a "$OUT/run.log" >/dev/null
  grep -E '^(VmRSS|VmHWM):' /proc/"$server_pid"/status > "$OUT/${dataset}_server_rss_before.txt"
  local client_rc=0
  local detail_src="$DROOT/benchs/pipeline/test/sift1M@1benchmark_details.txt"
  local result_src="$DROOT/benchs/pipeline/test/sift1M@1benchmark_results_test.txt"
  mkdir -p "$DROOT/benchs/pipeline/test"
  for ef in $EF_LIST; do
    rm -f "$detail_src" "$result_src"
    set +e
    timeout "$TIMEOUT_CLIENT_S" env LD_LIBRARY_PATH="$DHNSW_LD_LIBRARY_PATH" numactl --preferred=1 ./run_client \
      --server_address="$SERVER_IP:$PORT" --rdma_server_address="$RDMA_IP:$RDMA_PORT" --use_nic_idx="$NIC_IDX" \
      --dataset="$dataset" --benchmark_duration="$BENCHMARK_DURATION" \
      --worker_threads="$THREADS" --ef_override="$ef" --fixed_query_pool=true \
      --log_file="$OUT/${dataset}_ef${ef}_batch.log" \
      > "$OUT/${dataset}_ef${ef}_client.log" 2>&1
    local point_rc=$?
    set -e
    if [[ "$point_rc" -eq 0 && ! -s "$detail_src" ]]; then
      echo "missing fresh aggregate benchmark details: dataset=$dataset ef=$ef" \
        | tee -a "$OUT/run.log" >&2
      point_rc=86
    fi
    if [[ -s "$detail_src" ]]; then
      cp -f "$detail_src" "$OUT/${dataset}_ef${ef}_benchmark_details.txt"
    fi
    if [[ -s "$result_src" ]]; then
      cp -f "$result_src" "$OUT/${dataset}_ef${ef}_benchmark_results.txt"
    fi
    printf 'dataset=%s ef=%s client_rc=%s\n' "$dataset" "$ef" "$point_rc" | tee -a "$OUT/run.log"
    if [[ "$point_rc" -ne 0 ]]; then
      client_rc=$point_rc
    fi
  done
  if verify_owned_server_pid "$server_pid"; then
    grep -E '^(VmRSS|VmHWM):' /proc/"$server_pid"/status > "$OUT/${dataset}_server_rss_after.txt"
  else
    echo "missing server RSS after client run" > "$OUT/${dataset}_server_rss_after.txt"
    client_rc=1
  fi
  stop_server
  echo "client_rc=$client_rc" | tee -a "$OUT/run.log"
  echo "=== DONE $dataset ===" | tee -a "$OUT/run.log"
  return "$client_rc"
}

patch_dhnsw
if [[ "$PATCH_ONLY" == "1" ]]; then
  echo "Patched isolated d-HNSW source under $DROOT"
  exit 0
fi
if [[ "$BUILD_DHNSW" == "1" ]]; then
  build_dhnsw
else
  [[ -x "$DROOT/build/run_client" && -x "$DROOT/build/run_server" ]] || {
    echo "Missing prebuilt d-HNSW binaries under $DROOT/build" >&2
    exit 2
  }
  echo "Using prebuilt d-HNSW binaries under $DROOT/build"
fi
if [[ "$PREPARE_DATASETS" == "1" ]]; then
  prepare_datasets
else
  echo "Using prevalidated d-HNSW dataset tree under $DROOT/datasets"
fi
validate_prepared_datasets

: > "$OUT/run.log"
for dataset in $DATASETS; do
  case "$dataset" in
    sift1M) run_one "sift1M" "../datasets/sift/sift_base.fvecs" 128 160 32 48 ;;
    bigann1M) run_one "bigann1M" "../datasets/bigann1M/bigann1M_base.fvecs" 128 160 32 48 ;;
    spacev1M) run_one "spacev1M" "../datasets/spacev1M/spacev1M_base.fvecs" 100 160 32 48 ;;
    turing1M) run_one "turing1M" "../datasets/turing1M/turing1M_base.fvecs" 100 160 32 48 ;;
    gist1M) run_one "gist1M" "../datasets/gist/gist_base.fvecs" 960 120 32 48 ;;
    deep1M) run_one "deep1M" "../datasets/deep1M/deep1M_base.fvecs" 96 160 32 48 ;;
    text1M) run_one "text1M" "../datasets/text1M/text1M_base.fvecs" 200 160 32 48 1 ;;
    tti1M) run_one "tti1M" "../datasets/text1M/text1M_base.fvecs" 200 160 32 48 1 ;;
    deep10M) run_one "deep10M" "../datasets/deep100M/deep10M_base.fvecs" 96 200 32 48 ;;
    text10M) run_one "text10M" "../datasets/text10M/text10M_base.fvecs" 200 300 32 48 1 ;;
    tti10M) run_one "tti10M" "../datasets/text10M/text10M_base.fvecs" 200 300 32 48 1 ;;
    sift10M)
      if [[ -f "$DROOT/datasets/sift10M/bigann_base.fvecs" && -f "$DROOT/datasets/sift10M/bigann_query.fvecs" && -f "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" ]]; then
        run_one "sift10M" "../datasets/sift10M/bigann_base.fvecs" 128 200 32 48
      else
        echo "SKIP sift10M: missing prepared BIGANN first-10M fvecs/query/GT" | tee -a "$OUT/run.log"
      fi
      ;;
    *) echo "Unknown dataset: $dataset" >&2; exit 2 ;;
  esac
done

echo "Wrote raw d-HNSW logs under $OUT"
