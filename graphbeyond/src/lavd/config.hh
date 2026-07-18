#pragma once

// GraphBeyond LAVD — runtime config (static storage, mirrors the
// Node:: inline-static pattern). Initialized once on the CN after the
// neighborhood region is built; read by the fat-read primitive (Phase
// D) and the search path (Phase E). When bits == 0, LAVD is OFF and the
// baseline code path is taken unchanged.

#include <cstdlib>

#include <library/types.hh>

#include "common/constants.hh"
#include "common/quant.hh"
#include "common/slot_bitmap.hh"
#include "lavd/layout.hh"
#include "lavd/native_owner.hh"
#include "lavd/pq.hh"
#include "lavd/rabitq.hh"
#include "lavd/region_capacity.hh"

namespace lavd {

// Lightweight slot-based beam candidate (no Node object — LAVD never
// reads full nodes during fanout). Self-contained traversal: slot is
// the next-hop fat-read key, rptr is kept for the fp32 rerank (Phase F).
struct LavdCand {
  distance_t d;  // approx (quantized) distance to the query
  u32 slot;      // dense uid == neighborhood-region index
  u64 rptr;      // RemotePtr raw (memory_node | byte_offset)
};

struct Config {
  inline static u32 bits = 0;   // 0 = OFF (baseline); 4 or 8 = on
  inline static u32 dim = 0;
  inline static u32 m_max0 = 0;
  inline static u32 rerank = 0;  // top-R fp32 rerank width (Phase F)
  inline static Quantizer qz;    // fitted params for query encoding
  inline static u32 coloc_degree = 0;  // 0 = OFF (all entries co-located)
  inline static u64 neighborhood_region_capacity =
      REGION_CAPACITY_LEGACY_DEFAULT_BYTES;

  // GraphBeyond CWC — Cross-Walk read Coalescing (2nd contribution).
  // cwc = target coalescing width B. 0/1 => OFF: behaviour is byte-
  // identical to the published LAVD paper (single fat read per hop,
  // one signaled CQE each). B>1 => the scheduler coalesces up to B
  // ready coroutines' uniform fat-block reads into ONE ibv_post_send
  // linked WR chain with a single signaled completion (group barrier).
  // allsig: debug fallback — keep every WR signaled (K CQEs) to
  // isolate "linked-post" from "single-CQE"; not used in the paper.
  inline static u32 cwc = 0;
  inline static bool cwc_allsig = false;

  static u32 cwc_width() { return cwc; }
  static bool cwc_on() { return cwc > 1; }

  static u32 coloc_degree_from_env() { return lavd::coloc_degree_from_env_raw(); }
  static u32 coloc_d() {
    return (coloc_degree == 0 || coloc_degree >= m_max0) ? m_max0 : coloc_degree;
  }
  static bool coloc_on() {
    return coloc_degree != 0 && coloc_degree < m_max0;
  }

  // ---- memory-bounded LAVD: tunable co-location budget ----------------
  // Only the top-H "hot" nodes (ranked by hop-distance from the entry
  // point, the structurally-traversed set) get a co-located fat block;
  // the region is COMPACT (H blocks, not N) so MN memory == H/N of full
  // LAVD. Cold nodes fall back to per-neighbor reads + on-the-fly
  // re-quantization (search path), producing byte-identical LavdCands
  // => recall byte-identical to full LAVD at ANY budget.
  // SHINE_LAVD_BUDGET = f in (0,1]; unset or >=1.0 => OFF => full LAVD
  // (slot-indexed region, current published path, byte-identical).
  static constexpr u32 COLD = 0xFFFFFFFFu;
  inline static u32 total_n = 0;               // N (all nodes)
  inline static u32 budget_n = 0;              // H (co-located/hot nodes)
  inline static const u32* compact_idx = nullptr;  // [N] -> compact blk idx or COLD
  static bool budget_on() { return compact_idx != nullptr && budget_n < total_n; }
  static bool is_hot(u32 slot) { return !budget_on() || compact_idx[slot] != COLD; }
  static u32 cidx(u32 slot) { return compact_idx[slot]; }
  static f32 budget_frac() {
    const char* e = std::getenv("SHINE_LAVD_BUDGET");
    f32 f = e ? static_cast<f32>(std::atof(e)) : 1.0f;
    return (f <= 0.0f) ? 0.0f : (f >= 1.0f ? 1.0f : f);
  }
  static bool budget_build_on() { return budget_frac() < 1.0f; }
  static void set_budget_map(u32 N, u32 H, const u32* map) {
    total_n = N; budget_n = H; compact_idx = map;
    // Mirror to layout.hh so slot_qvec_ptr's tiered path can index
    // packed_qvec via the compact_idx without dragging Config into
    // layout.hh.
    lavd::g_compact_idx_ptr = map;
    lavd::g_layout_total_n = N;
    lavd::g_layout_budget_n = H;
  }

  static void init_cwc_from_env() {
    if (const char* e = std::getenv("SHINE_LAVD_CWC")) {
      cwc = static_cast<u32>(std::atoll(e));
    }
    if (const char* a = std::getenv("SHINE_LAVD_CWC_ALLSIG")) {
      cwc_allsig = (std::atoll(a) != 0);
    }
  }

  // ---- PQ fanout (dimension-independent co-located code) -------------
  // SHINE_LAVD_PQ_M = m > 0 replaces the scalar dim-proportional code
  // with an m-byte PQ code (m subquantizers, ADC distance). The co-
  // located block shrinks dim/m x; QPS-preserving (one fat read) and
  // recall recovered by the existing fp32 rerank. 0 = scalar SQ
  // (byte-identical to the published path). Single-CN: the initiator
  // both builds and searches, so the codebook stays CN-local (no MN
  // round-trip); multi-CN codebook sharing is future work.
  inline static u32 pq_m = 0;
  inline static PQ pq;
  static u32 pq_m_from_env() {
    const char* e = std::getenv("SHINE_LAVD_PQ_M");
    return e ? static_cast<u32>(std::atoll(e)) : 0;
  }
  // SHINE_LAVD_OPQ=1: random-rotation PQ (RR-PQ) — balances variance
  // across subspaces, recovering recall at small m on skewed-variance
  // (high-dim) data. Rotation is L2-preserving so ADC/rerank semantics
  // are unchanged.
  static bool opq_from_env() {
    const char* e = std::getenv("SHINE_LAVD_OPQ");
    return e && std::atoi(e) != 0;
  }
  static bool pq_on() { return pq_m > 0; }
  static void set_pq(u32 m, const PQ& p) { pq_m = m; pq = p; g_pq_m = m; }

  // ---- RaBitQ fanout (random-rotation bitwise co-located code) -----
  // SHINE_LAVD_RABITQ_B = B > 0 replaces scalar SQ/PQ qvec payloads
  // with [norm,dot,code] in the existing ent_qvec region. 0 = scalar SQ
  // when PQ is also off, byte-identical to the published path.
  inline static u32 rabitq_b = 0;
  inline static RaBitQ rabitq;
  static u32 rabitq_b_from_env() { return RaBitQ::b_from_env(); }
  static bool rabitq_on() { return rabitq_b != 0; }
  static void set_rabitq(u32 b, const RaBitQ& r) {
    rabitq_b = b;
    rabitq = r;
    g_rabitq_b = b;
    g_rabitq_code = r.code_bytes();
  }

  // ---- reorder-not-replicate: vector-clustered block co-location -------
  // Nodes grouped into k-means blocks (eps/block), each stored ONCE; the
  // Starling-style block search reads the blocks the search dwells in
  // (over-read free on idle bandwidth) -> ~1x memory, fewer ops at iso-
  // recall. SHINE_LAVD_REORDER_BLOCKOF=<file> (Python k-means labels)
  // enables it; SHINE_LAVD_RB_SQ8=1 stores sq8 block vectors (4x smaller,
  // fp32 rerank safety net) instead of full fp32.
  inline static u32 reorder_eps = 0;            // mean block size; 0 = off
  inline static u32 n_blocks = 0;
  inline static bool rb_sq8 = false;
  inline static const u32* block_of = nullptr;  // [N] uid -> block id
  inline static const u64* block_off = nullptr; // [n_blocks+1] absolute byte offsets
  inline static size_t rb_max_block_bytes = 0;  // largest block (freelist slab size)
  static bool reorder_on() { return block_of != nullptr; }
  static size_t rb_vbytes() { return rb_sq8 ? dim : static_cast<size_t>(dim) * 4; }
  static const char* reorder_blockof_path() { return std::getenv("SHINE_LAVD_REORDER_BLOCKOF"); }
  static bool reorder_build_on() { return reorder_blockof_path() != nullptr; }
  static bool rb_sq8_from_env() { const char* e = std::getenv("SHINE_LAVD_RB_SQ8"); return e && std::atoi(e) != 0; }
  static void set_reorder(u32 eps, u32 nb, bool sq8, const u32* bo, const u64* boff, size_t maxbb) {
    reorder_eps = eps; n_blocks = nb; rb_sq8 = sq8; block_of = bo; block_off = boff; rb_max_block_bytes = maxbb;
  }

  // ---- T6: slot-indexed visited bitmap ---------------------------------
  // GB_BITMAP_DEDUP=1 replaces the per-coroutine RemotePtr-keyed hashset
  // (FastPtrSet / std::unordered_set) with a dense slot-indexed bitmap at
  // the LAVD level-0 beam call sites. Because slot <-> RemotePtr is a
  // build-time bijection in LAVD, the visited sequence (and hence beam
  // evolution / search output) is byte-identical. Unset / 0 -> hashset
  // path, byte-identical baseline.
  static bool bitmap_dedup_on() { return gb::bitmap_dedup_on(); }

  // ---- MB-LAVD/native packed L0 sidecar -------------------------------
  // Default OFF. When a native writer publishes packed per-MN sidecars, the
  // L0 read path resolves a global slot through this formulaic layout instead
  // of using RemotePtr::memory_node() + global-uid sparse offsets.
  inline static bool native_packed_l0 = false;
  inline static native::PackedL0Layout native_l0;
  static bool native_packed_on() { return native_packed_l0; }
  static void set_native_packed_l0(const native::PackedL0Layout& layout) {
    native_l0 = layout;
    native_packed_l0 = true;
  }
  static void clear_native_packed_l0() {
    native_packed_l0 = false;
    native_l0 = native::PackedL0Layout{};
  }

  static bool on() { return bits != 0; }
  static void set_region_capacity_bytes(u64 bytes) {
    neighborhood_region_capacity = bytes;
  }
  static u64 region_capacity_bytes() { return neighborhood_region_capacity; }
  static size_t stride() {
    if (slot_only_on()) {
      return BLOCK_HEADER + static_cast<size_t>(m_max0) * SLOT_ONLY_ENTRY;
    }
    const u32 dc = coloc_d();
    const size_t full = ENTRY_FIXED + qbytes(dim, bits);
    return BLOCK_HEADER + static_cast<size_t>(dc) * full +
           static_cast<size_t>(m_max0 - dc) * ENTRY_FIXED;
  }

  static void init(u32 b, u32 d, u32 mm0, u32 r, const Quantizer& q) {
    bits = b;
    dim = d;
    m_max0 = mm0;
    rerank = r;
    qz = q;
    coloc_degree = coloc_degree_from_env();
    lavd::set_layout_coloc_degree(coloc_degree);
    clear_native_packed_l0();
  }
};

inline bool entry_has_qvec(u32 i) { return i < Config::coloc_d(); }

}  // namespace lavd
