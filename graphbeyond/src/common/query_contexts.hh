#pragma once

#include <algorithm>
#include <cstdint>

namespace query_contexts {

using count_t = std::uint32_t;

constexpr count_t LEGACY_DEFAULT = 4;
constexpr count_t MAX_CONTEXTS = 40;

constexpr bool is_valid_request(count_t threads, count_t requested) {
  return threads > 0 &&
         (requested == 0 ||
          (requested <= threads && requested <= MAX_CONTEXTS));
}

constexpr count_t resolve(count_t threads, count_t requested) {
  if (!is_valid_request(threads, requested)) return 0;
  return requested == 0 ? std::min(threads, LEGACY_DEFAULT) : requested;
}

constexpr count_t max_threads_per_context(count_t threads,
                                          count_t contexts) {
  return contexts == 0 ? 0 : (threads + contexts - 1) / contexts;
}

}  // namespace query_contexts
