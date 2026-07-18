#include "compute_node.hh"

#include <algorithm>

#include "cache/placement.hh"
#include "common/quantile.hh"
#include "common/result_fingerprint.hh"
#include "crane/crane.hh"
#include "hnsw/distance.hh"
#include "io/read_data.hh"
#include "lavd/build.hh"
#include "lavd/build_reorder.hh"
#include "lavd/maintenance_guard.hh"

template <class Distance>
ComputeNode<Distance>::ComputeNode(Configuration& config)
    : context_(config), cm_(context_, config), num_servers_(config.num_server_nodes()) {
  lavd_bits_ = config.lavd_bits;  // GraphBeyond LAVD (0 == baseline)
  const u64 lavd_region_capacity_bytes =
    lavd::resolve_region_capacity_bytes(
      config.lavd_region_bytes, lavd::REGION_CAPACITY_LEGACY_DEFAULT_BYTES);
  lib_assert(
    lavd::is_valid_region_capacity_bytes(
      lavd_region_capacity_bytes, lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES),
    "invalid resolved LAVD neighborhood region capacity");
  print_status("LAVD neighborhood region: requested bytes=" +
               std::to_string(config.lavd_region_bytes) +
               ", resolved bytes=" + std::to_string(lavd_region_capacity_bytes));
  init_remote_tokens();
  cm_.connect();

  // Resolve one authoritative startup contract before any CN decides how many
  // MN token messages to receive. A mismatched non-initiator acknowledges the
  // mismatch and both sides fail here instead of desynchronizing the RDMA
  // control stream later.
  const u64 effective_region_capacity =
      lavd_bits_ > 0 ? lavd_region_capacity_bytes : 0;
  const configuration::LavdStartupContract local_contract{
      2u, lavd_bits_, config.num_query_contexts, 0u,
      effective_region_capacity};
  if (cm_.is_initiator) {
    vec<u8> agreements(cm_.client_qps.size(), 0);
    if (!agreements.empty()) {
      LocalMemoryRegion agreement_region{context_, agreements.data(),
                                         agreements.size()};
      for (u32 i = 0; i < cm_.client_qps.size(); ++i) {
        cm_.client_qps[i]->post_receive(agreement_region, sizeof(u8), 0, i);
      }
      for (const QP& qp : cm_.client_qps) {
        qp->post_send_inlined(&local_contract, sizeof(local_contract),
                              IBV_WR_SEND);
        context_.poll_send_cq_until_completion();
      }
      context_.receive(static_cast<i32>(agreements.size()));
    }
    const bool all_agree =
        std::all_of(agreements.begin(), agreements.end(),
                    [](u8 agreement) { return agreement == 1; });
    lib_assert(all_agree,
               "compute nodes disagree on the LAVD startup contract");
  } else {
    configuration::LavdStartupContract authoritative{};
    LocalMemoryRegion contract_region{context_, &authoritative,
                                      sizeof(authoritative)};
    cm_.initiator_qp->post_receive(contract_region);
    context_.receive();
    const bool agrees = authoritative.version == local_contract.version &&
                        authoritative.lavd_bits == local_contract.lavd_bits &&
                        authoritative.query_contexts ==
                            local_contract.query_contexts &&
                        authoritative.region_capacity_bytes ==
                            local_contract.region_capacity_bytes;
    const u8 agreement = agrees ? 1u : 0u;
    cm_.initiator_qp->post_send_inlined(&agreement, sizeof(agreement),
                                        IBV_WR_SEND);
    context_.poll_send_cq_until_completion();
    lib_assert(agrees,
               "local LAVD startup options differ from the initiator");
  }

  if (!config.disable_thread_pinning) {
    const u32 core = core_assignment_.get_available_core();
    pin_main_thread(core);
    print_status("pinned main thread to core " + std::to_string(core));
  }

  if (cm_.is_initiator) {  // communicate parameters to the memory nodes
    configuration::Parameters p{config.num_threads,
                                static_cast<u8>(config.use_cache),
                                static_cast<u8>(config.routing),
                                static_cast<u16>(config.num_query_contexts),
                                config.lavd_bits};

    for (const QP& qp : cm_.server_qps) {
      qp->post_send_inlined(&p, sizeof(configuration::Parameters), IBV_WR_SEND);
      context_.poll_send_cq_until_completion();
      if (lavd_bits_ > 0) {
        qp->post_send_inlined(&lavd_region_capacity_bytes,
                              sizeof(lavd_region_capacity_bytes),
                              IBV_WR_SEND);
        context_.poll_send_cq_until_completion();
      }
    }
  }

  // enroll timings
  t_build_ = timing_.create_enroll("build_c0");
  t_query_ = timing_.create_enroll("query_c0");

  receive_remote_access_tokens();
  const u64 registered_region_bytes =
    lavd_bits_ > 0 ? lavd::Config::region_capacity_bytes() : 0;
  if (lavd_bits_ > 0) {
    lib_assert(registered_region_bytes == effective_region_capacity,
               "MN registered LAVD capacity differs from the CN contract");
  }
  statistics_.add_static_stat("lavd_region_requested_bytes", config.lavd_region_bytes);
  statistics_.add_static_stat("lavd_region_resolved_bytes", lavd_region_capacity_bytes);
  statistics_.add_static_stat("lavd_region_registered_bytes_per_mn",
                              registered_region_bytes);
  statistics_.add_static_stat("lavd_region_registered_bytes_total",
                              registered_region_bytes * num_servers_);
  print_status("LAVD neighborhood region: registered bytes per MN=" +
               std::to_string(registered_region_bytes));
  const bool compute_recall = not config.no_recall;
  read_dataset(config.data_path, config.query_suffix, config.load_index, compute_recall, config.use_cache);

  const u32 seed = config.seed == -1 ? std::random_device{}() : config.seed + cm_.client_id;
  hnsw::HNSW<Distance> hnsw{
    config.m, config.ef_construction, config.k, config.ef_search, seed, database_.dim, config.use_cache,
    config.spec_k};  // GraphBeyond C1: pass speculative width

  const size_t estimated_index_size = hnsw.estimate_index_size(database_.num_vectors_total);
  statistics_.add_static_stat("estimated_total_index_size", estimated_index_size);

  const size_t cache_size = static_cast<f32>(estimated_index_size) / 100. * config.cache_size_ratio;

  if (config.use_cache) {
    print_status("max cache size: " + std::to_string(cache_size));
    statistics_.add_nested_static_stat("cache", "cache_size_ratio", config.cache_size_ratio);
  }

  // ------------------------------------
  // - initialize cache and worker pool -
  // ------------------------------------

  const size_t num_cache_buckets = cache_size / Node::size_until_components();
  const size_t num_cooling_table_buckets = std::ceil(cache_size / Node::size_until_components() /
                                                     cache::COOLING_TABLE_BUCKET_ENTRIES * cache::COOLING_TABLE_RATIO);

  print_status("allocate worker threads and read buffers");
  WorkerPool worker_pool{config.num_threads,
                         config.num_query_contexts,
                         config.max_send_queue_wr,
                         cache_size,
                         num_cache_buckets,
                         num_cooling_table_buckets,
                         config.use_cache};

  worker_pool.allocate_worker_threads(
    context_, cm_, remote_access_tokens_, remote_neighborhood_tokens_, config.num_coroutines);

  statistics_.add_static_stat("allocated_local_buffer_size", COMPUTE_NODE_MAX_MEMORY);
  statistics_.add_static_stat("query_contexts", config.num_query_contexts);
  statistics_.add_static_stat("distance", config.ip_distance ? "inner_product" : "squared_l2");
  statistics_.add_static_stat("node_size", Node::size_until_components());
  statistics_.add_static_stat("neighborlist_size", Node::NEIGHBORLIST_SIZE);
  statistics_.add_static_stat("neighborlist_size_l0", Node::NEIGHBORLIST_SIZE_ZERO);
  statistics_.add_nested_static_stat("cache", "num_cache_buckets", num_cache_buckets);
  statistics_.add_nested_static_stat("cache", "num_cooling_table_buckets", num_cooling_table_buckets);

  cm_.synchronize();  // notify memory nodes that this compute node is ready

  // construct the index
  if (!config.load_index) {
    run_inserts(hnsw, worker_pool, config.num_coroutines, !config.disable_thread_pinning);
    join_threads(worker_pool.get_compute_threads());
    core_assignment_.get_available_core();  // join_t resets the assignments (but main thread's assignment remains)
    worker_pool.reset_barriers();

    // accumulate build statistics
    for (const auto& thread : worker_pool.get_compute_threads()) {
      cn_statistics_.build_distcomps += thread->stats.distcomps;
      cn_statistics_.build_rdma_reads += thread->stats.rdma_reads_in_bytes;
      cn_statistics_.build_rdma_writes += thread->stats.rdma_writes_in_bytes;
      cn_statistics_.remote_allocations += thread->stats.remote_allocations;
      cn_statistics_.total_allocation_size += thread->stats.allocation_size;
      cn_statistics_.max_level = std::max(cn_statistics_.max_level, thread->stats.max_level);
      thread->reset();
    }

    print_status("deallocate input vectors");
    database_.deallocate();
    sync_compute_nodes();  // do not send store message to memory nodes before all CNs are done constructing the index
  }

  wait_for_load_or_store(config);  // communicate to memory nodes whether to load or store the index (or do nothing)
  sync_compute_nodes();  // start with queries only if all compute nodes are done with constructing/loading the index

  // GraphBeyond LAVD Phase B2: graph is now ready (constructed or
  // loaded). The initiator CN builds the neighborhood region via
  // bulk-RDMA-READ + local parse + RDMA-WRITE. MN stays passive.
  if (lavd_bits_ > 0) {
    // GraphBeyond CWC: parse SHINE_LAVD_CWC[/_ALLSIG] once per CN
    // (initiator + non-initiators) before queries. Unset/<=1 => OFF =>
    // published-LAVD path is byte-identical.
    lavd::Config::init_cwc_from_env();
    auto* sctx = worker_pool.get_compute_threads().front()->ctx;
    const u32 m_max0 = (Node::NEIGHBORLIST_SIZE_ZERO - sizeof(u32)) / sizeof(u64);
    const u32 rr = config.lavd_rerank > 0
                     ? config.lavd_rerank
                     : std::max<u32>(config.k, config.ef_search / 2);

    // GraphBeyond reorder-not-replicate: when SHINE_LAVD_REORDER_BLOCKOF is
    // set the initiator builds the vector-clustered block region instead of
    // the replicated LAVD region (each vector stored once => ~1x memory);
    // every CN loads the block_of map + offset table after the barrier.
    const bool reorder_build = lavd::Config::reorder_build_on();

    // Only the initiator builds the neighborhood region + writes the
    // quantizer params into its header. MN stays passive (data-path QP).
    if (cm_.is_initiator) {
      if (reorder_build) {
        print_status("REORDER: building vector-clustered block region");
        lavd::Config::rb_sq8 = lavd::Config::rb_sq8_from_env();
        lavd::build_reorder_region(sctx, /*mn*/ 0u, lavd_bits_, timing_);
      } else {
      print_status("LAVD: building neighborhood region");
      const bool native_packed_build = [] {
        const char* value = std::getenv("SHINE_LAVD_NATIVE_PACKED_WRITE");
        return value && std::atoi(value) != 0;
      }();
      const bool force_multi_builder = [] {
        const char* value = std::getenv("SHINE_LAVD_MULTI_BUILDER");
        return value && std::atoi(value) != 0;
      }();
      const bool byte_budget_requested = [] {
        const char* value = std::getenv("SHINE_LAVD_BUDGET_BYTES");
        return value && *value != '\0';
      }();
      // Use the same native builder for S=1 and S>1. Otherwise the S=1
      // resource point would silently fall back to the legacy sparse path.
      const bool use_multi_builder =
          num_servers_ > 1 || native_packed_build || force_multi_builder ||
          byte_budget_requested;
      lavd::Quantizer qz_built = use_multi_builder
        ? lavd::build_neighborhood_multi(sctx, num_servers_, lavd_bits_, timing_)
        : lavd::build_neighborhood(sctx, /*mn*/ 0u, lavd_bits_, timing_);

      // Offline rematerialization control. NBEFORE partitions the final
      // authoritative graph into a base and a replayed insertion suffix;
      // queries have not started, and no epoch/concurrent-update semantics
      // are implied. The self-test invalidates and reconstructs selected
      // records before comparing the entire sidecar with fresh assembly.
      if (const char* nb = std::getenv("SHINE_LAVD_MAINT_NBEFORE"); nb && std::atol(nb) > 0) {
        const std::string_view rejection =
          lavd::offline_rematerialization_rejection(
            num_servers_, native_packed_build, reorder_build,
            lavd::varblock_on());
        lib_assert(
          rejection.empty(),
          "LAVD offline rematerialization rejected: " << rejection);
        // With a persistent mirror, later control ticks read only the tail
        // and selected authoritative records.
        int ticks = 1;
        if (const char* tk = std::getenv("SHINE_LAVD_MAINT_TICKS"); tk && std::atoi(tk) > 0)
          ticks = std::atoi(tk);
        for (int tick = 0; tick < ticks; ++tick) {
          print_status("LAVD: offline rematerialization control tick " +
                       std::to_string(tick + 1));
          lavd::offline_rematerialize_control(
            sctx, /*mn*/ 0u, lavd_bits_, qz_built,
            static_cast<u32>(std::atol(nb)), timing_, tick + 1 == ticks);
        }
      }  // end if (SHINE_LAVD_MAINT_NBEFORE)
      }  // end else (replicated LAVD build)
    }  // end if (cm_.is_initiator)

    // Barrier: every CN must wait until the initiator's build (incl. the
    // param header) is fully written before reading it.
    sync_compute_nodes();

    // EVERY CN reads the param header and inits Config identically, so
    // all CNs take the LAVD path with the same quantizer (multi-CN).
    lavd::read_params_init(sctx, /*mn*/ 0u, lavd_bits_, m_max0, rr);
    // PATCH 5 (cold-code-table fix, Track A): ensure per-uid RaBitQ codetab
    // is available on this CN before queries. No-op for the initiator (codetab
    // already filled inside build_neighborhood). Non-initiator CNs would
    // populate via bulk RDMA-read (helper currently logs a warning until the
    // multi-CN implementation lands; single-CN test matrix never hits this).
    lavd::fill_rabitq_codetab(sctx, /*mn*/ 0u);
    // reorder: also load the block_of map + offset table and wire Config
    // (every CN; mirrors read_params_init). Replaces the slot-indexed path.
    if (reorder_build) {
      lavd::reorder_load(sctx, /*mn*/ 0u);
    }
    print_status("LAVD: config ready (bits=" + std::to_string(lavd_bits_) +
                 " stride=" + std::to_string(lavd::Config::stride()) +
                 " rerank=" + std::to_string(rr) +
                 (lavd::Config::reorder_on() ? " REORDER n_blocks=" + std::to_string(lavd::Config::n_blocks) : "") + ")");

    // Structural Hub Cache (SHC): env-gated, optional. Loads a precomputed
    // hub list (offline-selected from access histogram targeting the workload)
    // into a CN-local dense array. Search bypasses RDMA for these slots.
    lavd::load_hub_cache_init(sctx, /*mn*/ 0u, lavd::Config::total_n, lavd::Config::stride());

    // CRANE: every CN builds its own CN-resident upper-level subgraph
    // cache (levels >=1, ~6% of nodes) from a one-time index-region
    // read. SHINE_CRANE unset/0 => OFF => published LAVD path
    // byte-identical. Requires LAVD (inside lavd_bits_>0).
    crane::Cfg::init_from_env();
    if (crane::Cfg::on()) {
      print_status("CRANE: building CN-resident upper-nav cache");
      if (num_servers_ > 1 ||
          lavd::authoritative_snapshot_available(num_servers_)) {
        crane::build_upper_cache_multi(sctx, num_servers_, timing_);
      } else {
        crane::build_upper_cache(sctx, /*mn*/ 0u, timing_);
      }
    }
  }

  // reset thread statistics and coroutines
  for (const auto& thread : worker_pool.get_compute_threads()) {
    thread->reset();
  }

  print_status("construct placement datastructure");
  const Placement<Distance> placement{cm_.num_total_clients, worker_pool.get_compute_threads().front(), timing_};

  // ----------------
  // - cache warmup -
  // ----------------

  if (config.use_cache && cache::CACHE_WARMUP) {
    print_status("cache warmup");
    run_query_router_and_queries(
      warmup_queries_, worker_pool, hnsw, timing_.create_enroll("warmup_routing"), placement, config);

    // reset threads and timer
    t_query_->clear();  // !!!
    warmup_queries_.deallocate();

    for (const auto& thread : worker_pool.get_compute_threads()) {
      thread->reset();
    }

    print_status("cache warmup done");
    std::cerr << "cache full?: " << worker_pool.get_compute_threads().front()->cache.is_full() << std::endl;
  }

  sync_compute_nodes();  // start with queries only if all compute nodes are done with the warmup

  // -------------------
  // - process queries -
  // -------------------

  print_status("run queries");
  if (hnsw::query_latency_enabled()) {
    const size_t per_thread_samples =
      statistics::latency_sample_reserve_per_thread(
        queries_.num_vectors_total, worker_pool.get_compute_threads().size());
    for (const auto& thread : worker_pool.get_compute_threads()) {
      thread->query_latency_ns.reserve(per_thread_samples);
    }
  }
  // Structural-hub profile: count beam-pop accesses per L0 slot (env-gated).
  if (lavd_bits_ > 0) hnsw::hub_profile_init(database_.num_vectors_total);
  run_query_router_and_queries(queries_, worker_pool, hnsw, timing_.create_enroll("routing"), placement, config);
  if (lavd_bits_ > 0) {
    char hp_path[256];
    snprintf(hp_path, sizeof(hp_path), "/tmp/hub_access_%s.bin",
             config.label.empty() ? "unlabeled" : config.label.c_str());
    hnsw::hub_profile_dump(hp_path);
    if (lavd::g_hub_cache.active()) {
      const u64 hits = lavd::g_hub_cache.hits.load();
      std::cerr << "[HUB_CACHE] n_hubs=" << lavd::g_hub_cache.n_hubs
                << " hits=" << hits << std::endl;
    }
  }

  size_t processed_queries = 0;
  for (const auto& t : worker_pool.get_compute_threads()) {
    processed_queries += t->stats.processed;
  }
  print_status("processed queries: " + std::to_string(processed_queries));

  std::vector<evidence::QueryResult> local_query_results;
  local_query_results.reserve(processed_queries);
  for (const auto& thread : worker_pool.get_compute_threads()) {
    for (const auto& [query_id, neighbors] : thread->query_results) {
      evidence::QueryResult result;
      result.query_id = static_cast<std::uint32_t>(query_id);
      result.neighbors.assign(neighbors.begin(), neighbors.end());
      local_query_results.push_back(std::move(result));
    }
  }
  const auto result_fingerprint =
      evidence::fingerprint_query_results(std::move(local_query_results));
  lib_assert(result_fingerprint.valid &&
                 result_fingerprint.queries == processed_queries,
             "query-result fingerprint does not cover the processed pool");
  statistics_.add_nested_static_stat(
      "queries", "local_result_hash", result_fingerprint.hash);
  statistics_.add_nested_static_stat(
      "queries", "local_result_hash_version",
      evidence::QUERY_RESULT_FINGERPRINT_VERSION);

  if (compute_recall) {
    const f64 local_recall = compute_local_recall(worker_pool.get_compute_threads(), config.k, processed_queries);
    print_status("local recall: " + std::to_string(local_recall));

    // distribute recalls to initiator and compute rolling recall:
    // sum_i^n recall_i * (num_queries_processed_i / num_queries_total)
    cn_statistics_.rolling_recall = local_recall * (static_cast<f64>(processed_queries) / queries_.num_vectors_total);
  }

  // accumulate query statistics
  vec<size_t> local_read_bytes_per_mn(num_servers_, 0);
  vec<size_t> local_read_wrs_per_mn(num_servers_, 0);
  vec<size_t> local_read_submits_per_mn(num_servers_, 0);
  vec<u64> local_query_latency_ns;
  local_query_latency_ns.reserve(processed_queries);
  for (const auto& thread : worker_pool.get_compute_threads()) {
    cn_statistics_.query_distcomps += thread->stats.distcomps;
    cn_statistics_.query_rdma_reads += thread->stats.rdma_reads_in_bytes;
    cn_statistics_.query_rdma_writes += thread->stats.rdma_writes_in_bytes;
    cn_statistics_.query_cache_hits += thread->stats.cache_hits;
    cn_statistics_.query_cache_misses += thread->stats.cache_misses;
    cn_statistics_.query_visited_nodes += thread->stats.visited_nodes;
    cn_statistics_.query_visited_nodes_l0 += thread->stats.visited_nodes_l0;
    cn_statistics_.query_visited_neighborlists += thread->stats.visited_neighborlists;
    cn_statistics_.rdma_posts += thread->stats.rdma_posts;
    cn_statistics_.rdma_wrs += thread->stats.rdma_wrs;
    cn_statistics_.rdma_cqes += thread->stats.rdma_cqes;
    cn_statistics_.cwc_batches += thread->stats.cwc_batches;
    cn_statistics_.cwc_batched_reads += thread->stats.cwc_batched_reads;
    cn_statistics_.posts_upnav += thread->stats.posts_upnav;
    cn_statistics_.posts_l0 += thread->stats.posts_l0;
    cn_statistics_.posts_rerank += thread->stats.posts_rerank;
    cn_statistics_.rerank_chunks += thread->stats.rerank_chunks;
    lib_assert(thread->read_bytes_per_mn.size() == num_servers_,
               "per-MN read accounting width mismatch");
    for (u32 mn = 0; mn < num_servers_; ++mn) {
      local_read_bytes_per_mn[mn] += thread->read_bytes_per_mn[mn];
      local_read_wrs_per_mn[mn] += thread->read_wrs_per_mn[mn];
      local_read_submits_per_mn[mn] += thread->read_submits_per_mn[mn];
    }
    local_query_latency_ns.insert(
      local_query_latency_ns.end(), thread->query_latency_ns.begin(),
      thread->query_latency_ns.end());
    thread->reset();
  }
  if (!local_query_latency_ns.empty()) {
    lib_assert(local_query_latency_ns.size() == processed_queries,
               "query-latency sample count does not match processed queries");
    statistics_.add_nested_static_stat(
      "queries", "local_latency_samples", local_query_latency_ns.size());
    std::sort(local_query_latency_ns.begin(), local_query_latency_ns.end());
    statistics_.add_nested_static_stat(
      "queries", "local_latency_p50_us",
      statistics::nearest_rank_quantile_sorted(local_query_latency_ns, 0.50) / 1000.0);
    statistics_.add_nested_static_stat(
      "queries", "local_latency_p95_us",
      statistics::nearest_rank_quantile_sorted(local_query_latency_ns, 0.95) / 1000.0);
    statistics_.add_nested_static_stat(
      "queries", "local_latency_p99_us",
      statistics::nearest_rank_quantile_sorted(local_query_latency_ns, 0.99) / 1000.0);
  }
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_bytes_per_mn", local_read_bytes_per_mn);
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_wrs_per_mn", local_read_wrs_per_mn);
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_submits_per_mn", local_read_submits_per_mn);
  const auto gini = [](vec<size_t> values) -> f64 {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    long double weighted = 0.0;
    long double total = 0.0;
    for (size_t i = 0; i < values.size(); ++i) {
      const long double coefficient =
          2.0L * static_cast<long double>(i + 1) -
          static_cast<long double>(values.size()) - 1.0L;
      weighted += coefficient * static_cast<long double>(values[i]);
      total += values[i];
    }
    return total == 0.0
             ? 0.0
             : static_cast<f64>(weighted /
                                (values.size() * total));
  };
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_bytes_gini", gini(local_read_bytes_per_mn));
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_wrs_gini", gini(local_read_wrs_per_mn));
  statistics_.add_nested_static_stat(
    "queries", "local_cn_read_submits_gini",
    gini(local_read_submits_per_mn));
  size_t accounted_bytes = 0;
  size_t accounted_wrs = 0;
  size_t accounted_submits = 0;
  for (u32 mn = 0; mn < num_servers_; ++mn) {
    accounted_bytes += local_read_bytes_per_mn[mn];
    accounted_wrs += local_read_wrs_per_mn[mn];
    accounted_submits += local_read_submits_per_mn[mn];
  }
  lib_assert(accounted_bytes == cn_statistics_.query_rdma_reads,
             "per-MN read bytes do not sum to aggregate bytes");
  lib_assert(accounted_wrs == cn_statistics_.rdma_wrs,
             "per-MN read WRs do not sum to aggregate WRs");
  lib_assert(accounted_submits == cn_statistics_.rdma_posts,
             "per-MN read submits do not sum to aggregate posts");

  cn_statistics_.processed_queries = processed_queries;
  cn_statistics_.local_allocation_size += worker_pool.get_buffer_allocator().allocated_memory();
  cn_statistics_.build_time = t_build_->time_;
  cn_statistics_.query_time = t_query_->time_;
  collect_statistics_and_timings();

  worker_pool.track_local_cache_statistics(statistics_);

  if (cm_.is_initiator) {
    add_meta_statistics(config);
    statistics_.add_static_stat("num_vectors", database_.num_vectors_total);
    statistics_.add_static_stat("num_queries", queries_.num_vectors_total);
    statistics_.add_nested_static_stat("queries", "compute_recall", compute_recall ? "true" : "false");
    statistics_.output_all(timing_.to_json());

  } else {
    std::cerr << timing_ << std::endl;
  }
}

template <class Distance>
void ComputeNode<Distance>::run_query_router_and_queries(io::Database<element_t>& queries,
                                                         WorkerPool& worker_pool,
                                                         hnsw::HNSW<Distance>& hnsw,
                                                         timing::Timing::IntervalPtr&& routing_timer,
                                                         const Placement<Distance>& placement,
                                                         const Configuration& config) {
  // -----------------------------
  // - initialize and run router -
  // -----------------------------

  query_router::QueryRouter query_router{placement,
                                         queries,
                                         context_,
                                         cm_.server_qps,
                                         cm_.num_total_clients,
                                         config.max_send_queue_wr,
                                         cm_.client_id,
                                         std::move(routing_timer)};

  const auto routing_thread = std::make_unique<Thread>(-1);
  if (cm_.num_total_clients > 1 && config.routing) {
    if (cm_.is_initiator) {
      // pass query routing message size to memory nodes
      for (u32 memory_node = 0; memory_node < num_servers_; ++memory_node) {
        const QP& qp = cm_.server_qps[memory_node];

        qp->post_send_inlined(&query_router.message_size, sizeof(size_t), IBV_WR_SEND);
        context_.poll_send_cq_until_completion();
      }
    }

    cm_.synchronize();  // wait until all MNs are ready
    routing_thread->start(&query_router::QueryRouter<Distance>::run_routing, &query_router);

  } else {  // no query routing with just one compute node
    query_router.done = true;
    query_router.queue_size += static_cast<i32>(queries.max_slot);

    for (idx_t slot = 0; slot < queries.max_slot; ++slot) {
      query_router.query_queue.enqueue(slot);
    }
  }

  // -------------------
  // - process queries -
  // -------------------

  run_queries(hnsw, worker_pool, queries, query_router, config.num_coroutines, !config.disable_thread_pinning);
  join_threads(worker_pool.get_compute_threads());
  routing_thread->join();

  core_assignment_.get_available_core();  // join_thread resets the assignments (but main thread's assignment remains)
  worker_pool.reset_barriers();

  terminate();  // notify memory nodes that we are done
}

template <class Distance>
void ComputeNode<Distance>::init_remote_tokens() {
  remote_access_tokens_.resize(num_servers_);

  for (auto& mrt : remote_access_tokens_) {
    mrt = std::make_unique<MemoryRegionToken>();  // ownership has the vector
  }

  // GraphBeyond LAVD: parallel token slots for the 2nd region. Always
  // sized (SharedContext takes a ref); only *filled* when --lavd > 0.
  remote_neighborhood_tokens_.resize(num_servers_);
  for (auto& mrt : remote_neighborhood_tokens_) {
    mrt = std::make_unique<MemoryRegionToken>();
  }
}

template <class Distance>
void ComputeNode<Distance>::receive_remote_access_tokens() {
  print_status("receive access tokens of remote memory regions");
  u64 registered_neighborhood_bytes = 0;
  for (u32 memory_node = 0; memory_node < num_servers_; ++memory_node) {
    const QP& qp = cm_.server_qps[memory_node];
    MRT& mrt = remote_access_tokens_[memory_node];

    LocalMemoryRegion token_region{context_, mrt.get(), sizeof(MemoryRegionToken)};
    qp->post_receive(token_region);
    context_.receive();

    // LAVD adds a neighborhood token and an 8-byte registered-capacity
    // message after the unchanged index token. With LAVD off, the baseline
    // message sequence remains index-token only.
    if (lavd_bits_ > 0) {
      MRT& nbh = remote_neighborhood_tokens_[memory_node];
      LocalMemoryRegion nbh_region{context_, nbh.get(), sizeof(MemoryRegionToken)};
      qp->post_receive(nbh_region);
      context_.receive();
      u64 remote_capacity_bytes = 0;
      LocalMemoryRegion capacity_region{context_, &remote_capacity_bytes,
                                        sizeof(remote_capacity_bytes)};
      qp->post_receive(capacity_region);
      context_.receive();
      lib_assert(
        lavd::is_valid_region_capacity_bytes(remote_capacity_bytes,
          lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES),
        "MN returned invalid LAVD neighborhood region capacity");
      if (registered_neighborhood_bytes == 0) {
        registered_neighborhood_bytes = remote_capacity_bytes;
      } else {
        lib_assert(registered_neighborhood_bytes == remote_capacity_bytes,
                   "LAVD neighborhood region capacities differ across MNs");
      }
    }
  }
  lavd::Config::set_region_capacity_bytes(
    lavd_bits_ > 0 ? registered_neighborhood_bytes : 0);
}

/**
 * @brief Reads base vectors (partially), query vectors (partially), and ground truth into Database objects.
 * @param data_path Filepath to the location of `base.fvecs` and the `query` directory.
 * @param query_suffix Filename suffix of the query file.
 * @param load_index If true, index will be read from the memory servers, no need to read it from file (only meta data).
 * @param include_groundtruth If true, also the ground truth will be loaded.
 */
template <class Distance>
void ComputeNode<Distance>::read_dataset(const filepath_t& data_path,
                                         const str& query_suffix,
                                         bool load_index,
                                         bool include_groundtruth,
                                         bool use_cache) {
  filepath_t data_file, query_file, ground_truth_file, warmup_file;

  for (const auto& file : std::filesystem::directory_iterator(data_path)) {
    if (file.path().stem() == "base") {
      data_file = file.path();
      break;
    }
  }

  auto query_path = data_path;
  query_path /= "queries/";

  for (const auto& file : std::filesystem::directory_iterator(query_path)) {
    if (file.path().stem() == str{"query-"} + query_suffix) {
      query_file = file.path();
    } else if (file.path().stem() == str{"groundtruth-"} + query_suffix) {
      ground_truth_file = file.path();
    } else if (file.path().stem() == str{"warmup-"} + query_suffix) {
      warmup_file = file.path();
    }
  }

  lib_assert(not data_file.empty() && not query_file.empty(), "base or query file missing");

  io::read_data_partially<element_t>(database_, data_file, cm_.client_id, cm_.num_total_clients, load_index);
  io::read_data_partially<element_t>(queries_, query_file, cm_.client_id, cm_.num_total_clients);

  if (use_cache && cache::CACHE_WARMUP) {
    lib_assert(not warmup_file.empty(), "warmup file missing");
    io::read_data_partially<element_t>(warmup_queries_, warmup_file, cm_.client_id, cm_.num_total_clients);
  }

  if (include_groundtruth) {
    lib_assert(!ground_truth_file.empty(), "ground truth file missing");
    io::read_data<node_t>(ground_truth_, ground_truth_file);  // read entirely
  }
}

template <class Distance>
void ComputeNode<Distance>::run_inserts(hnsw::HNSW<Distance>& hnsw,
                                        WorkerPool& worker_pool,
                                        u32 num_coroutines,
                                        bool pin_threads) {
  print_status("**INSERT**: running worker threads...");

  for (const auto& t : worker_pool.get_compute_threads()) {
    const u32 thread_id = t->get_id();

    if (thread_id != 0) {
      t->start(&WorkerPool::process_inserts<Distance>,
               &worker_pool,
               std::ref(hnsw),
               std::ref(next_insert_idx_),
               std::ref(database_),
               num_coroutines);

      if (pin_threads) {
        const u32 core = core_assignment_.get_available_core();
        t->set_affinity(core);
        print_status("pinned thread " + std::to_string(thread_id) + " to core " + std::to_string(core));
      }
    }
  }

  // main thread now is also a worker thread and will release the barrier
  t_build_->start();
  worker_pool.process_inserts<Distance>(hnsw, next_insert_idx_, database_, num_coroutines, 0);
  t_build_->stop();
}

template <class Distance>
void ComputeNode<Distance>::run_queries(hnsw::HNSW<Distance>& hnsw,
                                        WorkerPool& worker_pool,
                                        io::Database<element_t>& queries,
                                        query_router::QueryRouter<Distance>& query_router,
                                        u32 num_coroutines,
                                        bool pin_threads) {
  print_status("**QUERY**: running worker threads...");

  for (const auto& t : worker_pool.get_compute_threads()) {
    const u32 thread_id = t->get_id();

    if (thread_id != 0) {
      t->start(&WorkerPool::process_queries<Distance>,
               &worker_pool,
               std::ref(hnsw),
               std::ref(next_query_idx_),
               std::ref(queries),
               std::ref(query_router),
               num_coroutines);

      if (pin_threads) {
        const u32 core = core_assignment_.get_available_core();
        t->set_affinity(core);
        print_status("pinned thread " + std::to_string(thread_id) + " to core " + std::to_string(core));
      }
    }
  }

  // main thread now is also a worker thread and will release the barrier
  t_query_->start();
  worker_pool.process_queries<Distance>(hnsw, next_query_idx_, queries, query_router, num_coroutines, 0);
  t_query_->stop();
}

template <class Distance>
void ComputeNode<Distance>::join_threads(const ComputeThreads& compute_threads) {
  print_status("join compute threads");

  for (const auto& t : compute_threads) {
    // no need for joining the main thread
    if (t->get_id() != 0) {
      t->join();
    }

    // check post balances of coroutines
    for (auto& post_balance : t->post_balances) {
      lib_assert(post_balance == 0, "incomplete READs");
    }

    std::cerr << "t" << t->get_id() << " processed: " << t->stats.processed << ", CHR: " << t->stats.cache_hit_rate()
              << std::endl;
  }

  core_assignment_.reset();
}

/**
 * @brief Send instructions to all memory servers whether to load or store the index (or do nothing).
 *        If load or store: send an individual filepath to each memory server.
 *        Finally, wait until the memory servers are done and send their responses.
 */
template <class Distance>
void ComputeNode<Distance>::wait_for_load_or_store(const Configuration& config) {
  struct Message {
    bool load;
    size_t path_length;
  };

  if (cm_.is_initiator) {
    const size_t num_memory_servers = cm_.server_qps.size();

    // send instructions to all memory servers
    if (config.store_index || config.load_index) {  // both cannot be true
      for (idx_t i = 0; i < num_memory_servers; ++i) {
        filepath_t path = config.data_path;
        path /= "dump/index_m" + std::to_string(config.m) + "_efc" + std::to_string(config.ef_construction) + "_node" +
                std::to_string(i + 1) + "_of" + std::to_string(num_memory_servers) + ".dat";

        const Message msg{config.load_index, path.string().size()};
        const QP& qp = cm_.server_qps[i];

        qp->post_send_inlined(&msg, sizeof(Message), IBV_WR_SEND);
        qp->post_send_inlined(path.string().data(), path.string().size(), IBV_WR_SEND);
      }

      context_.poll_send_cq_until_completion(static_cast<i32>(2 * num_memory_servers));

    } else {  // send null message to indicate that nothing must be done
      for (const auto& qp : cm_.server_qps) {
        constexpr Message msg{false, 0};
        qp->post_send_inlined(&msg, sizeof(Message), IBV_WR_SEND);
      }

      context_.poll_send_cq_until_completion(static_cast<i32>(num_memory_servers));
    }

    const bool success = cm_.synchronize();  // wait until memory nodes respond
    lib_assert(success, "index loading failed");
  }
}

template <class Distance>
void ComputeNode<Distance>::sync_compute_nodes() {
  print_status("synchronize compute nodes");

  if (cm_.is_initiator) {
    bool sync;  // dummy region
    LocalMemoryRegion region{context_, &sync, sizeof(bool)};

    for (const QP& qp : cm_.client_qps) {
      qp->post_receive(region);
    }

    context_.receive(cm_.num_total_clients - 1);

  } else {
    constexpr bool sync = true;  // dummy value

    cm_.initiator_qp->post_send_inlined(&sync, sizeof(bool), IBV_WR_SEND);
    context_.poll_send_cq_until_completion();
  }
}

template <class Distance>
void ComputeNode<Distance>::add_meta_statistics(const Configuration& config) {
  const auto path_name = config.data_path.has_stem() ? config.data_path.stem() : config.data_path.parent_path().stem();
  auto zipf_parameter = config.query_suffix.substr(1, config.query_suffix.find_first_of('-') - 1);

  statistics_.add_meta_stats(
    std::make_pair("compute_nodes", cm_.num_total_clients),
    std::make_pair("memory_nodes", num_servers_),
    std::make_pair("compute_threads", cm_.num_total_clients * config.num_threads),
    std::make_pair("coroutines_per_thread", config.num_coroutines),
    std::make_pair("threads_pinned", config.disable_thread_pinning ? "false" : "true"),
    std::make_pair("hyperthreading", core_assignment_.hyperthreading_enabled() ? "true" : "false"),
    std::make_pair("dataset", path_name),
    std::make_pair("query_suffix", config.query_suffix),
    std::make_pair("zipf_parameter", zipf_parameter),
    std::make_pair("timestamp", timing::get_timestamp()),
    std::make_pair("label", config.label));

  statistics_.add_nested_static_stat("hnsw_parameters", "k", config.k);
  statistics_.add_nested_static_stat("hnsw_parameters", "m", config.m);
  statistics_.add_nested_static_stat("hnsw_parameters", "ef_search", config.ef_search);
  statistics_.add_nested_static_stat("hnsw_parameters", "ef_construction", config.ef_construction);
}

template <class Distance>
void ComputeNode<Distance>::collect_statistics_and_timings() {
  print_status("collect statistics and timings from compute nodes");
  const auto chr = [](size_t hits, size_t misses) { return static_cast<f64>(hits) / static_cast<f64>(hits + misses); };

  if (cm_.is_initiator) {
    timespec max_build_time = t_build_->time_;
    timespec max_query_time = t_query_->time_;

    // add local statistics
    statistics_.add_nested_static_stat(
      "cache", "local_hit_rates", "c0", chr(cn_statistics_.query_cache_hits, cn_statistics_.query_cache_misses));
    statistics_.add_nested_static_stat("queries", "processed_local", "c0", cn_statistics_.processed_queries);

    for (u32 client_id = 1; client_id < cm_.num_total_clients; ++client_id) {
      CNStatistics received_stats{};
      LocalMemoryRegion region(context_, &received_stats, sizeof(CNStatistics));

      const QP& qp = cm_.client_qps[client_id - 1];
      qp->post_receive(region);
      context_.receive();

      cn_statistics_.combine(received_stats);

      // add build and query timings of other compute nodes
      const auto build_interval = timing_.create_enroll("build_c" + std::to_string(client_id));
      const auto query_interval = timing_.create_enroll("query_c" + std::to_string(client_id));

      build_interval->time_ = received_stats.build_time;
      query_interval->time_ = received_stats.query_time;

      if (build_interval->get_ms() > timing::Timing::get_ms(max_build_time)) {
        max_build_time = build_interval->time_;
      }
      if (query_interval->get_ms() > timing::Timing::get_ms(max_query_time)) {
        max_query_time = query_interval->time_;
      }

      // add local statistics
      statistics_.add_nested_static_stat("cache",
                                         "local_hit_rates",
                                         'c' + std::to_string(client_id),
                                         chr(received_stats.query_cache_hits, received_stats.query_cache_misses));
      statistics_.add_nested_static_stat(
        "queries", "processed_local", 'c' + std::to_string(client_id), received_stats.processed_queries);
    }

    cn_statistics_.convert(statistics_);
    const auto t_build_max = timing_.create_enroll("build_max");
    const auto t_query_max = timing_.create_enroll("query_max");
    t_build_max->time_ = max_build_time;
    t_query_max->time_ = max_query_time;

    const f64 query_time = t_query_max->get_ms() / 1000.0;  // in sec
    statistics_.add_nested_static_stat(
      "queries", "queries_per_sec", static_cast<u64>(queries_.num_vectors_total / query_time));
    statistics_.add_nested_static_stat(
      "cache", "hit_rate", chr(cn_statistics_.query_cache_hits, cn_statistics_.query_cache_misses));

  } else {
    cm_.initiator_qp->post_send_inlined(&cn_statistics_, sizeof(CNStatistics), IBV_WR_SEND);
    context_.poll_send_cq_until_completion();
  }
}

template <class Distance>
void ComputeNode<Distance>::terminate() {
  constexpr bool done = true;  // dummy value

  // notify memory nodes
  for (const QP& qp : cm_.server_qps) {
    qp->post_send_inlined(&done, sizeof(bool), IBV_WR_SEND);
  }

  context_.poll_send_cq_until_completion(static_cast<i32>(num_servers_));
}

template <class Distance>
f64 ComputeNode<Distance>::compute_local_recall(const ComputeThreads& compute_threads,
                                                u32 k,
                                                size_t processed_queries) {
  u32 true_results = 0;

  for (const auto& thread : compute_threads) {
    for (const auto& [q_id, result] : thread->query_results) {
      for (const node_t hit : result) {
        // we use q_id because ground_truth is always fully read
        for (const node_t positive : ground_truth_.get_components(q_id).subspan(0, k)) {
          if (hit == positive) {
            ++true_results;
            break;
          }
        }
      }
    }
  }

  const f64 recall = true_results / static_cast<f64>(processed_queries) / k;
  return recall;
}

// explicitly initiate templates
template ComputeNode<L2Distance>::ComputeNode(Configuration& config);
template ComputeNode<IPDistance>::ComputeNode(Configuration& config);
