#pragma once

// GraphBeyond — "reorder, not replicate": vector-clustered block layout.
//
// Instead of LAVD's per-node fat block (each neighbor's vector REPLICATED
// ~M times -> M x memory), nodes are grouped into VECTOR-CLUSTER blocks
// (k-means on the vectors, eps nodes/block, each node stored EXACTLY ONCE).
// A hop reads the block holding the current node; the Starling-style block
// search scores ALL in-block nodes (the over-read is free on idle bandwidth
// -- the op-count-wall corollary) and follows neighbor uids to other blocks.
// Memory = 1x (no replication); recall-neutral (full fp32 / sq8 + fp32
// rerank). The win is FEWER ops at iso-recall (the over-read serves many
// query-relevant co-clustered nodes per read).
//
// Region layout (2nd MN region, reused):
//   [0, PARAMS_RESERVE)            quantizer params (sq8) + [N u32][n_blocks u32] @ HDR_COUNTS_OFF
//   [PARAMS_RESERVE, +N*4)         block_of[uid]            (u32)
//   [+, +(n_blocks+1)*8)           block_off[b]             (u64 absolute byte offset; last = end)
//   [+, ...)                       the variable-size blocks
//
// Block b (at block_off[b], size block_off[b+1]-block_off[b]):
//   [0) u32 n_nodes [4) u32 _pad   then n_nodes x node_entry:
//     [0)  u32 uid
//     [4)  u32 l0_count            (valid nbr_uid count, <= m_max0)
//     [8)  u64 rptr                (RemotePtr raw -> INDEX region, for fp32 rerank)
//     [16) vec[V]                  (V = dim*4 fp32 | dim sq8)
//     [16+V) u32 nbr_uid[m_max0]   (level-0 neighbor uids)

#include <library/types.hh>
#include "lavd/layout.hh"  // PARAMS_RESERVE, HDR_COUNTS_OFF

namespace lavd {

constexpr size_t RB_BLOCK_HEADER = 8;     // u32 n_nodes + u32 pad
constexpr size_t RB_ENTRY_FIXED = 16;     // u32 uid + u32 l0_count + u64 rptr

// reorder metadata in the params header (distinct from LAVD's HDR_COUNTS_OFF
// at PARAMS_RESERVE-8): [u32 n_blocks][u32 rb_sq8]. The initiator writes it;
// every CN reads it to size the offset-table RDMA read on load.
constexpr size_t RB_HDR_OFF = PARAMS_RESERVE - 16;

inline size_t rb_node_entry(u32 m_max0, size_t vbytes) {
  return RB_ENTRY_FIXED + vbytes + static_cast<size_t>(m_max0) * sizeof(u32);
}
inline size_t rb_block_bytes(u32 n_nodes, u32 m_max0, size_t vbytes) {
  return RB_BLOCK_HEADER + static_cast<size_t>(n_nodes) * rb_node_entry(m_max0, vbytes);
}

// region sub-region offsets
inline size_t rb_map_off() { return PARAMS_RESERVE; }                       // block_of[uid]
inline size_t rb_offtab_off(u32 N) { return PARAMS_RESERVE + static_cast<size_t>(N) * sizeof(u32); }
inline size_t rb_blocks_base(u32 N, u32 n_blocks) {
  return rb_offtab_off(N) + static_cast<size_t>(n_blocks + 1) * sizeof(u64);
}

// block accessors
inline u32& rb_n_nodes(byte_t* b) { return *reinterpret_cast<u32*>(b); }
inline u32 rb_n_nodes(const byte_t* b) { return *reinterpret_cast<const u32*>(b); }
inline byte_t* rb_entry(byte_t* b, u32 i, u32 m_max0, size_t vbytes) {
  return b + RB_BLOCK_HEADER + static_cast<size_t>(i) * rb_node_entry(m_max0, vbytes);
}
inline const byte_t* rb_entry(const byte_t* b, u32 i, u32 m_max0, size_t vbytes) {
  return b + RB_BLOCK_HEADER + static_cast<size_t>(i) * rb_node_entry(m_max0, vbytes);
}
inline u32& re_uid(byte_t* e) { return *reinterpret_cast<u32*>(e); }
inline u32 re_uid(const byte_t* e) { return *reinterpret_cast<const u32*>(e); }
inline u32& re_l0count(byte_t* e) { return *reinterpret_cast<u32*>(e + 4); }
inline u32 re_l0count(const byte_t* e) { return *reinterpret_cast<const u32*>(e + 4); }
inline u64& re_rptr(byte_t* e) { return *reinterpret_cast<u64*>(e + 8); }
inline u64 re_rptr(const byte_t* e) { return *reinterpret_cast<const u64*>(e + 8); }
inline byte_t* re_vec(byte_t* e) { return e + RB_ENTRY_FIXED; }
inline const byte_t* re_vec(const byte_t* e) { return e + RB_ENTRY_FIXED; }
inline u32* re_nbr(byte_t* e, size_t vbytes) { return reinterpret_cast<u32*>(e + RB_ENTRY_FIXED + vbytes); }
inline const u32* re_nbr(const byte_t* e, size_t vbytes) { return reinterpret_cast<const u32*>(e + RB_ENTRY_FIXED + vbytes); }

}  // namespace lavd
