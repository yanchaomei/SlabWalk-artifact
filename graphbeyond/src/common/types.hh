#pragma once

#include <filesystem>
#include <library/types.hh>
#include <unordered_map>
#include <unordered_set>

using node_t = u32;
using element_t = f32;
using distance_t = f32;

using filepath_t = std::filesystem::path;

#ifdef SHINE_OPT
// baseline-opt: the long-standing "faster hashset" TODO, realized.
// Only hashset_t<RemotePtr> is ever instantiated (the visited set);
// swap just that to an open-addressing set, leave everything else.
#include "common/fastset.hh"
struct RemotePtr;  // fwd-decl only (avoid circular include); spec tag
template <typename T>
struct hashset_sel {
  using type = std::unordered_set<T>;
};
template <>
struct hashset_sel<RemotePtr> {
  using type = FastPtrSet;
};
template <typename T>
using hashset_t = typename hashset_sel<T>::type;
#else
template <typename T>
using hashset_t = std::unordered_set<T>;  // TODO: replace with faster hashset
#endif

template <typename K, typename V>
using hashmap_t = std::unordered_map<K, V>;  // TODO: replace with faster hashmap