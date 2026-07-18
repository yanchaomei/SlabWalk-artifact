#pragma once

#include <cstdint>
#include <cstdlib>
#include <utility>
#include <vector>

namespace lavd {

struct AuthoritativeSnapshot {
  std::vector<void*> shards;
  std::vector<std::uint64_t> bytes;
  std::uint64_t entry_raw = 0;

  AuthoritativeSnapshot() = default;
  AuthoritativeSnapshot(const AuthoritativeSnapshot&) = delete;
  AuthoritativeSnapshot& operator=(const AuthoritativeSnapshot&) = delete;
  ~AuthoritativeSnapshot() { clear(); }

  void clear() {
    for (void* shard : shards) std::free(shard);
    shards.clear();
    bytes.clear();
    entry_raw = 0;
  }

  bool available(std::uint32_t expected_shards) const {
    return expected_shards > 0 && shards.size() == expected_shards &&
           bytes.size() == expected_shards && entry_raw != 0;
  }
};

inline AuthoritativeSnapshot g_authoritative_snapshot;

inline AuthoritativeSnapshot& authoritative_snapshot() {
  return g_authoritative_snapshot;
}

inline bool authoritative_snapshot_available(std::uint32_t expected_shards) {
  return g_authoritative_snapshot.available(expected_shards);
}

inline void clear_authoritative_snapshot() {
  g_authoritative_snapshot.clear();
}

inline void publish_authoritative_snapshot(
    std::vector<void*>&& shards, std::vector<std::uint64_t>&& bytes,
    std::uint64_t entry_raw) {
  if (shards.empty() || shards.size() != bytes.size() || entry_raw == 0) {
    for (void* shard : shards) std::free(shard);
    std::abort();
  }
  clear_authoritative_snapshot();
  g_authoritative_snapshot.shards = std::move(shards);
  g_authoritative_snapshot.bytes = std::move(bytes);
  g_authoritative_snapshot.entry_raw = entry_raw;
}

}  // namespace lavd
