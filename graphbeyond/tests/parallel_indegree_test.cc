#include <cassert>
#include <cstdint>
#include <vector>

#include "lavd/materialization_policy.hh"
#include "lavd/parallel_indegree.hh"

int main() {
  const std::vector<std::vector<std::uint32_t>> graph{
      {1, 2, 2, 99}, {2, 3}, {0, 3, 4}, {4}, {0, 1, 3, 5}, {5}};
  const std::vector<std::uint64_t> expected{2, 2, 3, 3, 2, 2};

  std::uint64_t reference_hash = 0;
  std::vector<std::uint32_t> reference_uids;
  for (const std::uint32_t workers : {1u, 2u, 3u, 6u, 12u}) {
    const auto indegree = lavd::parallel_indegree_u32(
        static_cast<std::uint32_t>(graph.size()), workers,
        [&](std::uint32_t uid, const auto& emit) {
          for (const std::uint32_t neighbor : graph[uid]) emit(neighbor);
        });
    assert(indegree == expected);

    std::vector<lavd::materialization::Candidate> candidates(graph.size());
    for (std::uint32_t uid = 0; uid < graph.size(); ++uid) {
      candidates[uid] = lavd::materialization::Candidate{
          uid, 16u + 8u * graph[uid].size(), indegree[uid],
          static_cast<std::uint32_t>(graph[uid].size()), uid % 3u};
    }
    const auto selection = lavd::materialization::select_records(
        candidates, lavd::materialization::Policy::kIndegree,
        /*requested_bytes=*/168u, /*fixed_bytes=*/32u);
    assert(selection.valid);
    if (reference_uids.empty()) {
      reference_uids = selection.selected_uids;
      reference_hash = selection.selection_hash;
    } else {
      assert(selection.selected_uids == reference_uids);
      assert(selection.selection_hash == reference_hash);
    }
  }

  struct StridedCounter {
    std::uint32_t uid = 0;
    std::uint64_t indegree = 0;
    std::uint32_t payload = 0;
  };
  std::vector<StridedCounter> direct(graph.size());
  lavd::parallel_accumulate_indegree_u32(
      static_cast<std::uint32_t>(graph.size()), 6u,
      [&](std::uint32_t uid, const auto& emit) {
        for (const std::uint32_t neighbor : graph[uid]) emit(neighbor);
      },
      [&](std::uint32_t uid) -> std::uint64_t& {
        return direct[uid].indegree;
      });
  for (std::uint32_t uid = 0; uid < graph.size(); ++uid) {
    assert(direct[uid].indegree == expected[uid]);
  }

  const auto empty = lavd::parallel_indegree_u32(
      0u, 8u, [](std::uint32_t, const auto&) { assert(false); });
  assert(empty.empty());
  return 0;
}
