#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace evidence {

inline constexpr std::uint64_t QUERY_RESULT_FINGERPRINT_VERSION = 1u;

struct QueryResult {
  std::uint32_t query_id = 0;
  std::vector<std::uint32_t> neighbors;
};

struct QueryResultFingerprint {
  bool valid = false;
  std::size_t queries = 0;
  std::uint64_t hash = 0;
};

inline void fingerprint_u64(std::uint64_t value, std::uint64_t* hash) {
  constexpr std::uint64_t kPrime = 1099511628211ull;
  for (unsigned shift = 0; shift < 64u; shift += 8u) {
    *hash ^= static_cast<std::uint8_t>(value >> shift);
    *hash *= kPrime;
  }
}

inline QueryResultFingerprint fingerprint_query_results(
    std::vector<QueryResult> results) {
  QueryResultFingerprint fingerprint;
  std::sort(results.begin(), results.end(),
            [](const QueryResult& lhs, const QueryResult& rhs) {
              return lhs.query_id < rhs.query_id;
            });
  for (std::size_t i = 1; i < results.size(); ++i) {
    if (results[i - 1].query_id == results[i].query_id) return fingerprint;
  }

  std::uint64_t hash = 1469598103934665603ull;
  fingerprint_u64(QUERY_RESULT_FINGERPRINT_VERSION, &hash);
  fingerprint_u64(results.size(), &hash);
  for (const auto& result : results) {
    fingerprint_u64(result.query_id, &hash);
    fingerprint_u64(result.neighbors.size(), &hash);
    for (const std::uint32_t neighbor : result.neighbors) {
      fingerprint_u64(neighbor, &hash);
    }
  }
  fingerprint.valid = true;
  fingerprint.queries = results.size();
  fingerprint.hash = hash;
  return fingerprint;
}

}  // namespace evidence
