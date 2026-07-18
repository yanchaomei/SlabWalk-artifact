#include <cassert>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

#include "lavd/staged_io.hh"

int main() {
  assert(lavd::aligned_allocation_bytes(0, 64) == 0);
  assert(lavd::aligned_allocation_bytes(1, 64) == 64);
  assert(lavd::aligned_allocation_bytes(64, 64) == 64);
  assert(lavd::aligned_allocation_bytes(3592, 64) == 3648);
  assert(lavd::aligned_allocation_bytes(
             std::numeric_limits<std::size_t>::max(), 64) == 0);
  assert(lavd::aligned_allocation_bytes(128, 3) == 0);
  assert(lavd::aligned_allocation_bytes(128, 1) == 0);
  assert(lavd::aligned_allocation_bytes(128, sizeof(void*) / 2) == 0);

  constexpr std::uint64_t total = 1376191224ull;
  std::uint64_t offset = 0;
  std::uint32_t chunks = 0;
  while (offset < total) {
    const std::uint32_t bytes = lavd::staged_chunk_bytes(total, offset);
    assert(bytes > 0);
    assert(bytes <= lavd::STAGED_IO_BYTES);
    offset += bytes;
    ++chunks;
  }
  assert(offset == total);
  assert(chunks == 21);
  assert(lavd::staged_chunk_bytes(16, 16) == 0);

  const auto fixed_first =
      lavd::next_fixed_staged_slot_range(1000, 0, 4096, 64 * 1024);
  assert(fixed_first.valid);
  assert(fixed_first.begin_slot == 0);
  assert(fixed_first.end_slot == 16);
  assert(fixed_first.bytes == 64 * 1024);
  const auto fixed_tail = lavd::next_fixed_staged_slot_range(
      1000, fixed_first.end_slot, 4096, 64 * 1024);
  assert(fixed_tail.valid);
  assert(fixed_tail.begin_slot == 16);
  assert(fixed_tail.end_slot == 32);
  assert(fixed_tail.bytes == 64 * 1024);
  const auto fixed_done =
      lavd::next_fixed_staged_slot_range(1000, 1000, 4096, 64 * 1024);
  assert(fixed_done.valid);
  assert(fixed_done.begin_slot == 1000);
  assert(fixed_done.end_slot == 1000);
  assert(fixed_done.bytes == 0);
  assert(!lavd::next_fixed_staged_slot_range(10, 0, 0, 64).valid);
  assert(!lavd::next_fixed_staged_slot_range(10, 0, 65, 64).valid);
  assert(!lavd::next_fixed_staged_slot_range(10, 11, 8, 64).valid);

  const std::vector<std::uint64_t> offsets{100, 100, 120, 120, 150, 210};
  const auto first = lavd::next_staged_slot_range(offsets, 0, 50);
  assert(first.valid);
  assert(first.begin_slot == 1);
  assert(first.end_slot == 4);
  assert(first.remote_offset == 100);
  assert(first.bytes == 50);

  const auto second = lavd::next_staged_slot_range(offsets, first.end_slot, 64);
  assert(second.valid);
  assert(second.begin_slot == 4);
  assert(second.end_slot == 5);
  assert(second.remote_offset == 150);
  assert(second.bytes == 60);

  const auto done = lavd::next_staged_slot_range(offsets, second.end_slot, 64);
  assert(done.valid);
  assert(done.begin_slot == 5);
  assert(done.end_slot == 5);
  assert(done.bytes == 0);

  const std::vector<std::uint64_t> cold{4096, 4096, 4096};
  const auto all_cold = lavd::next_staged_slot_range(cold, 0, 64);
  assert(all_cold.valid);
  assert(all_cold.begin_slot == 2);
  assert(all_cold.end_slot == 2);
  assert(all_cold.bytes == 0);

  const std::vector<std::uint64_t> oversized{0, 65};
  const auto too_large = lavd::next_staged_slot_range(oversized, 0, 64);
  assert(!too_large.valid);
  return 0;
}
