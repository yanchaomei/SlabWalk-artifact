#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <vector>

#include "lavd/build_snapshot.hh"

int main() {
  lavd::clear_authoritative_snapshot();
  std::vector<void*> shards{std::malloc(64), std::malloc(128)};
  std::vector<std::uint64_t> bytes{64, 128};
  lavd::publish_authoritative_snapshot(std::move(shards), std::move(bytes), 77);
  assert(lavd::authoritative_snapshot_available(2));
  assert(!lavd::authoritative_snapshot_available(1));
  assert(lavd::authoritative_snapshot().entry_raw == 77);
  assert(lavd::authoritative_snapshot().bytes[1] == 128);
  lavd::clear_authoritative_snapshot();
  assert(!lavd::authoritative_snapshot_available(2));
  return 0;
}
