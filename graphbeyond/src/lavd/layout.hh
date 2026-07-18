#pragma once

// GraphBeyond LAVD — shared neighborhood-block layout.
// Used by BOTH the CN build pass (Phase B2, writer) and the fat-read
// path (Phase D/E, reader). Explicit byte offsets (no packed struct) so
// writer/reader agree regardless of compiler struct padding.
//
//   block[slot] (stride = block_stride):
//     [0  ) u32 count            # actual L0 neighbor count (<= m_max0)
//     [4  ) u32 _pad
//     [8  ) entry[0..dc):
//             [0 ) u32 nbr_slot  # neighbor's dense uid (next-hop fat read)
//             [4 ) u32 _pad
//             [8 ) u64 nbr_rptr  # neighbor RemotePtr raw (fp32 rerank)
//             [16) u8  qvec[Q]   # neighbor quantized vector (fanout dist)
//     [.. ) entry[dc..m_max0):
//             [0 ) u32 nbr_slot
//             [4 ) u32 _pad
//             [8 ) u64 nbr_rptr
//
// Q = qbytes(dim,bits). dc == m_max0 when adaptive co-location is off,
// reducing to the published full-entry layout byte-for-byte. slot == HNSW
// uid (dense 0..N-1).

#include <cstdlib>
#include <unordered_map>
#include <vector>
#include <library/types.hh>

namespace lavd {

constexpr size_t BLOCK_HEADER = 8;   // u32 count + u32 pad
constexpr size_t ENTRY_FIXED = 16;   // u32 slot + u32 pad + u64 rptr

// Slot-only entry size: just a slot u32. CN-side lookup tables supply
// rptr (for rerank) and qvec (for distance computation). Forward decl
// of slot_only_on() so entry_size()/blk_entry()/varblock_stride() can
// branch on it; the env-reader + CN tables live further down.
constexpr size_t SLOT_ONLY_ENTRY = sizeof(u32);
inline bool slot_only_on() {
  static const bool on = [] {
    const char* e = std::getenv("SHINE_LAVD_SLOT_ONLY");
    return e && std::atoi(e) != 0;
  }();
  return on;
}

// Reserved header at the START of the neighborhood region: the
// initiator's build pass writes the fitted quantizer params here so
// EVERY CN (not just the initiator) can RDMA-read them at startup and
// init lavd::Config identically — no CN-to-CN messaging (all CNs hold
// the 2nd-region token from Phase C). Block[slot] starts AFTER this.
// params_bytes = 8 + 2*dim*4. 16 KB covers dim<=2046 (SIFT 128,
// GIST 960, OpenAI-3-large 3072 would need 24 KB — bump if needed).
constexpr size_t PARAMS_RESERVE = 16384;

// byte offset of block[slot] within the neighborhood region (full LAVD:
// slot-indexed, every node has a block right after the params header).
inline size_t region_offset(u32 slot, size_t stride) {
  return PARAMS_RESERVE + static_cast<size_t>(slot) * stride;
}

// ---- memory-bounded LAVD compact layout ----------------------------
// When a co-location budget is active the region is COMPACT: only H
// hot blocks are stored, preceded by a slot->compact-index map so any
// CN can resolve a hot slot's block. Layout:
//   [0, PARAMS_RESERVE)            quantizer params (+ total_n/budget_n
//                                  in the last 8 bytes of the reserve)
//   [PARAMS_RESERVE, +N*4)         compact_idx[slot]  (u32; COLD=0xFFFFFFFF)
//   [map_end, +H*stride)           H compact blocks, block j at j*stride
constexpr size_t HDR_COUNTS_OFF = PARAMS_RESERVE - 8;  // [total_n u32][budget_n u32]
// RaBitQ shared params (multi-CN): the initiator writes the centroid here so
// every querying CN reconstructs the identical encoder (rotation from seed,
// levels from dim/B). Placed 64-aligned right after the scalar params, before
// the counts tail. Layout at this offset: [u32 B][u32 dim][f32 centroid[dim]].
inline size_t rabitq_hdr_off(size_t scalar_params_bytes) {
  return (scalar_params_bytes + 63) & ~static_cast<size_t>(63);
}
inline size_t map_region_offset() { return PARAMS_RESERVE; }

// Structural Hub Cache (SHC): CN-local cache of top-X L0 fat blocks selected
// offline from an access histogram. Loaded after read_params_init via
// load_hub_cache_init when GB_HUB_CACHE_LIST=<path> is set.
struct HubCacheS {
  static constexpr u32 INVALID = 0xFFFFFFFFu;
  std::vector<u32> hub_map;      // [N] -> local_idx or INVALID
  std::vector<byte_t> blocks;    // n_hubs * stride
  size_t stride = 0;
  u32 n_hubs = 0;
  mutable std::atomic<u64> hits{0};  // hit counter (for diagnostics)
  bool active() const { return n_hubs > 0; }
  bool is_hub(u32 slot) const {
    return active() && slot < hub_map.size() && hub_map[slot] != INVALID;
  }
  const byte_t* block_data(u32 slot) const {
    return blocks.data() + static_cast<size_t>(hub_map[slot]) * stride;
  }
};
inline HubCacheS g_hub_cache;
inline size_t compact_blocks_base(u32 total_n) {
  return PARAMS_RESERVE + static_cast<size_t>(total_n) * sizeof(u32);
}
inline size_t compact_block_offset(u32 compact_idx, u32 total_n, size_t stride) {
  return compact_blocks_base(total_n) + static_cast<size_t>(compact_idx) * stride;
}

// RaBitQ/PQ overrides: route co-located qvec length through qbytes() so
// EVERY layout consumer (entry_size, block_stride, blk_entry, ent_qvec)
// is sized with zero call-site churn. 0 = scalar SQ (byte-identical to
// the published path). RaBitQ wins when enabled because its payload is
// [f32 norm][f32 dot][packed code].
inline u32 g_rabitq_b = 0;
inline size_t g_rabitq_code = 0;
// PATCH 1 (cold-code-table fix, Track A): per-uid RaBitQ codetab promoted
// from build.hh to layout.hh so the query-side enc lambda in hnsw.hh can
// lookup codes without dragging build.hh into the search header. Filled
// once during build_neighborhood{,_multi}; empty on non-initiator CNs ⇒
// enc lambda's (nb_uid < normtab.size()) guard falls back to rb.encode().
inline std::vector<byte_t> g_rabitq_codetab;
inline std::vector<f32> g_rabitq_normtab;
inline std::vector<f32> g_rabitq_dottab;
inline u32 g_pq_m = 0;
inline size_t qbytes(u32 dim, u32 bits) {
  if (g_rabitq_b) return 8 + g_rabitq_code;
  if (g_pq_m) return g_pq_m;
  return bits == 8 ? dim : (dim + 1) / 2;
}
inline size_t entry_size(u32 dim, u32 bits) {
  if (slot_only_on()) return SLOT_ONLY_ENTRY;
  return ENTRY_FIXED + qbytes(dim, bits);
}

// Adaptive co-location degree. 0/unset means OFF: every entry keeps qvec.
inline u32 g_coloc_degree = 0;
inline bool g_coloc_degree_inited = false;
inline u32 coloc_degree_from_env_raw() {
  const char* e = std::getenv("SHINE_LAVD_COLOC_DEGREE");
  const int v = e ? std::atoi(e) : 0;
  return v > 0 ? static_cast<u32>(v) : 0;
}
inline void set_layout_coloc_degree(u32 d) {
  g_coloc_degree = d;
  g_coloc_degree_inited = true;
}
inline u32 layout_coloc_degree() {
  if (!g_coloc_degree_inited) set_layout_coloc_degree(coloc_degree_from_env_raw());
  return g_coloc_degree;
}
inline u32 layout_coloc_d(u32 m_max0) {
  const u32 d = layout_coloc_degree();
  return (d == 0 || d >= m_max0) ? m_max0 : d;
}
inline bool layout_coloc_on(u32 m_max0) {
  const u32 d = layout_coloc_degree();
  return d != 0 && d < m_max0;
}
inline bool entry_has_qvec(u32 i, u32 m_max0) { return i < layout_coloc_d(m_max0); }

// ---- VARIABLE-LENGTH LAVD block layout (degree-aware compact) -------
//
// Motivation: fixed-stride blocks reserve m_max0 entries per node, but
// mean L0 degree is typically ~30 while m_max0=64 (54% padding zeros on
// gist200k+sift1m). Variable-block stores only `count` entries per node
// and indexes blocks via a prefix-sum byte-offset table.
//
// Layout (env gate SHINE_LAVD_VARBLOCK=1):
//   [0, PARAMS_RESERVE)            quantizer params (unchanged)
//   [PARAMS_RESERVE, +(N+1)*8)     offset_table[slot] u64 prefix sum:
//                                  offset_table[slot] = byte offset of
//                                  block[slot] (absolute from region base);
//                                  offset_table[N] = total bytes used
//   [varblock_blocks_base(N), ...) variable-length blocks, each:
//                                    [0)  u32 count    (actual L0 degree)
//                                    [4)  u32 _pad
//                                    [8)  count entries (entry_size(dim,bits))
//
// Block size = BLOCK_HEADER + count * entry_size  (NOT padded to m_max0).
// Memory savings vs fixed-stride ≈ (m_max0 - mean_degree) / m_max0
// ≈ 54% on gist200k/sift1m M=32 m_max0=64 mean_deg=29.4.
//
// Forward-compat: when SHINE_LAVD_VARBLOCK is unset (default), all
// var-block accessors are unused and the fixed-stride path is
// byte-identical to the published layout (regression guard).
//
// Multi-MN: each MN's region carries its own offset_table covering only
// the local shard's slot range (per build_neighborhood_multi's home
// assignment). CN-side cache: one std::vector<u64> per MN, total
// num_mns * N * 8 bytes (≈ 8 MB for 1M nodes at S=1).
//
// Off ⇒ varblock_on() returns false; build + search take fixed-stride
// path. On ⇒ build writes offset_table + packed variable blocks; search
// reads cached offset_table (init phase) and fat-reads (start, size).

inline bool g_varblock_on = false;
inline bool g_varblock_inited = false;
inline bool varblock_from_env_raw() {
  const char* e = std::getenv("SHINE_LAVD_VARBLOCK");
  return e != nullptr && std::atoi(e) != 0;
}
inline void set_varblock_on(bool v) {
  g_varblock_on = v;
  g_varblock_inited = true;
}
inline bool varblock_on() {
  if (!g_varblock_inited) set_varblock_on(varblock_from_env_raw());
  return g_varblock_on;
}

// CN-local offset_table cache: built once during read_params_init phase
// by RDMA-reading the on-MN offset_table[0..N]. Size N+1 (sentinel for
// slot==N-1 to compute the last block's size).
//
// SINGLE-MN cache (global-uid-indexed). Used when varblock_on() &&
// !native_packed_on(): one table over all N nodes, addressed by uid.
inline std::vector<u64> g_varblock_offsets;

// MULTI-MN cache (per-MN local-slot-indexed). Used when varblock_on() &&
// native_packed_on(): one table per MN, addressed by local_slot inside
// that MN's shard. Combined with OwnerResolver (Config::native_l0) which
// gives slot -> (owner_mn, local_slot).
//
// Layout: g_varblock_offsets_per_mn[mn][local_slot] = byte offset of
// block within MN[mn]'s neighborhood region.
// g_varblock_offsets_per_mn[mn][local_count(mn)] = total bytes used.
inline std::vector<std::vector<u64>> g_varblock_offsets_per_mn;

// Combined-mode accessors. Caller MUST verify
// (varblock_on() && native_packed_on()) before calling.
inline u64 varblock_offset_mn(u32 mn, u32 local_slot) {
  return g_varblock_offsets_per_mn[mn][local_slot];
}
inline u32 varblock_size_mn(u32 mn, u32 local_slot) {
  return static_cast<u32>(g_varblock_offsets_per_mn[mn][local_slot + 1] -
                          g_varblock_offsets_per_mn[mn][local_slot]);
}

inline size_t varblock_table_bytes(u32 total_n) {
  return static_cast<size_t>(total_n + 1) * sizeof(u64);
}
// Combined+budget mode: the budget compact_idx map sits at PARAMS_RESERVE
// (N*4 bytes; on MN[0] only). Var-block table must shift past it so the
// two writes do not collide. Set by build (initiator) and by read_params_init
// (every CN) when (varblock_on && budget_on). 0 in all other modes.
inline size_t g_varblock_map_shift = 0;

// mbLAVD cold-path pre-filter: reverse map (mn, byte_offset) -> uid.
// Populated by build_neighborhood_multi after parsing INDEX. Lets the
// cold fallback dedup against the slot bitmap *before* issuing the
// per-neighbor RDMA read (cold encode CPU is the wall at small f).
// Empty => fall back to post-read dedup. Lives in layout.hh so the
// search header (hnsw.hh) can read it without dragging in build.hh.
inline std::vector<std::unordered_map<u64, u32>> g_rev_index_per_mn;
inline bool rev_index_ready() {
  return !g_rev_index_per_mn.empty();
}
inline u32 rev_lookup(u32 mn, u64 byte_offset) {
  if (mn >= g_rev_index_per_mn.size()) return 0xFFFFFFFFu;
  const auto& m = g_rev_index_per_mn[mn];
  const auto it = m.find(byte_offset);
  return it == m.end() ? 0xFFFFFFFFu : it->second;
}

// =====================================================================
// Slot-only entry layout (env: SHINE_LAVD_SLOT_ONLY=1)
//
// Insight: hot blocks duplicate every neighbor's [norm, dot, code]
// across the ~M hot centers that list it. The per-uid CN-side tables
// (g_rabitq_*tab) already hold each qvec exactly once. So the LAVD
// region only needs to store the neighbor SLOT list per hot block;
// the qvec and rerank RemotePtr come from CN-resident lookup tables.
//
// MN-side layout (slot_only on):
//   block = [count u32][_pad u32][slot u32]*degree
//   per block = 8 + 4*degree (vs ~4000B at RaBitQ-2)
// CN-side tables (filled by initiator build):
//   g_uid_qvec_packed[uid * packed_qvec_size .. +packed_qvec_size)
//     = packed [norm 4][dot 4][code N_b] — same byte layout as in-block
//       ent_qvec, so the RaBitQ approx encoder is unchanged.
//   g_uid_to_rptr[uid] = RemotePtr raw u64 (for rerank).
//
// Gated by env so the published layout stays the byte-identical red
// line when off. v1: initiator-only (multi-CN clients require a
// startup-time RDMA pull of the qvec table from MN, follow-up commit).
// Tiered slot-only cache (env SHINE_LAVD_TIERED_CACHE=1, default off):
// When budget<1 and tiered is on, g_uid_qvec_packed shrinks from
// N*pqs to H*pqs (only hot uids) and is indexed via compact_idx[slot]
// instead of slot directly. Cold neighbors of hot centers fall back to
// the existing has_qvec=false path (read_node + on-the-fly encode).
//
// Why: at N=100M with f=0.25, full-table packed_qvec is 25M*248 ~ 6 GB,
// but the rest of the CN tables (rptr 800 MB + budget_map 400 MB +
// rev_index 3.2 GB) push the total past the 10 GB CN budget. Tiered
// caches only the hot fraction (f*N*248) plus the lightweight aux
// tables. At f=0.05 a 100M index packs into ~2.4 GB CN-side, ~13x
// below the 10 GB budget.
inline bool tiered_cache_on() {
  static const bool on = [] {
    const char* e = std::getenv("SHINE_LAVD_TIERED_CACHE");
    return e && std::atoi(e) != 0;
  }();
  return on;
}

inline std::vector<u8> g_uid_qvec_packed;  // [N*pqs] or [H*pqs] when tiered
inline std::vector<u64> g_uid_to_rptr;     // [N] RemotePtr raw

inline size_t slot_packed_qvec_size() {
  // Mirror the in-block ent_qvec layout: [norm f32][dot f32][code N_b]
  // for RaBitQ; (dim or dim/2) for scalar SQ.
  if (g_rabitq_b) return 8 + g_rabitq_code;
  if (g_pq_m) return g_pq_m;
  // scalar fallback: bits 8 => dim, bits 4 => (dim+1)/2 -- caller
  // supplies dim at config time, so derive from Config when needed.
  return 0;
}

// Local copy of (N, H, compact_idx) so slot_qvec_ptr can reach the
// compact index without dragging Config into layout.hh. Config keeps
// the authoritative pointer; build path mirrors it here at the same
// time it calls Config::set_budget_map.
inline const u32* g_compact_idx_ptr = nullptr;
inline u32 g_layout_total_n = 0;
inline u32 g_layout_budget_n = 0;
inline bool budget_active() { return g_compact_idx_ptr != nullptr && g_layout_budget_n < g_layout_total_n; }

inline const u8* slot_qvec_ptr(u32 slot) {
  const size_t pqs = slot_packed_qvec_size();
  if (tiered_cache_on() && budget_active()) {
    // Caller path guarantees the slot is hot (Neighborhood::has_qvec
    // already returned true). Use the compact index into the hot-only
    // table. Hot guarantee: compact_idx[slot] in [0, H).
    const u32 ci = g_compact_idx_ptr[slot];
    return g_uid_qvec_packed.data() + static_cast<size_t>(ci) * pqs;
  }
  return g_uid_qvec_packed.data() + static_cast<size_t>(slot) * pqs;
}

inline u64 slot_to_rptr_raw(u32 slot) {
  return g_uid_to_rptr[slot];
}
inline size_t varblock_table_offset() { return PARAMS_RESERVE + g_varblock_map_shift; }
inline size_t varblock_blocks_base(u32 total_n) {
  return varblock_table_offset() + varblock_table_bytes(total_n);
}

// Offset of block[slot] in the on-MN region. Requires the CN-local
// offset_table cache to be populated (initiator path: build pass; non-
// initiator path: read_params_init RDMA-read).
inline size_t varblock_offset(u32 slot) {
  return g_varblock_offsets[slot];
}
// Byte length of block[slot], from prefix-sum diff. slot in [0, N).
inline u32 varblock_size(u32 slot) {
  return static_cast<u32>(g_varblock_offsets[slot + 1] - g_varblock_offsets[slot]);
}
// Read the actual count from the block header (after fat-read returns).
inline u32 varblock_count(const byte_t* block_data) {
  return *reinterpret_cast<const u32*>(block_data);
}
// Variable-length block stride for a given count (utility, used at build).
// Slot-only mode: BLOCK_HEADER + count * 4 (slot u32 each).
// Otherwise honors COLOC_DEGREE: first dc entries full qvec, rest fixed.
inline size_t varblock_stride(u32 count, u32 dim, u32 bits) {
  if (slot_only_on()) {
    return BLOCK_HEADER + static_cast<size_t>(count) * SLOT_ONLY_ENTRY;
  }
  const u32 raw_d = layout_coloc_degree();
  const u32 dc = (raw_d == 0 || raw_d >= count) ? count : raw_d;
  const u32 scnt = count - dc;
  return BLOCK_HEADER + static_cast<size_t>(dc) * entry_size(dim, bits) +
         static_cast<size_t>(scnt) * ENTRY_FIXED;
}

inline size_t block_stride(u32 m_max0, u32 dim, u32 bits) {
  const u32 dc = layout_coloc_d(m_max0);
  const size_t full = entry_size(dim, bits);
  return BLOCK_HEADER + static_cast<size_t>(dc) * full +
         static_cast<size_t>(m_max0 - dc) * ENTRY_FIXED;
}

inline u32& blk_count(byte_t* b) { return *reinterpret_cast<u32*>(b); }
inline u32 blk_count(const byte_t* b) { return *reinterpret_cast<const u32*>(b); }

inline byte_t* blk_entry(byte_t* b, u32 i, u32 dim, u32 bits, u32 m_max0) {
  if (slot_only_on()) {
    return b + BLOCK_HEADER + static_cast<size_t>(i) * SLOT_ONLY_ENTRY;
  }
  const u32 dc = layout_coloc_d(m_max0);
  const size_t full = entry_size(dim, bits);
  const size_t off = (i < dc)
    ? BLOCK_HEADER + static_cast<size_t>(i) * full
    : BLOCK_HEADER + static_cast<size_t>(dc) * full +
        static_cast<size_t>(i - dc) * ENTRY_FIXED;
  return b + off;
}
inline const byte_t* blk_entry(const byte_t* b, u32 i, u32 dim, u32 bits, u32 m_max0) {
  if (slot_only_on()) {
    return b + BLOCK_HEADER + static_cast<size_t>(i) * SLOT_ONLY_ENTRY;
  }
  const u32 dc = layout_coloc_d(m_max0);
  const size_t full = entry_size(dim, bits);
  const size_t off = (i < dc)
    ? BLOCK_HEADER + static_cast<size_t>(i) * full
    : BLOCK_HEADER + static_cast<size_t>(dc) * full +
        static_cast<size_t>(i - dc) * ENTRY_FIXED;
  return b + off;
}

inline u32& ent_slot(byte_t* e) { return *reinterpret_cast<u32*>(e); }
inline u32 ent_slot(const byte_t* e) { return *reinterpret_cast<const u32*>(e); }
inline u64& ent_rptr(byte_t* e) { return *reinterpret_cast<u64*>(e + 8); }
inline u64 ent_rptr(const byte_t* e) { return *reinterpret_cast<const u64*>(e + 8); }
inline u8* ent_qvec(byte_t* e) { return reinterpret_cast<u8*>(e + ENTRY_FIXED); }
inline const u8* ent_qvec(const byte_t* e) { return reinterpret_cast<const u8*>(e + ENTRY_FIXED); }

struct DecodedBlockEntry {
  u32 slot;
  u64 rptr;
  const u8* qvec;
  bool has_qvec;
};

class BlockEntryDecoder {
public:
  BlockEntryDecoder(
      const byte_t* block, u32 dim, u32 bits, u32 m_max0)
      : block_(block),
        slot_only_(slot_only_on()),
        coloc_d_(slot_only_ ? 0 : layout_coloc_d(m_max0)),
        full_entry_size_(
            slot_only_ ? SLOT_ONLY_ENTRY : ENTRY_FIXED + qbytes(dim, bits)),
        tiered_budget_(slot_only_ && tiered_cache_on() && budget_active()) {}

  __attribute__((always_inline)) inline const byte_t* encoded(u32 i) const {
    if (slot_only_) {
      return block_ + BLOCK_HEADER + static_cast<size_t>(i) * SLOT_ONLY_ENTRY;
    }
    const size_t offset = i < coloc_d_
        ? BLOCK_HEADER + static_cast<size_t>(i) * full_entry_size_
        : BLOCK_HEADER + static_cast<size_t>(coloc_d_) * full_entry_size_ +
              static_cast<size_t>(i - coloc_d_) * ENTRY_FIXED;
    return block_ + offset;
  }

  __attribute__((always_inline)) inline DecodedBlockEntry decode(u32 i) const {
    const byte_t* entry = encoded(i);
    const u32 slot = ent_slot(entry);
    if (slot_only_) {
      const bool has_qvec =
          !tiered_budget_ || g_compact_idx_ptr[slot] != 0xFFFFFFFFu;
      return DecodedBlockEntry{
          slot,
          slot_to_rptr_raw(slot),
          has_qvec ? slot_qvec_ptr(slot) : nullptr,
          has_qvec};
    }
    const bool has_qvec = i < coloc_d_;
    return DecodedBlockEntry{
        slot,
        ent_rptr(entry),
        has_qvec ? ent_qvec(entry) : nullptr,
        has_qvec};
  }

private:
  const byte_t* block_;
  bool slot_only_;
  u32 coloc_d_;
  size_t full_entry_size_;
  bool tiered_budget_;
};

inline DecodedBlockEntry decode_block_entry(
    const byte_t* block, u32 i, u32 dim, u32 bits, u32 m_max0) {
  return BlockEntryDecoder(block, dim, bits, m_max0).decode(i);
}

}  // namespace lavd
