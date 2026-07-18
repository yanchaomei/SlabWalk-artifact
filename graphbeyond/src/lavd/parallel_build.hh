#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <limits>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <pthread.h>
#include <sched.h>
#endif

namespace lavd {

inline std::uint32_t build_worker_count(std::uint32_t items) {
  if (items == 0) return 1;
  std::uint32_t requested = 0;
  if (const char* env = std::getenv("SHINE_LAVD_BUILD_THREADS")) {
    char* end = nullptr;
    const unsigned long parsed = std::strtoul(env, &end, 10);
    if (end != env && *end == '\0' && parsed > 0) {
      requested = static_cast<std::uint32_t>(
          std::min<unsigned long>(parsed, 32));
    }
  }
  if (requested == 0) {
    requested = std::thread::hardware_concurrency();
    if (requested == 0) requested = 1;
    requested = std::min<std::uint32_t>(requested, 32);
  }
  return std::max<std::uint32_t>(
      1, std::min<std::uint32_t>(requested, items));
}

inline std::uint32_t build_worker_cpu(std::uint32_t worker,
                                      std::uint32_t online_cpus) {
  const char* base_env = std::getenv("SHINE_LAVD_BUILD_CPU_BASE");
  if (base_env == nullptr || online_cpus == 0) {
    return std::numeric_limits<std::uint32_t>::max();
  }
  char* base_end = nullptr;
  const unsigned long base = std::strtoul(base_env, &base_end, 10);
  if (base_end == base_env || *base_end != '\0') {
    return std::numeric_limits<std::uint32_t>::max();
  }
  unsigned long stride = 1;
  if (const char* stride_env =
          std::getenv("SHINE_LAVD_BUILD_CPU_STRIDE")) {
    char* stride_end = nullptr;
    const unsigned long parsed =
        std::strtoul(stride_env, &stride_end, 10);
    if (stride_end != stride_env && *stride_end == '\0' && parsed > 0) {
      stride = parsed;
    }
  }
  return static_cast<std::uint32_t>(
      (base + static_cast<unsigned long>(worker) * stride) % online_cpus);
}

inline void pin_build_worker(std::uint32_t worker) {
#if defined(__linux__)
  const std::uint32_t online = std::thread::hardware_concurrency();
  const std::uint32_t cpu = build_worker_cpu(worker, online);
  if (cpu == std::numeric_limits<std::uint32_t>::max()) return;
  cpu_set_t cpuset;
  CPU_ZERO(&cpuset);
  CPU_SET(cpu, &cpuset);
  if (pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset) != 0) {
    std::abort();
  }
#else
  (void)worker;
#endif
}

template <typename Fn>
inline void parallel_for_u32(std::uint32_t items, std::uint32_t workers,
                             Fn&& fn) {
  if (items == 0) return;
  workers = std::max<std::uint32_t>(
      1, std::min<std::uint32_t>(workers, items));
  if (workers == 1) {
    fn(0, items, 0);
    return;
  }

  std::vector<std::thread> threads;
  threads.reserve(workers);
  for (std::uint32_t worker = 0; worker < workers; ++worker) {
    const std::uint32_t begin = static_cast<std::uint32_t>(
        (static_cast<std::uint64_t>(items) * worker) / workers);
    const std::uint32_t end = static_cast<std::uint32_t>(
        (static_cast<std::uint64_t>(items) * (worker + 1)) / workers);
    threads.emplace_back([&, begin, end, worker] {
      pin_build_worker(worker);
      fn(begin, end, worker);
    });
  }
  for (auto& thread : threads) thread.join();
}

}  // namespace lavd
