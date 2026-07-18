#include "lavd/native_export.hh"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <limits>
#include <vector>

int main() {
  using lavd::native::BudgetMapPlacement;
  using lavd::native::PhysicalAccountingError;
  using lavd::native::RecordLayout;

  {
    const auto stats_only = lavd::native::make_fixed_export_plan(
        /*total_slots*/ 100000000u,
        /*num_mns*/ 3u,
        /*block_stride*/ 4096u,
        /*header_bytes*/ 16384u,
        lavd::native::ShardPolicy::kBlockCyclic,
        /*materialize_entries*/ false);

    assert(stats_only.entries.empty());
    assert(stats_only.blocks_per_mn[0] == 33333334u);
    assert(stats_only.blocks_per_mn[1] == 33333333u);
    assert(stats_only.blocks_per_mn[2] == 33333333u);
    assert(stats_only.total_write_bytes == 100000000ull * 4096ull);
  }

  {
    const auto plan = lavd::native::make_fixed_export_plan(
        /*total_slots*/ 10u,
        /*num_mns*/ 3u,
        /*block_stride*/ 4096u,
        /*header_bytes*/ 16384u,
        lavd::native::ShardPolicy::kBlockCyclic);

    assert(plan.entries.size() == 10u);
    assert(plan.blocks_per_mn.size() == 3u);
    assert(plan.region_bytes_per_mn.size() == 3u);

    assert(plan.blocks_per_mn[0] == 4u);
    assert(plan.blocks_per_mn[1] == 3u);
    assert(plan.blocks_per_mn[2] == 3u);

    const auto e7 = plan.entries[7];
    assert(e7.slot == 7u);
    assert(e7.owner_mn == 1u);
    assert(e7.local_slot == 2u);
    assert(e7.remote_offset == 16384u + 2u * 4096u);
    assert(e7.write_bytes == 4096u);

    assert(plan.total_write_bytes == 10u * 4096u);
    assert(plan.region_bytes_per_mn[0] == 16384u + 4u * 4096u);
    assert(plan.region_bytes_per_mn[1] == 16384u + 3u * 4096u);
    assert(plan.region_bytes_per_mn[2] == 16384u + 3u * 4096u);
    assert(plan.packed_region_bytes == 3u * 16384u + 10u * 4096u);
    assert(plan.sparse_region_bytes == 3u * (16384u + 10u * 4096u));
    assert(plan.saved_region_bytes() == plan.sparse_region_bytes - plan.packed_region_bytes);

    const auto accounting = lavd::native::make_fixed_physical_accounting(plan);
    assert(accounting.valid);
    assert(accounting.error == PhysicalAccountingError::kNone);
    assert(accounting.record_layout == RecordLayout::kFixedStride);
    assert(accounting.per_mn.size() == 3u);
    assert(accounting.total_bytes_across_mns == plan.packed_region_bytes);

    for (std::uint32_t mn = 0; mn < 3u; ++mn) {
      const auto& shard = accounting.per_mn[mn];
      assert(shard.local_slot_count == plan.blocks_per_mn[mn]);
      assert(shard.header_bytes == 16384u);
      assert(shard.budget_map_bytes == 0u);
      assert(shard.placement_padding_bytes == 0u);
      assert(shard.offset_table_bytes == 0u);
      assert(shard.record_bytes ==
             static_cast<std::uint64_t>(plan.blocks_per_mn[mn]) * 4096u);
      assert(shard.total_region_bytes == plan.region_bytes_per_mn[mn]);
      assert(shard.total_region_bytes ==
             shard.header_bytes + shard.budget_map_bytes +
             shard.placement_padding_bytes + shard.offset_table_bytes +
             shard.record_bytes);
    }

    auto corrupted_plan = plan;
    ++corrupted_plan.total_write_bytes;
    assert(!lavd::native::make_fixed_physical_accounting(corrupted_plan).valid);

    corrupted_plan = plan;
    ++corrupted_plan.packed_region_bytes;
    assert(!lavd::native::make_fixed_physical_accounting(corrupted_plan).valid);

    corrupted_plan = plan;
    ++corrupted_plan.sparse_region_bytes;
    assert(!lavd::native::make_fixed_physical_accounting(corrupted_plan).valid);

    corrupted_plan = plan;
    ++corrupted_plan.blocks_per_mn[0];
    assert(!lavd::native::make_fixed_physical_accounting(corrupted_plan).valid);

    corrupted_plan = plan;
    ++corrupted_plan.region_bytes_per_mn[0];
    assert(!lavd::native::make_fixed_physical_accounting(corrupted_plan).valid);

    corrupted_plan = plan;
    corrupted_plan.layout.header_bytes =
        std::numeric_limits<std::uint64_t>::max();
    const auto fixed_overflow =
        lavd::native::make_fixed_physical_accounting(corrupted_plan);
    assert(!fixed_overflow.valid);
    assert(fixed_overflow.error == PhysicalAccountingError::kArithmeticOverflow);
  }

  {
    const auto resolver = lavd::native::OwnerResolver::block_cyclic(10u, 3u);
    const std::vector<std::uint64_t> record_bytes{101u, 70u, 90u};
    const auto accounting = lavd::native::make_variable_physical_accounting(
        resolver, /*header_bytes*/ 16384u,
        BudgetMapPlacement::kMemoryNode0,
        /*map_shift_bytes*/ 40u, record_bytes);

    assert(accounting.valid);
    assert(accounting.error == PhysicalAccountingError::kNone);
    assert(accounting.record_layout == RecordLayout::kVariableRecords);
    assert(accounting.total_slots == 10u);
    assert(accounting.num_mns == 3u);
    assert(accounting.per_mn.size() == 3u);

    const auto& mn0 = accounting.per_mn[0];
    assert(mn0.local_slot_count == 4u);
    assert(mn0.header_bytes == 16384u);
    assert(mn0.budget_map_bytes == 40u);
    assert(mn0.placement_padding_bytes == 0u);
    assert(mn0.offset_table_bytes == 5u * sizeof(std::uint64_t));
    assert(mn0.record_bytes == 101u);
    assert(mn0.total_region_bytes == 16565u);

    const auto& mn1 = accounting.per_mn[1];
    assert(mn1.local_slot_count == 3u);
    assert(mn1.header_bytes == 16384u);
    assert(mn1.budget_map_bytes == 0u);
    assert(mn1.placement_padding_bytes == 40u);
    assert(mn1.offset_table_bytes == 4u * sizeof(std::uint64_t));
    assert(mn1.record_bytes == 70u);
    assert(mn1.total_region_bytes == 16526u);

    const auto& mn2 = accounting.per_mn[2];
    assert(mn2.local_slot_count == 3u);
    assert(mn2.budget_map_bytes == 0u);
    assert(mn2.placement_padding_bytes == 40u);
    assert(mn2.offset_table_bytes == 4u * sizeof(std::uint64_t));
    assert(mn2.record_bytes == 90u);
    assert(mn2.total_region_bytes == 16546u);

    std::uint64_t recomputed_total = 0;
    for (const auto& shard : accounting.per_mn) {
      assert(shard.total_region_bytes ==
             shard.header_bytes + shard.budget_map_bytes +
             shard.placement_padding_bytes + shard.offset_table_bytes +
             shard.record_bytes);
      recomputed_total += shard.total_region_bytes;
    }
    assert(recomputed_total == 49637u);
    assert(accounting.total_bytes_across_mns == recomputed_total);

    const auto wrong_length = lavd::native::make_variable_physical_accounting(
        resolver, 16384u, BudgetMapPlacement::kMemoryNode0, 40u,
        std::vector<std::uint64_t>{101u, 70u});
    assert(!wrong_length.valid);
    assert(wrong_length.error == PhysicalAccountingError::kVectorLengthMismatch);

    const auto short_shift = lavd::native::make_variable_physical_accounting(
        resolver, 16384u, BudgetMapPlacement::kMemoryNode0, 39u, record_bytes);
    assert(!short_shift.valid);
    assert(short_shift.error == PhysicalAccountingError::kMapShiftTooSmall);

    const auto overflow = lavd::native::make_variable_physical_accounting(
        resolver, 16384u, BudgetMapPlacement::kMemoryNode0, 40u,
        std::vector<std::uint64_t>{std::numeric_limits<std::uint64_t>::max(),
                                   70u, 90u});
    assert(!overflow.valid);
    assert(overflow.error == PhysicalAccountingError::kArithmeticOverflow);

    const auto odd_resolver =
        lavd::native::OwnerResolver::block_cyclic(11u, 3u);
    const auto unaligned_table =
        lavd::native::make_variable_physical_accounting(
            odd_resolver, 16384u, BudgetMapPlacement::kMemoryNode0,
            /*map_shift_bytes*/ 44u,
            std::vector<std::uint64_t>{101u, 70u, 90u});
    assert(!unaligned_table.valid);
    const auto aligned_table =
        lavd::native::make_variable_physical_accounting(
            odd_resolver, 16384u, BudgetMapPlacement::kMemoryNode0,
            /*map_shift_bytes*/ 48u,
            std::vector<std::uint64_t>{101u, 70u, 90u});
    assert(aligned_table.valid);
  }

  {
    const auto resolver = lavd::native::OwnerResolver::block_cyclic(10u, 3u);
    constexpr std::uint64_t kHeaderBytes = 16384u;
    constexpr std::uint64_t kMapShiftBytes = 40u;
    constexpr std::uint64_t kMn0Base =
        kHeaderBytes + kMapShiftBytes + 5u * sizeof(std::uint64_t);
    constexpr std::uint64_t kMn12Base =
        kHeaderBytes + kMapShiftBytes + 4u * sizeof(std::uint64_t);
    const std::vector<std::vector<std::uint64_t>> offset_tables{
        {kMn0Base, kMn0Base + 11u, kMn0Base + 37u, kMn0Base + 70u,
         kMn0Base + 101u},
        {kMn12Base, kMn12Base + 8u, kMn12Base + 30u, kMn12Base + 70u},
        {kMn12Base, kMn12Base + 15u, kMn12Base + 55u, kMn12Base + 90u},
    };

    const auto accounting =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, offset_tables);
    assert(accounting.valid);
    assert(accounting.per_mn.size() == offset_tables.size());
    assert(accounting.per_mn[0].record_bytes == 101u);
    assert(accounting.per_mn[1].record_bytes == 70u);
    assert(accounting.per_mn[2].record_bytes == 90u);
    for (std::size_t mn = 0; mn < offset_tables.size(); ++mn) {
      assert(accounting.per_mn[mn].total_region_bytes ==
             offset_tables[mn].back());
    }

    auto malformed_tables = offset_tables;
    malformed_tables.pop_back();
    const auto wrong_table_count =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, malformed_tables);
    assert(!wrong_table_count.valid);
    assert(wrong_table_count.error ==
           PhysicalAccountingError::kVectorLengthMismatch);

    malformed_tables = offset_tables;
    malformed_tables[1].pop_back();
    const auto wrong_table_length =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, malformed_tables);
    assert(!wrong_table_length.valid);
    assert(wrong_table_length.error ==
           PhysicalAccountingError::kVectorLengthMismatch);

    malformed_tables = offset_tables;
    ++malformed_tables[0][0];
    const auto wrong_base =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, malformed_tables);
    assert(!wrong_base.valid);
    assert(wrong_base.error == PhysicalAccountingError::kInvalidOffsetTable);

    malformed_tables = offset_tables;
    malformed_tables[0][2] = malformed_tables[0][1] - 1u;
    const auto non_monotonic =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, malformed_tables);
    assert(!non_monotonic.valid);
    assert(non_monotonic.error ==
           PhysicalAccountingError::kInvalidOffsetTable);

    malformed_tables = offset_tables;
    malformed_tables[0].back() =
        std::numeric_limits<std::uint64_t>::max();
    const auto overflowing_terminal =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            resolver, kHeaderBytes, BudgetMapPlacement::kMemoryNode0,
            kMapShiftBytes, malformed_tables);
    assert(!overflowing_terminal.valid);
    assert(overflowing_terminal.error ==
           PhysicalAccountingError::kArithmeticOverflow);
  }

  {
    const auto plan = lavd::native::make_fixed_export_plan(
        /*total_slots*/ 10u,
        /*num_mns*/ 3u,
        /*block_stride*/ 1024u,
        /*header_bytes*/ 4096u,
        lavd::native::ShardPolicy::kContiguousRange);

    assert(plan.blocks_per_mn[0] == 3u);
    assert(plan.blocks_per_mn[1] == 3u);
    assert(plan.blocks_per_mn[2] == 4u);

    const auto e3 = plan.entries[3];
    assert(e3.slot == 3u);
    assert(e3.owner_mn == 1u);
    assert(e3.local_slot == 0u);
    assert(e3.remote_offset == 4096u);

    const auto e9 = plan.entries[9];
    assert(e9.owner_mn == 2u);
    assert(e9.local_slot == 3u);
    assert(e9.remote_offset == 4096u + 3u * 1024u);
  }

  std::cout << "native_export_test PASS" << std::endl;
  return 0;
}
