#include "lavd/native_descriptor.hh"

#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <vector>

namespace {

void write_u32(std::uint8_t* dst, std::uint32_t value) {
  for (std::uint32_t i = 0; i < sizeof(value); ++i) {
    dst[i] = static_cast<std::uint8_t>((value >> (8u * i)) & 0xffu);
  }
}

void write_u64(std::uint8_t* dst, std::uint64_t value) {
  for (std::uint32_t i = 0; i < sizeof(value); ++i) {
    dst[i] = static_cast<std::uint8_t>((value >> (8u * i)) & 0xffu);
  }
}

std::uint32_t read_u32(const std::uint8_t* src) {
  std::uint32_t value = 0;
  for (std::uint32_t i = 0; i < sizeof(value); ++i) {
    value |= static_cast<std::uint32_t>(src[i]) << (8u * i);
  }
  return value;
}

std::uint64_t read_u64(const std::uint8_t* src) {
  std::uint64_t value = 0;
  for (std::uint32_t i = 0; i < sizeof(value); ++i) {
    value |= static_cast<std::uint64_t>(src[i]) << (8u * i);
  }
  return value;
}

}  // namespace

int main() {
  constexpr auto scalar_params_bytes = [](std::uint32_t dim) {
    return sizeof(std::uint32_t) * 2u +
           static_cast<std::size_t>(dim) * sizeof(float) * 2u;
  };
  assert(lavd::native::scalar_params_fit_before_metadata(
      scalar_params_bytes(2038), true));
  assert(!lavd::native::scalar_params_fit_before_metadata(
      scalar_params_bytes(2039), true));
  assert(!lavd::native::scalar_params_fit_before_metadata(
      scalar_params_bytes(2047), true));
  assert(lavd::native::scalar_params_fit_before_metadata(
      scalar_params_bytes(2046), false));
  assert(!lavd::native::scalar_params_fit_before_metadata(
      scalar_params_bytes(2047), false));

  using lavd::native::BudgetMapPlacement;
  using lavd::native::RecordLayout;
  using lavd::native::ScoringCodeKind;

  static_assert(lavd::native::DESCRIPTOR_BYTES == 64u);
  static_assert(lavd::native::DESCRIPTOR_OFFSET + lavd::native::DESCRIPTOR_BYTES ==
                lavd::native::HDR_COUNTS_OFFSET);

  std::array<std::uint8_t, lavd::native::PARAMS_RESERVE_BYTES> header{};
  assert(!lavd::native::descriptor_present(header.data()));
  auto replica = header;
  assert(lavd::native::replicated_header_matches(header.data(), replica.data()));
  replica[17] ^= 0x1u;
  assert(!lavd::native::replicated_header_matches(header.data(), replica.data()));
  assert(!lavd::native::replicated_header_matches(nullptr, replica.data()));

  const auto fixed_plan = lavd::native::make_fixed_export_plan(
      /*total_slots*/ 10u,
      /*num_mns*/ 3u,
      /*block_stride*/ 4096u,
      /*header_bytes*/ lavd::native::PARAMS_RESERVE_BYTES,
      lavd::native::ShardPolicy::kBlockCyclic);
  const auto fixed_desc = lavd::native::make_fixed_descriptor(
      fixed_plan, ScoringCodeKind::kScalarQuantizer, /*scoring_bits*/ 8u,
      /*max_degree*/ 64u, /*colocated_degree*/ 16u,
      /*slot_only*/ false);
  assert(fixed_desc.valid);
  assert(fixed_desc.version == lavd::native::DESCRIPTOR_VERSION);

  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET, 10u);
  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET + 4u, 10u);
  assert(lavd::native::write_descriptor(header.data(), fixed_desc));
  assert(lavd::native::descriptor_present(header.data()));
  assert(header[lavd::native::DESCRIPTOR_OFFSET + 0u] == 'N');
  assert(header[lavd::native::DESCRIPTOR_OFFSET + 1u] == 'V');
  assert(header[lavd::native::DESCRIPTOR_OFFSET + 2u] == 'D');
  assert(header[lavd::native::DESCRIPTOR_OFFSET + 3u] == '2');
  assert(read_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET) == 10u);
  assert(read_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET + 4u) == 10u);

  const auto fixed_parsed = lavd::native::read_descriptor(header.data());
  assert(fixed_parsed.valid);
  assert(!fixed_parsed.legacy_v1);
  assert(fixed_parsed.total_slots == 10u);
  assert(fixed_parsed.num_mns == 3u);
  assert(fixed_parsed.policy == lavd::native::ShardPolicy::kBlockCyclic);
  assert(fixed_parsed.record_layout == RecordLayout::kFixedStride);
  assert(fixed_parsed.block_stride == 4096u);
  assert(fixed_parsed.header_bytes == lavd::native::PARAMS_RESERVE_BYTES);
  assert(fixed_parsed.packed_region_bytes == fixed_plan.packed_region_bytes);
  assert(fixed_parsed.sparse_region_bytes == fixed_plan.sparse_region_bytes);
  assert(fixed_parsed.feature_flags == 0u);
  assert(fixed_parsed.scoring_code_kind == ScoringCodeKind::kScalarQuantizer);
  assert(fixed_parsed.scoring_code_bits == 8u);
  assert(fixed_parsed.budget_map_placement == BudgetMapPlacement::kNone);
  assert(fixed_parsed.map_shift_bytes == 0u);
  assert(fixed_parsed.max_degree == 64u);
  assert(fixed_parsed.colocated_degree == 16u);
  assert(!fixed_parsed.slot_only);

  assert(lavd::native::descriptor_compatible(
      fixed_parsed, RecordLayout::kFixedStride,
      ScoringCodeKind::kScalarQuantizer, 8u, /*supported_features*/ 0u));
  assert(!lavd::native::descriptor_compatible(
      fixed_parsed, RecordLayout::kFixedStride,
      ScoringCodeKind::kScalarQuantizer, 4u, /*supported_features*/ 0u));

  lavd::native::PackedL0Layout fixed_layout;
  assert(lavd::native::try_layout_from_descriptor(fixed_parsed, &fixed_layout));
  const auto fixed_read = fixed_layout.read_plan(9u);
  assert(fixed_read.owner_mn == 0u);
  assert(fixed_read.local_slot == 3u);
  assert(fixed_read.remote_offset ==
         lavd::native::PARAMS_RESERVE_BYTES + 3ull * 4096ull);
  assert(!lavd::native::try_layout_from_descriptor(
      lavd::native::NativeDescriptor{}, &fixed_layout));

  auto invalid_to_write = fixed_desc;
  invalid_to_write.feature_flags = 0x10u;
  auto write_guard = header;
  assert(!lavd::native::write_descriptor(write_guard.data(), invalid_to_write));
  assert(write_guard == header);

  auto mismatched_tail = header;
  write_u32(mismatched_tail.data() + lavd::native::HDR_COUNTS_OFFSET, 11u);
  assert(!lavd::native::read_descriptor(mismatched_tail.data()).valid);

  auto invalid_fixed_header = header;
  write_u64(invalid_fixed_header.data() + lavd::native::DESCRIPTOR_OFFSET + 32u,
            fixed_plan.packed_region_bytes + 1u);
  assert(!lavd::native::read_descriptor(invalid_fixed_header.data()).valid);

  invalid_fixed_header = header;
  write_u64(invalid_fixed_header.data() + lavd::native::DESCRIPTOR_OFFSET + 40u,
            fixed_plan.sparse_region_bytes + 1u);
  assert(!lavd::native::read_descriptor(invalid_fixed_header.data()).valid);

  const auto variable_accounting = lavd::native::make_variable_physical_accounting(
      lavd::native::OwnerResolver::block_cyclic(10u, 3u),
      lavd::native::PARAMS_RESERVE_BYTES,
      BudgetMapPlacement::kMemoryNode0,
      /*map_shift_bytes*/ 10u * sizeof(std::uint32_t),
      std::vector<std::uint64_t>{101u, 70u, 90u});
  assert(variable_accounting.valid);

  const auto variable_desc = lavd::native::make_variable_descriptor(
      variable_accounting, /*max_record_bytes*/ 4096u,
      ScoringCodeKind::kRaBitQ, /*scoring_bits*/ 2u,
      /*max_degree*/ 64u, /*colocated_degree*/ 16u,
      /*slot_only*/ false);
  assert(variable_desc.valid);
  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET + 4u, 7u);
  assert(lavd::native::write_descriptor(header.data(), variable_desc));
  assert(read_u64(header.data() + lavd::native::DESCRIPTOR_OFFSET + 56u) == 40u);

  const auto variable_parsed = lavd::native::read_descriptor(header.data());
  assert(variable_parsed.valid);
  assert(variable_parsed.record_layout == RecordLayout::kVariableRecords);
  assert(variable_parsed.block_stride == 4096u);
  assert(variable_parsed.feature_flags ==
         (lavd::native::FEATURE_BUDGET_MAP |
          lavd::native::FEATURE_PER_MN_OFFSET_TABLES));
  assert(variable_parsed.budget_map_placement == BudgetMapPlacement::kMemoryNode0);
  assert(variable_parsed.map_shift_bytes == 40u);
  assert(variable_parsed.scoring_code_kind == ScoringCodeKind::kRaBitQ);
  assert(variable_parsed.scoring_code_bits == 2u);
  assert(variable_parsed.max_degree == 64u);
  assert(variable_parsed.colocated_degree == 16u);
  assert(!variable_parsed.slot_only);
  assert(lavd::native::descriptor_record_abi_matches(
      variable_parsed, /*max_record_bytes*/ 4096u, /*max_degree*/ 64u,
      /*colocated_degree*/ 16u, /*slot_only*/ false));
  assert(!lavd::native::descriptor_record_abi_matches(
      variable_parsed, /*max_record_bytes*/ 4096u, /*max_degree*/ 64u,
      /*colocated_degree*/ 64u, /*slot_only*/ false));
  assert(!lavd::native::descriptor_record_abi_matches(
      variable_parsed, /*max_record_bytes*/ 4096u, /*max_degree*/ 64u,
      /*colocated_degree*/ 0u, /*slot_only*/ true));
  assert(variable_parsed.packed_region_bytes ==
         variable_accounting.total_bytes_across_mns);
  assert(lavd::native::descriptor_offset_table_offset(variable_parsed) ==
         lavd::native::PARAMS_RESERVE_BYTES + 40u);
  assert(lavd::native::descriptor_record_region_offset(variable_parsed, 0u) ==
         lavd::native::PARAMS_RESERVE_BYTES + 40u + 5u * sizeof(std::uint64_t));
  assert(lavd::native::descriptor_record_region_offset(variable_parsed, 1u) ==
         lavd::native::PARAMS_RESERVE_BYTES + 40u + 4u * sizeof(std::uint64_t));

  std::uint64_t checked_offset = 0;
  assert(lavd::native::try_descriptor_offset_table_offset(variable_parsed,
                                                          &checked_offset));
  assert(checked_offset == lavd::native::PARAMS_RESERVE_BYTES + 40u);
  assert(lavd::native::try_descriptor_record_region_offset(
      variable_parsed, 0u, &checked_offset));
  assert(checked_offset ==
         lavd::native::PARAMS_RESERVE_BYTES + 40u +
             5u * sizeof(std::uint64_t));
  assert(!lavd::native::try_descriptor_record_region_offset(
      variable_parsed, variable_parsed.num_mns, &checked_offset));

  auto tampered_accounting = variable_accounting;
  ++tampered_accounting.total_bytes_across_mns;
  assert(!lavd::native::make_variable_descriptor(
              tampered_accounting, 4096u, ScoringCodeKind::kRaBitQ, 2u,
              64u, 16u, false)
              .valid);
  tampered_accounting = variable_accounting;
  tampered_accounting.policy =
      static_cast<lavd::native::ShardPolicy>(0xffu);
  assert(!lavd::native::make_variable_descriptor(
              tampered_accounting, 4096u, ScoringCodeKind::kRaBitQ, 2u,
              64u, 16u, false)
              .valid);

  assert(!lavd::native::make_variable_descriptor(
              variable_accounting, 4096u, ScoringCodeKind::kRaBitQ, 2u,
              /*max_degree*/ 0u, /*colocated_degree*/ 0u,
              /*slot_only*/ true)
              .valid);
  assert(!lavd::native::make_variable_descriptor(
              variable_accounting, 4096u, ScoringCodeKind::kRaBitQ, 2u,
              /*max_degree*/ 64u, /*colocated_degree*/ 65u,
              /*slot_only*/ false)
              .valid);
  assert(!lavd::native::make_variable_descriptor(
              variable_accounting, 4096u, ScoringCodeKind::kRaBitQ, 2u,
              /*max_degree*/ 64u, /*colocated_degree*/ 1u,
              /*slot_only*/ true)
              .valid);

  mismatched_tail = header;
  write_u32(mismatched_tail.data() + lavd::native::HDR_COUNTS_OFFSET, 11u);
  assert(!lavd::native::read_descriptor(mismatched_tail.data()).valid);

  mismatched_tail = header;
  write_u32(mismatched_tail.data() + lavd::native::HDR_COUNTS_OFFSET + 4u, 10u);
  assert(!lavd::native::read_descriptor(mismatched_tail.data()).valid);

  auto forged_overflowing_descriptor = variable_parsed;
  forged_overflowing_descriptor.map_shift_bytes =
      std::numeric_limits<std::uint64_t>::max();
  assert(!lavd::native::try_descriptor_offset_table_offset(
      forged_overflowing_descriptor, &checked_offset));
  assert(!lavd::native::try_descriptor_record_region_offset(
      forged_overflowing_descriptor, 0u, &checked_offset));

  const std::uint32_t variable_features =
      lavd::native::FEATURE_BUDGET_MAP |
      lavd::native::FEATURE_PER_MN_OFFSET_TABLES;
  assert(lavd::native::descriptor_compatible(
      variable_parsed, RecordLayout::kVariableRecords,
      ScoringCodeKind::kRaBitQ, 2u, variable_features));
  assert(!lavd::native::descriptor_compatible(
      variable_parsed, RecordLayout::kVariableRecords,
      ScoringCodeKind::kProductQuantizer, 2u, variable_features));
  assert(!lavd::native::descriptor_compatible(
      variable_parsed, RecordLayout::kVariableRecords,
      ScoringCodeKind::kRaBitQ, 2u,
      lavd::native::FEATURE_PER_MN_OFFSET_TABLES));

  auto malformed_variable_header = header;
  write_u64(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 40u,
            0u);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  write_u64(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 56u,
            std::numeric_limits<std::uint64_t>::max());
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  write_u32(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 12u,
            std::numeric_limits<std::uint32_t>::max());
  write_u64(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 32u,
            std::numeric_limits<std::uint64_t>::max());
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  write_u32(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 52u,
            lavd::native::DESCRIPTOR_BYTES - 1u);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  std::uint32_t malformed_semantics =
      read_u32(malformed_variable_header.data() +
               lavd::native::DESCRIPTOR_OFFSET + 48u);
  malformed_semantics &= ~(0xffu << 8u);
  write_u32(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u,
            malformed_semantics);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  malformed_semantics = read_u32(malformed_variable_header.data() +
                                 lavd::native::DESCRIPTOR_OFFSET + 48u);
  malformed_semantics = (malformed_semantics & ~(0xffu << 8u)) | (0x7fu << 8u);
  write_u32(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u,
            malformed_semantics);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  malformed_semantics = read_u32(malformed_variable_header.data() +
                                 lavd::native::DESCRIPTOR_OFFSET + 48u);
  malformed_semantics &= ~(0xffu << 16u);
  write_u32(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u,
            malformed_semantics);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  malformed_variable_header = header;
  write_u64(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 56u,
            10u * sizeof(std::uint32_t) - 1u);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  constexpr std::uint64_t kVariableMinimumPhysicalBytes =
      3u * (lavd::native::PARAMS_RESERVE_BYTES + 40u) +
      (5u + 4u + 4u) * sizeof(std::uint64_t);
  malformed_variable_header = header;
  write_u64(malformed_variable_header.data() + lavd::native::DESCRIPTOR_OFFSET + 32u,
            kVariableMinimumPhysicalBytes - 1u);
  assert(!lavd::native::read_descriptor(malformed_variable_header.data()).valid);

  auto invalid_header = header;
  write_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 4u, 2u);
  assert(lavd::native::descriptor_present(invalid_header.data()));
  assert(!lavd::native::read_descriptor(invalid_header.data()).valid);

  invalid_header = header;
  write_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 4u, 99u);
  assert(!lavd::native::read_descriptor(invalid_header.data()).valid);

  invalid_header = header;
  std::uint32_t semantics =
      read_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u);
  semantics = (semantics & ~0x0fu) | 0x0fu;
  write_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u, semantics);
  assert(!lavd::native::read_descriptor(invalid_header.data()).valid);

  invalid_header = header;
  semantics = read_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u);
  semantics |= 0x40u;
  write_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u, semantics);
  assert(!lavd::native::read_descriptor(invalid_header.data()).valid);

  invalid_header = header;
  semantics = read_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u);
  semantics &= 0x00ffffffu;
  write_u32(invalid_header.data() + lavd::native::DESCRIPTOR_OFFSET + 48u, semantics);
  assert(!lavd::native::read_descriptor(invalid_header.data()).valid);

  const auto legacy_desc = lavd::native::make_descriptor(fixed_plan);
  assert(legacy_desc.version == lavd::native::LEGACY_DESCRIPTOR_VERSION);
  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET + 4u, 10u);
  assert(lavd::native::write_descriptor(header.data(), legacy_desc));
  assert(read_u64(header.data() + lavd::native::DESCRIPTOR_OFFSET + 56u) == 0u);

  header.fill(0);
  std::uint8_t* legacy_wire = header.data() + lavd::native::DESCRIPTOR_OFFSET;
  constexpr std::uint32_t kLegacyFlags = 0xa5f00f5au;
  write_u32(legacy_wire + 0u, lavd::native::DESCRIPTOR_MAGIC);
  write_u32(legacy_wire + 4u, lavd::native::LEGACY_DESCRIPTOR_VERSION);
  write_u32(legacy_wire + 8u, fixed_plan.layout.resolver.total_slots);
  write_u32(legacy_wire + 12u, fixed_plan.layout.resolver.num_mns);
  write_u32(legacy_wire + 16u,
            static_cast<std::uint32_t>(fixed_plan.layout.resolver.policy));
  write_u32(legacy_wire + 20u, fixed_plan.layout.block_stride);
  write_u64(legacy_wire + 24u, fixed_plan.layout.header_bytes);
  write_u64(legacy_wire + 32u, fixed_plan.packed_region_bytes);
  write_u64(legacy_wire + 40u, fixed_plan.sparse_region_bytes);
  write_u32(legacy_wire + 48u, kLegacyFlags);
  write_u32(legacy_wire + 52u, lavd::native::DESCRIPTOR_BYTES);
  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET,
            fixed_plan.layout.resolver.total_slots);
  write_u32(header.data() + lavd::native::HDR_COUNTS_OFFSET + 4u,
            fixed_plan.layout.resolver.total_slots);

  const auto legacy_parsed = lavd::native::read_descriptor(header.data());
  assert(legacy_parsed.valid);
  assert(legacy_parsed.legacy_v1);
  assert(legacy_parsed.record_layout == RecordLayout::kFixedStride);
  assert(legacy_parsed.feature_flags == kLegacyFlags);
  assert(legacy_parsed.scoring_code_kind == ScoringCodeKind::kLegacyUnspecified);
  assert(!lavd::native::descriptor_compatible(
      legacy_parsed, RecordLayout::kFixedStride,
      ScoringCodeKind::kScalarQuantizer, 8u, /*supported_features*/ 0u));

  std::array<std::uint8_t, lavd::native::PARAMS_RESERVE_BYTES> legacy_roundtrip{};
  write_u32(legacy_roundtrip.data() + lavd::native::HDR_COUNTS_OFFSET,
            fixed_plan.layout.resolver.total_slots);
  write_u32(legacy_roundtrip.data() + lavd::native::HDR_COUNTS_OFFSET + 4u,
            fixed_plan.layout.resolver.total_slots);
  assert(lavd::native::write_descriptor(legacy_roundtrip.data(), legacy_parsed));
  assert(read_u32(legacy_roundtrip.data() + lavd::native::DESCRIPTOR_OFFSET + 48u) ==
         kLegacyFlags);
  const auto legacy_reparsed =
      lavd::native::read_descriptor(legacy_roundtrip.data());
  assert(legacy_reparsed.valid);
  assert(legacy_reparsed.feature_flags == kLegacyFlags);

  std::cout << "native_descriptor_test PASS" << std::endl;
  return 0;
}
