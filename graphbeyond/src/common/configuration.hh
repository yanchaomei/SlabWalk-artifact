#pragma once

#include <iomanip>
#include <iostream>
#include <library/configuration.hh>

#include "constants.hh"
#include "common/index_region_capacity.hh"
#include "common/query_contexts.hh"
#include "common/startup_wire.hh"
#include "lavd/region_capacity.hh"
#include "types.hh"

namespace configuration {

class IndexConfiguration : public Configuration {
public:
  filepath_t data_path{};
  str query_suffix{};
  u32 num_threads{};
  u32 num_coroutines{};
  u32 num_query_contexts{};
  i32 seed{};
  bool disable_thread_pinning{};
  str label{};  // for labeling benchmarks

  // HNSW parameters
  u32 ef_search{};
  u32 ef_construction{};
  u32 k{};
  u32 m{};

  bool store_index{};  // memory servers store the index; index is constructed from scratch; location is data_path
  bool load_index{};  // memory servers load index from file; cannot be used with store_index; location is data_path
  bool no_recall{};  // does not calculate the recall and thus requires no groundtruth
  bool ip_distance{};  // use the inner product distance rather than squared L2 norm

  u32 cache_size_ratio{};  // in %
  bool use_cache{};
  bool routing{};

  // GraphBeyond C1: Speculative Wide-Beam Navigation.
  // spec_k > 1 enables popping K candidates per search_level iteration and
  // batching their neighbor-list reads via read_neighborlists_batch.
  // spec_k == 1 (default) preserves original SHINE single-pop behavior.
  u32 spec_k{};

  // GraphBeyond LAVD: Layout-Aware Vector Disaggregation.
  // 0 = off (hard baseline: no 2nd MN region).
  // 4 / 8 = quantized-fanout bits; one fat neighborhood read per L0 hop.
  u32 lavd_bits{};
  u64 index_region_bytes{};  // authoritative HNSW MR; 0 = legacy 4 GiB
  u64 lavd_region_bytes{};  // requested per-MN capacity; 0 = legacy maximum
  u32 lavd_rerank{};  // top-R fp32 rerank width (0 -> default = max(k, ef/2))

public:
  IndexConfiguration(int argc, char** argv) {
    add_options();
    process_program_options(argc, argv);
    index_region_bytes = index_region::resolve_capacity_bytes(index_region_bytes);
    validate_index_region_options(argv);
    index_region::set_capacity_bytes(index_region_bytes);

    if (!is_server) {
      validate_compute_node_options(argv);
      num_query_contexts =
          query_contexts::resolve(num_threads, num_query_contexts);
    }

    operator<<(std::cerr, *this);
  }

private:
  void add_options() {
    desc.add_options()("data-path,d",
                       po::value<filepath_t>(&data_path),
                       "Path to input directory containing the base vectors (\"base.fvecs\") and the \"query\" "
                       "directory (which contains the query and the groundtruth file).")(
      "threads,t", po::value<u32>(&num_threads), "Number of threads per compute node.")(
      "coroutines,C", po::value<u32>(&num_coroutines)->default_value(4), "Number of coroutines per compute thread.")(
      "query-contexts",
      po::value<u32>(&num_query_contexts)->default_value(0),
      "Number of independent query QP/CQ contexts per compute node. "
      "0 preserves the legacy min(threads,4) policy; explicit values must not "
      "exceed threads or 40.")(
      "disable-thread-pinning,p",
      po::bool_switch(&disable_thread_pinning)->default_value(false),
      "Disables pinning compute threads to physical cores if set.")(
      "seed", po::value<i32>(&seed)->default_value(1234), "Seed for PRNG; setting to -1 uses std::random_device.")(
      "label", po::value<str>(&label), "Optional label to identify benchmarks.")(
      "query-suffix,q", po::value<str>(&query_suffix), "Filename suffix for the query file.")(
      "store-index,s",
      po::bool_switch(&store_index),
      "Construct the index from scratch and the memory servers store the index to a file.")(
      "load-index,l",
      po::bool_switch(&load_index),
      "The index is not built, the memory servers load the index from a file.")(
      "cache", po::bool_switch(&use_cache), "Activate cache on CNs.")(
      "routing", po::bool_switch(&routing), "Activate adaptive query routing.")(
      "cache-ratio",
      po::value<u32>(&cache_size_ratio)->default_value(5),
      "Cache size ratio relative to the index size in %.")(
      "no-recall", po::bool_switch(&no_recall), "No recall computation, ground truth file can be omitted.")(
      "ip-dist", po::bool_switch(&ip_distance), "Use the inner product distance rather than the squared L2 norm.")(
      "ef-search", po::value<u32>(&ef_search), "Beam width during search.")(
      "ef-construction", po::value<u32>(&ef_construction)->default_value(200), "Beam width during construction.")(
      "k,k", po::value<u32>(&k), "Number of k nearest neighbors.")(
      "m,m", po::value<u32>(&m)->default_value(32), "Number of bidirectional connections in the HNSW graph.")(
      "spec-k",
      po::value<u32>(&spec_k)->default_value(1),
      "GraphBeyond C1: pop K candidates per search step and batch their neighbor-list reads. "
      "spec-k=1 disables (original SHINE behavior). Recommended sweep: 1, 2, 4, 8.")(
      "lavd",
      po::value<u32>(&lavd_bits)->default_value(0),
      "GraphBeyond LAVD: 0=off (hard baseline). 4 or 8 = quantized-fanout bits; "
      "one fat neighborhood RDMA read per level-0 hop instead of 1 list + M vector reads.")(
      "index-region-bytes",
      po::value<u64>(&index_region_bytes)->default_value(0),
      "Exact authoritative-HNSW MR capacity in bytes. "
      "0 preserves the legacy 4 GiB default; 10M indexes should request 16 GiB.")(
      "lavd-region-bytes",
      po::value<u64>(&lavd_region_bytes)->default_value(0),
      "Exact per-MN LAVD neighborhood MR capacity in bytes. "
      "0 uses the legacy 6 GiB default; explicit requests may be larger.")(
      "lavd-rerank",
      po::value<u32>(&lavd_rerank)->default_value(0),
      "LAVD top-R fp32 rerank width. 0 -> default max(k, ef_search/2).");
  }

  void validate_index_region_options(char** argv) const {
    static_assert(MEMORY_NODE_MAX_MEMORY == index_region::LEGACY_DEFAULT_BYTES,
                  "legacy index-region defaults diverged");
    if (!index_region::is_valid_capacity_bytes(index_region_bytes)) {
      std::cerr << "[ERROR]: --index-region-bytes must be 0 or in ["
                << index_region::MIN_CAPACITY_BYTES << ", "
                << index_region::MAX_CAPACITY_BYTES
                << "] and a multiple of " << index_region::ALIGNMENT
                << std::endl;
      exit_with_help_message(argv);
    }
  }

  void validate_compute_node_options(char** argv) const {
    if (data_path.empty() || query_suffix.empty()) {
      std::cerr << "[ERROR]: Data path and query suffix cannot be empty" << std::endl;
      exit_with_help_message(argv);
    }

    if (num_threads == 0 || num_coroutines == 0 || ef_search == 0 || k == 0) {
      std::cerr << "[ERROR]: Parameters threads, coroutines, ef-search, and k are required" << std::endl;
      exit_with_help_message(argv);
    }

    if (max_send_queue_wr <= 0 ||
        num_coroutines > static_cast<u32>(max_send_queue_wr)) {
      std::cerr << "[ERROR]: --coroutines must not exceed --max-send-wrs"
                << std::endl;
      exit_with_help_message(argv);
    }
    if (!query_contexts::is_valid_request(num_threads, num_query_contexts)) {
      std::cerr << "[ERROR]: --query-contexts must be 0 or in [1, min(threads, "
                << query_contexts::MAX_CONTEXTS << ")]" << std::endl;
      exit_with_help_message(argv);
    }
    const u32 num_shared_contexts =
        query_contexts::resolve(num_threads, num_query_contexts);
    const u32 max_threads_per_context =
        query_contexts::max_threads_per_context(num_threads,
                                                num_shared_contexts);
    const u64 max_coroutines_per_qp =
        static_cast<u64>(max_threads_per_context) * num_coroutines;
    if (max_coroutines_per_qp > static_cast<u64>(max_send_queue_wr)) {
      std::cerr << "[ERROR]: coroutines sharing one QP exceed --max-send-wrs"
                << std::endl;
      exit_with_help_message(argv);
    }

    const u64 resolved_region_bytes =
      lavd::resolve_region_capacity_bytes(
        lavd_region_bytes, lavd::REGION_CAPACITY_LEGACY_DEFAULT_BYTES);
    if (!lavd::is_valid_region_capacity_bytes(
          resolved_region_bytes, lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES)) {
      std::cerr << "[ERROR]: --lavd-region-bytes must be 0 or in ["
                << lavd::minimum_region_capacity_bytes() << ", "
                << lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES
                << "] and a multiple of "
                << lavd::REGION_CAPACITY_ALIGNMENT << std::endl;
      exit_with_help_message(argv);
    }

    if (store_index && load_index) {
      std::cerr << "[ERROR]: --store-index and --load-index cannot be used in conjunction" << std::endl;
      exit_with_help_message(argv);
    }

    if (use_cache && cache_size_ratio == 0) {
      std::cerr << "[ERROR]: If --cache is set, --cache-ratio must be > 0" << std::endl;
      exit_with_help_message(argv);
    }

    if (routing && not use_cache) {
      std::cerr << "[ERROR]: --routing can only be used in conjunction with --cache" << std::endl;
      exit_with_help_message(argv);
    }
  }

public:
  friend std::ostream& operator<<(std::ostream& os, const IndexConfiguration& config) {
    os << static_cast<const Configuration&>(config);
    os << std::left << std::setfill(' ') << std::setw(30)
       << "index region bytes: " << config.index_region_bytes << std::endl;

    if (config.is_initiator) {
      constexpr i32 width = 30;
      constexpr i32 max_width = width * 2;

      os << std::left << std::setfill(' ');
      os << std::setw(width) << "data path: " << config.data_path << std::endl;
      os << std::setw(width) << "query suffix: " << config.query_suffix << std::endl;
      os << std::setw(width) << "number of threads: " << config.num_threads << std::endl;
      os << std::setw(width) << "number of coroutines: " << config.num_coroutines << std::endl;
      os << std::setw(width) << "query QP/CQ contexts: "
         << config.num_query_contexts << std::endl;
      os << std::setw(width) << "threads pinned: " << (config.disable_thread_pinning ? "false" : "true") << std::endl;
      os << std::setw(width) << "seed: " << config.seed << std::endl;
      os << std::setfill('-') << std::setw(max_width) << "" << std::endl;
      os << std::left << std::setfill(' ');
      os << std::setw(width) << "K: " << config.k << std::endl;
      os << std::setw(width) << "M: " << config.m << std::endl;
      os << std::setw(width) << "ef search: " << config.ef_search << std::endl;
      os << std::setw(width) << "ef construction: " << config.ef_construction << std::endl;
      os << std::setw(width) << "spec-k (GraphBeyond C1): " << config.spec_k << std::endl;
      os << std::setfill('=') << std::setw(max_width) << "" << std::endl;
    }
    return os;
  }
};

}  // namespace configuration
