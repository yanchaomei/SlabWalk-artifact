#pragma once

// MB-LAVD / LAVD-native: formulaic global-slot ownership for a packed
// per-MN L0 sidecar. The current multi-MN LAVD path writes block[uid] at
// the same global uid offset on every MN, which is simple but sparse. This
// helper is the non-invasive first step toward a native layout: search code
// can resolve a global slot into {owner MN, local slot, byte offset} without
// carrying an N-entry owner table on CN memory.

#include <cassert>
#include <cstddef>
#include <cstdint>

namespace lavd::native {

using u32 = std::uint32_t;
using u64 = std::uint64_t;

enum class ShardPolicy : std::uint8_t {
  // slot s lives on MN (s % S), local slot floor(s / S). This gives near-perfect
  // balance, needs no CN table, and is the default for a newly-built native
  // index where physical index placement does not have to constrain L0 layout.
  kBlockCyclic = 0,

  // MN i owns the contiguous range [floor(N*i/S), floor(N*(i+1)/S)).
  // Useful when a loader/exporter wants sequential writes per MN.
  kContiguousRange = 1,
};

struct SlotRef {
  u32 owner_mn = 0;
  u32 local_slot = 0;
};

struct OwnerResolver {
  u32 total_slots = 0;
  u32 num_mns = 1;
  ShardPolicy policy = ShardPolicy::kBlockCyclic;

  static OwnerResolver block_cyclic(u32 total_slots, u32 num_mns) {
    assert(num_mns > 0);
    return OwnerResolver{total_slots, num_mns, ShardPolicy::kBlockCyclic};
  }

  static OwnerResolver contiguous_range(u32 total_slots, u32 num_mns) {
    assert(num_mns > 0);
    return OwnerResolver{total_slots, num_mns, ShardPolicy::kContiguousRange};
  }

  u32 range_start(u32 mn) const {
    assert(mn <= num_mns);
    return static_cast<u32>((static_cast<u64>(total_slots) * mn) / num_mns);
  }

  u32 range_end(u32 mn) const {
    assert(mn < num_mns);
    return range_start(mn + 1);
  }

  u32 local_count(u32 mn) const {
    assert(mn < num_mns);
    if (policy == ShardPolicy::kContiguousRange) {
      return range_end(mn) - range_start(mn);
    }
    const u32 base = total_slots / num_mns;
    const u32 rem = total_slots % num_mns;
    return base + (mn < rem ? 1u : 0u);
  }

  SlotRef resolve(u32 global_slot) const {
    assert(global_slot < total_slots);
    if (policy == ShardPolicy::kContiguousRange) {
      const u32 owner = static_cast<u32>(
          ((static_cast<u64>(global_slot) + 1u) * num_mns - 1u) / total_slots);
      return SlotRef{owner, global_slot - range_start(owner)};
    }
    return SlotRef{global_slot % num_mns, global_slot / num_mns};
  }

  u32 global_slot(SlotRef ref) const {
    assert(ref.owner_mn < num_mns);
    assert(ref.local_slot < local_count(ref.owner_mn));
    if (policy == ShardPolicy::kContiguousRange) {
      return range_start(ref.owner_mn) + ref.local_slot;
    }
    return ref.local_slot * num_mns + ref.owner_mn;
  }

  bool valid(SlotRef ref) const {
    return ref.owner_mn < num_mns && ref.local_slot < local_count(ref.owner_mn);
  }
};

struct FixedL0ReadPlan {
  u32 owner_mn = 0;
  u32 local_slot = 0;
  u64 remote_offset = 0;
  u32 read_bytes = 0;
};

inline FixedL0ReadPlan sparse_fixed_read_plan(u32 slot,
                                             u32 fallback_mn,
                                             u32 block_stride,
                                             u64 header_bytes) {
  assert(block_stride > 0);
  return FixedL0ReadPlan{fallback_mn, slot,
                         header_bytes + static_cast<u64>(slot) * block_stride,
                         block_stride};
}

struct PackedL0Layout {
  OwnerResolver resolver;
  u32 block_stride = 0;
  u64 header_bytes = 0;

  static PackedL0Layout fixed_stride(OwnerResolver resolver,
                                    u32 block_stride,
                                    u64 header_bytes) {
    assert(block_stride > 0);
    return PackedL0Layout{resolver, block_stride, header_bytes};
  }

  u64 block_offset(u32 local_slot) const {
    return header_bytes + static_cast<u64>(local_slot) * block_stride;
  }

  FixedL0ReadPlan read_plan(u32 global_slot) const {
    const SlotRef ref = resolver.resolve(global_slot);
    return FixedL0ReadPlan{ref.owner_mn, ref.local_slot,
                           block_offset(ref.local_slot), block_stride};
  }

  u64 shard_bytes(u32 mn) const {
    return header_bytes + static_cast<u64>(resolver.local_count(mn)) * block_stride;
  }

  u64 total_packed_bytes() const {
    return static_cast<u64>(resolver.num_mns) * header_bytes +
           static_cast<u64>(resolver.total_slots) * block_stride;
  }

  u64 total_sparse_bytes() const {
    return static_cast<u64>(resolver.num_mns) *
           (header_bytes + static_cast<u64>(resolver.total_slots) * block_stride);
  }
};

}  // namespace lavd::native
