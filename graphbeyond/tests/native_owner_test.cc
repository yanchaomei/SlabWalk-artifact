#include "lavd/native_owner.hh"

#include <cassert>
#include <cstdint>
#include <iostream>

int main() {
  {
    const auto resolver = lavd::native::OwnerResolver::block_cyclic(100000000u, 3u);

    assert(resolver.local_count(0) == 33333334u);
    assert(resolver.local_count(1) == 33333333u);
    assert(resolver.local_count(2) == 33333333u);

    const auto s0 = resolver.resolve(0);
    assert(s0.owner_mn == 0u);
    assert(s0.local_slot == 0u);
    assert(resolver.global_slot(s0) == 0u);

    const auto s1 = resolver.resolve(1);
    assert(s1.owner_mn == 1u);
    assert(s1.local_slot == 0u);
    assert(resolver.global_slot(s1) == 1u);

    const auto s3 = resolver.resolve(3);
    assert(s3.owner_mn == 0u);
    assert(s3.local_slot == 1u);
    assert(resolver.global_slot(s3) == 3u);

    const auto last = resolver.resolve(99999999u);
    assert(last.owner_mn == 0u);
    assert(last.local_slot == 33333333u);
    assert(resolver.global_slot(last) == 99999999u);
  }

  {
    const auto resolver = lavd::native::OwnerResolver::contiguous_range(10u, 3u);

    assert(resolver.local_count(0) == 3u);
    assert(resolver.local_count(1) == 3u);
    assert(resolver.local_count(2) == 4u);

    const auto first_mn1 = resolver.resolve(3u);
    assert(first_mn1.owner_mn == 1u);
    assert(first_mn1.local_slot == 0u);
    assert(resolver.global_slot(first_mn1) == 3u);

    const auto last = resolver.resolve(9u);
    assert(last.owner_mn == 2u);
    assert(last.local_slot == 3u);
    assert(resolver.global_slot(last) == 9u);
  }

  {
    const auto layout = lavd::native::PackedL0Layout::fixed_stride(
        lavd::native::OwnerResolver::block_cyclic(10u, 3u),
        /*block_stride*/ 4096u,
        /*header_bytes*/ 16384u);

    const auto read = layout.read_plan(7u);
    assert(read.owner_mn == 1u);
    assert(read.local_slot == 2u);
    assert(read.remote_offset == 16384u + 2u * 4096u);
    assert(read.read_bytes == 4096u);

    assert(layout.shard_bytes(0u) == 16384u + 4u * 4096u);
    assert(layout.shard_bytes(1u) == 16384u + 3u * 4096u);
    assert(layout.shard_bytes(2u) == 16384u + 3u * 4096u);
    assert(layout.total_packed_bytes() == 3u * 16384u + 10u * 4096u);
    assert(layout.total_sparse_bytes() == 3u * (16384u + 10u * 4096u));
  }

  {
    const auto read = lavd::native::sparse_fixed_read_plan(
        /*slot*/ 7u,
        /*fallback_mn*/ 2u,
        /*block_stride*/ 4096u,
        /*header_bytes*/ 16384u);

    assert(read.owner_mn == 2u);
    assert(read.local_slot == 7u);
    assert(read.remote_offset == 16384u + 7u * 4096u);
    assert(read.read_bytes == 4096u);
  }

  std::cout << "native_owner_test PASS" << std::endl;
  return 0;
}
