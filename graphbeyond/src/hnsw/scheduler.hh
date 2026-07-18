#pragma once

#include <algorithm>

#include <library/batched_read.hh>

#include "hnsw.hh"
#include "router/query_router.hh"

namespace hnsw {

static HNSWCoroutine dummy_coroutine() {
  co_return;
}

/**
 * @brief Schedules coroutine processing (HNSW inserts or knn-queries) of a compute thread.

 * @tparam insert If true, hnsw.insert() is called, hnsw.knn() otherwise.
 * @param next_idx Next unprocessed slot. Shared across all threads. Increased via FAA. Only used for inserts.
 * @param db Either containing the (partial) vectors to insert or (partial) queries.
 */
template <class Distance, bool insert>
void schedule(HNSW<Distance>& hnsw,
              std::atomic<idx_t>& next_idx,
              io::Database<element_t>& db,
              u32 num_coroutines,
              const u_ptr<ComputeThread>& thread,
              query_router::QueryRouter<Distance>* query_router = nullptr) {
  const auto print_status = [&db](idx_t slot) {
    if (slot % (db.num_vectors_total / 10) == 0) {
      std::cerr << (insert ? "insert " : "query ") << db.get_id(slot) << "/" << db.num_vectors_total << std::endl;
    }
  };

  if constexpr (not insert) {
    lib_assert(query_router, "invalid query_router");
  }

  // initialize coroutines
  thread->coroutines.reserve(num_coroutines);
  for (u32 i = 0; i < num_coroutines; ++i) {
    thread->coroutines.emplace_back(std::make_unique<HNSWCoroutine>(dummy_coroutine()));
  }

  for (;;) {
    bool all_done = true;
    bool progressed = false;  // CWC: a coroutine resumed or got a new query this sweep
    for (u32 coroutine_id = 0; coroutine_id < thread->coroutines.size(); ++coroutine_id) {
      auto& coroutine = *thread->coroutines[coroutine_id];
      thread->poll_cq();

      // recycle coroutine (assign new query)
      if (coroutine.handle.done()) {
        if constexpr (insert) {
          const idx_t slot = next_idx.fetch_add(1);

          if (slot < db.num_vectors_read) {
            print_status(slot);
            all_done = false;

            coroutine.handle.destroy();
            thread->set_current_coroutine(coroutine_id);

            coroutine.handle = hnsw.insert(db.get_id(slot), db.get_components(slot), thread).handle;
            progressed = true;
          }

        } else {
          if (not query_router->done || query_router->queue_size > 0) {
            idx_t slot;
            all_done = false;

            if (query_router->query_queue.try_dequeue(slot)) {
              query_router->queue_size.fetch_sub(1);
              print_status(slot);

              coroutine.handle.destroy();
              thread->set_current_coroutine(coroutine_id);

              coroutine.handle = hnsw.knn(db.get_id(slot), db.get_components(slot), thread).handle;
              progressed = true;
            }
          }
        }

        // resume coroutine
      } else if (thread->is_ready(coroutine_id)) {
        all_done = false;
        progressed = true;

        thread->set_current_coroutine(coroutine_id);
        coroutine.handle.resume();

        // keep polling
      } else {
        all_done = false;
      }
    }

    // GraphBeyond CWC — coalesced frontier flush. Resumed level-0
    // coroutines park their uniform fat-block reads in
    // thread->cwc_pending() instead of posting individually; here we
    // drain them into linked WR chains with ONE signaled completion
    // per chain (group barrier). Flush a chain when it reaches width B,
    // or — to stay deadlock-free — flush whatever is parked when the
    // sweep made no progress (every live coroutine is parked).
    if constexpr (not insert) {
      if (lavd::Config::cwc_on()) {
        auto& pend = thread->cwc_pending();
        const u32 B = lavd::Config::cwc_width();
        const bool stalled = !progressed;
        // Multi-MN CWC bucket fix: under interleaved owners the existing
        // contiguous-same-mn scan (line below) was finding 1-element runs
        // and never reaching width B. Stable-sort the parked queue by
        // mn so same-mn requests cluster, restoring the full B-wide
        // coalescing under multi-MN topologies. Single-MN preserves
        // FIFO order trivially (stable_sort with equal keys is a no-op).
        std::stable_sort(pend.begin(), pend.end(),
                         [](const auto& a, const auto& b) { return a.mn < b.mn; });
        while (!pend.empty() && (pend.size() >= B || stalled)) {
          // contiguous run of same-memory-node requests, capped at B
          const u32 mn = pend.front().mn;
          u32 n = 0;
          while (n < pend.size() && n < B && pend[n].mn == mn) {
            ++n;
          }
          vec<u32> members;
          members.reserve(n);
          for (u32 i = 0; i < n; ++i) {
            members.push_back(pend[i].cid);
          }
          const u32 lkey = thread->ctx->get_lkey();
          auto* mrt = thread->ctx->get_remote_neighborhood_mrt(mn);
          BatchedREAD br(n);
          if (lavd::Config::cwc_allsig) {
            // debug isolation: linked post but K signaled CQEs (each
            // clears its own coroutine via the non-group poll path).
            for (u32 i = 0; i < n; ++i) {
              br.add_to_batch(reinterpret_cast<u64>(pend[i].buffer),
                              mrt->address + pend[i].remote_offset,
                              pend[i].read_bytes, lkey, mrt->rkey,
                              encode_64bit(thread->ctx_tid, pend[i].cid), true);
            }
          } else {
            // production: only the final WR signaled -> ONE CQE whose
            // group handle releases all n member coroutines.
            const u64 gwr = thread->cwc_group_wr_id(thread->cwc_alloc_group(members));
            for (u32 i = 0; i < n; ++i) {
              br.add_to_batch(reinterpret_cast<u64>(pend[i].buffer),
                              mrt->address + pend[i].remote_offset,
                              pend[i].read_bytes, lkey, mrt->rkey,
                              (i + 1 == n) ? gwr : 0, false);
            }
          }
          br.post_batch(thread->ctx->qps[mn]->qp);
          thread->account_remote_submit(mn);
          thread->track_cwc_batch(members);
          pend.erase(pend.begin(), pend.begin() + n);
          if (!stalled && pend.size() < B) {
            break;  // keep the sub-B remainder to coalesce next sweep
          }
        }
      }
    }

    if (all_done) {
      break;
    }
  }

  for (const auto& coroutine : thread->coroutines) {
    lib_assert(coroutine->handle.done(), "coroutine not done yet");
    coroutine->handle.destroy();
  }
}

}  // namespace hnsw
