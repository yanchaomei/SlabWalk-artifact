#pragma once
// GraphBeyond — slot-indexed visited bitmap (T6).
//
// One bit per dense slot (uid). Replaces FastPtrSet / std::unordered_set
// keyed on RemotePtr::raw_address at the LAVD level-0 beam visited-set
// call sites in hnsw.hh, gated by env var GB_BITMAP_DEDUP=1.
//
// WHY THIS IS BYTE-IDENTICAL TO THE HASHSET PATH:
//   In the LAVD path, every visited node has a dense `slot` (the
//   neighborhood-region index) AND a unique `RemotePtr`. Because the
//   build pass enforces a 1:1 bijection slot <-> RemotePtr (the slot
//   IS the dense uid into the neighborhood region; see lavd::layout
//   and Neighborhood::nbr_slot / nbr_rptr), the predicate
//       "rptr already in `visited`"
//   is mathematically equivalent to
//       "slot(rptr) already in `visited_slot_bitmap`".
//   Every call site that hashset.insert(nrp)'d is paired with a
//   nh->nbr_slot(i) (or seed_slot for the seed) on the SAME entry — see
//   neighborhood.hh: nbr_slot(i)/nbr_rptr(i) read the same entry(i).
//   Therefore the insert/contains decisions, and hence the iteration
//   order of next/top heap pushes, are bit-for-bit identical between
//   the two paths. The bitmap is only a faster O(1) data structure for
//   the same set semantics.
//
// CLEAR STRATEGY: epoch-tagged words (mirrors FastPtrSet) — clear() bumps
// the epoch in O(1) instead of memset'ing the bit array. Each word stores
// (epoch | bits): on read we treat a word from an older epoch as zero.
// Worst case (epoch wrap @ ~4e9 queries) does a full memset.

#include <cstdint>
#include <cstring>
#include <vector>

#include <library/types.hh>

namespace gb {

class SlotBitmap {
 public:
  SlotBitmap() = default;

  // Reserve enough words to cover [0, capacity_slots) without bounds
  // checks on the hot path. Called once when the bitmap path is enabled.
  void reserve(u32 capacity_slots) {
    capacity_ = capacity_slots;
    const size_t nw = (static_cast<size_t>(capacity_slots) + 63) >> 6;
    words_.assign(nw, Word{0, 0});
  }

  u32 capacity() const { return capacity_; }

  // contains(slot): true iff insert(slot) has been called this epoch.
  // No bounds check — callers must use slot < capacity (LAVD slots are
  // dense uids in [0, N), reserve() is sized to N).
  bool contains(u32 slot) const {
    const size_t wi = slot >> 6;
    const Word& w = words_[wi];
    if (w.epoch != epoch_) return false;
    return (w.bits >> (slot & 63)) & 1ull;
  }

  // insert(slot): set the bit; returns true iff the bit was previously
  // unset (mimics std::unordered_set::insert().second / FastPtrSet
  // semantics). The LAVD call sites do `if (contains) continue; insert;`,
  // so they only consume the void/bool side-effect, but we preserve the
  // bool return for completeness.
  bool insert(u32 slot) {
    const size_t wi = slot >> 6;
    Word& w = words_[wi];
    if (w.epoch != epoch_) {
      w.epoch = epoch_;
      w.bits = 0;  // lazy-zero stale word from a previous query
    }
    const u64 mask = 1ull << (slot & 63);
    const bool was_set = (w.bits & mask) != 0;
    w.bits |= mask;
    return !was_set;
  }

  // O(1) clear: bump epoch, all old (epoch != epoch_) words read as zero.
  // On wrap (~4e9 queries) do a hard reset so stale words from epoch 0
  // can't be mistaken for current.
  void clear() {
    if (++epoch_ == 0) {
      std::memset(words_.data(), 0, words_.size() * sizeof(Word));
      epoch_ = 1;
    }
  }

 private:
  struct Word {
    u64 bits;
    u32 epoch;
  };
  std::vector<Word> words_;
  u32 capacity_ = 0;
  u32 epoch_ = 1;
};

// Process-wide switch: GB_BITMAP_DEDUP=1 turns on the bitmap path at the
// LAVD visited call sites. Read once and cached. Unset / "0" -> the
// original hashset path is taken byte-identically.
inline bool bitmap_dedup_on() {
  static const bool on = [] {
    const char* e = std::getenv("GB_BITMAP_DEDUP");
    return e && std::atoi(e) != 0;
  }();
  return on;
}

}  // namespace gb
