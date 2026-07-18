#pragma once

#include <cstdint>

namespace lavd {

// Reserve half of a QP's SQ for interleaved graph/search work when possible.
// In the worst case every coroutine reranks candidates on the same MN, so the
// per-coroutine chunk is derived from the shared queue depth, not from R alone.
constexpr std::uint32_t queue_safe_rerank_chunk(
    std::uint32_t max_send_queue_wr,
    std::uint32_t num_coroutines) noexcept {
  if (max_send_queue_wr == 0 || num_coroutines == 0 ||
      num_coroutines > max_send_queue_wr) {
    return 0;
  }
  const std::uint64_t denominator =
      static_cast<std::uint64_t>(num_coroutines) * 2u;
  const std::uint32_t half_queue_share = static_cast<std::uint32_t>(
      static_cast<std::uint64_t>(max_send_queue_wr) / denominator);
  return half_queue_share == 0 ? 1u : half_queue_share;
}

}  // namespace lavd
