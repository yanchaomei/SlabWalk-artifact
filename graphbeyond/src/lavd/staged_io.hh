#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

namespace lavd {

inline constexpr std::uint32_t STAGED_IO_BYTES = 64u * 1024u * 1024u;

inline std::size_t aligned_allocation_bytes(std::size_t bytes,
                                            std::size_t alignment) {
  if (bytes == 0 || alignment < sizeof(void*) ||
      (alignment & (alignment - 1u)) != 0 ||
      bytes > std::numeric_limits<std::size_t>::max() - (alignment - 1u)) {
    return 0;
  }
  return (bytes + alignment - 1u) & ~(alignment - 1u);
}

struct StagedSlotRange {
  bool valid = false;
  std::uint32_t begin_slot = 0;
  std::uint32_t end_slot = 0;
  std::uint64_t remote_offset = 0;
  std::uint32_t bytes = 0;
};

struct FixedStagedSlotRange {
  bool valid = false;
  std::uint32_t begin_slot = 0;
  std::uint32_t end_slot = 0;
  std::uint32_t bytes = 0;
};

inline std::uint32_t staged_chunk_bytes(std::uint64_t total,
                                        std::uint64_t offset) {
  if (offset >= total) return 0;
  const std::uint64_t remaining = total - offset;
  return static_cast<std::uint32_t>(
      std::min<std::uint64_t>(remaining, STAGED_IO_BYTES));
}

inline FixedStagedSlotRange next_fixed_staged_slot_range(
    std::uint32_t total_slots, std::uint32_t cursor, std::size_t stride,
    std::uint32_t max_bytes = STAGED_IO_BYTES) {
  FixedStagedSlotRange range;
  if (cursor > total_slots || stride == 0 || max_bytes == 0 ||
      stride > max_bytes) {
    return range;
  }
  range.valid = true;
  range.begin_slot = cursor;
  if (cursor == total_slots) {
    range.end_slot = cursor;
    return range;
  }
  const std::uint64_t slots_per_stage = max_bytes / stride;
  if (slots_per_stage == 0) return FixedStagedSlotRange{};
  const std::uint64_t remaining = total_slots - cursor;
  const std::uint64_t count =
      std::min<std::uint64_t>(remaining, slots_per_stage);
  const std::uint64_t bytes = count * stride;
  if (bytes > std::numeric_limits<std::uint32_t>::max()) {
    return FixedStagedSlotRange{};
  }
  range.end_slot = cursor + static_cast<std::uint32_t>(count);
  range.bytes = static_cast<std::uint32_t>(bytes);
  return range;
}

inline StagedSlotRange next_staged_slot_range(
    const std::vector<std::uint64_t>& offsets, std::uint32_t cursor,
    std::uint32_t max_bytes = STAGED_IO_BYTES) {
  StagedSlotRange range;
  if (offsets.empty() || max_bytes == 0) return range;
  const std::size_t slots = offsets.size() - 1;
  if (cursor > slots || slots > std::numeric_limits<std::uint32_t>::max()) {
    return range;
  }

  while (cursor < slots && offsets[cursor] == offsets[cursor + 1]) {
    ++cursor;
  }
  if (cursor == slots) {
    range.valid = true;
    range.begin_slot = cursor;
    range.end_slot = cursor;
    range.remote_offset = offsets.back();
    return range;
  }
  if (offsets[cursor + 1] < offsets[cursor] ||
      offsets[cursor + 1] - offsets[cursor] > max_bytes) {
    return range;
  }

  const std::uint64_t start = offsets[cursor];
  std::uint32_t end = cursor + 1;
  while (end < slots) {
    if (offsets[end + 1] < offsets[end]) return StagedSlotRange{};
    if (offsets[end + 1] - start > max_bytes) break;
    ++end;
  }

  range.valid = true;
  range.begin_slot = cursor;
  range.end_slot = end;
  range.remote_offset = start;
  range.bytes = static_cast<std::uint32_t>(offsets[end] - start);
  return range;
}

}  // namespace lavd
