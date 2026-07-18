#pragma once

// GraphBeyond LAVD — view over one fat-read neighborhood block.
// Mirrors Neighborlist: holds the buffer slice + owner, frees the slot
// back to the owner thread's neighborhood freelist on destruction.
// Block layout/accessors are defined in lavd/layout.hh (shared with the
// CN build pass so writer and reader agree byte-for-byte).

#include "compute_thread.hh"
#include "lavd/config.hh"
#include "lavd/layout.hh"
#include "remote_pointer.hh"

class Neighborhood {
public:
  struct EntryView {
    u32 slot;
    RemotePtr rptr;
    const u8* qvec;
    bool has_qvec;
  };

  Neighborhood(byte_t* buffer_ptr, const u_ptr<ComputeThread>& owner)
      : buffer_slice_(buffer_ptr),
        owner_(owner.get()),
        decoder_(buffer_ptr, lavd::Config::dim, lavd::Config::bits,
                 lavd::Config::m_max0) {}

  ~Neighborhood() { owner_->buffer_allocator.free_neighborhood(buffer_slice_, owner_->get_id()); }

  Neighborhood(const Neighborhood&) = delete;
  Neighborhood(Neighborhood&&) noexcept = delete;
  Neighborhood& operator=(const Neighborhood&) = delete;
  Neighborhood& operator=(Neighborhood&&) noexcept = delete;

  u32 count() const {
    const u32 c = lavd::blk_count(buffer_slice_);
    return c <= lavd::Config::m_max0 ? c : lavd::Config::m_max0;
  }

  // entry i accessors
  const byte_t* entry(u32 i) const {
    return lavd::blk_entry(buffer_slice_, i, lavd::Config::dim, lavd::Config::bits,
                           lavd::Config::m_max0);
  }
  u32 nbr_slot(u32 i) const { return lavd::ent_slot(entry(i)); }
  RemotePtr nbr_rptr(u32 i) const {
    if (lavd::slot_only_on()) {
      return RemotePtr{lavd::slot_to_rptr_raw(nbr_slot(i))};
    }
    return RemotePtr{lavd::ent_rptr(entry(i))};
  }
  bool has_qvec(u32 i) const {
    if (lavd::slot_only_on()) {
      // Tiered mode: only hot uids have a packed_qvec entry; cold
      // neighbors of hot centers fall back to read_node + on-the-fly
      // encode (the existing has_qvec=false branch). Off => CN cache
      // covers every uid (N*pqs CN table).
      if (lavd::tiered_cache_on() && lavd::Config::budget_on()) {
        return lavd::Config::is_hot(nbr_slot(i));
      }
      return true;
    }
    return lavd::entry_has_qvec(i, lavd::Config::m_max0);
  }
  const u8* nbr_qvec(u32 i) const {
    if (lavd::slot_only_on()) return lavd::slot_qvec_ptr(nbr_slot(i));
    return lavd::ent_qvec(entry(i));
  }
  __attribute__((always_inline)) inline EntryView decode_entry(u32 i) const {
    const auto decoded = decoder_.decode(i);
    return EntryView{
        decoded.slot, RemotePtr{decoded.rptr}, decoded.qvec,
        decoded.has_qvec};
  }

  byte_t* buffer_ptr() const { return buffer_slice_; }

private:
  byte_t* buffer_slice_{};
  ComputeThread* owner_{};
  lavd::BlockEntryDecoder decoder_;
};
