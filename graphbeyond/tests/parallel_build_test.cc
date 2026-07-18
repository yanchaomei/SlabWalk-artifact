#include <atomic>
#include <cassert>
#include <cstdlib>
#include <vector>

#include "lavd/parallel_build.hh"

int main() {
  setenv("SHINE_LAVD_BUILD_THREADS", "4", 1);
  assert(lavd::build_worker_count(100) == 4);
  assert(lavd::build_worker_count(2) == 2);

  setenv("SHINE_LAVD_BUILD_THREADS", "1", 1);
  assert(lavd::build_worker_count(100) == 1);

  setenv("SHINE_LAVD_BUILD_THREADS", "invalid", 1);
  const unsigned fallback = lavd::build_worker_count(100);
  assert(fallback >= 1 && fallback <= 32);

  setenv("SHINE_LAVD_BUILD_CPU_BASE", "1", 1);
  setenv("SHINE_LAVD_BUILD_CPU_STRIDE", "2", 1);
  assert(lavd::build_worker_cpu(0, 80) == 1);
  assert(lavd::build_worker_cpu(19, 80) == 39);
  assert(lavd::build_worker_cpu(40, 80) == 1);

  constexpr unsigned N = 1003;
  std::vector<std::atomic<unsigned>> visits(N);
  for (auto& visit : visits) visit.store(0);
  lavd::parallel_for_u32(N, 7, [&](unsigned begin, unsigned end, unsigned worker) {
    assert(worker < 7);
    assert(begin <= end && end <= N);
    for (unsigned uid = begin; uid < end; ++uid) ++visits[uid];
  });
  for (const auto& visit : visits) assert(visit.load() == 1);

  unsetenv("SHINE_LAVD_BUILD_THREADS");
  unsetenv("SHINE_LAVD_BUILD_CPU_BASE");
  unsetenv("SHINE_LAVD_BUILD_CPU_STRIDE");
  return 0;
}
