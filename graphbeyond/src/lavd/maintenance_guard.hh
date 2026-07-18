#pragma once

#include <cstddef>
#include <string_view>

namespace lavd {

inline std::string_view offline_rematerialization_rejection(
    std::size_t memory_nodes, bool native_packed, bool reordered,
    bool variable_records) {
  if (memory_nodes != 1) return "requires exactly one memory node";
  if (native_packed) return "does not support native packed placement";
  if (reordered) return "does not support reordered placement";
  if (variable_records) return "does not support variable records";
  return {};
}

inline std::string_view offline_mirror_growth_rejection(
    std::size_t current_bytes, std::size_t next_bytes,
    std::size_t mirror_capacity) {
  if (next_bytes < current_bytes) return "authoritative index shrank";
  if (next_bytes > mirror_capacity)
    return "authoritative index exceeds mirror capacity";
  return {};
}

}  // namespace lavd
