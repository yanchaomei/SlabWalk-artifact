#include <cassert>
#include <cstdint>
#include <vector>

#include "common/result_fingerprint.hh"

int main() {
  using evidence::QueryResult;

  const std::vector<QueryResult> ordered{
      QueryResult{0u, {1u, 2u}},
      QueryResult{1u, {3u}},
      QueryResult{2u, {5u, 6u}},
  };
  const std::vector<QueryResult> shuffled{
      QueryResult{2u, {5u, 6u}},
      QueryResult{0u, {1u, 2u}},
      QueryResult{1u, {3u}},
  };
  const auto expected = evidence::fingerprint_query_results(ordered);
  const auto reordered = evidence::fingerprint_query_results(shuffled);
  assert(expected.valid);
  assert(expected.queries == 3u);
  assert(expected.hash != 0u);
  assert(reordered.valid);
  assert(reordered.hash == expected.hash);

  auto changed_order = ordered;
  changed_order[0].neighbors = {2u, 1u};
  assert(evidence::fingerprint_query_results(changed_order).hash != expected.hash);

  auto duplicate_query = ordered;
  duplicate_query.push_back(QueryResult{1u, {9u}});
  assert(!evidence::fingerprint_query_results(duplicate_query).valid);
  return 0;
}
