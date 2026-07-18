#pragma once

#include <coroutine>

#include "common/slot_bitmap.hh"
#include "hnsw/heap.hh"
#include "lavd/config.hh"
#include "remote_pointer.hh"

/**
 * Coroutines called by other coroutines.
 * Handle is destroyed by the destructor to prevent memory leaks.
 */
struct MinorCoroutine {
  struct promise_type {
    MinorCoroutine get_return_object() { return MinorCoroutine{Handle::from_promise(*this)}; }
    // std::suspend_never directly runs the coroutine (the object is created after first suspend)
    static std::suspend_never initial_suspend() { return {}; }
    static std::suspend_always final_suspend() noexcept { return {}; }
    static void return_void() {}
    static void unhandled_exception() { throw; }
  };

  using Handle = std::coroutine_handle<promise_type>;

  explicit MinorCoroutine(Handle handle) : handle(handle) {}

  ~MinorCoroutine() {
    if (handle) {
      handle.destroy();
    }
  }

  MinorCoroutine(const MinorCoroutine&) = delete;
  MinorCoroutine(MinorCoroutine&&) = delete;
  MinorCoroutine& operator=(const MinorCoroutine&) = delete;
  MinorCoroutine& operator=(MinorCoroutine&&) noexcept = delete;

  Handle handle;
};

/**
 * Fixed number of HNSWCoroutines per ComputeThread.
 * Method schedule() in scheduler.hh is responsible for destroying HNSWCoroutine handles.
 */
struct HNSWCoroutine {
  struct promise_type {
    HNSWCoroutine get_return_object() { return HNSWCoroutine{Handle::from_promise(*this)}; }
    // std::suspend_always directly creates the coroutine object
    // (otherwise we cannot access our members before the first co_await)
    static std::suspend_always initial_suspend() { return {}; }
    static std::suspend_always final_suspend() noexcept { return {}; }
    static void return_void() {}
    static void unhandled_exception() { throw; }
  };

  using Handle = std::coroutine_handle<promise_type>;
  Handle handle;

  // HNSW parameters
  RemotePtr cached_ep_ptr{};
  hashset_t<RemotePtr> visited_nodes{};
  MaxHeap top_candidates{};
  MinHeap next_candidates{};

  // T6: slot-indexed visited bitmap (GB_BITMAP_DEDUP=1). Only allocated
  // / used by the LAVD level-0 beam (search_level_lavd) when the env
  // flag is set; otherwise dormant and visited_nodes (hashset) carries
  // the dedup as before. Both paths produce byte-identical visited
  // sequences (slot <-> RemotePtr is a build-time bijection in LAVD).
  gb::SlotBitmap visited_slots{};

  // GraphBeyond LAVD scratch (unused when --lavd 0).
  vec<u8> lavd_qcode{};            // query encoded once per query (scalar SQ)
  vec<f32> lavd_lut{};             // per-query PQ ADC table (m*256); per-coroutine
                                   // because coroutines interleave queries
  vec<lavd::LavdCand> lavd_cands{};  // level-0 beam result (asc by approx d)

  // CRANE Phase-0 instrumentation: per-coroutine RDMA post counts by
  // search phase (0=upper-nav, 1=level-0 beam, 2=fp32 rerank).
  // Interleaving-invariant (per-coroutine, a pure count). Accumulated
  // into thread stats at query completion, then reset.
  u64 ph_posts[3]{};
  u32 cur_phase{0};

  // Query-local tracing counters (env-gated by GB_QUERY_TRACE in hnsw.hh).
  // `q_trace_posts` counts posts issued directly by this coroutine; CWC posts
  // are issued by the scheduler and attributed as a fractional physical post.
  u64 q_trace_posts{0};
  f64 q_trace_cwc_posts{0};
  u64 q_trace_cwc_batched_reads{0};
};
