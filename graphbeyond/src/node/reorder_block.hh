#pragma once

// GraphBeyond — view over a reorder block fetched by rdma::read_reorder_block.
// Mirrors Neighborhood: holds the buffer slice + owner, frees the slab back to
// the owner thread's reorder freelist on destruction. Exposes the in-block
// nodes: uid, rptr (for fp32 rerank from the INDEX region), the stored vector
// (fp32 or sq8), and the level-0 neighbor uids. Layout in lavd/reorder_layout.hh.

#include <library/types.hh>
#include "compute_thread.hh"
#include "lavd/config.hh"
#include "lavd/reorder_layout.hh"

class ReorderBlock {
public:
  ReorderBlock(byte_t* buffer_ptr, const u_ptr<ComputeThread>& owner)
      : buf_(buffer_ptr), owner_(owner.get()) {}

  ~ReorderBlock() { owner_->buffer_allocator.free_reorder_block(buf_, owner_->get_id()); }

  ReorderBlock(const ReorderBlock&) = delete;
  ReorderBlock(ReorderBlock&&) noexcept = delete;
  ReorderBlock& operator=(const ReorderBlock&) = delete;
  ReorderBlock& operator=(ReorderBlock&&) noexcept = delete;

  u32 n_nodes() const { return lavd::rb_n_nodes(buf_); }

  const byte_t* entry(u32 i) const {
    return lavd::rb_entry(buf_, i, lavd::Config::m_max0, lavd::Config::rb_vbytes());
  }
  u32 uid(u32 i) const { return lavd::re_uid(entry(i)); }
  u32 l0count(u32 i) const { return lavd::re_l0count(entry(i)); }
  u64 rptr(u32 i) const { return lavd::re_rptr(entry(i)); }
  const byte_t* vec(u32 i) const { return lavd::re_vec(entry(i)); }
  const u32* nbr(u32 i) const { return lavd::re_nbr(entry(i), lavd::Config::rb_vbytes()); }

  byte_t* buffer_ptr() const { return buf_; }

private:
  byte_t* buf_{};
  ComputeThread* owner_{};
};
