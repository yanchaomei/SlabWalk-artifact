#include <cassert>
#include <cstdint>
#include <iostream>

#include "common/index_region_capacity.hh"

int main() {
  using namespace index_region;

  assert(resolve_capacity_bytes(0) == LEGACY_DEFAULT_BYTES);
  assert(resolve_capacity_bytes(16ULL << 30) == (16ULL << 30));

  assert(is_valid_capacity_bytes(1ULL << 20));
  assert(is_valid_capacity_bytes(16ULL << 30));
  assert(is_valid_capacity_bytes(MAX_CAPACITY_BYTES));
  assert(!is_valid_capacity_bytes((1ULL << 20) - ALIGNMENT));
  assert(!is_valid_capacity_bytes((16ULL << 30) + 1));
  assert(!is_valid_capacity_bytes(MAX_CAPACITY_BYTES + ALIGNMENT));

  set_capacity_bytes(16ULL << 30);
  assert(capacity_bytes() == (16ULL << 30));
  set_capacity_bytes(LEGACY_DEFAULT_BYTES);
  assert(capacity_bytes() == LEGACY_DEFAULT_BYTES);

  std::cout << "index_region_capacity_test PASS\n";
  return 0;
}
