#pragma once

#include <algorithm>
#include <cstdint>
#include <utility>
#include <vector>

#include "lavd/parallel_build.hh"

namespace lavd {

template <typename VisitNeighbors, typename CounterAt>
inline void parallel_accumulate_indegree_u32(
    std::uint32_t items, std::uint32_t workers,
    VisitNeighbors&& visit_neighbors, CounterAt&& counter_at) {
  if (items == 0u) return;

  workers = std::max<std::uint32_t>(
      1u, std::min<std::uint32_t>(workers, items));
  if (workers == 1u) {
    for (std::uint32_t uid = 0; uid < items; ++uid) {
      visit_neighbors(uid, [&](std::uint32_t neighbor) {
        if (neighbor < items) ++counter_at(neighbor);
      });
    }
    return;
  }

  parallel_for_u32(
      items, workers,
      [&](std::uint32_t begin, std::uint32_t end, std::uint32_t) {
        for (std::uint32_t uid = begin; uid < end; ++uid) {
          visit_neighbors(uid, [&](std::uint32_t neighbor) {
            if (neighbor >= items) return;
            auto& counter = counter_at(neighbor);
            __atomic_fetch_add(&counter, std::uint64_t{1},
                               __ATOMIC_RELAXED);
          });
        }
      });
}

template <typename VisitNeighbors>
inline std::vector<std::uint64_t> parallel_indegree_u32(
    std::uint32_t items, std::uint32_t workers,
    VisitNeighbors&& visit_neighbors) {
  std::vector<std::uint64_t> indegree(items, 0u);
  parallel_accumulate_indegree_u32(
      items, workers, std::forward<VisitNeighbors>(visit_neighbors),
      [&](std::uint32_t uid) -> std::uint64_t& { return indegree[uid]; });
  return indegree;
}

}  // namespace lavd
