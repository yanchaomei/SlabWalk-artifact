#include <cassert>
#include <cstdint>
#include <iostream>

#include "lavd/region_capacity.hh"

int main() {
  constexpr std::uint64_t legacy_default =
      lavd::REGION_CAPACITY_LEGACY_DEFAULT_BYTES;
  constexpr std::uint64_t maximum =
      lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES;
  constexpr std::uint64_t minimum = lavd::minimum_region_capacity_bytes();
  constexpr std::uint64_t exact = minimum + 4096;

  static_assert(
      minimum >= static_cast<std::uint64_t>(lavd::native::PARAMS_RESERVE_BYTES));
  static_assert(
      minimum >= static_cast<std::uint64_t>(lavd::native::DESCRIPTOR_BYTES));

  assert(lavd::resolve_region_capacity_bytes(0, legacy_default) ==
         legacy_default);
  assert(lavd::is_valid_region_capacity_bytes(legacy_default, maximum));
  assert(lavd::is_valid_region_capacity_bytes(maximum, maximum));

  assert(lavd::resolve_region_capacity_bytes(exact, legacy_default) == exact);
  assert(lavd::is_valid_region_capacity_bytes(exact, maximum));

  assert(lavd::is_valid_region_capacity_bytes(minimum, maximum));
  assert(!lavd::is_valid_region_capacity_bytes(minimum + 1, maximum));
  assert(lavd::is_valid_region_capacity_bytes(minimum + 64, maximum));
  assert(!lavd::is_valid_region_capacity_bytes(minimum - 1, maximum));
  assert(!lavd::is_valid_region_capacity_bytes(0, maximum));
  assert(!lavd::is_valid_region_capacity_bytes(maximum + 1, maximum));

  assert(lavd::region_range_fits(0, minimum, minimum));
  assert(lavd::region_range_fits(minimum, 0, minimum));
  assert(lavd::region_range_fits(minimum - 64, 64, minimum));
  assert(!lavd::region_range_fits(minimum - 64, 65, minimum));
  assert(!lavd::region_range_fits(minimum + 1, 0, minimum));
  assert(!lavd::region_range_fits(UINT64_MAX - 7, 8, UINT64_MAX));

  std::cout << "native_region_capacity_test PASS" << std::endl;
  return 0;
}
