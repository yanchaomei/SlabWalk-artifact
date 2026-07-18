#include <array>
#include <cassert>
#include <cstdint>
#include <string>

#include "common/evidence_hash.hh"

int main() {
  evidence::Fnv1a64 empty;
  assert(empty.hex() == "cbf29ce484222325");

  constexpr std::array<std::uint8_t, 5> hello{'h', 'e', 'l', 'l', 'o'};
  evidence::Fnv1a64 contiguous;
  contiguous.update(hello.data(), hello.size());
  assert(contiguous.hex() == "a430d84680aabd0b");

  evidence::Fnv1a64 chunked;
  chunked.update(hello.data(), 2);
  chunked.update(hello.data() + 2, hello.size() - 2);
  assert(chunked.value() == contiguous.value());

  auto changed = hello;
  changed[4] = '!';
  evidence::Fnv1a64 corrupted;
  corrupted.update(changed.data(), changed.size());
  assert(corrupted.value() != contiguous.value());

  evidence::Fnv1a64 canonical_integer;
  canonical_integer.update_u32_le(0x04030201u);
  evidence::Fnv1a64 canonical_bytes;
  constexpr std::array<std::uint8_t, 4> little_endian{1, 2, 3, 4};
  canonical_bytes.update(little_endian.data(), little_endian.size());
  assert(canonical_integer.value() == canonical_bytes.value());

  assert(evidence::PHYSICAL_HASH_VERSION == 2u);
  assert(std::string(evidence::PHYSICAL_HASH_ALGORITHM) == "fnv1a64");
  assert(std::string(evidence::PHYSICAL_HASH_SCOPE) ==
         "field_scoped_physical_artifacts");
  assert(std::string(evidence::PHYSICAL_HEADER_HASH_SCOPE) ==
         "replicated_header_source_bytes");
  assert(std::string(evidence::PHYSICAL_DESCRIPTOR_HASH_SCOPE) ==
         "descriptor_slice_of_replicated_header");
  assert(std::string(evidence::PHYSICAL_MAP_HASH_SCOPE) ==
         "global_budget_map_source_bytes");
  assert(std::string(evidence::PHYSICAL_OFFSET_TABLE_HASH_SCOPE) ==
         "per_mn_offset_table_source_bytes");
  assert(std::string(evidence::PHYSICAL_RECORD_PAYLOAD_HASH_SCOPE) ==
         "per_mn_record_payload_source_bytes");
  assert(std::string(evidence::PHYSICAL_SELECTED_UID_HASH_SCOPE) ==
         "global_selected_uid_u32le_sequence");
  assert(evidence::PHYSICAL_BUDGET_MAP_OWNER_MN == 0u);
  return 0;
}
