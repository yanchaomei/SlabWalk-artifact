#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>

#include "common/startup_wire.hh"

int main() {
  const configuration::Parameters params{
      0x01020304u, 1u, 0u, 0x090au, 0x05060708u};
  std::array<std::uint8_t, sizeof(params)> bytes{};
  std::memcpy(bytes.data(), &params, sizeof(params));
  const std::array<std::uint8_t, sizeof(params)> expected{
      0x04, 0x03, 0x02, 0x01, 0x01, 0x00,
      0x0a, 0x09, 0x08, 0x07, 0x06, 0x05};
  assert(bytes == expected);
  assert(params.query_contexts == 0x090au);

  const configuration::LavdStartupContract contract{2u, 8u, 10u, 0u, 65536u};
  assert(contract.version == 2u);
  assert(contract.lavd_bits == 8u);
  assert(contract.query_contexts == 10u);
  assert(contract.region_capacity_bytes == 65536u);

  std::cout << "startup_wire_test PASS" << std::endl;
  return 0;
}
