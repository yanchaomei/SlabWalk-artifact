#pragma once

// Self-describing header for MB-LAVD / LAVD-native packed L0 sidecars.
// It remains immediately before the legacy [total_n, budget_n] tail.

#include <cassert>
#include <cstdlib>
#include <cstdint>
#include <cstring>

#include "lavd/native_export.hh"

namespace lavd::native {

constexpr u32 DESCRIPTOR_MAGIC = 0x3244564Eu;  // "NVD2" little-endian
constexpr u32 LEGACY_DESCRIPTOR_VERSION = 1;
constexpr u32 DESCRIPTOR_VERSION = 3;
constexpr u32 DESCRIPTOR_BYTES = 64;
constexpr u32 MAX_DESCRIPTOR_MNS = 1u << 16;
constexpr size_t PARAMS_RESERVE_BYTES = 16384;
constexpr size_t HDR_COUNTS_OFFSET = PARAMS_RESERVE_BYTES - 8;
constexpr size_t DESCRIPTOR_OFFSET = HDR_COUNTS_OFFSET - DESCRIPTOR_BYTES;

inline bool replicated_header_matches(const std::uint8_t* reference,
                                      const std::uint8_t* replica) {
  return reference != nullptr && replica != nullptr &&
         std::memcmp(reference, replica, PARAMS_RESERVE_BYTES) == 0;
}

constexpr u32 FEATURE_BUDGET_MAP = 1u << 0;
constexpr u32 FEATURE_PER_MN_OFFSET_TABLES = 1u << 1;
constexpr u32 KNOWN_FEATURE_FLAGS =
    FEATURE_BUDGET_MAP | FEATURE_PER_MN_OFFSET_TABLES;

enum class ScoringCodeKind : std::uint8_t {
  kLegacyUnspecified = 0,
  kScalarQuantizer = 1,
  kRaBitQ = 2,
  kProductQuantizer = 3,
};

struct NativeDescriptor {
  bool valid = false;
  bool legacy_v1 = false;
  u32 version = DESCRIPTOR_VERSION;
  u32 total_slots = 0;
  u32 num_mns = 1;
  ShardPolicy policy = ShardPolicy::kBlockCyclic;
  RecordLayout record_layout = RecordLayout::kFixedStride;
  u32 block_stride = 0;
  u64 header_bytes = PARAMS_RESERVE_BYTES;
  u64 packed_region_bytes = 0;
  u64 sparse_region_bytes = 0;
  // Uninterpreted legacy flags for v1; negotiated feature bits for v3.
  u32 feature_flags = 0;
  ScoringCodeKind scoring_code_kind = ScoringCodeKind::kLegacyUnspecified;
  u32 scoring_code_bits = 0;
  BudgetMapPlacement budget_map_placement = BudgetMapPlacement::kNone;
  u64 map_shift_bytes = 0;
  u32 max_degree = 0;
  u32 colocated_degree = 0;
  bool slot_only = false;
};

inline void store_u32(std::uint8_t* dst, u32 v) {
  for (u32 i = 0; i < sizeof(v); ++i) {
    dst[i] = static_cast<std::uint8_t>((v >> (8u * i)) & 0xffu);
  }
}
inline void store_u64(std::uint8_t* dst, u64 v) {
  for (u32 i = 0; i < sizeof(v); ++i) {
    dst[i] = static_cast<std::uint8_t>((v >> (8u * i)) & 0xffu);
  }
}
inline u32 load_u32(const std::uint8_t* src) {
  u32 v = 0;
  for (u32 i = 0; i < sizeof(v); ++i) {
    v |= static_cast<u32>(src[i]) << (8u * i);
  }
  return v;
}
inline u64 load_u64(const std::uint8_t* src) {
  u64 v = 0;
  for (u32 i = 0; i < sizeof(v); ++i) {
    v |= static_cast<u64>(src[i]) << (8u * i);
  }
  return v;
}

namespace detail {

constexpr u32 RECORD_LAYOUT_MASK = 0x0fu;
constexpr u32 FEATURE_SHIFT = 4;
constexpr u32 FEATURE_MASK = 0x0fu;
constexpr u32 SCORING_KIND_SHIFT = 8;
constexpr u32 SCORING_BITS_SHIFT = 16;
constexpr u32 MAP_PLACEMENT_SHIFT = 24;
constexpr u64 ABI_SLOT_ONLY_MASK = 1ull << 63u;
constexpr u64 ABI_COLOCATED_DEGREE_MASK = 0x7fffffffull;

inline bool valid_record_abi(const NativeDescriptor& d) {
  if (d.max_degree == 0 || d.colocated_degree > d.max_degree ||
      d.colocated_degree > ABI_COLOCATED_DEGREE_MASK) {
    return false;
  }
  return d.slot_only ? d.colocated_degree == 0
                     : d.colocated_degree > 0;
}

inline u64 pack_record_abi(const NativeDescriptor& d) {
  return static_cast<u64>(d.max_degree) |
         (static_cast<u64>(d.colocated_degree) << 32u) |
         (d.slot_only ? ABI_SLOT_ONLY_MASK : 0u);
}

inline void unpack_record_abi(u64 packed, NativeDescriptor* d) {
  d->max_degree = static_cast<u32>(packed & 0xffffffffull);
  d->colocated_degree = static_cast<u32>(
      (packed >> 32u) & ABI_COLOCATED_DEGREE_MASK);
  d->slot_only = (packed & ABI_SLOT_ONLY_MASK) != 0;
}

inline bool valid_record_layout(RecordLayout layout) {
  return layout == RecordLayout::kFixedStride ||
         layout == RecordLayout::kVariableRecords;
}

inline bool valid_scoring_code(ScoringCodeKind kind) {
  return kind == ScoringCodeKind::kScalarQuantizer ||
         kind == ScoringCodeKind::kRaBitQ ||
         kind == ScoringCodeKind::kProductQuantizer;
}

inline bool valid_common_descriptor(const NativeDescriptor& d) {
  u64 minimum_region_bytes = 0;
  return d.total_slots > 0 && d.num_mns > 0 &&
         d.num_mns <= MAX_DESCRIPTOR_MNS && valid_policy(d.policy) &&
         d.header_bytes >= PARAMS_RESERVE_BYTES && d.packed_region_bytes > 0 &&
         checked_mul(d.num_mns, d.header_bytes, &minimum_region_bytes) &&
         d.packed_region_bytes >= minimum_region_bytes;
}

inline bool valid_fixed_descriptor_totals(const NativeDescriptor& d) {
  FixedPhysicalTotals expected;
  return checked_fixed_physical_totals(d.total_slots, d.num_mns,
                                       d.block_stride, d.header_bytes,
                                       &expected) &&
         d.packed_region_bytes == expected.packed_region_bytes &&
         d.sparse_region_bytes == expected.sparse_region_bytes;
}

inline bool valid_variable_descriptor_prefix(const NativeDescriptor& d) {
  u64 offset_table_base = 0;
  if (!checked_add(d.header_bytes, d.map_shift_bytes, &offset_table_base)) {
    return false;
  }
  if ((offset_table_base % OFFSET_TABLE_ENTRY_BYTES) != 0) return false;

  u64 offset_table_entries = 0;
  u64 offset_table_bytes = 0;
  u64 bases_across_mns = 0;
  u64 aggregate_minimum_bytes = 0;
  return checked_add(d.total_slots, d.num_mns, &offset_table_entries) &&
         checked_mul(offset_table_entries, OFFSET_TABLE_ENTRY_BYTES,
                     &offset_table_bytes) &&
         checked_mul(d.num_mns, offset_table_base, &bases_across_mns) &&
         checked_add(bases_across_mns, offset_table_bytes,
                     &aggregate_minimum_bytes) &&
         aggregate_minimum_bytes <= d.packed_region_bytes;
}

inline bool valid_legacy_descriptor(const NativeDescriptor& d) {
  return d.version == LEGACY_DESCRIPTOR_VERSION &&
         d.record_layout == RecordLayout::kFixedStride && d.block_stride > 0 &&
         d.map_shift_bytes == 0 && valid_common_descriptor(d) &&
         valid_fixed_descriptor_totals(d);
}

inline bool valid_current_descriptor(const NativeDescriptor& d) {
  if (d.version != DESCRIPTOR_VERSION || !valid_common_descriptor(d) ||
      !valid_record_layout(d.record_layout) ||
      (d.feature_flags & ~KNOWN_FEATURE_FLAGS) != 0 ||
      !valid_scoring_code(d.scoring_code_kind) || d.scoring_code_bits == 0 ||
      d.scoring_code_bits > 255u || d.block_stride == 0 ||
      !valid_record_abi(d)) {
    return false;
  }

  const bool variable = d.record_layout == RecordLayout::kVariableRecords;
  const bool per_mn_tables =
      (d.feature_flags & FEATURE_PER_MN_OFFSET_TABLES) != 0;
  if (!variable) {
    return d.feature_flags == 0 &&
           d.budget_map_placement == BudgetMapPlacement::kNone &&
           d.map_shift_bytes == 0 && valid_fixed_descriptor_totals(d);
  }
  if (!per_mn_tables) {
    return false;
  }

  const bool has_budget_map = (d.feature_flags & FEATURE_BUDGET_MAP) != 0;
  if (!has_budget_map) {
    if (d.budget_map_placement != BudgetMapPlacement::kNone ||
        d.map_shift_bytes != 0) {
      return false;
    }
  } else {
    u64 map_payload_bytes = 0;
    if (d.budget_map_placement != BudgetMapPlacement::kMemoryNode0 ||
        !checked_mul(d.total_slots, BUDGET_MAP_ENTRY_BYTES,
                     &map_payload_bytes) ||
        !checked_align_up(map_payload_bytes, OFFSET_TABLE_ENTRY_BYTES,
                          &map_payload_bytes) ||
        d.map_shift_bytes < map_payload_bytes) {
      return false;
    }
  }
  return valid_variable_descriptor_prefix(d);
}

inline u32 pack_semantics(const NativeDescriptor& d) {
  return static_cast<u32>(d.record_layout) |
         ((d.feature_flags & FEATURE_MASK) << FEATURE_SHIFT) |
         (static_cast<u32>(d.scoring_code_kind) << SCORING_KIND_SHIFT) |
         ((d.scoring_code_bits & 0xffu) << SCORING_BITS_SHIFT) |
         (static_cast<u32>(d.budget_map_placement) << MAP_PLACEMENT_SHIFT);
}

}  // namespace detail

// Compatibility factory for legacy fixed-layout descriptors.
inline NativeDescriptor make_descriptor(const FixedExportPlan& plan) {
  NativeDescriptor d;
  const auto accounting = make_fixed_physical_accounting(plan);
  d.version = LEGACY_DESCRIPTOR_VERSION;
  d.legacy_v1 = true;
  d.total_slots = plan.layout.resolver.total_slots;
  d.num_mns = plan.layout.resolver.num_mns;
  d.policy = plan.layout.resolver.policy;
  d.record_layout = RecordLayout::kFixedStride;
  d.block_stride = plan.layout.block_stride;
  d.header_bytes = plan.layout.header_bytes;
  d.packed_region_bytes = plan.packed_region_bytes;
  d.sparse_region_bytes = plan.sparse_region_bytes;
  d.valid = accounting.valid && detail::valid_legacy_descriptor(d);
  return d;
}

inline NativeDescriptor make_fixed_descriptor(const FixedExportPlan& plan,
                                              ScoringCodeKind scoring_code_kind,
                                              u32 scoring_code_bits,
                                              u32 max_degree,
                                              u32 colocated_degree,
                                              bool slot_only) {
  NativeDescriptor d;
  const auto accounting = make_fixed_physical_accounting(plan);
  if (!accounting.valid) return d;

  d.total_slots = plan.layout.resolver.total_slots;
  d.num_mns = plan.layout.resolver.num_mns;
  d.policy = plan.layout.resolver.policy;
  d.record_layout = RecordLayout::kFixedStride;
  d.block_stride = plan.layout.block_stride;
  d.header_bytes = plan.layout.header_bytes;
  d.packed_region_bytes = plan.packed_region_bytes;
  d.sparse_region_bytes = plan.sparse_region_bytes;
  d.scoring_code_kind = scoring_code_kind;
  d.scoring_code_bits = scoring_code_bits;
  d.max_degree = max_degree;
  d.colocated_degree = colocated_degree;
  d.slot_only = slot_only;
  d.valid = detail::valid_current_descriptor(d);
  return d;
}

inline NativeDescriptor make_variable_descriptor(
    const PhysicalByteAccounting& accounting,
    u32 max_record_bytes,
    ScoringCodeKind scoring_code_kind,
    u32 scoring_code_bits,
    u32 max_degree,
    u32 colocated_degree,
    bool slot_only) {
  NativeDescriptor d;
  if (!validate_variable_physical_accounting(accounting)) {
    return d;
  }

  d.total_slots = accounting.total_slots;
  d.num_mns = accounting.num_mns;
  d.policy = accounting.policy;
  d.record_layout = RecordLayout::kVariableRecords;
  d.block_stride = max_record_bytes;
  d.header_bytes = accounting.per_mn.front().header_bytes;
  d.packed_region_bytes = accounting.total_bytes_across_mns;
  d.feature_flags = FEATURE_PER_MN_OFFSET_TABLES;
  if (accounting.budget_map_placement != BudgetMapPlacement::kNone) {
    d.feature_flags |= FEATURE_BUDGET_MAP;
  }
  d.scoring_code_kind = scoring_code_kind;
  d.scoring_code_bits = scoring_code_bits;
  d.budget_map_placement = accounting.budget_map_placement;
  d.map_shift_bytes = accounting.map_shift_bytes;
  d.max_degree = max_degree;
  d.colocated_degree = colocated_degree;
  d.slot_only = slot_only;
  d.valid = detail::valid_current_descriptor(d);
  return d;
}

inline bool descriptor_present(const std::uint8_t* header) {
  return header != nullptr &&
         load_u32(header + DESCRIPTOR_OFFSET) == DESCRIPTOR_MAGIC;
}

inline bool descriptor_matches_counts(const std::uint8_t* header,
                                      const NativeDescriptor& d) {
  if (header == nullptr) return false;
  const u32 total_slots = load_u32(header + HDR_COUNTS_OFFSET);
  const u32 budget_slots =
      load_u32(header + HDR_COUNTS_OFFSET + sizeof(u32));
  if (total_slots != d.total_slots || budget_slots == 0 ||
      budget_slots > total_slots) {
    return false;
  }
  if (d.version == DESCRIPTOR_VERSION) {
    const bool descriptor_has_budget =
        (d.feature_flags & FEATURE_BUDGET_MAP) != 0;
    if (descriptor_has_budget != (budget_slots < total_slots)) return false;
  }
  return true;
}

inline bool descriptor_is_canonical(const NativeDescriptor& d) {
  if (!d.valid) return false;
  if (d.version == LEGACY_DESCRIPTOR_VERSION) {
    return detail::valid_legacy_descriptor(d);
  }
  return d.version == DESCRIPTOR_VERSION &&
         detail::valid_current_descriptor(d);
}

inline bool write_descriptor(std::uint8_t* header, const NativeDescriptor& d) {
  if (!descriptor_is_canonical(d) || !descriptor_matches_counts(header, d)) {
    return false;
  }
  std::uint8_t encoded[DESCRIPTOR_BYTES]{};
  std::uint8_t* p = encoded;
  store_u32(p + 0, DESCRIPTOR_MAGIC);
  store_u32(p + 4, d.version);
  store_u32(p + 8, d.total_slots);
  store_u32(p + 12, d.num_mns);
  store_u32(p + 16, static_cast<u32>(d.policy));
  store_u32(p + 20, d.block_stride);
  store_u64(p + 24, d.header_bytes);
  store_u64(p + 32, d.packed_region_bytes);
  store_u32(p + 48, d.version == LEGACY_DESCRIPTOR_VERSION
                        ? d.feature_flags
                        : detail::pack_semantics(d));
  store_u32(p + 52, DESCRIPTOR_BYTES);
  if (d.version == LEGACY_DESCRIPTOR_VERSION) {
    store_u64(p + 40, d.sparse_region_bytes);
  } else if (d.record_layout == RecordLayout::kVariableRecords) {
    store_u64(p + 40, detail::pack_record_abi(d));
    store_u64(p + 56, d.map_shift_bytes);
  } else {
    store_u64(p + 40, d.sparse_region_bytes);
    store_u64(p + 56, detail::pack_record_abi(d));
  }
  for (u32 i = 0; i < DESCRIPTOR_BYTES; ++i) {
    header[DESCRIPTOR_OFFSET + i] = encoded[i];
  }
  return true;
}

inline NativeDescriptor read_descriptor(const std::uint8_t* header) {
  NativeDescriptor d;
  if (header == nullptr) return d;
  const std::uint8_t* p = header + DESCRIPTOR_OFFSET;
  if (load_u32(p + 0) != DESCRIPTOR_MAGIC) return d;

  d.version = load_u32(p + 4);
  if (d.version != LEGACY_DESCRIPTOR_VERSION &&
      d.version != DESCRIPTOR_VERSION) {
    return d;
  }
  const u32 policy_raw = load_u32(p + 16);
  if (policy_raw > static_cast<u32>(ShardPolicy::kContiguousRange) ||
      load_u32(p + 52) != DESCRIPTOR_BYTES) {
    return d;
  }

  d.total_slots = load_u32(p + 8);
  d.num_mns = load_u32(p + 12);
  d.policy = static_cast<ShardPolicy>(policy_raw);
  d.block_stride = load_u32(p + 20);
  d.header_bytes = load_u64(p + 24);
  d.packed_region_bytes = load_u64(p + 32);

  const u32 semantics = load_u32(p + 48);
  if (d.version == LEGACY_DESCRIPTOR_VERSION) {
    d.sparse_region_bytes = load_u64(p + 40);
    d.legacy_v1 = true;
    d.record_layout = RecordLayout::kFixedStride;
    d.feature_flags = semantics;
    d.valid = detail::valid_legacy_descriptor(d) &&
              descriptor_matches_counts(header, d);
    return d;
  }

  d.record_layout =
      static_cast<RecordLayout>(semantics & detail::RECORD_LAYOUT_MASK);
  d.feature_flags =
      (semantics >> detail::FEATURE_SHIFT) & detail::FEATURE_MASK;
  d.scoring_code_kind = static_cast<ScoringCodeKind>(
      (semantics >> detail::SCORING_KIND_SHIFT) & 0xffu);
  d.scoring_code_bits =
      (semantics >> detail::SCORING_BITS_SHIFT) & 0xffu;
  d.budget_map_placement = static_cast<BudgetMapPlacement>(
      (semantics >> detail::MAP_PLACEMENT_SHIFT) & 0xffu);
  if (d.record_layout == RecordLayout::kVariableRecords) {
    detail::unpack_record_abi(load_u64(p + 40), &d);
    d.map_shift_bytes = load_u64(p + 56);
  } else {
    d.sparse_region_bytes = load_u64(p + 40);
    detail::unpack_record_abi(load_u64(p + 56), &d);
  }
  d.valid = detail::valid_current_descriptor(d) &&
            descriptor_matches_counts(header, d);
  return d;
}

inline bool descriptor_record_abi_matches(const NativeDescriptor& d,
                                          u32 max_record_bytes,
                                          u32 max_degree,
                                          u32 colocated_degree,
                                          bool slot_only) {
  return d.valid && d.version == DESCRIPTOR_VERSION &&
         d.block_stride == max_record_bytes &&
         d.max_degree == max_degree &&
         d.colocated_degree == colocated_degree &&
         d.slot_only == slot_only;
}

inline bool descriptor_compatible(const NativeDescriptor& d,
                                  RecordLayout expected_layout,
                                  ScoringCodeKind expected_scoring_code,
                                  u32 expected_scoring_bits,
                                  u32 supported_features) {
  return d.valid && d.version == DESCRIPTOR_VERSION &&
         d.record_layout == expected_layout &&
         d.scoring_code_kind == expected_scoring_code &&
         d.scoring_code_bits == expected_scoring_bits &&
         (d.feature_flags & ~supported_features) == 0;
}

inline bool try_descriptor_offset_table_offset(const NativeDescriptor& d,
                                               u64* offset) {
  return offset != nullptr && d.valid &&
         d.record_layout == RecordLayout::kVariableRecords &&
         detail::checked_add(d.header_bytes, d.map_shift_bytes, offset);
}

inline u64 descriptor_offset_table_offset(const NativeDescriptor& d) {
  u64 offset = 0;
  const bool valid = try_descriptor_offset_table_offset(d, &offset);
  if (!valid) std::abort();
  return offset;
}

inline bool try_descriptor_record_region_offset(const NativeDescriptor& d,
                                                u32 mn,
                                                u64* offset) {
  if (offset == nullptr || !d.valid ||
      d.record_layout != RecordLayout::kVariableRecords || mn >= d.num_mns) {
    return false;
  }

  u64 table_offset = 0;
  u64 table_entries = 0;
  u64 table_bytes = 0;
  const auto resolver = make_resolver(d.total_slots, d.num_mns, d.policy);
  return try_descriptor_offset_table_offset(d, &table_offset) &&
         detail::checked_add(resolver.local_count(mn), 1u, &table_entries) &&
         detail::checked_mul(table_entries, OFFSET_TABLE_ENTRY_BYTES,
                             &table_bytes) &&
         detail::checked_add(table_offset, table_bytes, offset);
}

inline u64 descriptor_record_region_offset(const NativeDescriptor& d, u32 mn) {
  u64 offset = 0;
  const bool valid = try_descriptor_record_region_offset(d, mn, &offset);
  if (!valid) std::abort();
  return offset;
}

inline bool try_layout_from_descriptor(const NativeDescriptor& d,
                                       PackedL0Layout* layout) {
  if (layout == nullptr || !d.valid ||
      d.record_layout != RecordLayout::kFixedStride || d.block_stride == 0) {
    return false;
  }
  const auto resolver = make_resolver(d.total_slots, d.num_mns, d.policy);
  u64 record_region_offset = 0;
  if (!detail::checked_add(d.header_bytes, d.map_shift_bytes,
                           &record_region_offset)) {
    return false;
  }
  *layout = PackedL0Layout::fixed_stride(resolver, d.block_stride,
                                         record_region_offset);
  return true;
}

inline PackedL0Layout layout_from_descriptor(const NativeDescriptor& d) {
  PackedL0Layout layout;
  if (!try_layout_from_descriptor(d, &layout)) std::abort();
  return layout;
}

inline bool scalar_params_fit_before_metadata(size_t scalar_params_bytes,
                                              bool native_descriptor) {
  const size_t limit =
      native_descriptor ? DESCRIPTOR_OFFSET : HDR_COUNTS_OFFSET;
  return scalar_params_bytes <= limit;
}

inline bool rabitq_fits_before_descriptor(size_t scalar_params_bytes, u32 dim) {
  const size_t ro = (scalar_params_bytes + 63) & ~static_cast<size_t>(63);
  return ro + 8 + static_cast<size_t>(dim) * sizeof(float) <= DESCRIPTOR_OFFSET;
}

}  // namespace lavd::native
