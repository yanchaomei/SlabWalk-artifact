#include <cassert>
#include <string_view>

#include "../src/lavd/maintenance_guard.hh"

int main() {
  using lavd::offline_rematerialization_rejection;

  assert(offline_rematerialization_rejection(1, false, false, false).empty());
  assert(offline_rematerialization_rejection(2, false, false, false) ==
         std::string_view{"requires exactly one memory node"});
  assert(offline_rematerialization_rejection(1, true, false, false) ==
         std::string_view{"does not support native packed placement"});
  assert(offline_rematerialization_rejection(1, false, true, false) ==
         std::string_view{"does not support reordered placement"});
  assert(offline_rematerialization_rejection(1, false, false, true) ==
         std::string_view{"does not support variable records"});

  assert(lavd::offline_mirror_growth_rejection(100, 100, 128).empty());
  assert(lavd::offline_mirror_growth_rejection(100, 120, 128).empty());
  assert(lavd::offline_mirror_growth_rejection(100, 99, 128) ==
         std::string_view{"authoritative index shrank"});
  assert(lavd::offline_mirror_growth_rejection(100, 129, 128) ==
         std::string_view{"authoritative index exceeds mirror capacity"});
}
