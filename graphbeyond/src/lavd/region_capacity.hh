#pragma once

#include <cstdint>

#include "lavd/native_descriptor.hh"

namespace lavd {

constexpr std::uint64_t REGION_CAPACITY_ALIGNMENT = 64;
constexpr std::uint64_t REGION_CAPACITY_LEGACY_DEFAULT_BYTES =
    6ull * 1073741824ull;
constexpr std::uint64_t REGION_CAPACITY_EXPLICIT_MAX_BYTES =
    128ull * 1073741824ull;

constexpr std::uint64_t minimum_region_capacity_bytes() noexcept {
  constexpr std::uint64_t params_reserve =
      static_cast<std::uint64_t>(native::PARAMS_RESERVE_BYTES);
  constexpr std::uint64_t descriptor_header =
      static_cast<std::uint64_t>(native::DESCRIPTOR_BYTES);
  return params_reserve > descriptor_header ? params_reserve : descriptor_header;
}

constexpr std::uint64_t resolve_region_capacity_bytes(
    std::uint64_t requested_bytes, std::uint64_t legacy_max_bytes) noexcept {
  return requested_bytes == 0 ? legacy_max_bytes : requested_bytes;
}

constexpr bool is_valid_region_capacity_bytes(
    std::uint64_t resolved_bytes, std::uint64_t maximum_bytes) noexcept {
  return resolved_bytes >= minimum_region_capacity_bytes() &&
         resolved_bytes <= maximum_bytes &&
         resolved_bytes % REGION_CAPACITY_ALIGNMENT == 0;
}

constexpr bool region_range_fits(std::uint64_t offset_bytes,
                                 std::uint64_t length_bytes,
                                 std::uint64_t capacity_bytes) noexcept {
  return offset_bytes <= capacity_bytes &&
         length_bytes <= capacity_bytes - offset_bytes;
}

}  // namespace lavd
