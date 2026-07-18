#pragma once

#include <cstdint>

namespace index_region {

using bytes_t = std::uint64_t;

constexpr bytes_t ALIGNMENT = 64;
constexpr bytes_t MIN_CAPACITY_BYTES = 1ULL << 20;
constexpr bytes_t LEGACY_DEFAULT_BYTES = 4ULL << 30;
constexpr bytes_t MAX_CAPACITY_BYTES = 48ULL << 30;

inline bytes_t g_capacity_bytes = LEGACY_DEFAULT_BYTES;

inline bytes_t resolve_capacity_bytes(bytes_t requested_bytes) {
  return requested_bytes == 0 ? LEGACY_DEFAULT_BYTES : requested_bytes;
}

inline bool is_valid_capacity_bytes(bytes_t bytes) {
  return bytes >= MIN_CAPACITY_BYTES && bytes <= MAX_CAPACITY_BYTES &&
         bytes % ALIGNMENT == 0;
}

inline void set_capacity_bytes(bytes_t bytes) { g_capacity_bytes = bytes; }

inline bytes_t capacity_bytes() { return g_capacity_bytes; }

}  // namespace index_region
