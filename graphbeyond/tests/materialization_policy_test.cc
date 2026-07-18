#include <cassert>
#include <cstdint>
#include <iostream>
#include <limits>
#include <vector>

#include "lavd/materialization_policy.hh"

namespace {

using lavd::materialization::Candidate;
using lavd::materialization::ConfigurationError;
using lavd::materialization::Policy;
using lavd::materialization::SelectionError;

std::vector<Candidate> candidates() {
  return {
      Candidate{/*uid=*/0u, /*record_bytes=*/20u, /*indegree=*/3u,
                /*live_degree=*/5u, /*hop=*/2u},
      Candidate{/*uid=*/1u, /*record_bytes=*/15u, /*indegree=*/5u,
                /*live_degree=*/3u, /*hop=*/1u},
      Candidate{/*uid=*/2u, /*record_bytes=*/10u, /*indegree=*/0u,
                /*live_degree=*/100u, /*hop=*/3u},
      Candidate{/*uid=*/3u, /*record_bytes=*/8u, /*indegree=*/1u,
                /*live_degree=*/1u, /*hop=*/0u},
  };
}

}  // namespace

int main() {
  constexpr std::uint64_t kFixedBytes = 100u;

  {
    std::uint64_t parsed_bytes = 99u;
    assert(lavd::materialization::parse_budget_bytes(nullptr, &parsed_bytes));
    assert(parsed_bytes == 0u);
    assert(lavd::materialization::parse_budget_bytes("", &parsed_bytes));
    assert(parsed_bytes == 0u);
    assert(lavd::materialization::parse_budget_bytes("0", &parsed_bytes));
    assert(parsed_bytes == 0u);
    assert(lavd::materialization::parse_budget_bytes("2147483648", &parsed_bytes));
    assert(parsed_bytes == 2147483648ull);
    assert(!lavd::materialization::parse_budget_bytes("-1", &parsed_bytes));
    assert(!lavd::materialization::parse_budget_bytes("1GiB", &parsed_bytes));
    assert(!lavd::materialization::parse_budget_bytes(
        "18446744073709551616", &parsed_bytes));

    Policy parsed_policy = Policy::kBenefit;
    assert(lavd::materialization::parse_policy(nullptr, &parsed_policy));
    assert(parsed_policy == Policy::kHop);
    assert(lavd::materialization::parse_policy("benefit", &parsed_policy));
    assert(parsed_policy == Policy::kBenefit);
    assert(lavd::materialization::parse_policy("indeg", &parsed_policy));
    assert(parsed_policy == Policy::kIndegree);
    assert(lavd::materialization::parse_policy("hop", &parsed_policy));
    assert(parsed_policy == Policy::kHop);
    assert(!lavd::materialization::parse_policy("frequency", &parsed_policy));

    assert(lavd::materialization::validate_byte_budget_configuration(
               /*budget_bytes=*/0u, /*budget_frac=*/0.5f,
               /*variable_records=*/false, /*native_packed=*/false) ==
           ConfigurationError::kNone);
    assert(lavd::materialization::validate_byte_budget_configuration(
               /*budget_bytes=*/1024u, /*budget_frac=*/0.5f,
               /*variable_records=*/true, /*native_packed=*/true) ==
           ConfigurationError::kAmbiguousBudgets);
    assert(lavd::materialization::validate_byte_budget_configuration(
               /*budget_bytes=*/1024u, /*budget_frac=*/1.0f,
               /*variable_records=*/false, /*native_packed=*/true) ==
           ConfigurationError::kRequiresVariableRecords);
    assert(lavd::materialization::validate_byte_budget_configuration(
               /*budget_bytes=*/1024u, /*budget_frac=*/1.0f,
               /*variable_records=*/true, /*native_packed=*/false) ==
           ConfigurationError::kRequiresNativePackedLayout);
    assert(lavd::materialization::validate_byte_budget_configuration(
               /*budget_bytes=*/1024u, /*budget_frac=*/1.0f,
               /*variable_records=*/true, /*native_packed=*/true) ==
           ConfigurationError::kNone);
  }

  {
    const auto full_without_map =
        lavd::materialization::select_records_with_optional_map(
            candidates(), Policy::kBenefit,
            /*requested_bytes=*/153u,
            /*fixed_bytes_without_map=*/100u,
            /*fixed_bytes_with_map=*/120u);
    assert(full_without_map.selection.valid);
    assert(!full_without_map.budget_map_required);
    assert(full_without_map.selection.selected_uids.size() == candidates().size());
    assert(full_without_map.selection.fixed_bytes == 100u);
    assert(full_without_map.selection.admitted_bytes == 153u);

    const auto partial_with_map =
        lavd::materialization::select_records_with_optional_map(
            candidates(), Policy::kBenefit,
            /*requested_bytes=*/152u,
            /*fixed_bytes_without_map=*/100u,
            /*fixed_bytes_with_map=*/120u);
    assert(partial_with_map.selection.valid);
    assert(partial_with_map.budget_map_required);
    assert(partial_with_map.selection.selected_uids.size() < candidates().size());
    assert(partial_with_map.selection.fixed_bytes == 120u);
    assert(partial_with_map.selection.admitted_bytes <= 152u);

    const auto invalid_fixed_order =
        lavd::materialization::select_records_with_optional_map(
            candidates(), Policy::kBenefit,
            /*requested_bytes=*/200u,
            /*fixed_bytes_without_map=*/120u,
            /*fixed_bytes_with_map=*/100u);
    assert(!invalid_fixed_order.selection.valid);
    assert(invalid_fixed_order.selection.error ==
           SelectionError::kInvalidFixedAccounting);
  }

  {
    const auto selected = lavd::materialization::select_records(
        candidates(), Policy::kBenefit, /*requested_bytes=*/135u,
        kFixedBytes);
    assert(selected.valid);
    assert(selected.error == SelectionError::kNone);
    assert((selected.selected_uids == std::vector<std::uint32_t>{2u, 1u, 3u}));
    assert(selected.fixed_bytes == kFixedBytes);
    assert(selected.record_bytes == 33u);
    assert(selected.admitted_bytes == 133u);
    assert(selected.unused_bytes == 2u);
    assert(selected.total_benefit == 120u);
    assert(selected.selection_hash != 0u);
    assert(selected.compact_idx.size() == 4u);
    assert(selected.compact_idx[2] == 0u);
    assert(selected.compact_idx[1] == 1u);
    assert(selected.compact_idx[3] == 2u);
    assert(selected.compact_idx[0] == lavd::materialization::COLD);

    const auto repeated = lavd::materialization::select_records(
        candidates(), Policy::kBenefit, /*requested_bytes=*/135u,
        kFixedBytes);
    assert(repeated.selected_uids == selected.selected_uids);
    assert(repeated.selection_hash == selected.selection_hash);
  }

  {
    const auto selected = lavd::materialization::select_records(
        candidates(), Policy::kIndegree, /*requested_bytes=*/143u,
        kFixedBytes);
    assert(selected.valid);
    assert((selected.selected_uids == std::vector<std::uint32_t>{1u, 0u, 3u}));
    assert(selected.admitted_bytes == 143u);
    assert(selected.unused_bytes == 0u);
  }

  {
    const auto selected = lavd::materialization::select_records(
        candidates(), Policy::kHop, /*requested_bytes=*/133u, kFixedBytes);
    assert(selected.valid);
    assert((selected.selected_uids == std::vector<std::uint32_t>{3u, 1u, 2u}));
    assert(selected.admitted_bytes == 133u);
  }

  {
    std::vector<Candidate> tied{
        Candidate{0u, 10u, 1u, 5u, 0u},   // benefit 10, ratio 1
        Candidate{1u, 20u, 3u, 5u, 0u},   // benefit 20, ratio 1
        Candidate{2u, 20u, 3u, 5u, 0u},   // same benefit and ratio
    };
    const auto selected = lavd::materialization::select_records(
        tied, Policy::kBenefit, /*requested_bytes=*/150u, kFixedBytes);
    assert(selected.valid);
    assert((selected.selected_uids == std::vector<std::uint32_t>{1u, 2u, 0u}));
  }

  {
    const auto below_fixed = lavd::materialization::select_records(
        candidates(), Policy::kBenefit, /*requested_bytes=*/99u, kFixedBytes);
    assert(!below_fixed.valid);
    assert(below_fixed.error == SelectionError::kBudgetBelowFixedBytes);

    const auto no_fit = lavd::materialization::select_records(
        candidates(), Policy::kBenefit, /*requested_bytes=*/107u, kFixedBytes);
    assert(!no_fit.valid);
    assert(no_fit.error == SelectionError::kNoRecordFits);
  }

  {
    auto invalid = candidates();
    invalid[2].uid = 9u;
    const auto selected = lavd::materialization::select_records(
        invalid, Policy::kBenefit, /*requested_bytes=*/200u, kFixedBytes);
    assert(!selected.valid);
    assert(selected.error == SelectionError::kInvalidCandidateOrder);
  }

  {
    auto invalid = candidates();
    invalid[0].record_bytes = 0u;
    const auto selected = lavd::materialization::select_records(
        invalid, Policy::kBenefit, /*requested_bytes=*/200u, kFixedBytes);
    assert(!selected.valid);
    assert(selected.error == SelectionError::kZeroRecordBytes);
  }

  {
    auto overflow = candidates();
    overflow[0].indegree = std::numeric_limits<std::uint64_t>::max();
    overflow[0].live_degree = 2u;
    const auto selected = lavd::materialization::select_records(
        overflow, Policy::kBenefit, /*requested_bytes=*/200u, kFixedBytes);
    assert(!selected.valid);
    assert(selected.error == SelectionError::kArithmeticOverflow);
  }

  std::cout << "materialization_policy_test PASS" << std::endl;
  return 0;
}
