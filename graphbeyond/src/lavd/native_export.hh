#pragma once

// MB-LAVD / LAVD-native export planning. This header is intentionally pure
// address arithmetic: no RDMA types, no Node dependency. The multi-MN builder
// can use the resulting entries to write packed per-MN L0 sidecars, and tests
// can validate the memory math without requiring the SKV cluster.

#include <cassert>
#include <cstdint>
#include <limits>
#include <vector>

#include "lavd/native_owner.hh"

namespace lavd::native {

enum class RecordLayout : std::uint8_t {
  kFixedStride = 1,
  kVariableRecords = 2,
};

enum class BudgetMapPlacement : std::uint8_t {
  kNone = 0,
  kMemoryNode0 = 1,
};

constexpr u64 BUDGET_MAP_ENTRY_BYTES = sizeof(u32);
constexpr u64 OFFSET_TABLE_ENTRY_BYTES = sizeof(u64);

struct FixedExportEntry {
  u32 slot = 0;
  u32 owner_mn = 0;
  u32 local_slot = 0;
  u64 remote_offset = 0;
  u32 write_bytes = 0;
};

struct FixedExportPlan {
  PackedL0Layout layout;
  std::vector<FixedExportEntry> entries;
  std::vector<u32> blocks_per_mn;
  std::vector<u64> region_bytes_per_mn;
  u64 total_write_bytes = 0;
  u64 packed_region_bytes = 0;
  u64 sparse_region_bytes = 0;

  u64 saved_region_bytes() const {
    return sparse_region_bytes > packed_region_bytes
        ? sparse_region_bytes - packed_region_bytes
        : 0;
  }
};

enum class PhysicalAccountingError : std::uint8_t {
  kNone = 0,
  kInvalidTopology,
  kVectorLengthMismatch,
  kInvalidMapPlacement,
  kMapShiftTooSmall,
  kArithmeticOverflow,
  kPlanMismatch,
  kInvalidOffsetTable,
  kMisalignedOffsetTable,
};

struct MnPhysicalAccounting {
  u32 local_slot_count = 0;
  u64 header_bytes = 0;
  u64 budget_map_bytes = 0;
  u64 placement_padding_bytes = 0;
  u64 offset_table_bytes = 0;
  u64 record_bytes = 0;
  u64 total_region_bytes = 0;
};

struct PhysicalByteAccounting {
  bool valid = false;
  PhysicalAccountingError error = PhysicalAccountingError::kNone;
  RecordLayout record_layout = RecordLayout::kFixedStride;
  u32 total_slots = 0;
  u32 num_mns = 0;
  ShardPolicy policy = ShardPolicy::kBlockCyclic;
  BudgetMapPlacement budget_map_placement = BudgetMapPlacement::kNone;
  u64 map_shift_bytes = 0;
  std::vector<MnPhysicalAccounting> per_mn;
  u64 total_bytes_across_mns = 0;
};

namespace detail {

inline bool checked_add(u64 a, u64 b, u64* out) {
  if (b > std::numeric_limits<u64>::max() - a) return false;
  *out = a + b;
  return true;
}

inline bool checked_mul(u64 a, u64 b, u64* out) {
  if (a != 0 && b > std::numeric_limits<u64>::max() / a) return false;
  *out = a * b;
  return true;
}

inline bool checked_align_up(u64 value, u64 alignment, u64* out) {
  if (out == nullptr || alignment == 0 ||
      (alignment & (alignment - 1u)) != 0) {
    return false;
  }
  u64 rounded = 0;
  if (!checked_add(value, alignment - 1u, &rounded)) return false;
  *out = rounded & ~(alignment - 1u);
  return true;
}

struct FixedPhysicalTotals {
  u64 total_write_bytes = 0;
  u64 packed_region_bytes = 0;
  u64 sparse_region_bytes = 0;
};

inline bool checked_fixed_physical_totals(u32 total_slots,
                                          u32 num_mns,
                                          u32 block_stride,
                                          u64 header_bytes,
                                          FixedPhysicalTotals* totals) {
  if (totals == nullptr) return false;

  u64 headers_across_mns = 0;
  u64 sparse_bytes_per_mn = 0;
  return checked_mul(total_slots, block_stride, &totals->total_write_bytes) &&
         checked_mul(num_mns, header_bytes, &headers_across_mns) &&
         checked_add(headers_across_mns, totals->total_write_bytes,
                     &totals->packed_region_bytes) &&
         checked_add(header_bytes, totals->total_write_bytes,
                     &sparse_bytes_per_mn) &&
         checked_mul(num_mns, sparse_bytes_per_mn,
                     &totals->sparse_region_bytes);
}

inline bool valid_policy(ShardPolicy policy) {
  return policy == ShardPolicy::kBlockCyclic ||
         policy == ShardPolicy::kContiguousRange;
}

inline PhysicalByteAccounting accounting_error(PhysicalByteAccounting accounting,
                                                PhysicalAccountingError error) {
  accounting.valid = false;
  accounting.error = error;
  accounting.per_mn.clear();
  accounting.total_bytes_across_mns = 0;
  return accounting;
}

}  // namespace detail

inline OwnerResolver make_resolver(u32 total_slots, u32 num_mns, ShardPolicy policy) {
  return policy == ShardPolicy::kContiguousRange
      ? OwnerResolver::contiguous_range(total_slots, num_mns)
      : OwnerResolver::block_cyclic(total_slots, num_mns);
}

inline const char* policy_name(ShardPolicy policy) {
  return policy == ShardPolicy::kContiguousRange ? "range" : "block_cyclic";
}

inline FixedExportPlan make_fixed_export_plan(u32 total_slots,
                                             u32 num_mns,
                                             u32 block_stride,
                                             u64 header_bytes,
                                             ShardPolicy policy,
                                             bool materialize_entries = true) {
  assert(num_mns > 0);
  assert(block_stride > 0);
  const auto resolver = make_resolver(total_slots, num_mns, policy);
  const auto layout = PackedL0Layout::fixed_stride(resolver, block_stride, header_bytes);

  FixedExportPlan plan;
  plan.layout = layout;
  if (materialize_entries) {
    plan.entries.reserve(total_slots);
  }
  plan.blocks_per_mn.assign(num_mns, 0);
  plan.region_bytes_per_mn.assign(num_mns, header_bytes);

  for (u32 slot = 0; slot < total_slots; ++slot) {
    const FixedL0ReadPlan rp = layout.read_plan(slot);
    if (materialize_entries) {
      plan.entries.push_back(FixedExportEntry{
          slot, rp.owner_mn, rp.local_slot, rp.remote_offset, rp.read_bytes});
    }
    ++plan.blocks_per_mn[rp.owner_mn];
    plan.total_write_bytes += rp.read_bytes;
  }

  for (u32 mn = 0; mn < num_mns; ++mn) {
    plan.region_bytes_per_mn[mn] =
        header_bytes + static_cast<u64>(plan.blocks_per_mn[mn]) * block_stride;
  }
  plan.packed_region_bytes = layout.total_packed_bytes();
  plan.sparse_region_bytes = layout.total_sparse_bytes();
  return plan;
}

inline PhysicalByteAccounting make_fixed_physical_accounting(
    const FixedExportPlan& plan) {
  PhysicalByteAccounting accounting;
  accounting.record_layout = RecordLayout::kFixedStride;
  accounting.total_slots = plan.layout.resolver.total_slots;
  accounting.num_mns = plan.layout.resolver.num_mns;
  accounting.policy = plan.layout.resolver.policy;

  if (accounting.total_slots == 0 || accounting.num_mns == 0 ||
      plan.layout.block_stride == 0 || !detail::valid_policy(accounting.policy)) {
    return detail::accounting_error(accounting,
                                    PhysicalAccountingError::kInvalidTopology);
  }
  if (plan.blocks_per_mn.size() != accounting.num_mns ||
      plan.region_bytes_per_mn.size() != accounting.num_mns) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kVectorLengthMismatch);
  }

  detail::FixedPhysicalTotals expected_totals;
  if (!detail::checked_fixed_physical_totals(
          accounting.total_slots, accounting.num_mns,
          plan.layout.block_stride, plan.layout.header_bytes,
          &expected_totals)) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kArithmeticOverflow);
  }
  if (plan.total_write_bytes != expected_totals.total_write_bytes ||
      plan.packed_region_bytes != expected_totals.packed_region_bytes ||
      plan.sparse_region_bytes != expected_totals.sparse_region_bytes) {
    return detail::accounting_error(accounting,
                                    PhysicalAccountingError::kPlanMismatch);
  }

  accounting.per_mn.reserve(accounting.num_mns);
  u64 local_slot_sum = 0;
  for (u32 mn = 0; mn < accounting.num_mns; ++mn) {
    const u32 local_slots = plan.layout.resolver.local_count(mn);
    if (plan.blocks_per_mn[mn] != local_slots) {
      return detail::accounting_error(accounting,
                                      PhysicalAccountingError::kPlanMismatch);
    }

    MnPhysicalAccounting shard;
    shard.local_slot_count = local_slots;
    shard.header_bytes = plan.layout.header_bytes;
    if (!detail::checked_mul(local_slots, plan.layout.block_stride,
                             &shard.record_bytes) ||
        !detail::checked_add(shard.header_bytes, shard.record_bytes,
                             &shard.total_region_bytes)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }
    if (shard.total_region_bytes != plan.region_bytes_per_mn[mn]) {
      return detail::accounting_error(accounting,
                                      PhysicalAccountingError::kPlanMismatch);
    }
    if (!detail::checked_add(accounting.total_bytes_across_mns,
                             shard.total_region_bytes,
                             &accounting.total_bytes_across_mns) ||
        !detail::checked_add(local_slot_sum, local_slots, &local_slot_sum)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }
    accounting.per_mn.push_back(shard);
  }

  if (local_slot_sum != accounting.total_slots ||
      accounting.total_bytes_across_mns !=
          expected_totals.packed_region_bytes) {
    return detail::accounting_error(accounting,
                                    PhysicalAccountingError::kPlanMismatch);
  }
  accounting.valid = true;
  return accounting;
}

inline PhysicalByteAccounting make_variable_physical_accounting(
    const OwnerResolver& resolver,
    u64 header_bytes,
    BudgetMapPlacement budget_map_placement,
    u64 map_shift_bytes,
    const std::vector<u64>& record_bytes_per_mn) {
  PhysicalByteAccounting accounting;
  accounting.record_layout = RecordLayout::kVariableRecords;
  accounting.total_slots = resolver.total_slots;
  accounting.num_mns = resolver.num_mns;
  accounting.policy = resolver.policy;
  accounting.budget_map_placement = budget_map_placement;
  accounting.map_shift_bytes = map_shift_bytes;

  if (accounting.total_slots == 0 || accounting.num_mns == 0 ||
      !detail::valid_policy(accounting.policy)) {
    return detail::accounting_error(accounting,
                                    PhysicalAccountingError::kInvalidTopology);
  }
  if (record_bytes_per_mn.size() != accounting.num_mns) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kVectorLengthMismatch);
  }
  if (budget_map_placement != BudgetMapPlacement::kNone &&
      budget_map_placement != BudgetMapPlacement::kMemoryNode0) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kInvalidMapPlacement);
  }

  u64 map_payload_bytes = 0;
  u64 minimum_map_shift = 0;
  if (budget_map_placement == BudgetMapPlacement::kNone) {
    if (map_shift_bytes != 0) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kInvalidMapPlacement);
    }
  } else {
    if (!detail::checked_mul(accounting.total_slots, BUDGET_MAP_ENTRY_BYTES,
                             &map_payload_bytes) ||
        !detail::checked_align_up(map_payload_bytes,
                                  OFFSET_TABLE_ENTRY_BYTES,
                                  &minimum_map_shift)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }
    if (map_shift_bytes < minimum_map_shift) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kMapShiftTooSmall);
    }
  }

  u64 offset_table_base = 0;
  if (!detail::checked_add(header_bytes, map_shift_bytes,
                           &offset_table_base)) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kArithmeticOverflow);
  }
  if ((offset_table_base % OFFSET_TABLE_ENTRY_BYTES) != 0) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kMisalignedOffsetTable);
  }

  accounting.per_mn.reserve(accounting.num_mns);
  u64 local_slot_sum = 0;
  for (u32 mn = 0; mn < accounting.num_mns; ++mn) {
    MnPhysicalAccounting shard;
    shard.local_slot_count = resolver.local_count(mn);
    shard.header_bytes = header_bytes;
    shard.budget_map_bytes =
        budget_map_placement == BudgetMapPlacement::kMemoryNode0 && mn == 0
            ? map_payload_bytes
            : 0;
    shard.placement_padding_bytes = map_shift_bytes - shard.budget_map_bytes;
    shard.record_bytes = record_bytes_per_mn[mn];

    u64 table_entries = 0;
    if (!detail::checked_add(shard.local_slot_count, 1u, &table_entries) ||
        !detail::checked_mul(table_entries, OFFSET_TABLE_ENTRY_BYTES,
                             &shard.offset_table_bytes)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }

    u64 total = 0;
    if (!detail::checked_add(shard.header_bytes, shard.budget_map_bytes, &total) ||
        !detail::checked_add(total, shard.placement_padding_bytes, &total) ||
        !detail::checked_add(total, shard.offset_table_bytes, &total) ||
        !detail::checked_add(total, shard.record_bytes, &shard.total_region_bytes) ||
        !detail::checked_add(accounting.total_bytes_across_mns,
                             shard.total_region_bytes,
                             &accounting.total_bytes_across_mns) ||
        !detail::checked_add(local_slot_sum, shard.local_slot_count,
                             &local_slot_sum)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }
    accounting.per_mn.push_back(shard);
  }

  if (local_slot_sum != accounting.total_slots) {
    return detail::accounting_error(accounting,
                                    PhysicalAccountingError::kPlanMismatch);
  }
  accounting.valid = true;
  return accounting;
}

inline bool same_physical_shard(const MnPhysicalAccounting& lhs,
                                const MnPhysicalAccounting& rhs) {
  return lhs.local_slot_count == rhs.local_slot_count &&
         lhs.header_bytes == rhs.header_bytes &&
         lhs.budget_map_bytes == rhs.budget_map_bytes &&
         lhs.placement_padding_bytes == rhs.placement_padding_bytes &&
         lhs.offset_table_bytes == rhs.offset_table_bytes &&
         lhs.record_bytes == rhs.record_bytes &&
         lhs.total_region_bytes == rhs.total_region_bytes;
}

inline bool validate_variable_physical_accounting(
    const PhysicalByteAccounting& accounting) {
  if (!accounting.valid || accounting.error != PhysicalAccountingError::kNone ||
      accounting.record_layout != RecordLayout::kVariableRecords ||
      accounting.total_slots == 0 || accounting.num_mns == 0 ||
      accounting.per_mn.size() != accounting.num_mns) {
    return false;
  }

  std::vector<u64> record_bytes_per_mn;
  record_bytes_per_mn.reserve(accounting.num_mns);
  for (const auto& shard : accounting.per_mn) {
    record_bytes_per_mn.push_back(shard.record_bytes);
  }
  const auto resolver = make_resolver(accounting.total_slots,
                                      accounting.num_mns,
                                      accounting.policy);
  const auto canonical = make_variable_physical_accounting(
      resolver, accounting.per_mn.front().header_bytes,
      accounting.budget_map_placement, accounting.map_shift_bytes,
      record_bytes_per_mn);
  if (!canonical.valid || canonical.record_layout != accounting.record_layout ||
      canonical.total_slots != accounting.total_slots ||
      canonical.num_mns != accounting.num_mns ||
      canonical.policy != accounting.policy ||
      canonical.budget_map_placement != accounting.budget_map_placement ||
      canonical.map_shift_bytes != accounting.map_shift_bytes ||
      canonical.total_bytes_across_mns != accounting.total_bytes_across_mns ||
      canonical.per_mn.size() != accounting.per_mn.size()) {
    return false;
  }
  for (u32 mn = 0; mn < accounting.num_mns; ++mn) {
    if (!same_physical_shard(canonical.per_mn[mn],
                             accounting.per_mn[mn])) {
      return false;
    }
  }
  return true;
}

inline PhysicalByteAccounting
make_variable_physical_accounting_from_offset_tables(
    const OwnerResolver& resolver,
    u64 header_bytes,
    BudgetMapPlacement budget_map_placement,
    u64 map_shift_bytes,
    const std::vector<std::vector<u64>>& offset_tables_per_mn) {
  PhysicalByteAccounting accounting;
  accounting.record_layout = RecordLayout::kVariableRecords;
  accounting.total_slots = resolver.total_slots;
  accounting.num_mns = resolver.num_mns;
  accounting.policy = resolver.policy;
  accounting.budget_map_placement = budget_map_placement;
  accounting.map_shift_bytes = map_shift_bytes;

  if (offset_tables_per_mn.size() != accounting.num_mns) {
    return detail::accounting_error(
        accounting, PhysicalAccountingError::kVectorLengthMismatch);
  }

  const std::vector<u64> empty_record_bytes(accounting.num_mns, 0u);
  const auto prefix_only = make_variable_physical_accounting(
      resolver, header_bytes, budget_map_placement, map_shift_bytes,
      empty_record_bytes);
  if (!prefix_only.valid) return prefix_only;

  std::vector<u64> record_bytes_per_mn;
  record_bytes_per_mn.reserve(accounting.num_mns);
  for (u32 mn = 0; mn < accounting.num_mns; ++mn) {
    u64 expected_entries = 0;
    if (!detail::checked_add(resolver.local_count(mn), 1u,
                             &expected_entries)) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kArithmeticOverflow);
    }

    const auto& offsets = offset_tables_per_mn[mn];
    if (static_cast<u64>(offsets.size()) != expected_entries) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kVectorLengthMismatch);
    }

    const u64 expected_base = prefix_only.per_mn[mn].total_region_bytes;
    if (offsets.front() != expected_base) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kInvalidOffsetTable);
    }
    for (std::size_t i = 1; i < offsets.size(); ++i) {
      if (offsets[i] < offsets[i - 1]) {
        return detail::accounting_error(
            accounting, PhysicalAccountingError::kInvalidOffsetTable);
      }
    }
    record_bytes_per_mn.push_back(offsets.back() - expected_base);
  }

  accounting = make_variable_physical_accounting(
      resolver, header_bytes, budget_map_placement, map_shift_bytes,
      record_bytes_per_mn);
  if (!accounting.valid) return accounting;

  for (u32 mn = 0; mn < accounting.num_mns; ++mn) {
    if (accounting.per_mn[mn].total_region_bytes !=
        offset_tables_per_mn[mn].back()) {
      return detail::accounting_error(
          accounting, PhysicalAccountingError::kInvalidOffsetTable);
    }
  }
  return accounting;
}

}  // namespace lavd::native
