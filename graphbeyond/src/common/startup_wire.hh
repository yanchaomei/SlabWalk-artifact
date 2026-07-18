#pragma once

#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace configuration {

// Canonical 12-byte CN-to-MN frame. query_contexts occupies the former
// two-byte padding slot, preserving every legacy field offset and wire size.
struct Parameters {
  std::uint32_t num_threads{};
  std::uint8_t use_cache{};
  std::uint8_t routing{};
  std::uint16_t query_contexts{};
  std::uint32_t lavd_bits{};
};
static_assert(sizeof(Parameters) == 12,
              "baseline CN-to-MN Parameters wire size changed");
static_assert(offsetof(Parameters, num_threads) == 0);
static_assert(offsetof(Parameters, use_cache) == 4);
static_assert(offsetof(Parameters, routing) == 5);
static_assert(offsetof(Parameters, query_contexts) == 6);
static_assert(offsetof(Parameters, lavd_bits) == 8);
static_assert(std::has_unique_object_representations_v<Parameters>,
              "Parameters contains implicit wire padding");

// Initiator-to-CN startup agreement. This is exchanged only among compute
// nodes; it does not alter the baseline CN-to-MN protocol.
struct LavdStartupContract {
  std::uint32_t version{2};
  std::uint32_t lavd_bits{};
  std::uint32_t query_contexts{};
  std::uint32_t reserved{};
  std::uint64_t region_capacity_bytes{};
};
static_assert(sizeof(LavdStartupContract) == 24);
static_assert(std::has_unique_object_representations_v<LavdStartupContract>);

}  // namespace configuration
