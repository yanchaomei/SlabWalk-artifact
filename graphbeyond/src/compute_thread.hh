#pragma once

#include <algorithm>
#include <library/hugepage.hh>
#include <library/thread.hh>
#include <limits>
#include <random>

#include "buffer_allocator.hh"
#include "common/statistics.hh"
#include "coroutine.hh"
#include "lavd/queue_budget.hh"
#include "shared_context.hh"

// forward declaration
namespace cache {
class Cache;
}

class ComputeThread : public Thread {
public:
  ComputeThread(u32 id,
                u32 compute_node_id,
                i32 max_send_queue_wr,
                BufferAllocator& buffer_allocator,
                cache::Cache& cache,
                u32 num_memory_nodes,
                u32 num_coroutines)
      : Thread(id),
        node_id(compute_node_id),
        send_wcs(max_send_queue_wr),
        buffer_allocator(buffer_allocator),
        cache(cache),
        post_balances(num_coroutines),
        read_bytes_per_mn(num_memory_nodes, 0),
        read_wrs_per_mn(num_memory_nodes, 0),
        read_submits_per_mn(num_memory_nodes, 0),
        max_send_queue_wr_(max_send_queue_wr) {
    // allocate single pointer slot (for RDMA requests) per coroutine
    for (idx_t i = 0; i < num_coroutines; ++i) {
      pointer_slots_.push_back(buffer_allocator.allocate_pointer());
    }

    // CWC group ring (fixed; never realloced so membership stays
    // stable between post and its single signaled completion).
    cwc_groups_.resize(CWC_RING);

    // initialize PRNG
    dist_ = std::uniform_int_distribution<u32>(0, num_memory_nodes - 1);
  }

  // GraphBeyond CWC — one signaled completion can release a *group* of
  // coroutines whose uniform fat-block reads were coalesced into a
  // single linked WR chain. Bit 31 of the low word of wr_id flags a
  // group handle; the low 31 bits index the per-thread group ring.
  static constexpr u32 CWC_FLAG = 0x80000000u;
  static constexpr u32 CWC_RING = 256;  // >> max in-flight CWC batches

  void poll_cq() {
    Context::poll_send_cq(send_wcs.data(), max_send_queue_wr_, ctx->get_cq(), [&](u64 wr_id) {
      auto [ctx_offset, low] = decode_64bit(wr_id);
      auto* owner = ctx->registered_threads[ctx_offset];
      ++owner->stats.rdma_cqes;  // one consumed signaled completion
      if (low & CWC_FLAG) {
        // group barrier: this single CQE releases every member coroutine
        for (u32 cid : owner->cwc_groups_[low & ~CWC_FLAG]) {
          --owner->post_balances[cid];
        }
      } else {
        --owner->post_balances[low];
      }
    });
  }

  // Allocate a CWC group handle holding `members` (coroutine ids that
  // share one coalesced post). Ring-reused; CWC_RING >> in-flight so a
  // slot is long-drained before reuse. Returns the low-word handle
  // (CWC_FLAG | slot) for the signaled WR's wr_id.
  u32 cwc_alloc_group(const vec<u32>& members) {
    const u32 slot = cwc_ring_++ % CWC_RING;
    cwc_groups_[slot] = members;
    return CWC_FLAG | slot;
  }
  u64 cwc_group_wr_id(u32 low_handle) const { return encode_64bit(ctx_tid, low_handle); }

  void reset() {
    stats = statistics::ThreadStatistics{};
    query_results.clear();
    query_latency_ns.clear();
    std::fill(read_bytes_per_mn.begin(), read_bytes_per_mn.end(), 0);
    std::fill(read_wrs_per_mn.begin(), read_wrs_per_mn.end(), 0);
    std::fill(read_submits_per_mn.begin(), read_submits_per_mn.end(), 0);

    running_coroutine_ = 0;
    coroutines.clear();
  }

  u32 get_random_memory_node() { return dist_(generator_); }
  u64 create_wr_id() const { return encode_64bit(ctx_tid, running_coroutine_); }
  bool is_ready(u32 coroutine_id) const { return post_balances[coroutine_id] == 0; }
  u32 queue_safe_rerank_chunk() const {
    u64 shared_coroutines = 0;
    for (const auto* registered : ctx->registered_threads) {
      shared_coroutines += registered->post_balances.size();
    }
    lib_assert(shared_coroutines <= std::numeric_limits<u32>::max(),
               "shared coroutine count exceeds queue-budget representation");
    return lavd::queue_safe_rerank_chunk(
        static_cast<u32>(max_send_queue_wr_),
        static_cast<u32>(shared_coroutines));
  }

  void track_post() { track_post_batch(1); }

  void account_remote_read(u32 mn, size_t bytes, size_t logical_wrs = 1) {
    lib_assert(mn < read_bytes_per_mn.size(),
               "remote-read accounting MN is out of range");
    read_bytes_per_mn[mn] += bytes;
    read_wrs_per_mn[mn] += logical_wrs;
  }

  void account_remote_submit(u32 mn) {
    lib_assert(mn < read_submits_per_mn.size(),
               "remote-submit accounting MN is out of range");
    ++read_submits_per_mn[mn];
  }

  // Account for one ibv_post_send carrying `logical_wrs` linked work
  // requests. Queue readiness follows the single signaled tail CQE, while
  // the phase counters retain the operation-count unit used by the model.
  void track_post_batch(size_t logical_wrs) {
    lib_assert(logical_wrs > 0, "an RDMA post must carry at least one WR");
    ++post_balances[running_coroutine_];
    ++stats.rdma_posts;
    stats.rdma_wrs += logical_wrs;
    // CRANE Phase-0: attribute this post to the running coroutine's
    // current search phase. Guard: pre-scheduler RDMA (LAVD region
    // build / placement / warmup) runs with an empty coroutines vec —
    // those posts are not query phases, skip attribution (no OOB).
    if (running_coroutine_ < coroutines.size()) {
      auto& c = *coroutines[running_coroutine_];
      c.ph_posts[c.cur_phase] += logical_wrs;
      ++c.q_trace_posts;
    }
  }

  void track_cwc_batch(const vec<u32>& members) {
    ++stats.rdma_posts;       // one ibv_post_send for the linked WR chain
    stats.rdma_wrs += members.size();
    ++stats.cwc_batches;
    stats.cwc_batched_reads += members.size();

    if (members.empty()) return;
    const f64 share = 1.0 / static_cast<f64>(members.size());
    for (u32 cid : members) {
      if (cid < coroutines.size()) {
        auto& c = *coroutines[cid];
        ++c.ph_posts[c.cur_phase];
        c.q_trace_cwc_posts += share;
        ++c.q_trace_cwc_batched_reads;
      }
    }
  }

  // CWC: a coroutine parks a pending fat-block request (its local
  // buffer is pre-allocated so await_resume can build the Neighborhood
  // exactly as the single-read path does). The scheduler later
  // coalesces up to B of these into one linked WR chain.
  struct CwcPending {
    u32 slot;
    u32 mn;
    u64 remote_offset;
    u32 read_bytes;
    byte_t* buffer;
    u32 cid;
  };
  void cwc_park(u32 slot, u32 mn, u64 remote_offset, u32 read_bytes, byte_t* buffer) {
    cwc_pending_.push_back({slot, mn, remote_offset, read_bytes, buffer, running_coroutine_});
    ++post_balances[running_coroutine_];  // not ready until group CQE
  }
  vec<CwcPending>& cwc_pending() { return cwc_pending_; }
  void set_current_coroutine(u32 id) { running_coroutine_ = id; }
  HNSWCoroutine& current_coroutine() const { return *coroutines[running_coroutine_]; }
  u64* coros_pointer_slot() const { return pointer_slots_[running_coroutine_]; }

public:
  const u32 node_id;
  vec<ibv_wc> send_wcs;

  BufferAllocator& buffer_allocator;  // global per compute node
  cache::Cache& cache;  // global per compute node

  SharedContext<ComputeThread>* ctx{nullptr};  // initialized by WorkerPool
  u32 ctx_tid{};

  // stores the k-NNs (node ids) of this thread's processed queries (for recall computation)
  hashmap_t<node_t, vec<node_t>> query_results;

  vec<u_ptr<HNSWCoroutine>> coroutines;  // use u_ptr here to ensure pointer stability
  vec<std::atomic<i32>> post_balances;  // per coroutine
  vec<size_t> read_bytes_per_mn;
  vec<size_t> read_wrs_per_mn;
  vec<size_t> read_submits_per_mn;
  vec<u64> query_latency_ns;

  // CWC state (only touched when lavd::Config::cwc_on()).
  vec<vec<u32>> cwc_groups_;  // ring of in-flight coalesced-group memberships
  u32 cwc_ring_{0};
  vec<CwcPending> cwc_pending_;  // parked hop requests awaiting coalesced post

  statistics::ThreadStatistics stats{};

private:
  const i32 max_send_queue_wr_;
  u32 running_coroutine_{};  // tracks the id of the currently running coroutine
  vec<u64*> pointer_slots_;  // memory region for a single pointer per coroutine

  std::mt19937 generator_{std::random_device{}()};
  std::uniform_int_distribution<u32> dist_;
};
