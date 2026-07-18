#pragma once
// baseline-opt (SHINE_OPT): open-addressing visited set, the codebase's
// own "TODO: replace with faster hashset". Drop-in for the subset used
// by the search path (insert / contains / clear; never iterated).
// Keyed on RemotePtr::raw_address (u64). O(1) clear via an epoch tag so
// a per-query reset costs no rehash/alloc/free (the std::unordered_set
// churn the profile attributes ~10% of the saturated CN thread to).

#include <cstdint>
#include <vector>
#include <library/types.hh>

// Key-agnostic: insert/contains are member templates that read
// `.raw_address`, so this header does NOT need RemotePtr's definition
// (avoids a circular include: remote_pointer.hh -> types.hh ->
// fastset.hh). Instantiated only at call sites where RemotePtr is
// complete.
class FastPtrSet {
  struct Slot { u64 key; u32 epoch; };
  std::vector<Slot> t_;
  u64 mask_;
  u32 epoch_ = 1;
  size_t size_ = 0;

  void grow() {
    std::vector<Slot> old = std::move(t_);
    t_.assign((mask_ + 1) * 2, Slot{0, 0});
    mask_ = t_.size() - 1;
    size_ = 0;
    const u32 e = epoch_;
    for (auto& s : old)
      if (s.epoch == e) raw_insert(s.key);
  }
  bool raw_insert(u64 k) {
    u64 i = (k * 0x9E3779B97F4A7C15ull) & mask_;
    while (t_[i].epoch == epoch_) {
      if (t_[i].key == k) return false;
      i = (i + 1) & mask_;
    }
    t_[i] = Slot{k, epoch_};
    ++size_;
    return true;
  }

 public:
  explicit FastPtrSet(size_t cap = 4096) {
    size_t n = 1;
    while (n < cap) n <<= 1;
    t_.assign(n, Slot{0, 0});
    mask_ = n - 1;
  }
  template <class P>
  bool insert(const P& p) {
    if ((size_ + 1) * 4 >= (mask_ + 1) * 3) grow();  // load factor 0.75
    return raw_insert(p.raw_address);
  }
  template <class P>
  bool contains(const P& p) const {
    u64 k = p.raw_address;
    u64 i = (k * 0x9E3779B97F4A7C15ull) & mask_;
    while (t_[i].epoch == epoch_) {
      if (t_[i].key == k) return true;
      i = (i + 1) & mask_;
    }
    return false;
  }
  void clear() {
    if (++epoch_ == 0) {  // epoch wrapped (~4e9 queries): hard reset
      for (auto& s : t_) s.epoch = 0;
      epoch_ = 1;
    }
    size_ = 0;
  }
  size_t size() const { return size_; }
};
