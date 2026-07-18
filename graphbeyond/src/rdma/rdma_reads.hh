#pragma once

#include <cstdlib>

#include <library/batched_read.hh>

#include "compute_thread.hh"
#include "coroutine.hh"
#include "lavd/config.hh"
#include "lavd/region_capacity.hh"
#include "lavd/reorder_layout.hh"
#include "node/neighborhood.hh"
#include "node/reorder_block.hh"
#include "remote_pointer.hh"

namespace rdma {

/**
 * GraphBeyond LAVD: one fat RDMA READ per level-0 hop.
 *
 * Reads neighborhood[slot] (lavd::Config::stride bytes) from the MN's
 * 2nd region — the block contains, for each L0 neighbor: its slot, its
 * RemotePtr (for fp32 rerank), and its quantized vector (for the
 * approx-distance fanout). Replaces `1 list read + M vector reads`.
 *
 * @param slot  dense node id == HNSW uid (self-contained traversal:
 *              every block entry carries the neighbor's slot).
 * @param mn    memory node holding the neighborhood region (v1: 0).
 */
inline auto read_neighborhood(u32 slot, u32 mn, const u_ptr<ComputeThread>& thread) {
  const size_t stride = lavd::Config::stride();
  if (lavd::Config::native_packed_on()) {
    lib_assert(slot < lavd::Config::native_l0.resolver.total_slots,
               "LAVD-native: slot outside packed sidecar");
  }
  byte_t* local_buffer = thread->buffer_allocator.allocate_neighborhood(thread->get_id());

  // Variable-length LAVD block layout (degree-aware compact). When
  // SHINE_LAVD_VARBLOCK=1, the on-MN region replaces fixed-stride
  // padding with a per-slot offset_table; the fat read pulls only
  // BLOCK_HEADER + count * entry_size bytes. Buffer is still
  // stride-sized (= max possible) so the freelist stays uniform.
  // Var-block is mutex with budget for now (commit 2 guard); the
  // search will not reach this with both flags on.
  const bool vb_on = lavd::varblock_on();
  lavd::native::FixedL0ReadPlan fixed_plan;
  fixed_plan = lavd::Config::native_packed_on()
      ? lavd::Config::native_l0.read_plan(slot)
      : lavd::native::sparse_fixed_read_plan(slot, mn, static_cast<u32>(stride), lavd::PARAMS_RESERVE);
  u32 read_mn = fixed_plan.owner_mn;
  size_t remote_off = 0;
  u32 read_bytes = static_cast<u32>(stride);
  if (lavd::Config::native_packed_on()) {
    // Multi-MN combined: native packed gives slot -> (owner_mn,
    // local_slot); var-block (when also on) overrides the fixed-stride
    // read plan with per-MN per-local_slot offsets/sizes from the
    // g_varblock_offsets_per_mn cache.
    if (vb_on) {
      lib_assert(fixed_plan.owner_mn < lavd::g_varblock_offsets_per_mn.size(),
                 "LAVD-native: missing owner offset table");
      const auto& offsets =
          lavd::g_varblock_offsets_per_mn[fixed_plan.owner_mn];
      lib_assert(static_cast<size_t>(fixed_plan.local_slot) + 1 <
                     offsets.size(),
                 "LAVD-native: local slot outside offset table");
      remote_off = lavd::varblock_offset_mn(fixed_plan.owner_mn, fixed_plan.local_slot);
      read_bytes = lavd::varblock_size_mn(fixed_plan.owner_mn, fixed_plan.local_slot);
    } else {
      remote_off = fixed_plan.remote_offset;
      read_bytes = fixed_plan.read_bytes;
    }
  } else {
    if (vb_on) {
      lib_assert(static_cast<size_t>(slot) + 1 <
                     lavd::g_varblock_offsets.size(),
                 "LAVD: slot outside single-MN offset table");
    }
    remote_off = vb_on
      ? lavd::varblock_offset(slot)
      : (lavd::Config::budget_on()
           ? lavd::compact_block_offset(lavd::Config::cidx(slot), lavd::Config::total_n, stride)
           : lavd::region_offset(slot, stride));
    read_bytes = vb_on ? lavd::varblock_size(slot) : static_cast<u32>(stride);
  }
  lib_assert(read_mn < thread->ctx->qps.size(),
             "LAVD: resolved memory-node id is out of range");
  lib_assert(read_bytes > 0 && read_bytes <= stride,
             "LAVD: Slab read exceeds the local stride buffer");
  lib_assert(lavd::region_range_fits(
                 remote_off, read_bytes,
                 lavd::Config::region_capacity_bytes()),
             "LAVD: Slab read exceeds the registered remote region");

  thread->stats.rdma_reads_in_bytes += read_bytes;
  thread->account_remote_read(read_mn, read_bytes);
  thread->account_remote_submit(read_mn);
  thread->track_post();

  const QP& qp = thread->ctx->qps[read_mn]->qp;
  qp->post_send(reinterpret_cast<u64>(local_buffer),
                read_bytes,
                thread->ctx->get_lkey(),
                IBV_WR_RDMA_READ,
                true,
                false,
                thread->ctx->get_remote_neighborhood_mrt(read_mn),
                remote_off,
                0,
                thread->create_wr_id());

  struct awaitable {
    byte_t* local_buffer;
    const u_ptr<ComputeThread>& thread;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    s_ptr<Neighborhood> await_resume() const {
      return std::make_shared<Neighborhood>(local_buffer, thread);
    }
  };

  return awaitable{local_buffer, thread};
}

/**
 * GraphBeyond reorder-not-replicate: ONE RDMA READ per (cross-)block hop.
 *
 * Reads block `bid` ([block_off[bid], block_off[bid+1])) from the MN's 2nd
 * region. The block holds, for each co-clustered node: its uid, rptr (for
 * the fp32 rerank), its stored vector (fp32 default | sq8), and its level-0
 * neighbor uids. The Starling-style block search scores ALL in-block nodes
 * from this single read (the over-read is free on the idle wire) and follows
 * neighbor uids -- in-block neighbors are already loaded (free), cross-block
 * neighbors trigger one more read. Each block stored EXACTLY ONCE => ~1x
 * memory (no LAVD replication). Variable block size; the slab is sized to the
 * largest block so the freelist recycles uniformly.
 */
inline auto read_reorder_block(u32 bid, u32 mn, const u_ptr<ComputeThread>& thread) {
  const u64 off = lavd::Config::block_off[bid];
  const u32 size = static_cast<u32>(lavd::Config::block_off[bid + 1] - off);
  byte_t* local_buffer = thread->buffer_allocator.allocate_reorder_block(thread->get_id());

  thread->stats.rdma_reads_in_bytes += size;
  thread->account_remote_read(mn, size);
  thread->account_remote_submit(mn);
  thread->track_post();

  const QP& qp = thread->ctx->qps[mn]->qp;
  qp->post_send(reinterpret_cast<u64>(local_buffer),
                size,
                thread->ctx->get_lkey(),
                IBV_WR_RDMA_READ,
                true,
                false,
                thread->ctx->get_remote_neighborhood_mrt(mn),
                off,
                0,
                thread->create_wr_id());

  struct awaitable {
    byte_t* local_buffer;
    const u_ptr<ComputeThread>& thread;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    s_ptr<ReorderBlock> await_resume() const {
      return std::make_shared<ReorderBlock>(local_buffer, thread);
    }
  };

  return awaitable{local_buffer, thread};
}

/**
 * GraphBeyond CWC — coalesced per-hop fat read (only used when
 * lavd::Config::cwc_on(); the OFF path stays the literal
 * read_neighborhood call, so the published-LAVD behaviour is
 * byte-identical with zero drift risk).
 *
 * Allocates the local buffer and accounts the read bytes EXACTLY as
 * the single path (bytes/query unchanged), then PARKS the request
 * instead of posting. The scheduler coalesces up to B parked requests
 * into one linked WR chain with a single signaled completion (group
 * barrier). await_resume builds the Neighborhood from the same
 * pre-allocated buffer as read_neighborhood — block contents,
 * traversal, distances and recall are identical; only *how the reads
 * are issued and completed* differs.
 */
inline auto lavd_fetch_block(u32 slot, u32 mn, const u_ptr<ComputeThread>& thread) {
  const size_t stride = lavd::Config::stride();
  lib_assert(!lavd::varblock_on(),
             "LAVD CWC does not support variable-record Slabs");
  if (lavd::Config::native_packed_on()) {
    lib_assert(slot < lavd::Config::native_l0.resolver.total_slots,
               "LAVD-native: CWC slot outside packed sidecar");
  }
  const lavd::native::FixedL0ReadPlan plan = lavd::Config::native_packed_on()
      ? lavd::Config::native_l0.read_plan(slot)
      : lavd::native::sparse_fixed_read_plan(slot, mn, static_cast<u32>(stride), lavd::PARAMS_RESERVE);
  lib_assert(plan.owner_mn < thread->ctx->qps.size(),
             "LAVD CWC resolved memory-node id is out of range");
  lib_assert(plan.read_bytes > 0 && plan.read_bytes <= stride,
             "LAVD CWC read exceeds the local stride buffer");
  lib_assert(lavd::region_range_fits(
                 plan.remote_offset, plan.read_bytes,
                 lavd::Config::region_capacity_bytes()),
             "LAVD CWC read exceeds the registered remote region");
  byte_t* local_buffer = thread->buffer_allocator.allocate_neighborhood(thread->get_id());
  thread->stats.rdma_reads_in_bytes += plan.read_bytes;  // same accounting as single path
  thread->account_remote_read(plan.owner_mn, plan.read_bytes);
  thread->cwc_park(slot, plan.owner_mn, plan.remote_offset, plan.read_bytes, local_buffer);

  struct awaitable {
    byte_t* local_buffer;
    const u_ptr<ComputeThread>& thread;
    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}  // scheduler drives via post_balances
    s_ptr<Neighborhood> await_resume() const {
      return std::make_shared<Neighborhood>(local_buffer, thread);
    }
  };
  return awaitable{local_buffer, thread};
}

// GraphBeyond de-risk knob: SHINE_QTF_READ_BYTES caps the per-neighbor
// node RDMA read length (0 = full size_until_components()). Isolates the
// pure "smaller per-op payload -> QPS?" systems question WITHOUT real
// quantization. Distance comp still touches DIM floats (garbage tail,
// fine under --no-recall). Decides QTF-vs-LAVD priority before we spend
// 250+ LoC on real quantization.
inline size_t qtf_read_bytes(size_t full) {
  static const size_t cap = []() -> size_t {
    const char* e = std::getenv("SHINE_QTF_READ_BYTES");
    return e ? static_cast<size_t>(std::atoll(e)) : 0;
  }();
  return (cap > 0 && cap < full) ? cap : full;
}

inline auto read_node(RemotePtr rptr, const u_ptr<ComputeThread>& thread) {
  byte_t* node_ptr = thread->buffer_allocator.allocate_node(thread->get_id());

  const size_t rb = qtf_read_bytes(Node::size_until_components());
  thread->stats.rdma_reads_in_bytes += rb;
  thread->account_remote_read(rptr.memory_node(), rb);
  thread->account_remote_submit(rptr.memory_node());
  thread->track_post();

  const QP& qp = thread->ctx->qps[rptr.memory_node()]->qp;
  qp->post_send(reinterpret_cast<u64>(node_ptr),
                rb,
                thread->ctx->get_lkey(),
                IBV_WR_RDMA_READ,
                true,
                false,
                thread->ctx->get_remote_mrt(rptr.memory_node()),
                rptr.byte_offset(),
                0,
                thread->create_wr_id());

  struct awaitable {
    RemotePtr rptr;
    byte_t* node_ptr;
    const u_ptr<ComputeThread>& thread;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    s_ptr<Node> await_resume() { return std::make_shared<Node>(node_ptr, rptr, thread.get()); }
  };

  return awaitable{rptr, node_ptr, thread};
}

// Batched per-neighbor node read (within-coroutine fan-out).
//
// Cold-path optimization: a budget<1 expansion needs M per-neighbor reads
// from INDEX after the neighbor-list read. The published code issued M
// separate signaled posts -> M sequential RDMA RTTs serialized on the
// single coroutine. This helper coalesces them per MN (sharded build:
// home(uid) = uid % S, so M neighbors spread across up to S MNs) and
// posts each MN's slice as ONE chained BatchedREAD with a single
// signaled CQE per MN. wall-clock per cold expansion drops from
// ~M serialized RTTs to ~S parallel RTTs.
//
// SHINE_LAVD_COLD_BATCH=0 disables the path (single-read fallback,
// byte-identical to the published cold loop). The default is on.
//
// Per-neighbor visibility (visited bitmap / hashset) MUST still be
// enforced by the caller *after* await_resume -- here we just issue
// the I/O.
inline auto read_nodes_batch(const vec<RemotePtr>& rps,
                             const u_ptr<ComputeThread>& thread,
                             bool full_precision = false) {
  const size_t N = rps.size();
  vec<byte_t*> bufs;
  bufs.reserve(N);
  for (size_t i = 0; i < N; ++i) {
    bufs.push_back(thread->buffer_allocator.allocate_node(thread->get_id()));
  }

  if (N > 0) {
    const size_t rb = full_precision
                          ? Node::size_until_components()
                          : qtf_read_bytes(Node::size_until_components());
    const u32 lkey = thread->ctx->get_lkey();
    // Bucket indices by owner MN. Common case S<=8 so a tiny vec<vec<u32>>
    // is fine; for larger S a flat sort would be cheaper but this stays
    // clear and is off the hot (HOT-path) critical path.
    vec<vec<u32>> by_mn;
    for (u32 i = 0; i < N; ++i) {
      const u32 mn = rps[i].memory_node();
      if (mn >= by_mn.size()) by_mn.resize(mn + 1);
      by_mn[mn].push_back(i);
    }
    const u64 wr_id = thread->create_wr_id();
    for (u32 mn = 0; mn < by_mn.size(); ++mn) {
      if (by_mn[mn].empty()) continue;
      auto* mrt = thread->ctx->get_remote_mrt(mn);
      BatchedREAD br(by_mn[mn].size());
      for (size_t j = 0; j < by_mn[mn].size(); ++j) {
        const u32 idx = by_mn[mn][j];
        br.add_to_batch(reinterpret_cast<u64>(bufs[idx]),
                        mrt->address + rps[idx].byte_offset(),
                        static_cast<u32>(rb), lkey, mrt->rkey, wr_id,
                        /*signaled=*/false);  // post_batch flips last to signaled
      }
      br.post_batch(thread->ctx->qps[mn]->qp);
      thread->stats.rdma_reads_in_bytes += rb * by_mn[mn].size();
      thread->account_remote_read(mn, rb * by_mn[mn].size(),
                                  by_mn[mn].size());
      thread->account_remote_submit(mn);
      thread->track_post_batch(by_mn[mn].size());  // one signaled tail CQE per MN
    }
  }

  struct awaitable {
    vec<RemotePtr> rps;
    vec<byte_t*> bufs;
    const u_ptr<ComputeThread>& thread;
    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    vec<s_ptr<Node>> await_resume() {
      vec<s_ptr<Node>> nodes;
      nodes.reserve(bufs.size());
      for (size_t i = 0; i < bufs.size(); ++i) {
        nodes.push_back(std::make_shared<Node>(bufs[i], rps[i], thread.get()));
      }
      return nodes;
    }
  };
  return awaitable{rps, std::move(bufs), thread};
}

inline auto read_neighborlist(RemotePtr rptr, u32 level, const u_ptr<ComputeThread>& thread) {
  const size_t size = level > 0 ? Node::NEIGHBORLIST_SIZE : Node::NEIGHBORLIST_SIZE_ZERO;

  byte_t* local_buffer = level > 0 ? thread->buffer_allocator.allocate_layer(thread->get_id())
                                   : thread->buffer_allocator.allocate_layer_zero(thread->get_id());

  thread->stats.rdma_reads_in_bytes += size;
  thread->account_remote_read(rptr.memory_node(), size);
  thread->account_remote_submit(rptr.memory_node());
  thread->track_post();

  const QP& qp = thread->ctx->qps[rptr.memory_node()]->qp;
  qp->post_send(reinterpret_cast<u64>(local_buffer),
                size,
                thread->ctx->get_lkey(),
                IBV_WR_RDMA_READ,
                true,
                false,
                thread->ctx->get_remote_mrt(rptr.memory_node()),
                rptr.byte_offset(),
                0,
                thread->create_wr_id());

  struct awaitable {
    const u32 level;
    byte_t* local_buffer;
    const u_ptr<ComputeThread>& thread;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    s_ptr<Neighborlist> await_resume() const { return std::make_shared<Neighborlist>(level, local_buffer, thread); }
  };

  return awaitable{level, local_buffer, thread};
}

inline auto read_entry_point_ptr(const u_ptr<ComputeThread>& thread) {
  thread->stats.rdma_reads_in_bytes += sizeof(u64);
  thread->account_remote_read(0, sizeof(u64));
  thread->account_remote_submit(0);
  thread->track_post();

  const QP& qp = thread->ctx->qps[0]->qp;  // ep_ptr is always on memory node 0
  qp->post_send(reinterpret_cast<u64>(thread->coros_pointer_slot()),
                sizeof(u64),
                thread->ctx->get_lkey(),
                IBV_WR_RDMA_READ,
                true,
                false,
                thread->ctx->get_remote_mrt(0),
                8,  // ep_ptr is stored at the very beginning after the free_ptr
                0,
                thread->create_wr_id());

  struct awaitable {
    const u_ptr<ComputeThread>& thread;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    RemotePtr await_resume() const { return RemotePtr{*thread->coros_pointer_slot()}; }
  };

  return awaitable{thread};
}

/**
 * GraphBeyond C1: Speculative Wide-Beam Navigation.
 *
 * Issue N neighbor-list RDMA READs in a single batch (one doorbell ring).
 * Mirror of read_nodes() but for neighbor lists at a specific level.
 * All N reads share one coroutine suspension — the framework resumes the
 * coroutine after all N completions arrive on the CQ.
 *
 * @param rptrs    Remote pointers, each pointing at a neighbor-list region.
 *                 Caller is responsible for computing the per-level offset
 *                 via Node::compute_remote_neighborlist_offset(level).
 * @param level    Graph level (0 = base, >0 = upper). Determines list size.
 * @param thread   Compute thread (owns buffer pool + QPs).
 *
 * Cost accounting: each list contributes its level-specific size to
 * stats.rdma_reads_in_bytes; visited_neighborlists is bumped by the caller
 * (typically by the size of `rptrs`) so it stays consistent with the
 * single-read path.
 */
inline auto read_neighborlists_batch(const span<RemotePtr> rptrs,
                                     u32 level,
                                     const u_ptr<ComputeThread>& thread) {
  const size_t size = level > 0 ? Node::NEIGHBORLIST_SIZE : Node::NEIGHBORLIST_SIZE_ZERO;

  vec<s_ptr<Neighborlist>> nlists;
  nlists.reserve(rptrs.size());

  for (auto& rptr : rptrs) {
    byte_t* local_buffer = level > 0
                             ? thread->buffer_allocator.allocate_layer(thread->get_id())
                             : thread->buffer_allocator.allocate_layer_zero(thread->get_id());

    nlists.emplace_back(std::make_shared<Neighborlist>(level, local_buffer, thread));

    thread->stats.rdma_reads_in_bytes += size;
    thread->account_remote_read(rptr.memory_node(), size);
    thread->account_remote_submit(rptr.memory_node());
    thread->track_post();

    const QP& qp = thread->ctx->qps[rptr.memory_node()]->qp;
    qp->post_send(reinterpret_cast<u64>(local_buffer),
                  size,
                  thread->ctx->get_lkey(),
                  IBV_WR_RDMA_READ,
                  true,
                  false,
                  thread->ctx->get_remote_mrt(rptr.memory_node()),
                  rptr.byte_offset(),
                  0,
                  thread->create_wr_id());
  }

  struct awaitable {
    vec<s_ptr<Neighborlist>> nlists;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    vec<s_ptr<Neighborlist>> await_resume() const { return nlists; }
  };

  return awaitable{nlists};
}

inline auto read_nodes(const span<RemotePtr> remote_ptrs, const u_ptr<ComputeThread>& thread) {
  vec<s_ptr<Node>> nodes;
  nodes.reserve(remote_ptrs.size());

  for (auto& rptr : remote_ptrs) {
    byte_t* node_ptr = thread->buffer_allocator.allocate_node(thread->get_id());
    nodes.emplace_back(std::make_shared<Node>(node_ptr, rptr, thread.get()));

    thread->stats.rdma_reads_in_bytes += Node::size_until_components();
    thread->account_remote_read(rptr.memory_node(),
                                Node::size_until_components());
    thread->account_remote_submit(rptr.memory_node());
    thread->track_post();

    const QP& qp = thread->ctx->qps[rptr.memory_node()]->qp;
    qp->post_send(reinterpret_cast<u64>(node_ptr),
                  Node::size_until_components(),
                  thread->ctx->get_lkey(),
                  IBV_WR_RDMA_READ,
                  true,
                  false,
                  thread->ctx->get_remote_mrt(rptr.memory_node()),
                  rptr.byte_offset(),
                  0,
                  thread->create_wr_id());
  }

  struct awaitable {
    vec<s_ptr<Node>> nodes;

    static bool await_ready() { return false; }
    static void await_suspend(std::coroutine_handle<>) {}
    vec<s_ptr<Node>> await_resume() const { return nodes; }  // every node will be freed by Node's dtor
  };

  return awaitable{nodes};
}

}  // namespace rdma
