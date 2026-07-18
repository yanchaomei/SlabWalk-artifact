#pragma once

#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>

namespace evidence {

inline constexpr std::uint32_t PHYSICAL_HASH_VERSION = 2u;
inline constexpr char PHYSICAL_HASH_ALGORITHM[] = "fnv1a64";
inline constexpr char PHYSICAL_HASH_SCOPE[] =
    "field_scoped_physical_artifacts";
inline constexpr char PHYSICAL_HEADER_HASH_SCOPE[] =
    "replicated_header_source_bytes";
inline constexpr char PHYSICAL_DESCRIPTOR_HASH_SCOPE[] =
    "descriptor_slice_of_replicated_header";
inline constexpr char PHYSICAL_MAP_HASH_SCOPE[] =
    "global_budget_map_source_bytes";
inline constexpr char PHYSICAL_OFFSET_TABLE_HASH_SCOPE[] =
    "per_mn_offset_table_source_bytes";
inline constexpr char PHYSICAL_RECORD_PAYLOAD_HASH_SCOPE[] =
    "per_mn_record_payload_source_bytes";
inline constexpr char PHYSICAL_SELECTED_UID_HASH_SCOPE[] =
    "global_selected_uid_u32le_sequence";
inline constexpr std::uint32_t PHYSICAL_BUDGET_MAP_OWNER_MN = 0u;

class Fnv1a64 {
 public:
  void update(const void* data, std::size_t bytes) {
    const auto* input = static_cast<const std::uint8_t*>(data);
    for (std::size_t i = 0; i < bytes; ++i) {
      value_ ^= input[i];
      value_ *= kPrime;
    }
  }

  void update_u32_le(std::uint32_t value) {
    for (unsigned shift = 0; shift < 32u; shift += 8u) {
      const std::uint8_t byte = static_cast<std::uint8_t>(value >> shift);
      update(&byte, sizeof(byte));
    }
  }

  void update_u64_le(std::uint64_t value) {
    for (unsigned shift = 0; shift < 64u; shift += 8u) {
      const std::uint8_t byte = static_cast<std::uint8_t>(value >> shift);
      update(&byte, sizeof(byte));
    }
  }

  [[nodiscard]] std::uint64_t value() const { return value_; }

  [[nodiscard]] std::string hex() const {
    std::ostringstream stream;
    stream << std::hex << std::setfill('0') << std::setw(16) << value_;
    return stream.str();
  }

 private:
  static constexpr std::uint64_t kOffsetBasis = 14695981039346656037ull;
  static constexpr std::uint64_t kPrime = 1099511628211ull;
  std::uint64_t value_ = kOffsetBasis;
};

}  // namespace evidence
