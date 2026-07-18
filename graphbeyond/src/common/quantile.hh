#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace statistics {

inline constexpr std::size_t latency_sample_reserve_per_thread(
    std::size_t local_query_count, std::size_t /*num_threads*/) {
  // The router is work-conserving, so fast workers can consume more than the
  // average share.  Reserving the full local pool keeps vector growth outside
  // the measured query phase; normal evaluation pools are only 10K queries.
  return local_query_count;
}

inline std::uint64_t nearest_rank_quantile_sorted(
    const std::vector<std::uint64_t>& values, double quantile) {
  if (values.empty()) return 0;
  if (quantile <= 0.0) quantile = 0.0;
  if (quantile >= 1.0) quantile = 1.0;
  const std::size_t rank = quantile == 0.0
      ? 0
      : static_cast<std::size_t>(std::ceil(quantile * values.size())) - 1;
  return values[std::min(rank, values.size() - 1)];
}

inline std::uint64_t nearest_rank_quantile(
    std::vector<std::uint64_t> values, double quantile) {
  std::sort(values.begin(), values.end());
  return nearest_rank_quantile_sorted(values, quantile);
}

}  // namespace statistics
