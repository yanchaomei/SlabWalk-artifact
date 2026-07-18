#pragma once

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>

namespace lavd::materialization {

constexpr std::uint32_t COLD = std::numeric_limits<std::uint32_t>::max();
constexpr std::uint32_t SELECTION_VERSION = 1u;

enum class Policy : std::uint8_t {
  kBenefit = 1,
  kIndegree = 2,
  kHop = 3,
};

enum class SelectionError : std::uint8_t {
  kNone = 0,
  kBudgetBelowFixedBytes,
  kNoRecordFits,
  kInvalidCandidateOrder,
  kZeroRecordBytes,
  kArithmeticOverflow,
  kInvalidFixedAccounting,
};

enum class ConfigurationError : std::uint8_t {
  kNone = 0,
  kAmbiguousBudgets,
  kRequiresVariableRecords,
  kRequiresNativePackedLayout,
};

struct Candidate {
  std::uint32_t uid = 0;
  std::uint64_t record_bytes = 0;
  std::uint64_t indegree = 0;
  std::uint32_t live_degree = 0;
  std::uint32_t hop = 0;
};

struct Selection {
  bool valid = false;
  SelectionError error = SelectionError::kNone;
  Policy policy = Policy::kBenefit;
  std::uint64_t requested_bytes = 0;
  std::uint64_t fixed_bytes = 0;
  std::uint64_t record_bytes = 0;
  std::uint64_t admitted_bytes = 0;
  std::uint64_t unused_bytes = 0;
  std::uint64_t total_benefit = 0;
  std::uint64_t selection_hash = 0;
  std::vector<std::uint32_t> selected_uids;
  std::vector<std::uint32_t> compact_idx;
};

struct OptionalMapSelection {
  Selection selection;
  bool budget_map_required = false;
};

namespace detail {

__extension__ typedef unsigned __int128 u128;

struct RankedCandidate {
  std::uint32_t uid = 0;
  std::uint64_t benefit = 0;
};

inline bool compute_benefit(const Candidate& candidate,
                            std::uint64_t* benefit) {
  if (benefit == nullptr ||
      candidate.indegree == std::numeric_limits<std::uint64_t>::max()) {
    return false;
  }
  const std::uint64_t frequency = candidate.indegree + 1u;
  if (candidate.live_degree != 0u &&
      frequency > std::numeric_limits<std::uint64_t>::max() /
                      candidate.live_degree) {
    return false;
  }
  *benefit = frequency * candidate.live_degree;
  return true;
}

inline void hash_u64(std::uint64_t value, std::uint64_t* hash) {
  constexpr std::uint64_t kPrime = 1099511628211ull;
  for (unsigned shift = 0; shift < 64u; shift += 8u) {
    *hash ^= static_cast<std::uint8_t>(value >> shift);
    *hash *= kPrime;
  }
}

}  // namespace detail

inline const char* policy_name(Policy policy) {
  switch (policy) {
    case Policy::kBenefit:
      return "benefit";
    case Policy::kIndegree:
      return "indeg";
    case Policy::kHop:
      return "hop";
  }
  return "unknown";
}

inline bool parse_budget_bytes(const char* text, std::uint64_t* value) {
  if (value == nullptr) return false;
  if (text == nullptr || *text == '\0') {
    *value = 0u;
    return true;
  }
  if (*text == '-') return false;
  errno = 0;
  char* end = nullptr;
  const unsigned long long parsed = std::strtoull(text, &end, 10);
  if (errno == ERANGE || end == text || *end != '\0') return false;
  *value = static_cast<std::uint64_t>(parsed);
  return true;
}

inline bool parse_policy(const char* text, Policy* policy) {
  if (policy == nullptr) return false;
  if (text == nullptr || *text == '\0' || std::strcmp(text, "hop") == 0) {
    *policy = Policy::kHop;
    return true;
  }
  if (std::strcmp(text, "benefit") == 0) {
    *policy = Policy::kBenefit;
    return true;
  }
  if (std::strcmp(text, "indeg") == 0) {
    *policy = Policy::kIndegree;
    return true;
  }
  return false;
}

inline ConfigurationError validate_byte_budget_configuration(
    std::uint64_t budget_bytes, float budget_frac, bool variable_records,
    bool native_packed) {
  if (budget_bytes == 0u) return ConfigurationError::kNone;
  if (budget_frac < 1.0f) return ConfigurationError::kAmbiguousBudgets;
  if (!variable_records) {
    return ConfigurationError::kRequiresVariableRecords;
  }
  if (!native_packed) {
    return ConfigurationError::kRequiresNativePackedLayout;
  }
  return ConfigurationError::kNone;
}

inline Selection select_records(const std::vector<Candidate>& candidates,
                                Policy policy,
                                std::uint64_t requested_bytes,
                                std::uint64_t fixed_bytes) {
  Selection result;
  result.policy = policy;
  result.requested_bytes = requested_bytes;
  result.fixed_bytes = fixed_bytes;

  if (requested_bytes < fixed_bytes) {
    result.error = SelectionError::kBudgetBelowFixedBytes;
    return result;
  }
  if (candidates.size() > std::numeric_limits<std::uint32_t>::max()) {
    result.error = SelectionError::kInvalidCandidateOrder;
    return result;
  }

  std::vector<detail::RankedCandidate> ranked;
  ranked.reserve(candidates.size());
  for (std::size_t i = 0; i < candidates.size(); ++i) {
    const Candidate& candidate = candidates[i];
    if (candidate.uid != i) {
      result.error = SelectionError::kInvalidCandidateOrder;
      return result;
    }
    if (candidate.record_bytes == 0u) {
      result.error = SelectionError::kZeroRecordBytes;
      return result;
    }
    std::uint64_t benefit = 0;
    if (!detail::compute_benefit(candidate, &benefit)) {
      result.error = SelectionError::kArithmeticOverflow;
      return result;
    }
    ranked.push_back(detail::RankedCandidate{candidate.uid, benefit});
  }

  switch (policy) {
    case Policy::kBenefit:
      std::sort(ranked.begin(), ranked.end(),
                [&](const auto& lhs, const auto& rhs) {
                  const auto lhs_cross =
                      static_cast<detail::u128>(lhs.benefit) *
                      candidates[rhs.uid].record_bytes;
                  const auto rhs_cross =
                      static_cast<detail::u128>(rhs.benefit) *
                      candidates[lhs.uid].record_bytes;
                  if (lhs_cross != rhs_cross) return lhs_cross > rhs_cross;
                  if (lhs.benefit != rhs.benefit) {
                    return lhs.benefit > rhs.benefit;
                  }
                  return lhs.uid < rhs.uid;
                });
      break;
    case Policy::kIndegree:
      std::sort(ranked.begin(), ranked.end(),
                [&](const auto& lhs, const auto& rhs) {
                  if (candidates[lhs.uid].indegree !=
                      candidates[rhs.uid].indegree) {
                    return candidates[lhs.uid].indegree >
                           candidates[rhs.uid].indegree;
                  }
                  return lhs.uid < rhs.uid;
                });
      break;
    case Policy::kHop:
      std::sort(ranked.begin(), ranked.end(),
                [&](const auto& lhs, const auto& rhs) {
                  if (candidates[lhs.uid].hop != candidates[rhs.uid].hop) {
                    return candidates[lhs.uid].hop < candidates[rhs.uid].hop;
                  }
                  return lhs.uid < rhs.uid;
                });
      break;
  }

  result.compact_idx.assign(candidates.size(), COLD);
  std::uint64_t remaining = requested_bytes - fixed_bytes;
  for (const auto& ranked_candidate : ranked) {
    const Candidate& candidate = candidates[ranked_candidate.uid];
    if (candidate.record_bytes > remaining) continue;
    if (result.total_benefit >
        std::numeric_limits<std::uint64_t>::max() - ranked_candidate.benefit) {
      result.error = SelectionError::kArithmeticOverflow;
      result.selected_uids.clear();
      result.compact_idx.clear();
      return result;
    }
    result.compact_idx[candidate.uid] =
        static_cast<std::uint32_t>(result.selected_uids.size());
    result.selected_uids.push_back(candidate.uid);
    result.record_bytes += candidate.record_bytes;
    result.total_benefit += ranked_candidate.benefit;
    remaining -= candidate.record_bytes;
  }

  if (result.selected_uids.empty()) {
    result.error = SelectionError::kNoRecordFits;
    result.compact_idx.clear();
    return result;
  }

  result.admitted_bytes = fixed_bytes + result.record_bytes;
  result.unused_bytes = requested_bytes - result.admitted_bytes;
  std::uint64_t hash = 1469598103934665603ull;
  detail::hash_u64(SELECTION_VERSION, &hash);
  detail::hash_u64(static_cast<std::uint8_t>(policy), &hash);
  for (const std::uint32_t uid : result.selected_uids) {
    detail::hash_u64(uid, &hash);
    detail::hash_u64(candidates[uid].record_bytes, &hash);
  }
  result.selection_hash = hash;
  result.valid = true;
  return result;
}

inline OptionalMapSelection select_records_with_optional_map(
    const std::vector<Candidate>& candidates, Policy policy,
    std::uint64_t requested_bytes, std::uint64_t fixed_bytes_without_map,
    std::uint64_t fixed_bytes_with_map) {
  OptionalMapSelection result;
  if (fixed_bytes_with_map < fixed_bytes_without_map) {
    result.selection.policy = policy;
    result.selection.requested_bytes = requested_bytes;
    result.selection.fixed_bytes = fixed_bytes_without_map;
    result.selection.error = SelectionError::kInvalidFixedAccounting;
    return result;
  }

  result.selection = select_records(candidates, policy, requested_bytes,
                                    fixed_bytes_without_map);
  if (result.selection.valid &&
      result.selection.selected_uids.size() == candidates.size()) {
    return result;
  }

  result.selection = select_records(candidates, policy, requested_bytes,
                                    fixed_bytes_with_map);
  result.budget_map_required = result.selection.valid;
  return result;
}

}  // namespace lavd::materialization
