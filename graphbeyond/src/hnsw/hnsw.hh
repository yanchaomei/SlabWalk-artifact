#pragma once

#include <common/constants.hh>
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <mutex>
#include <queue>
#include <random>
#include <vector>

#include "cache/cache.hh"
#include "crane/crane.hh"
#include "common/debug.hh"
#include "common/types.hh"
#include "compute_thread.hh"
#include "distance.hh"
#include "heap.hh"
#include "node/neighborlist.hh"
#include "node/node.hh"
#include "node/reorder_block.hh"
#include "rdma/rdma_operations.hh"
#include "remote_pointer.hh"

namespace hnsw {

// =====================================================================
// L0 access histogram (env-gated: GB_HUB_PROFILE=1).
// For each beam pop in search_level_lavd, increment g_l0_access[slot].
// At end of query phase, dump (slot, access_count) pairs to a file so
// we can analyze access concentration (top-x% slots vs % of total reads).
// Used to evaluate whether a CN-local structural hub fat-block cache
// (SHC) would have meaningful hit rate vs SHINE's brittle workload-driven
// cache. Recall: SHINE's cache fails under uniform queries because it
// chases query-driven reuse, not structural hub reuse.
// =====================================================================
inline std::unique_ptr<std::atomic<u32>[]> g_l0_access;
inline u32 g_l0_access_n = 0;

inline bool hub_profile_on() {
  static const bool on = std::getenv("GB_HUB_PROFILE") && std::atoi(std::getenv("GB_HUB_PROFILE")) != 0;
  return on;
}

inline void hub_profile_init(u32 N) {
  if (!hub_profile_on()) return;
  g_l0_access = std::make_unique<std::atomic<u32>[]>(N);
  g_l0_access_n = N;
  for (u32 i = 0; i < N; ++i) g_l0_access[i].store(0);
  std::cerr << "[HUB_PROFILE] init N=" << N << std::endl;
}

inline void hub_profile_dump(const char* path) {
  if (!hub_profile_on() || g_l0_access_n == 0) return;
  std::ofstream out(path, std::ios::binary);
  out.write(reinterpret_cast<const char*>(&g_l0_access_n), sizeof(g_l0_access_n));
  u64 total = 0;
  for (u32 i = 0; i < g_l0_access_n; ++i) {
    u32 v = g_l0_access[i].load();
    out.write(reinterpret_cast<const char*>(&v), sizeof(v));
    total += v;
  }
  std::cerr << "[HUB_PROFILE] dumped " << g_l0_access_n << " slots, total_accesses=" << total
            << " to " << path << std::endl;
}

// =====================================================================
// Phase F rerank tap (env-gated: GB_PHASEF_LOG=<path>).
// For each query, after the top-R fp32 rerank list is finalized, emit one
// CSV line per candidate: q_id,rank,slot,rptr (rank is 0-indexed position
// within the top-R survivor list, ordered by exact distance).
// Pure observability: write-only, does not alter beam evolution or output.
// Stream is opened lazily per-thread in append mode and reused.
// =====================================================================
inline const char* phasef_log_path() {
  static const char* p = std::getenv("GB_PHASEF_LOG");
  return (p && *p) ? p : nullptr;
}

inline std::ofstream& phasef_log_stream() {
  thread_local std::ofstream s;
  if (!s.is_open()) {
    const char* path = phasef_log_path();
    if (path) {
      s.open(path, std::ios::out | std::ios::app);
    }
  }
  return s;
}

// Query-level latency and operation trace (env-gated: GB_QUERY_TRACE=<path>).
// Emits one CSV row per completed query. The query-local post columns are
// coroutine-owned; thread-delta columns are most exact with one coroutine/thread.
inline const char* query_trace_path() {
  static const char* p = std::getenv("GB_QUERY_TRACE");
  return (p && *p) ? p : nullptr;
}

inline bool query_latency_enabled() {
  static const bool enabled = [] {
    const char* value = std::getenv("GB_QUERY_LATENCY");
    return value && std::atoi(value) != 0;
  }();
  return enabled;
}

struct QueryTrace {
  bool on{false};
  node_t q_id{};
  u32 thread_id{};
  std::chrono::steady_clock::time_point t0{};
  size_t rdma_posts{0};
  size_t rdma_wrs{0};
  size_t rdma_reads_in_bytes{0};
  size_t distcomps{0};
  size_t visited_nodes{0};
  size_t visited_nodes_l0{0};
  size_t visited_neighborlists{0};
  size_t cache_hits{0};
  size_t cache_misses{0};
  size_t cwc_batches{0};
  size_t cwc_batched_reads{0};
};

inline QueryTrace query_trace_begin(node_t q_id, const u_ptr<ComputeThread>& thread) {
  if (!query_trace_path() && !query_latency_enabled()) return {};
  const auto& s = thread->stats;
  return QueryTrace{true,
                    q_id,
                    thread->get_id(),
                    std::chrono::steady_clock::now(),
                    s.rdma_posts,
                    s.rdma_wrs,
                    s.rdma_reads_in_bytes,
                    s.distcomps,
                    s.visited_nodes,
                    s.visited_nodes_l0,
                    s.visited_neighborlists,
                    s.cache_hits,
                    s.cache_misses,
                    s.cwc_batches,
                    s.cwc_batched_reads};
}

inline std::mutex& query_trace_mutex() {
  static std::mutex m;
  return m;
}

inline std::ofstream& query_trace_stream() {
  static std::ofstream s;
  if (!s.is_open()) {
    const char* path = query_trace_path();
    if (path) {
      s.open(path, std::ios::out | std::ios::app);
    }
  }
  return s;
}

inline void query_trace_finish(const QueryTrace& qt, const u_ptr<ComputeThread>& thread) {
  if (!qt.on) return;

  const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::steady_clock::now() - qt.t0).count();
  if (query_latency_enabled()) {
    thread->query_latency_ns.push_back(static_cast<u64>(elapsed));
  }
  if (!query_trace_path()) return;
  const auto& s = thread->stats;
  const auto& cc = thread->current_coroutine();
  const f64 query_posts = static_cast<f64>(cc.q_trace_posts) + cc.q_trace_cwc_posts;

  std::lock_guard<std::mutex> lock(query_trace_mutex());
  std::ofstream& out = query_trace_stream();
  if (!out.is_open()) return;

  static bool header_written = false;
  if (!header_written) {
    out << "q_id,thread_id,latency_ns,query_rdma_posts,query_single_posts,"
           "query_cwc_post_share,query_cwc_batched_reads,query_phase_upnav,"
           "query_phase_l0,query_phase_rerank,thread_rdma_posts_delta,"
           "thread_rdmabytes_delta,thread_distcomps_delta,"
           "thread_visited_nodes_delta,thread_visited_l0_delta,"
           "thread_visited_neighborlists_delta,thread_cache_hits_delta,"
           "thread_cache_misses_delta,thread_cwc_batches_delta,"
           "thread_cwc_batched_reads_delta,query_rdma_wrs,"
           "thread_rdma_wrs_delta\n";
    header_written = true;
  }

  out << qt.q_id << ',' << qt.thread_id << ',' << elapsed << ',' << query_posts << ','
      << cc.q_trace_posts << ',' << cc.q_trace_cwc_posts << ','
      << cc.q_trace_cwc_batched_reads << ',' << cc.ph_posts[0] << ','
      << cc.ph_posts[1] << ',' << cc.ph_posts[2] << ','
      << (s.rdma_posts - qt.rdma_posts) << ','
      << (s.rdma_reads_in_bytes - qt.rdma_reads_in_bytes) << ','
      << (s.distcomps - qt.distcomps) << ','
      << (s.visited_nodes - qt.visited_nodes) << ','
      << (s.visited_nodes_l0 - qt.visited_nodes_l0) << ','
      << (s.visited_neighborlists - qt.visited_neighborlists) << ','
      << (s.cache_hits - qt.cache_hits) << ','
      << (s.cache_misses - qt.cache_misses) << ','
      << (s.cwc_batches - qt.cwc_batches) << ','
      << (s.cwc_batched_reads - qt.cwc_batched_reads) << ','
      << (cc.ph_posts[0] + cc.ph_posts[1] + cc.ph_posts[2]) << ','
      << (s.rdma_wrs - qt.rdma_wrs) << '\n';
}

template <class Distance>
class HNSW {
private:
  enum Lock { with_lock = true, without_lock = false };

public:
  HNSW(u32 m, u32 ef_construction, u32 k, u32 ef_search, u32 seed, u32 dim, bool use_cache,
       u32 spec_k = 1)
      : m_(m),
        m_max_(m),
        m_max_zero_(m * 2),
        ef_construction_(ef_construction),
        normalization_factor_(1. / std::log(static_cast<f64>(m))),
        k_(k),
        ef_search_(ef_search),
        use_cache_(use_cache),
        spec_k_(spec_k == 0 ? 1 : spec_k),
        prng_(seed),
        uniform_(0., 1.) {
    lib_assert(ef_search_ >= k_, "ef_search must be >= k");
    Node::init_static_storage(dim, m_max_, m_max_zero_);
  }

  HNSWCoroutine insert(node_t id, const span<element_t> components, const u_ptr<ComputeThread>& thread) {
    dbg::print(dbg::stream{} << "T" << thread->get_id() << " inserts " << id << "\n--------------\n\n");
    ++thread->stats.processed;

    /**
     * Draw level: Note that with `m_L` = normalization_factor = 1/ln(M) (as suggested in the paper) the probability
     *             of inserting the node at level l is just 1/M^l or more generally, e^-(1/m_L * l).
     */
    u32 new_node_level = std::floor(-std::log(uniform_(prng_)) * normalization_factor_);
    bool allocated = false;

    RemotePtr new_node_ptr;
    s_ptr<Node> new_node;

    auto& cached_ep_ptr = thread->current_coroutine().cached_ep_ptr;

    if (cached_ep_ptr.is_null()) {
      cached_ep_ptr = co_await rdma::read_entry_point_ptr(thread);

      // if still null, index not yet initialized
      if (cached_ep_ptr.is_null()) {
        new_node_level = 0;
        new_node_ptr = co_await rdma::allocate_node(new_node_level, thread);
        new_node = co_await rdma::write_node(new_node_ptr, id, components, new_node_level, false, false, true, thread);

        allocated = true;
        const RemotePtr old_ep_ptr = cached_ep_ptr;

        // try to compare-and-swap ep pointer
        cached_ep_ptr = co_await rdma::swap_entry_point_ptr(old_ep_ptr, new_node_ptr, thread);
        if (cached_ep_ptr == old_ep_ptr) {
          // success: the index is now initialized, set the is_entry_node bit
          co_await rdma::write_header(new_node_ptr, true, false, false, thread);
          cached_ep_ptr = new_node_ptr;  // fix cache

          dbg::print(dbg::stream{} << "T" << thread->get_id() << " allocated " << new_node_ptr
                                   << " and set first EP\n");
          co_return;
        }

        // failure: the index has been initilized by a thread that won the race

        dbg::print(dbg::stream{} << "T" << thread->get_id() << " allocated " << new_node_ptr
                                 << " but failed CAS, another T initialized the index\n");
      }
    }

    // READ and LOCK entry point
    s_ptr<Node> entry_point = co_await rdma::read_node(cached_ep_ptr, thread);
    {
      auto coro = rdma::lock_and_update_entry_point(cached_ep_ptr, entry_point, thread);

      while (!coro.handle.done()) {
        co_await std::suspend_always{};  // gives control back to caller of insert
        coro.handle.resume();
      }
    }

    dbg::print(dbg::stream{} << "T" << thread->get_id() << " successfully locked entry point " << *entry_point << "\n");

    const u32 top_level = entry_point->level();
    const bool is_new_level = new_node_level > top_level;

    if (!is_new_level) {
      co_await rdma::unlock_new_level_lock(entry_point, thread);  // release global lock
    } else {
      new_node_level = top_level + 1;  // make sure to not overshoot
      dbg::print(dbg::stream{} << "T" << thread->get_id() << " will set new level " << new_node_level << "\n");
    }

    thread->stats.max_level = std::max(thread->stats.max_level, new_node_level);

    // we allocate at this point, because now we know the new level
    if (!allocated) {
      new_node_ptr = co_await rdma::allocate_node(new_node_level, thread);
      new_node = co_await rdma::write_node(new_node_ptr, id, components, new_node_level, false, false, true, thread);

      dbg::print(dbg::stream{} << "T" << thread->get_id() << " allocated: " << *new_node << "\n");
    }

    // at this point, we have the node allocated, locked, and the entry point available
    //                if new level, we have a global lock (i.e., the entry point is locked)

    const distance_t ep_distance = Distance::dist(components, entry_point->components(), Node::DIM);
    ++thread->stats.distcomps;

    MaxHeap& top_candidates = thread->current_coroutine().top_candidates;

    // go through upper levels and greedily determine the nearest entry point
    if (new_node_level < top_level) {
      s_ptr<Node> nn = entry_point;
      auto coro = search_for_one<with_lock>(components, nn, ep_distance, top_level, new_node_level, thread);

      while (!coro.handle.done()) {
        co_await std::suspend_always{};  // gives control back to caller of insert
        coro.handle.resume();
      }

      top_candidates.push({nn, Distance::dist(nn->components(), components, Node::DIM)});
      ++thread->stats.distcomps;

    } else {
      top_candidates.push({entry_point, ep_distance});
    }

    // start one level below because there is no entry point yet at this level
    if (is_new_level) {
      --new_node_level;
    }

    // connect node
    for (i32 current_level = static_cast<i32>(new_node_level); current_level >= 0; --current_level) {
      {
        auto coro = search_level<with_lock>(components, ef_construction_, current_level, thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};  // gives control back to caller of insert
          coro.handle.resume();
        }
      }

      dbg::print(dbg::stream{} << "T" << thread->get_id() << " search_level done for level " << current_level << "\n");

      // picks up to M nearest neighbors by running the heuristic
      select_heuristic(top_candidates, m_, thread);

      {  // write selected neighbors of `new_node` to remote memory
        byte_t* neighborlist_ptr = current_level == 0 ? thread->buffer_allocator.allocate_layer_zero(thread->get_id())
                                                      : thread->buffer_allocator.allocate_layer(thread->get_id());
        auto neighborlist = std::make_shared<Neighborlist>(current_level, neighborlist_ptr, thread);

        for (auto& [neighbor, _] : top_candidates.heap) {
          neighborlist->add(neighbor->rptr);
        }

        co_await rdma::write_neighborlist(neighborlist, new_node, thread);
      }

      const u32 m_max = current_level == 0 ? m_max_zero_ : m_max_;

      // connect node to neighbor lists of node's neighbors
      for (auto& [neighbor, neighbor_dist] : top_candidates.heap) {
        {
          auto coro = rdma::spinlock_node(neighbor, thread);
          while (!coro.handle.done()) {
            co_await std::suspend_always{};  // gives control back to caller of insert
            coro.handle.resume();
          }
        }

        const RemotePtr nlist_rptr{neighbor->rptr.memory_node(),
                                   neighbor->compute_remote_neighborlist_offset(current_level)};
        s_ptr<Neighborlist> neighborlist = co_await rdma::read_neighborlist(nlist_rptr, current_level, thread);

        if (neighborlist->num_neighbors() < m_max) {
          neighborlist->add(new_node_ptr);
          co_await rdma::write_last_neighbor_in_neighborlist(neighborlist, neighbor, thread);

        } else {
          // read neighbor's neighbors
          vec<s_ptr<Node>> old_neighbors = co_await rdma::read_nodes(neighborlist->view(), thread);

          MaxHeap new_neighbors;
          new_neighbors.push({new_node, neighbor_dist});

          for (const auto& old_neighbor : old_neighbors) {
            // we could save the distance computation when storing the distance to remote memory
            new_neighbors.push(
              {old_neighbor, Distance::dist(neighbor->components(), old_neighbor->components(), Node::DIM)});
            ++thread->stats.distcomps;
          }

          // shrink connections
          select_heuristic(new_neighbors, m_max, thread);

          // set new neighbors
          neighborlist->reset();
          for (const auto& [new_neighbor, _] : new_neighbors.heap) {
            neighborlist->add(new_neighbor->rptr);
          }

          // write new neighbors to remote memory
          co_await rdma::write_neighborlist(neighborlist, neighbor, thread);
        }

        co_await rdma::unlock_node(neighbor, thread);
      }

      // keep only 1-NN as next entry point
      while (current_level > 0 && top_candidates.size() > 1) {
        top_candidates.pop();
      }
    }

    // unlock node; we use write_header to set multiple bits, the node is locked anyway and may not have `new_lvl_lock`
    co_await rdma::write_header(new_node_ptr, is_new_level, false, false, thread);

    if (is_new_level) {
      // TODO: combine
      co_await rdma::clear_entry_node_bit(entry_point, thread);  // invalidates caches of other threads
      co_await rdma::unlock_new_level_lock(entry_point, thread);  // releases (global) entry-point lock

      // now another thread T could read the old EP, but it's no longer entry node,
      // hence T reads the EP-pointer again, and maybe repeats

      co_await rdma::write_entry_point_ptr(new_node_ptr, thread);
      dbg::print(dbg::stream{} << "T" << thread->get_id() << " ======== NEW EP PTR SET: " << new_node_ptr << "\n");

      cached_ep_ptr = new_node_ptr;
    }

    top_candidates.clear();
  }

  HNSWCoroutine knn(node_t q_id, const span<element_t> components, const u_ptr<ComputeThread>& thread) const {
    dbg::print(std::stringstream{} << "T" << thread->get_id() << " queries " << q_id << "\n--------------\n\n");

    // CRANE Phase-0: reset per-coroutine phase post counters; phase 0
    // = upper-level navigation (entry-point + cache_lookup +
    // search_for_one) until the level-0 beam begins.
    {
      auto& cc = thread->current_coroutine();
      cc.ph_posts[0] = cc.ph_posts[1] = cc.ph_posts[2] = 0;
      cc.cur_phase = 0;
      cc.q_trace_posts = 0;
      cc.q_trace_cwc_posts = 0;
      cc.q_trace_cwc_batched_reads = 0;
    }
    const QueryTrace query_trace = query_trace_begin(q_id, thread);
    const auto finish_query = [&]() {
      query_trace_finish(query_trace, thread);
      ++thread->stats.processed;
    };

    // ===== CRANE fast path (additive, fully gated; non-CRANE code
    // below is untouched and byte-identical). Upper-level descent runs
    // CN-LOCAL over the cached subgraph => ZERO upper-level remote ops;
    // then LAVD's level-0 beam + fp32 rerank (unchanged). The seed is
    // identical to the remote search_for_one seed by construction
    // (cached subgraph == exact remote graph) => recall byte-identical.
    if (crane::Cfg::on()) {
      const u32 cuid = crane::descend<Distance>(components);
      const crane::Cache& CC = crane::CACHE;
      const u32 cs_slot = CC.slot[cuid];
      const u64 cs_rptr = CC.rptr_raw[cuid];
      const element_t* cs_comp = CC.vec_of(cuid);

      thread->current_coroutine().cur_phase = 1;  // level-0 beam
      if (lavd::Config::reorder_on()) {
        auto coro = search_level_reorder(components, ef_search_, cs_slot, cs_rptr, cs_comp, thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};
          coro.handle.resume();
        }
      } else {
        auto coro = search_level_lavd(components, ef_search_, cs_slot, cs_rptr, cs_comp, thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};
          coro.handle.resume();
        }
      }

      auto& results = thread->query_results[q_id];
      auto& out = thread->current_coroutine().lavd_cands;
      const u32 R = std::min<u32>(lavd::Config::rerank, static_cast<u32>(out.size()));

      auto flush_phase = [&]() {
        auto& cc = thread->current_coroutine();
        thread->stats.posts_upnav += cc.ph_posts[0];   // == 0 under CRANE (validation)
        thread->stats.posts_l0 += cc.ph_posts[1];
        thread->stats.posts_rerank += cc.ph_posts[2];
      };

      if (R == 0) {
        flush_phase();
        thread->current_coroutine().lavd_cands.clear();
        finish_query();
        co_return;
      }

      vec<RemotePtr> rptrs;
      rptrs.reserve(R);
      for (u32 i = 0; i < R; ++i) {
        rptrs.push_back(RemotePtr{out[i].rptr});
      }

      thread->current_coroutine().cur_phase = 2;  // fp32 rerank
      vec<s_ptr<Node>> rr;
      rr.reserve(R);
      const u32 rerank_chunk = thread->queue_safe_rerank_chunk();
      lib_assert(rerank_chunk > 0,
                 "invalid SQ/coroutine configuration for rerank");
      for (u32 begin = 0; begin < R; begin += rerank_chunk) {
        const u32 count = std::min(rerank_chunk, R - begin);
        vec<RemotePtr> chunk_rptrs;
        chunk_rptrs.reserve(count);
        for (u32 i = 0; i < count; ++i) {
          chunk_rptrs.push_back(rptrs[begin + i]);
        }
        auto part = co_await rdma::read_nodes_batch(
            chunk_rptrs, thread, /*full_precision=*/true);
        for (auto& node : part) rr.push_back(std::move(node));
        ++thread->stats.rerank_chunks;
      }

      vec<lavd::LavdCand> ex;
      ex.reserve(R);
      for (u32 i = 0; i < R; ++i) {
        const distance_t d = Distance::dist(components, rr[i]->components(), Node::DIM);
        ++thread->stats.distcomps;
        ex.push_back(lavd::LavdCand{d, out[i].slot, out[i].rptr});
      }
      std::sort(ex.begin(), ex.end(),
                [](const lavd::LavdCand& a, const lavd::LavdCand& b) { return a.d < b.d; });

      // Phase F observability tap (env-gated: GB_PHASEF_LOG=<path>).
      // One CSV line per top-R survivor: q_id,rank,slot,rptr. Pure write,
      // no effect on results or beam evolution.
      if (phasef_log_path()) {
        std::ofstream& flog = phasef_log_stream();
        if (flog.is_open()) {
          for (u32 i = 0; i < R; ++i) {
            flog << q_id << ',' << i << ',' << ex[i].slot << ',' << ex[i].rptr << '\n';
          }
        }
      }

      const u32 kk = std::min<u32>(k_, R);
      for (u32 i = 0; i < kk; ++i) {
        results.push_back(ex[i].slot);
      }
      flush_phase();
      thread->current_coroutine().lavd_cands.clear();
      finish_query();
      co_return;
    }
    // ===== end CRANE fast path =====

    auto& ep_ptr = thread->current_coroutine().cached_ep_ptr;
    if (ep_ptr.is_null()) {
      ep_ptr = co_await rdma::read_entry_point_ptr(thread);
    }

    s_ptr<Node> entry_point;
    {
      auto coro = cache_lookup(ep_ptr, entry_point, thread, thread->get_id() == 0);
      while (!coro.handle.done()) {
        co_await std::suspend_always{};  // gives control back to caller of knn
        coro.handle.resume();
      }
    }

    thread->stats.inc_visited_nodes(entry_point->level());
    const distance_t ep_distance = Distance::dist(components, entry_point->components(), Node::DIM);
    ++thread->stats.distcomps;

    MaxHeap& top_candidates = thread->current_coroutine().top_candidates;

    {
      s_ptr<Node> nn = entry_point;

      auto coro = search_for_one<without_lock>(components, nn, ep_distance, entry_point->level(), 0, thread);
      while (!coro.handle.done()) {
        co_await std::suspend_always{};  // gives control back to caller of knn
        coro.handle.resume();
      }

      top_candidates.push({nn, Distance::dist(components, nn->components(), Node::DIM)});
      ++thread->stats.distcomps;
    }

    // search base layer
    if (lavd::Config::on()) {
      // GraphBeyond LAVD: slot-based level-0 beam, 1 fat read per hop.
      // Seed = the level-0 entry from search_for_one (sole entry now).
      const s_ptr<Node> seed = top_candidates.heap.front().node;
      top_candidates.clear();

      thread->current_coroutine().cur_phase = 1;  // CRANE: level-0 beam
      if (lavd::Config::reorder_on()) {
        auto coro = search_level_reorder(components, ef_search_, seed->id(),
                                         seed->rptr.raw_address, seed->components().data(), thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};
          coro.handle.resume();
        }
      } else {
        auto coro = search_level_lavd(components, ef_search_, seed->id(),
                                      seed->rptr.raw_address, seed->components().data(), thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};
          coro.handle.resume();
        }
      }

      // Phase F: top-R fp32 rerank. lavd_cands is ascending by APPROX
      // distance; the true top-k may be reordered within the top-R by
      // quantization error. Batch-read the R best nodes' fp32 vectors
      // (one doorbell) and re-rank by exact distance.
      auto& results = thread->query_results[q_id];
      auto& out = thread->current_coroutine().lavd_cands;
      const u32 R = std::min<u32>(lavd::Config::rerank, static_cast<u32>(out.size()));

      if (R == 0) {
        {  // CRANE Phase-0: flush phase counters (no rerank phase here)
          auto& cc = thread->current_coroutine();
          thread->stats.posts_upnav += cc.ph_posts[0];
          thread->stats.posts_l0 += cc.ph_posts[1];
          thread->stats.posts_rerank += cc.ph_posts[2];
        }
        thread->current_coroutine().lavd_cands.clear();
        finish_query();
        co_return;
      }

      vec<RemotePtr> rptrs;
      rptrs.reserve(R);
      for (u32 i = 0; i < R; ++i) {
        rptrs.push_back(RemotePtr{out[i].rptr});
      }

      thread->current_coroutine().cur_phase = 2;  // CRANE: fp32 rerank
      vec<s_ptr<Node>> rr;
      rr.reserve(R);
      const u32 rerank_chunk = thread->queue_safe_rerank_chunk();
      lib_assert(rerank_chunk > 0,
                 "invalid SQ/coroutine configuration for rerank");
      for (u32 begin = 0; begin < R; begin += rerank_chunk) {
        const u32 count = std::min(rerank_chunk, R - begin);
        vec<RemotePtr> chunk_rptrs;
        chunk_rptrs.reserve(count);
        for (u32 i = 0; i < count; ++i) {
          chunk_rptrs.push_back(rptrs[begin + i]);
        }
        auto part = co_await rdma::read_nodes_batch(
            chunk_rptrs, thread, /*full_precision=*/true);
        for (auto& node : part) rr.push_back(std::move(node));
        ++thread->stats.rerank_chunks;
      }

      // exact distance for the R reranked candidates
      vec<lavd::LavdCand> ex;
      ex.reserve(R);
      for (u32 i = 0; i < R; ++i) {
        const distance_t d = Distance::dist(components, rr[i]->components(), Node::DIM);
        ++thread->stats.distcomps;
        ex.push_back(lavd::LavdCand{d, out[i].slot, out[i].rptr});
      }
      std::sort(ex.begin(), ex.end(),
                [](const lavd::LavdCand& a, const lavd::LavdCand& b) { return a.d < b.d; });

      // Phase F observability tap (env-gated: GB_PHASEF_LOG=<path>).
      // One CSV line per top-R survivor: q_id,rank,slot,rptr. Pure write,
      // no effect on results or beam evolution.
      if (phasef_log_path()) {
        std::ofstream& flog = phasef_log_stream();
        if (flog.is_open()) {
          for (u32 i = 0; i < R; ++i) {
            flog << q_id << ',' << i << ',' << ex[i].slot << ',' << ex[i].rptr << '\n';
          }
        }
      }

      const u32 kk = std::min<u32>(k_, R);
      for (u32 i = 0; i < kk; ++i) {
        results.push_back(ex[i].slot);
      }
      {  // CRANE Phase-0: flush per-coroutine phase post counters
        auto& cc = thread->current_coroutine();
        thread->stats.posts_upnav += cc.ph_posts[0];
        thread->stats.posts_l0 += cc.ph_posts[1];
        thread->stats.posts_rerank += cc.ph_posts[2];
      }
      thread->current_coroutine().lavd_cands.clear();
      finish_query();
      co_return;
    }

    thread->current_coroutine().cur_phase = 1;  // baseline level-0 beam
    auto coro = search_level<without_lock>(components, ef_search_, 0, thread);
    while (!coro.handle.done()) {
      co_await std::suspend_always{};  // gives control back to caller of knn
      coro.handle.resume();
    }

    while (top_candidates.size() > k_) {
      top_candidates.pop();
    }

    auto& results = thread->query_results[q_id];  // coroutine independent
    for (const auto& [nn, _] : top_candidates.heap) {
      results.push_back(nn->id());
    }

    top_candidates.clear();
    finish_query();
  }

  size_t estimate_index_size(size_t num_nodes) const {
    size_t index_size = 0;
    const u32 num_levels = std::round(std::log(num_nodes) / std::log(m_));

    for (u32 i = 0; i < num_levels; ++i) {
      const size_t size =
        i == 0 ? Node::size_until_components() + Node::NEIGHBORLIST_SIZE_ZERO : Node::NEIGHBORLIST_SIZE;
      const f64 probability = std::pow(1. / m_, i);
      index_size += std::round(probability * num_nodes) * size;
    }

    return index_size;
  }

private:
  /**
   * @brief Traverse the graph down to `target_level`.
   *        Greedily find nearest neighbor (1-NN) of `q` starting from begin_level down to `target_level`.
   *
   * @param nearest_neighbor Will contain the closest neighbor to `q`; at the beginning it's the entry point.
   * @param closest_distance The distance from `q` to the initial nearest neighbor (i.e., the entry point).
   */
  template <Lock do_lock>
  MinorCoroutine search_for_one(const span<element_t> q,
                                s_ptr<Node>& nearest_neighbor,
                                distance_t closest_distance,
                                u32 begin_level,
                                u32 target_level,
                                const u_ptr<ComputeThread>& thread) const {
    s_ptr<Node> locked_node;
    bool changed;

    for (u32 level = begin_level; level > target_level; level--) {
      do {
        changed = false;

        if constexpr (do_lock) {
          locked_node = nearest_neighbor;  // no reference, otherwise nearest_neighbor could be destructed
          auto coro = rdma::spinlock_node(locked_node, thread);

          while (!coro.handle.done()) {
            co_await std::suspend_always{};  // gives control back to caller of search_for_one
            coro.handle.resume();
          }
        }

        // READ neighbor list of nearest_neighbor w.r.t. level
        const RemotePtr nlist_rptr{nearest_neighbor->rptr.memory_node(),
                                   nearest_neighbor->compute_remote_neighborlist_offset(level)};
        const s_ptr<Neighborlist> neighborlist = co_await rdma::read_neighborlist(nlist_rptr, level, thread);
        ++thread->stats.visited_neighborlists;

        // find closest neighbor
        s_ptr<Node> best_candidate;

        for (const RemotePtr& r_ptr : neighborlist->view()) {
          thread->stats.inc_visited_nodes(level);
          s_ptr<Node> candidate;
          {
            auto coro = cache_lookup(r_ptr, candidate, thread, not do_lock);  // always admit inner nodes
            while (!coro.handle.done()) {
              co_await std::suspend_always{};  // gives control back to caller
              coro.handle.resume();
            }
          }

          const f32 distance = Distance::dist(q, candidate->components(), Node::DIM);
          ++thread->stats.distcomps;

          if (distance < closest_distance) {
            closest_distance = distance;
            best_candidate = candidate;  // use temporary here, otherwise nearest_neighbor dangles with coroutines
            changed = true;
          }
        }

        nearest_neighbor = changed ? best_candidate : nearest_neighbor;

        if constexpr (do_lock) {
          co_await rdma::unlock_node(locked_node, thread);
        }

      } while (changed);
    }
  }

  static bool admit_to_cache(f32 prob) {
    thread_local std::mt19937 gen{std::random_device{}()};
    thread_local std::uniform_real_distribution<f32> dist(0., 1.);

    return (dist(gen) < prob);
  }

  /**
   * @brief Searches for the ef-NNs on the given `level` and stores them in `top_candidates` of `thread`.
   *        In the beginning, `top_candidates` contains a single entry point.
   *
   * GraphBeyond C1: when `spec_k_ > 1`, pop up to K closest candidates per
   * iteration and issue their neighbor-list reads in a single batched RDMA
   * call. Trades a small amount of "speculative" work (some popped candidates
   * may be filtered by early termination after their list arrives) for a
   * K-fold reduction in serial round-trips.
   *
   * `do_lock = with_lock` (insertion path) keeps the original single-pop
   * behavior — locks must be acquired sequentially.
   */
  template <Lock do_lock>
  MinorCoroutine search_level(const span<element_t> q, u32 ef, u32 level, const u_ptr<ComputeThread>& thread) const {
    hashset_t<RemotePtr>& visited_nodes = thread->current_coroutine().visited_nodes;
    MaxHeap& top_candidates = thread->current_coroutine().top_candidates;
    MinHeap& next_candidates = thread->current_coroutine().next_candidates;

    for (const auto& [node, dist] : top_candidates.heap) {
      next_candidates.push({node, dist});  // copies shared_ptr
      visited_nodes.insert(node->rptr);
    }

    // C1 path: K-speculative wide-beam (only on read-only base-level search).
    constexpr u32 K_MAX = 16;
    const u32 K = (do_lock == without_lock) ? std::min<u32>(spec_k_, K_MAX) : 1u;

    while (!next_candidates.empty()) {
      // -----------------------------------------------------------------
      // Step 1: pop up to K closest candidates (early-stop on first miss
      //         to keep speculation bounded).
      // -----------------------------------------------------------------
      std::array<s_ptr<Node>, K_MAX> active_nodes{};
      std::array<distance_t, K_MAX> active_dists{};
      std::array<RemotePtr, K_MAX> active_nlist_ptrs{};
      u32 k_actual = 0;

      const distance_t farthest_at_pop = top_candidates.top().distance;

      while (k_actual < K && !next_candidates.empty()) {
        const auto [c, d] = next_candidates.top();
        if (d > farthest_at_pop) {
          break;  // remaining candidates can't improve top-ef
        }
        next_candidates.pop();
        active_nodes[k_actual] = c;
        active_dists[k_actual] = d;
        active_nlist_ptrs[k_actual] = RemotePtr{
          c->rptr.memory_node(), c->compute_remote_neighborlist_offset(level)
        };
        ++k_actual;
      }

      if (k_actual == 0) {
        break;
      }

      // -----------------------------------------------------------------
      // Step 2: optional locking (insertion path only — k_actual == 1).
      // -----------------------------------------------------------------
      if constexpr (do_lock) {
        s_ptr<Node>& candidate = active_nodes[0];
        auto coro = rdma::spinlock_node(candidate, thread);
        while (!coro.handle.done()) {
          co_await std::suspend_always{};
          coro.handle.resume();
        }
      }

      // -----------------------------------------------------------------
      // Step 3: fetch neighbor lists. K=1 → single read; K>1 → batched read.
      // -----------------------------------------------------------------
      vec<s_ptr<Neighborlist>> nlists;
      if (k_actual == 1) {
        s_ptr<Neighborlist> nl = co_await rdma::read_neighborlist(active_nlist_ptrs[0], level, thread);
        nlists.emplace_back(std::move(nl));
      } else {
        nlists = co_await rdma::read_neighborlists_batch(
          span<RemotePtr>{active_nlist_ptrs.data(), k_actual}, level, thread);
      }
      thread->stats.visited_neighborlists += k_actual;

      // -----------------------------------------------------------------
      // Step 4: process all k_actual neighbor lists. Each neighbor is
      //         deduped via visited_nodes (so cross-list duplicates only
      //         pay one cache_lookup + dist comp).
      // -----------------------------------------------------------------
      for (u32 a = 0; a < k_actual; ++a) {
        // Re-check farthest after each list — top_candidates may have shrunk.
        distance_t farthest_dist = top_candidates.top().distance;

        // The candidate is the one whose list we're now consuming.
        if (active_dists[a] > farthest_dist) {
          // Speculative pop turned out to be useless — skip its neighbors.
          continue;
        }

        for (RemotePtr& neighbor_ptr : nlists[a]->view()) {
          if (visited_nodes.contains(neighbor_ptr)) {
            continue;
          }
          thread->stats.inc_visited_nodes(level);
          visited_nodes.insert(neighbor_ptr);

          s_ptr<Node> neighbor;
          {
            const bool admit =
              do_lock ? false : (not thread->cache.is_full() ? true : admit_to_cache(cache::ADMISSION_RATIO));
            auto coro = cache_lookup(neighbor_ptr, neighbor, thread, admit);
            while (!coro.handle.done()) {
              co_await std::suspend_always{};
              coro.handle.resume();
            }
          }

          farthest_dist = top_candidates.top().distance;

          const distance_t neighbor_dist = Distance::dist(q, neighbor->components(), Node::DIM);
          ++thread->stats.distcomps;

          if (neighbor_dist < farthest_dist || top_candidates.size() < ef) {
            next_candidates.push({neighbor, neighbor_dist});
            top_candidates.push_k({neighbor, neighbor_dist}, ef);
          }
        }
      }

      if constexpr (do_lock) {
        co_await rdma::unlock_node(active_nodes[0], thread);
      }
    }

    next_candidates.clear();
    visited_nodes.clear();
  }

  /**
   * GraphBeyond LAVD — level-0 beam, ONE fat RDMA read per hop.
   *
   * Replaces the level-0 `search_level<without_lock>` call in knn().
   * Slot-based self-contained traversal: every popped candidate triggers
   * exactly one read_neighborhood (the block carries each neighbor's
   * slot + rptr + quantized vector), so the per-neighbor vector reads
   * vanish (op-count ~1700 -> ~hops). Beam runs on approximate
   * (quantized) distances; the fp32 rerank is Phase F. Results land in
   * `coroutine.lavd_cands` ascending by approx distance.
   *
   * @param seed  level-0 entry node from search_for_one (has components
   *              for the seed approx distance, and id()==slot, rptr).
   */
  // Seed passed as primitives (slot / RemotePtr raw / fp32 comp ptr)
  // so the level-0 beam is identical whether the seed came from the
  // remote upper descent (search_for_one) or CRANE's CN-local descent
  // — byte-identical values either way.
  MinorCoroutine search_level_lavd(const span<element_t> q,
                                   u32 ef,
                                   u32 seed_slot,
                                   u64 seed_rptr_raw,
                                   const element_t* seed_comp,
                                   const u_ptr<ComputeThread>& thread) const {
    auto& cor = thread->current_coroutine();
    hashset_t<RemotePtr>& visited = cor.visited_nodes;
    // T6: slot-indexed visited bitmap (GB_BITMAP_DEDUP=1). Sized once
    // per process to N (all dense slots in the neighborhood region).
    // Byte-identity: slot <-> RemotePtr is a 1:1 LAVD-build bijection,
    // so vis_contains/vis_insert produce the exact same insert/contains
    // decisions (and hence the same heap-push order) as the hashset.
    const bool bm_on = lavd::Config::bitmap_dedup_on();
    gb::SlotBitmap& vbm = cor.visited_slots;
    if (bm_on && vbm.capacity() < lavd::Config::total_n) {
      // total_n is set on the CN at LAVD init; fall back to 0 (off) if
      // unset — caller (knn dispatch) only enters this path when LAVD on.
      vbm.reserve(lavd::Config::total_n ? lavd::Config::total_n : 1u);
    }
    auto vis_contains = [&](const RemotePtr& nrp, u32 slot) -> bool {
      return bm_on ? vbm.contains(slot) : visited.contains(nrp);
    };
    auto vis_insert = [&](const RemotePtr& nrp, u32 slot) {
      if (bm_on) vbm.insert(slot);
      else visited.insert(nrp);
    };
    vec<u8>& qcode = cor.lavd_qcode;
    vec<f32>& lut = cor.lavd_lut;
    vec<lavd::LavdCand>& out = cor.lavd_cands;
    const lavd::Quantizer& qz = lavd::Config::qz;
    const u32 dim = lavd::Config::dim;
    const bool rb_on = lavd::Config::rabitq_on();
    const lavd::RaBitQ& rb = lavd::Config::rabitq;
    const bool pq_on = lavd::Config::pq_on();
    const lavd::PQ& pq = lavd::Config::pq;
    const size_t qb = lavd::qbytes(dim, lavd::Config::bits);  // qvec payload bytes

    // query prep: RaBitQ rotates once; PQ builds a per-query ADC LUT
    // (m*256); scalar encodes the query once. Helpers below dispatch on
    // the active fanout code so hot, cold and seed paths agree.
    std::vector<f32> qrot;
    std::vector<f32> rb_su;
    std::vector<f32> rb_sr;
    f32 nq = 0;
    if (rb_on) {
      qrot.resize(dim);
      rb_su.resize(dim);
      rb_sr.resize(dim);
      nq = rb.query_prep(q.data(), qrot.data(), rb_su.data());
    } else if (pq_on) { lut.resize(pq.lut_size()); pq.build_lut(q.data(), lut.data()); }
    else { qcode.resize(qb); qz.encode(q.data(), qcode.data()); }
    auto enc = [&](const element_t* v, u8* o, u32 nb_uid = lavd::Config::COLD) {
      if (rb_on) {
        // Fast path: uid-aware lookup into precomputed codetab (one-time
        // build-time encode). Cuts per-query rotation O(dim^2) -> memcpy.
        if (nb_uid < lavd::g_rabitq_normtab.size()) {
          *reinterpret_cast<f32*>(o) = lavd::g_rabitq_normtab[nb_uid];
          *reinterpret_cast<f32*>(o + 4) = lavd::g_rabitq_dottab[nb_uid];
          std::memcpy(o + 8, &lavd::g_rabitq_codetab[static_cast<size_t>(nb_uid) * lavd::g_rabitq_code],
                      lavd::g_rabitq_code);
          return;
        }
        rb.encode(v, o + 8, reinterpret_cast<f32*>(o), reinterpret_cast<f32*>(o + 4),
                  rb_su.data(), rb_sr.data());
      } else if (pq_on) pq.encode(v, o);
      else qz.encode(v, o);
    };
    auto approx = [&](const u8* code) -> distance_t {
      if (rb_on) {
        return rb.estimate(code + 8, *reinterpret_cast<const f32*>(code),
                           *reinterpret_cast<const f32*>(code + 4), qrot.data(), nq);
      }
      return pq_on ? pq.adc(lut.data(), code) : qz.approx_d2(qcode.data(), code);
    };

    // seed approx distance (encode the seed's fp32 components once)
    distance_t seed_d;
    if (rb_on) {
      f32 sn = 0;
      f32 sd_ = 0;
      std::vector<byte_t> sc(rb.code_bytes());
      rb.encode(seed_comp, sc.data(), &sn, &sd_, rb_su.data(), rb_sr.data());
      seed_d = rb.estimate(sc.data(), sn, sd_, qrot.data(), nq);
    } else {
      std::array<u8, 1024> sbuf{};
      enc(seed_comp, sbuf.data());
      seed_d = approx(sbuf.data());
    }

    using C = lavd::LavdCand;
    const auto cmp_min = [](const C& a, const C& b) { return a.d > b.d; };  // min-heap
    const auto cmp_max = [](const C& a, const C& b) { return a.d < b.d; };  // max-heap
    std::priority_queue<C, vec<C>, decltype(cmp_min)> next(cmp_min);
    std::priority_queue<C, vec<C>, decltype(cmp_max)> top(cmp_max);

    const C seed_c{seed_d, seed_slot, seed_rptr_raw};
    next.push(seed_c);
    top.push(seed_c);
    vis_insert(RemotePtr{seed_rptr_raw}, seed_slot);

    while (!next.empty()) {
      const C cur = next.top();
      next.pop();
      if (top.size() >= ef && cur.d > top.top().d) {
        break;  // best remaining can't enter the beam
      }
      // structural-hub profile (GB_HUB_PROFILE=1): count each beam pop per slot.
      if (g_l0_access_n > 0 && cur.slot < g_l0_access_n) {
        g_l0_access[cur.slot].fetch_add(1, std::memory_order_relaxed);
      }
      const RemotePtr crp{cur.rptr};
      // memory-bounded LAVD: a node is COLD when a co-location budget is
      // active and it is not in the hot top-H. Cold expansions fall back
      // to per-neighbor reads + on-the-fly re-quantization, producing
      // LavdCands {ad, slot, rptr} that are BYTE-IDENTICAL to the
      // co-located path (same qz, same fp32 -> same qvec -> same ad,
      // same iteration order) => recall byte-identical to full LAVD at
      // any budget. Op-count for cold expansions == baseline.
      const bool cold = lavd::Config::budget_on() && !lavd::Config::is_hot(cur.slot);
      if (!cold) {
        // GraphBeyond CWC: when cwc_on() (and no budget), park this
        // uniform fat-block read for the scheduler to coalesce. OFF =>
        // the literal original single-read call (byte-identical).
        s_ptr<Neighborhood> nh;
        // SHC: hub hit -> memcpy from CN-local cache, skip RDMA.
        if (lavd::g_hub_cache.is_hub(cur.slot)) {
          byte_t* local_buffer =
            thread->buffer_allocator.allocate_neighborhood(thread->get_id());
          std::memcpy(local_buffer, lavd::g_hub_cache.block_data(cur.slot),
                      lavd::g_hub_cache.stride);
          nh = std::make_shared<Neighborhood>(local_buffer, thread);
          lavd::g_hub_cache.hits.fetch_add(1, std::memory_order_relaxed);
        } else if (lavd::Config::cwc_on() && !lavd::Config::budget_on() &&
                   !lavd::varblock_on()) {
          nh = co_await rdma::lavd_fetch_block(cur.slot, crp.memory_node(), thread);
        } else {
          nh = co_await rdma::read_neighborhood(cur.slot, crp.memory_node(), thread);
        }
        ++thread->stats.visited_neighborlists;  // exactly 1 fat read per hop

        const u32 cnt = nh->count();
        // slot_only_on() routes through the coloc_on path below so the
        // has_qvec() guard covers the tiered+cold fall-through (cold
        // neighbors of a hot center have no compact_idx -> no packed_qvec
        // entry; they must take the read_node+encode fallback). The fast
        // path here assumes every entry carries a valid qvec.
        if (!lavd::Config::coloc_on() && !lavd::slot_only_on()) {
          for (u32 i = 0; i < cnt; ++i) {
            const auto entry = nh->decode_entry(i);
            const RemotePtr nrp = entry.rptr;
            const u32 nslot = entry.slot;
            if (vis_contains(nrp, nslot)) {
              continue;
            }
            vis_insert(nrp, nslot);
            thread->stats.inc_visited_nodes(0);
            const distance_t ad = approx(entry.qvec);
            ++thread->stats.distcomps;

            if (top.size() < ef || ad < top.top().d) {
              const C nc{ad, nslot, nrp.raw_address};
              next.push(nc);
              top.push(nc);
              if (top.size() > ef) {
                top.pop();
              }
            }
          }
        } else {
          // Tiered hot-cache: hot block neighbors whose uid is cold have
          // no packed_qvec entry. The published code issued one serial
          // RDMA read_node per such cold neighbor; under hub-rooted
          // budget that is M~15 serialized RTTs per hop. Batch the cold
          // neighbors into ONE chained per-MN post (same pattern as
          // cold-center cold_batch). Gated by SHINE_LAVD_HOT_COLD_BATCH=1
          // (default on); off => the original per-neighbor read.
          static const bool hot_cold_batch_on = [] {
            const char* e = std::getenv("SHINE_LAVD_HOT_COLD_BATCH");
            return !(e && std::atoi(e) == 0);
          }();
          std::vector<u8> tq(qb);
          if (hot_cold_batch_on) {
            // Single pass over entries: process hot neighbors inline
            // (CN cache lookup), buffer cold neighbors for one batched
            // RDMA. Pending entries record (i, nrp, nslot) so a single
            // result loop can re-emit their beam pushes in entry order.
            struct Pend { u32 i; RemotePtr nrp; u32 nslot; };
            vec<Pend> pending;
            pending.reserve(cnt);
            for (u32 i = 0; i < cnt; ++i) {
              const auto entry = nh->decode_entry(i);
              const RemotePtr nrp = entry.rptr;
              const u32 nslot = entry.slot;
              if (vis_contains(nrp, nslot)) continue;
              vis_insert(nrp, nslot);
              thread->stats.inc_visited_nodes(0);
              if (entry.has_qvec) {
                const distance_t ad = approx(entry.qvec);
                ++thread->stats.distcomps;
                if (top.size() < ef || ad < top.top().d) {
                  const C nc{ad, nslot, nrp.raw_address};
                  next.push(nc);
                  top.push(nc);
                  if (top.size() > ef) top.pop();
                }
              } else {
                pending.push_back(Pend{i, nrp, nslot});
              }
            }
            if (!pending.empty()) {
              vec<RemotePtr> rps;
              rps.reserve(pending.size());
              for (const auto& p : pending) rps.push_back(p.nrp);
              auto nodes = co_await rdma::read_nodes_batch(rps, thread);
              for (size_t k = 0; k < pending.size(); ++k) {
                const auto& nbr = nodes[k];
                enc(nbr->components().data(), tq.data(), pending[k].nslot);
                const distance_t ad = approx(tq.data());
                ++thread->stats.distcomps;
                if (top.size() < ef || ad < top.top().d) {
                  const C nc{ad, pending[k].nslot, pending[k].nrp.raw_address};
                  next.push(nc);
                  top.push(nc);
                  if (top.size() > ef) top.pop();
                }
              }
            }
          } else {
            for (u32 i = 0; i < cnt; ++i) {
              const auto entry = nh->decode_entry(i);
              const RemotePtr nrp = entry.rptr;
              const u32 nslot = entry.slot;
              if (vis_contains(nrp, nslot)) continue;
              vis_insert(nrp, nslot);
              thread->stats.inc_visited_nodes(0);
              distance_t ad;
              if (entry.has_qvec) {
                ad = approx(entry.qvec);
              } else {
                auto nbr = co_await rdma::read_node(nrp, thread);
                enc(nbr->components().data(), tq.data(), nslot);
                ad = approx(tq.data());
              }
              ++thread->stats.distcomps;
              if (top.size() < ef || ad < top.top().d) {
                const C nc{ad, nslot, nrp.raw_address};
                next.push(nc);
                top.push(nc);
                if (top.size() > ef) top.pop();
              }
            }
          }
        }
      } else {
        // cold fallback: 1 index neighbor-list read + per-neighbor node
        // read, re-quantizing each neighbor's fp32 with the same qz.
        // The level-0 neighbor list sits at node_base + size_until_components()
        // (== build.hh's l0_off; constant offset, level-independent).
        const RemotePtr nlist_rptr{crp.memory_node(),
                                   crp.byte_offset() + Node::size_until_components()};
        auto nl = co_await rdma::read_neighborlist(nlist_rptr, 0, thread);
        ++thread->stats.visited_neighborlists;
        std::vector<u8> tq(qb);
        // Cold-batch: pre-dedup via the hashset, then issue all surviving
        // per-neighbor reads in ONE chained post per MN (S signaled CQEs
        // total instead of M). Disabled when SHINE_LAVD_COLD_BATCH=0 ->
        // serialized single-read loop, byte-identical to the published
        // cold path. Visibility (vbm) is still enforced post-read using
        // nbr->id() — the only authoritative slot for cold nodes.
        static const bool cold_batch_on = [] {
          const char* e = std::getenv("SHINE_LAVD_COLD_BATCH");
          return !(e && std::atoi(e) == 0);
        }();
        if (cold_batch_on) {
          // Pre-filter via the slot bitmap when the reverse index is
          // available (mbLAVD CN-resident map, populated by the
          // initiator's build path). Eliminates the wasted read+encode
          // for cold neighbors already visited by an earlier hot
          // expansion. SHINE_LAVD_COLD_PREFILTER=0 disables the path.
          static const bool prefilter_on = [] {
            const char* e = std::getenv("SHINE_LAVD_COLD_PREFILTER");
            return !(e && std::atoi(e) == 0);
          }();
          const bool use_prefilter =
              prefilter_on && bm_on && lavd::rev_index_ready();
          vec<RemotePtr> fresh;
          fresh.reserve(nl->num_neighbors());
          for (const RemotePtr nrp : nl->view()) {
            if (visited.contains(nrp)) continue;
            visited.insert(nrp);
            if (use_prefilter) {
              const u32 nslot =
                  lavd::rev_lookup(nrp.memory_node(), nrp.byte_offset());
              if (nslot != 0xFFFFFFFFu && vbm.contains(nslot)) {
                continue;  // already seen via hot path; skip read+encode
              }
            }
            fresh.push_back(nrp);
          }
          if (!fresh.empty()) {
            auto nodes = co_await rdma::read_nodes_batch(fresh, thread);
            for (size_t k = 0; k < nodes.size(); ++k) {
              const auto& nbr = nodes[k];
              const RemotePtr nrp = fresh[k];
              const u32 nslot = nbr->id();
              if (bm_on) {
                if (vbm.contains(nslot)) continue;
                vbm.insert(nslot);
              }
              thread->stats.inc_visited_nodes(0);
              enc(nbr->components().data(), tq.data(), nslot);
              const distance_t ad = approx(tq.data());
              ++thread->stats.distcomps;
              if (top.size() < ef || ad < top.top().d) {
                const C nc{ad, nslot, nrp.raw_address};
                next.push(nc);
                top.push(nc);
                if (top.size() > ef) top.pop();
              }
            }
          }
        } else {
          for (const RemotePtr nrp : nl->view()) {
            if (visited.contains(nrp)) {
              continue;
            }
            visited.insert(nrp);
            thread->stats.inc_visited_nodes(0);
            auto nbr = co_await rdma::read_node(nrp, thread);
            if (bm_on) {
              const u32 nslot = nbr->id();
              if (vbm.contains(nslot)) continue;
              vbm.insert(nslot);
            }
            enc(nbr->components().data(), tq.data(), nbr->id());
            const distance_t ad = approx(tq.data());
            ++thread->stats.distcomps;
            if (top.size() < ef || ad < top.top().d) {
              const C nc{ad, nbr->id(), nrp.raw_address};
              next.push(nc);
              top.push(nc);
              if (top.size() > ef) top.pop();
            }
          }
        }
      }
    }

    // emit beam ascending by approx distance (best first)
    out.clear();
    out.reserve(top.size());
    while (!top.empty()) {
      out.push_back(top.top());
      top.pop();
    }
    std::reverse(out.begin(), out.end());
    visited.clear();
    if (bm_on) vbm.clear();  // T6: O(1) epoch bump
  }

  /**
   * GraphBeyond reorder-not-replicate — level-0 beam, ONE RDMA read per
   * (cross-)block hop, vector co-located by k-means clustering and stored
   * EXACTLY ONCE (no LAVD replication => ~1x memory).
   *
   * Starling-style block search: loading a block scores ALL its co-clustered
   * nodes from a single over-read (free on the idle wire — the op-count-wall
   * corollary). Following a node's L0 neighbor uids: in-block neighbors are
   * already loaded+scored (FREE, zero extra ops); cross-block neighbors
   * trigger one more bounded read (deduped via `loaded`). Edges followed are
   * the EXACT L0 graph (neighbor uids stored per node) — the over-read only
   * ADDS candidates (filtered by the same `top` threshold), never misses a
   * neighbor LAVD would find. fp32 blocks => exact beam distances (>= LAVD);
   * sq8 blocks => same Quantizer approx as LAVD. Phase F fp32 rerank
   * (unchanged) re-reads INDEX-region fp32 => recall-neutral by construction.
   *
   * Per-query state lives in the coroutine FRAME (locals): persists across
   * co_awaits, freed at query end. Result lands in `coroutine.lavd_cands`
   * ascending by approx distance — identical contract to search_level_lavd,
   * so the knn() rerank path is shared verbatim.
   */
  MinorCoroutine search_level_reorder(const span<element_t> q,
                                      u32 ef,
                                      u32 seed_slot,
                                      u64 /*seed_rptr_raw*/,
                                      const element_t* /*seed_comp*/,
                                      const u_ptr<ComputeThread>& thread) const {
    auto& cor = thread->current_coroutine();
    vec<lavd::LavdCand>& out = cor.lavd_cands;
    const u32 dim = lavd::Config::dim;
    const u32 mn = 0;  // single MN
    const bool sq8 = lavd::Config::rb_sq8;

    // sq8 path encodes the query once (same Quantizer as the build/LAVD);
    // fp32 path computes exact L2 against the in-block fp32 vector.
    vec<u8>& qcode = cor.lavd_qcode;
    const lavd::Quantizer& qz = lavd::Config::qz;
    if (sq8) { qcode.resize(lavd::qbytes(dim, lavd::Config::bits)); qz.encode(q.data(), qcode.data()); }
    auto approx = [&](const byte_t* v) -> distance_t {
      if (sq8) return qz.approx_d2(qcode.data(), reinterpret_cast<const u8*>(v));
      return Distance::dist(q, span<const element_t>{reinterpret_cast<const element_t*>(v), dim}, dim);
    };

    using C = lavd::LavdCand;
    const auto cmp_min = [](const C& a, const C& b) { return a.d > b.d; };  // min-heap
    const auto cmp_max = [](const C& a, const C& b) { return a.d < b.d; };  // max-heap
    std::priority_queue<C, vec<C>, decltype(cmp_min)> next(cmp_min);
    std::priority_queue<C, vec<C>, decltype(cmp_max)> top(cmp_max);

    hashset_t<u32> visited;                 // scored uids
    hashset_t<u32> loaded;                  // loaded block ids (dedup reads)
    vec<s_ptr<ReorderBlock>> alive;         // pin loaded blocks for the query
    hashmap_t<u32, u64> loc;                // uid -> (alive_idx<<32 | node_idx)

    std::vector<u32> pending;               // block ids queued for this step
    pending.push_back(lavd::Config::block_of[seed_slot]);

    while (true) {
      // 1. drain pending: one RDMA read per NEW block, score ALL in-block nodes
      for (size_t pi = 0; pi < pending.size(); ++pi) {
        const u32 bid = pending[pi];
        if (loaded.contains(bid)) continue;
        loaded.insert(bid);
        auto blk = co_await rdma::read_reorder_block(bid, mn, thread);
        ++thread->stats.visited_neighborlists;  // 1 block read == 1 hop op (Omega)
        const u32 ai = static_cast<u32>(alive.size());
        alive.push_back(blk);
        const u32 nn = blk->n_nodes();
        for (u32 i = 0; i < nn; ++i) {
          const u32 uid = blk->uid(i);
          loc[uid] = (static_cast<u64>(ai) << 32) | i;  // resolver for expansion
          if (visited.contains(uid)) continue;
          visited.insert(uid);
          thread->stats.inc_visited_nodes(0);
          const distance_t ad = approx(blk->vec(i));
          ++thread->stats.distcomps;
          if (top.size() < ef || ad < top.top().d) {
            const C nc{ad, uid, blk->rptr(i)};
            next.push(nc);
            top.push(nc);
            if (top.size() > ef) top.pop();
          }
        }
      }
      pending.clear();

      // 2. pop the best unexpanded candidate; stop when it cannot improve top
      if (next.empty()) break;
      const C cur = next.top();
      next.pop();
      if (top.size() >= ef && cur.d > top.top().d) break;

      // 3. expand cur: follow its EXACT L0 neighbor uids. In-block (already
      //    loaded => visited) neighbors are free; cross-block queue one read.
      auto it = loc.find(cur.slot);
      if (it == loc.end()) continue;  // cur was scored from its block => present
      const u32 ai = static_cast<u32>(it->second >> 32);
      const u32 idx = static_cast<u32>(it->second & 0xFFFFFFFFu);
      ReorderBlock* cb = alive[ai].get();
      const u32 lc = cb->l0count(idx);
      const u32* nbr = cb->nbr(idx);
      for (u32 j = 0; j < lc; ++j) {
        const u32 nuid = nbr[j];
        if (visited.contains(nuid)) continue;  // already scored in a loaded block
        const u32 nbid = lavd::Config::block_of[nuid];
        if (!loaded.contains(nbid)) pending.push_back(nbid);
      }
    }

    // emit beam ascending by approx distance (best first) — identical to LAVD
    out.clear();
    out.reserve(top.size());
    while (!top.empty()) { out.push_back(top.top()); top.pop(); }
    std::reverse(out.begin(), out.end());
  }

  /**
   * @brief Selects `m` neighbors using a heuristic to preserve the connectivity of the graph.
   *        The number of resulting neighbors in `top_candidates` is <= `m`.
   */
  static void select_heuristic(MaxHeap& top_candidates, u32 m, const u_ptr<ComputeThread>& thread) {
    if (top_candidates.size() < m) {
      return;
    }

    // to get nearest candidates (destroys the max-heap property)
    top_candidates.sort_ascending();

    const size_t initial_heap_size = top_candidates.size();

    idx_t selected = 1;  // candidates selected so far (nearest neighbor is always selected)
    idx_t consumed = 1;  // candidates consumed so far (always >= selected)

    while (selected < m && consumed < initial_heap_size) {
      bool is_selected = true;
      const auto& [c_node, c_dist_to_query] = top_candidates.heap[consumed];  // get closest node to query

      // if the distance of the closest node C to all already selected nodes is larger than the distance from C to the
      // query, the heuristics selects C
      for (idx_t i = 0; i < selected; ++i) {
        const auto& selected_node = top_candidates.heap[i].node;
        const auto c_dist_to_selected = Distance::dist(selected_node->components(), c_node->components(), Node::DIM);
        ++thread->stats.distcomps;

        if (c_dist_to_selected < c_dist_to_query) {
          is_selected = false;
          break;
        }
      }

      if (is_selected) {
        std::swap(top_candidates.heap[selected], top_candidates.heap[consumed]);
        ++selected;
      }

      ++consumed;
    }

    top_candidates.heap.resize(selected);
    top_candidates.make_heap();
  }

  template <cache::Cacheable T>
  MinorCoroutine cache_lookup(RemotePtr rptr,
                              s_ptr<T>& value,
                              const u_ptr<ComputeThread>& thread,
                              bool admit_to_cache) const {
    if (not use_cache_) {
      value = co_await rdma::read_node(rptr, thread);
      co_return;
    }

    auto cache_entry = thread->cache.get<T>(rptr);
    if (cache_entry.has_value()) {
      value = *cache_entry;
      ++thread->stats.cache_hits;

    } else {
      value = co_await rdma::read_node(rptr, thread);

      if (admit_to_cache) {
        thread->cache.insert(rptr, value, thread->get_id());
      }

      ++thread->stats.cache_misses;
    }
  }

private:
  // construction parameters
  const u32 m_;
  const u32 m_max_;
  const u32 m_max_zero_;
  const u32 ef_construction_;
  const f64 normalization_factor_;

  // search parameters
  const u32 k_;
  const u32 ef_search_;
  const bool use_cache_;

  // GraphBeyond C1: Speculative Wide-Beam Navigation. spec_k_ > 1 enables
  // popping K candidates per outer iteration and batching their neighbor-list
  // RDMA reads. spec_k_ == 1 is identical to original SHINE.
  const u32 spec_k_;

  std::mt19937 prng_;
  std::uniform_real_distribution<> uniform_;
};

}  // namespace hnsw
