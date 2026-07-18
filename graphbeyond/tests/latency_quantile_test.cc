#include <cassert>
#include <cstdint>
#include <vector>

#include "../src/common/quantile.hh"

int main() {
  using statistics::nearest_rank_quantile;

  assert(nearest_rank_quantile(std::vector<std::uint64_t>{4, 1, 3, 2}, 0.50) == 2);

  std::vector<std::uint64_t> values;
  for (std::uint64_t value = 1; value <= 100; ++value) values.push_back(value);
  assert(nearest_rank_quantile(values, 0.95) == 95);
  assert(nearest_rank_quantile(values, 0.99) == 99);
  assert(nearest_rank_quantile(values, 1.00) == 100);
  assert(nearest_rank_quantile({}, 0.50) == 0);
  assert(statistics::nearest_rank_quantile_sorted(values, 0.99) == 99);
  // Dynamic query routing is intentionally unbalanced.  Reserving only the
  // average per-thread share can reallocate inside the measured phase, so
  // each thread reserves the complete local query pool.
  assert(statistics::latency_sample_reserve_per_thread(10000, 10) == 10000);
  assert(statistics::latency_sample_reserve_per_thread(0, 40) == 0);
}
