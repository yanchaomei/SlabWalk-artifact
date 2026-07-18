#pragma once

// GraphBeyond LAVD — Phase B2: CN-side neighborhood build pass.
//
// Runs ONCE on the initiator CN after the graph is ready (constructed
// or --load-index'd) and before queries. MN stays 100% passive: the CN
// bulk-RDMA-READs the whole index region, parses it locally, encodes
// quantized vectors, and RDMA-WRITES the neighborhood blocks into the
// MN's 2nd region. v1: single MN.
//
// Uses the DATA-PATH SharedContext QP + a scratch registered in the
// SAME PD/Context as that QP (mirrors rdma_reads.hh exactly). The
// connection-manager control QPs are NOT used here — they live in a
// different PD/CQ and cannot do one-sided RDMA against the index token.

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <random>
#include <string>
#include <sys/resource.h>
#include <unordered_map>
#include <utility>
#include <vector>

#include <library/memory_region.hh>

#include "common/quant.hh"
#include "common/timing.hh"
#include "common/evidence_hash.hh"
#include "lavd/config.hh"
#include "lavd/build_snapshot.hh"
#include "lavd/layout.hh"
#include "lavd/maintenance_guard.hh"
#include "lavd/materialization_policy.hh"
#include "lavd/native_descriptor.hh"
#include "lavd/native_export.hh"
#include "lavd/or_probe.hh"
#include "lavd/parallel_build.hh"
#include "lavd/parallel_indegree.hh"
#include "lavd/region_capacity.hh"
#include "lavd/staged_io.hh"
#include "node/node.hh"
#include "remote_pointer.hh"
#include "shared_context.hh"

namespace lavd {

static_assert(lavd::native::PARAMS_RESERVE_BYTES == lavd::PARAMS_RESERVE,
              "native descriptor constants must match LAVD params reserve");
static_assert(lavd::native::HDR_COUNTS_OFFSET == lavd::HDR_COUNTS_OFF,
              "native descriptor constants must match LAVD counts offset");

// Persistent CN-resident co-location map (memory-bounded LAVD). Filled
// by read_params_init on every CN when a budget is active; pointed to
// by lavd::Config::compact_idx. Lifetime = process (search reads it).
inline std::vector<u32> g_budget_map;

// (rev_index_per_mn lives in layout.hh; populated below.)

// --- T4 (Phase H) shared per-slot block assembler ------------------
// Fills `blk` (>= stride bytes) with block[slot] from a parsed,
// CN-resident copy of the index. EXACTLY the logic that was inlined in
// build_neighborhood step 5, factored out so the one-time full build
// and the incremental touched-delta maintenance share ONE writer (no
// divergence => byte-identical by construction).

// RaBitQ build memoization: the rotation is expensive (O(dim^2)/vector) and the
// denormalized layout would otherwise re-encode each vector ~M times (once per
// node that lists it). We encode each uid ONCE into these tables; assemble_block
// then copies. Empty => fall back to per-call encode (e.g. the T4 maintain path).
// NOTE (cold-code-table fix, Track A): canonical definitions promoted to
// lavd/layout.hh so query-side hnsw.hh enc lambda can see them. Kept here
// as comments for historical anchor.

inline void assemble_block(u32 slot, const byte_t* scratch, const vec<u64>& off_by_uid,
                           const Quantizer& qz, u32 dim, u32 bits, u32 m_max0,
                           size_t l0_off, size_t comp_off, size_t stride, byte_t* blk) {
  std::memset(blk, 0, stride);
  std::vector<f32> rb_su;
  std::vector<f32> rb_sr;
  if (lavd::g_rabitq_b) {
    rb_su.resize(dim);
    rb_sr.resize(dim);
  }
  const u64 o = off_by_uid[slot];
  const byte_t* l0 = scratch + o + l0_off;
  u32 cnt = *reinterpret_cast<const u32*>(l0);
  if (cnt > m_max0) cnt = m_max0;
  blk_count(blk) = cnt;
  const u64* nptr = reinterpret_cast<const u64*>(l0 + sizeof(u32));
  const u32 dc = lavd::layout_coloc_d(m_max0);
  for (u32 i = 0; i < cnt; ++i) {
    const RemotePtr rp{nptr[i]};
    const u64 nb_off = rp.byte_offset();
    const u32 nb_uid = *reinterpret_cast<const u32*>(scratch + nb_off + Node::HEADER_SIZE);
    byte_t* e = blk_entry(blk, i, dim, bits, m_max0);
    ent_slot(e) = nb_uid;
    ent_rptr(e) = nptr[i];
    if (i >= dc) continue;  // short entry: slot+rptr only; qvec read on demand
    const element_t* nbvec = reinterpret_cast<const element_t*>(scratch + nb_off + comp_off);
    // RaBitQ writes [f32 norm][f32 dot][packed code] into ent_qvec.
    // PQ writes an m-byte code; scalar writes the dim-proportional SQ
    // code. encode_norot: the scratch vector is already in PQ space
    // (raw for plain PQ; pre-rotated by step 4a' for OPQ), so no
    // per-neighbor rotation here.
    if (lavd::g_rabitq_b) {
      u8* qv = ent_qvec(e);
      if (nb_uid < lavd::g_rabitq_normtab.size()) {  // memoized: copy precomputed
        *reinterpret_cast<f32*>(qv) = lavd::g_rabitq_normtab[nb_uid];
        *reinterpret_cast<f32*>(qv + 4) = lavd::g_rabitq_dottab[nb_uid];
        std::memcpy(qv + 8, &lavd::g_rabitq_codetab[static_cast<size_t>(nb_uid) * lavd::g_rabitq_code],
                    lavd::g_rabitq_code);
      } else {                                       // fallback: encode on the fly
        lavd::Config::rabitq.encode(nbvec, qv + 8, reinterpret_cast<f32*>(qv),
                                    reinterpret_cast<f32*>(qv + 4), rb_su.data(), rb_sr.data());
      }
    } else if (lavd::g_pq_m) lavd::Config::pq.encode_norot(nbvec, ent_qvec(e));
    else qz.encode(nbvec, ent_qvec(e));
  }
}

// --- Multi-MN variant: per-uid lookup via (mn, byte_offset) tuple, cross-MN
//     neighbor data read from per_mn_scratch[nb_rp.memory_node()]. Identical
//     output bytes to assemble_block when num_mns=1 (same nb_uid/rptr/qvec). ---
struct UidInfoM { u32 mn; u64 offset; };
inline void assemble_block_multi(const UidInfoM& info,
                                 const std::vector<byte_t*>& per_mn_scratch,
                                 const Quantizer& qz, u32 dim, u32 bits, u32 m_max0,
                                 size_t l0_off, size_t comp_off,
                                 size_t block_bytes, byte_t* blk) {
  std::memset(blk, 0, block_bytes);
  std::vector<f32> rb_su, rb_sr;
  if (lavd::g_rabitq_b) { rb_su.resize(dim); rb_sr.resize(dim); }
  const byte_t* my_scratch = per_mn_scratch[info.mn];
  const byte_t* l0 = my_scratch + info.offset + l0_off;
  u32 cnt = *reinterpret_cast<const u32*>(l0);
  if (cnt > m_max0) cnt = m_max0;
  blk_count(blk) = cnt;
  const u64* nptr = reinterpret_cast<const u64*>(l0 + sizeof(u32));
  // Slot-only fast path: entry = u32 slot only. rptr/qvec are looked up
  // CN-side via g_uid_to_rptr / g_uid_qvec_packed (populated below by
  // the build path). Mirrors blk_entry()'s SLOT_ONLY_ENTRY arithmetic.
  if (lavd::slot_only_on()) {
    for (u32 i = 0; i < cnt; ++i) {
      const RemotePtr rp{nptr[i]};
      const u32 nb_mn = rp.memory_node();
      const u64 nb_off = rp.byte_offset();
      const u32 nb_uid = *reinterpret_cast<const u32*>(per_mn_scratch[nb_mn] + nb_off + Node::HEADER_SIZE);
      byte_t* e = blk_entry(blk, i, dim, bits, m_max0);
      ent_slot(e) = nb_uid;
    }
    return;
  }
  const u32 dc = lavd::layout_coloc_d(m_max0);
  for (u32 i = 0; i < cnt; ++i) {
    const RemotePtr rp{nptr[i]};
    const u32 nb_mn = rp.memory_node();
    const u64 nb_off = rp.byte_offset();
    const byte_t* nb_scratch = per_mn_scratch[nb_mn];
    const u32 nb_uid = *reinterpret_cast<const u32*>(nb_scratch + nb_off + Node::HEADER_SIZE);
    byte_t* e = blk_entry(blk, i, dim, bits, m_max0);
    ent_slot(e) = nb_uid;
    ent_rptr(e) = nptr[i];
    if (i >= dc) continue;
    const element_t* nbvec = reinterpret_cast<const element_t*>(nb_scratch + nb_off + comp_off);
    if (lavd::g_rabitq_b) {
      u8* qv = ent_qvec(e);
      if (nb_uid < lavd::g_rabitq_normtab.size()) {
        *reinterpret_cast<f32*>(qv) = lavd::g_rabitq_normtab[nb_uid];
        *reinterpret_cast<f32*>(qv + 4) = lavd::g_rabitq_dottab[nb_uid];
        std::memcpy(qv + 8, &lavd::g_rabitq_codetab[static_cast<size_t>(nb_uid) * lavd::g_rabitq_code],
                    lavd::g_rabitq_code);
      } else {
        lavd::Config::rabitq.encode(nbvec, qv + 8, reinterpret_cast<f32*>(qv),
                                    reinterpret_cast<f32*>(qv + 4), rb_su.data(), rb_sr.data());
      }
    } else {
      qz.encode(nbvec, ent_qvec(e));
    }
  }
}

// Returns the fitted quantizer (caller keeps it; the search path must
// encode the query with the IDENTICAL lo/scale — Phase E).
template <typename Ctx>
inline Quantizer build_neighborhood(Ctx* ctx, u32 mn, u32 bits, timing::Timing& timing) {
  Context& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);
  MemoryRegionToken* nbh_tok = ctx->get_remote_neighborhood_mrt(mn);

  const u32 dim = Node::DIM;
  const u32 m_max0 = (Node::NEIGHBORLIST_SIZE_ZERO - sizeof(u32)) / sizeof(u64);
  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;  // 16
  const size_t l0_off = Node::size_until_components();          // hdr+meta+DIM*4
  // PQ fanout: set the code length (m) from env NOW so `stride` and the
  // region are PQ-sized; the codebook itself is fitted in step 4 below
  // (m is known from env, the fit needs the bulk-read sample). 0 = scalar.
  const u32 pq_m = lavd::Config::pq_m_from_env();
  lavd::g_pq_m = pq_m;
  const u32 rb = lavd::Config::rabitq_b_from_env();
  lavd::g_rabitq_b = rb;
  lavd::g_rabitq_code = rb ? ((static_cast<size_t>(dim) * rb + 7) / 8) : 0;
  const size_t stride = block_stride(m_max0, dim, bits);

  auto t_total = timing.create_enroll("lavd_build");
  auto t_fetch = timing.create_enroll("lavd_build_fetch");
  auto t_parse = timing.create_enroll("lavd_build_parse");
  auto t_rank = timing.create_enroll("lavd_build_rank");
  auto t_encode = timing.create_enroll("lavd_build_encode");
  auto t_metadata = timing.create_enroll("lavd_build_metadata");
  auto t_materialize = timing.create_enroll("lavd_build_materialize");
  t_total->start();
  t_fetch->start();

  // ---- 1. read free_ptr (total used bytes of the index region) ----
  auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
  LocalMemoryRegion hdr_mr{context, hdr, 64};
  qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(), IBV_WR_RDMA_READ,
                true, false, idx_tok, /*remote_off*/ 0, 0, 0);
  context.poll_send_cq_until_completion();
  const u64 free_ptr = *reinterpret_cast<u64*>(hdr);  // bytes used incl. 16B head
  lib_assert(free_ptr > 16 && free_ptr < (1ull << 48), "LAVD: bad free_ptr");

  // ---- 2. bulk-read the whole index region into a registered scratch ----
  const size_t idx_bytes = free_ptr;
  const size_t scratch_capacity =
      lavd::aligned_allocation_bytes(idx_bytes, 64u);
  lib_assert(scratch_capacity != 0,
             "LAVD: invalid aligned scratch capacity");
  auto* scratch =
      static_cast<byte_t*>(std::aligned_alloc(64, scratch_capacity));
  lib_assert(scratch != nullptr, "LAVD: scratch alloc failed");
  LocalMemoryRegion scratch_mr{context, scratch, scratch_capacity};
  const size_t CHUNK = 64ull * 1024 * 1024;  // 64 MB per RDMA READ
  for (size_t off = 0; off < idx_bytes; off += CHUNK) {
    const u32 n = static_cast<u32>(std::min(CHUNK, idx_bytes - off));
    qp->post_send(reinterpret_cast<u64>(scratch + off), n, scratch_mr.get_lkey(),
                  IBV_WR_RDMA_READ, true, false, idx_tok, off, 0, 0);
    context.poll_send_cq_until_completion();
  }
  t_fetch->stop();

  // ---- 3. parse: walk nodes, record uid -> byte offset ----
  t_parse->start();
  vec<u64> off_by_uid;
  off_by_uid.reserve(1u << 20);
  u64 walk = 16;  // skip free_ptr(8) + ep_ptr(8)
  u32 max_uid = 0;
  while (walk < idx_bytes) {
    const u32 uid = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE);
    const u32 level = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE + sizeof(u32));
    // MUST match rdma::allocate_node exactly: total_size rounded up to 8B
    // (rdma_atomics.hh: while (node_size % 8 != 0) node_size += 4;).
    size_t node_total =
      l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
    while (node_total % 8 != 0) node_total += 4;
    if (uid >= off_by_uid.size()) off_by_uid.resize(uid + 1, 0);
    off_by_uid[uid] = walk;
    max_uid = std::max(max_uid, uid);
    walk += node_total;
  }
  const u32 N = max_uid + 1;
  lib_assert(walk == idx_bytes,
             "LAVD: node walk did not consume the index region exactly "
             "(node layout/stride assumption wrong)");
  t_parse->stop();

  // Phase-0 reorder feasibility gate: measure the Overlap Ratio (OR) of a
  // locality reorder on THIS parsed L0 graph, print the table, and exit.
  // Decides go/no-go for the reorder-not-replicate layout before any
  // build/search code is written. Gated; never runs in normal builds.
  if (std::getenv("SHINE_LAVD_OR_PROBE")) {
    lavd::run_or_probe(scratch, off_by_uid, N, l0_off, m_max0, dim);  // std::exit(0)
  }
  if (const char* ap = std::getenv("SHINE_LAVD_ADJ_DUMP")) {
    lavd::run_adj_dump(scratch, off_by_uid, N, l0_off, m_max0, ap);   // std::exit(0)
  }

  // ---- 3b. memory-bounded LAVD: rank nodes and co-locate only the
  //          top-H "hot" ones. Two query-free hot-set signals
  //          (SHINE_LAVD_HOTSET, default "hop"):
  //            hop   : hop-distance from the entry point (navigation
  //                    funnel; cf. CXL-ANNS Fig.9).
  //            indeg : level-0 in-degree (hubness) -- the nodes that
  //                    many others list as a neighbor are reached from
  //                    many directions and visited often, a structural
  //                    proxy for visit frequency (cf. FlatNav hubs).
  //          budget OFF (f>=1) => H=N => compact_idx unused => the
  //          slot-indexed full-LAVD path below is byte-identical. The
  //          cold fallback is correct for ANY hot set, so the ranking
  //          signal changes only QPS/op-count, never recall. ----
  t_rank->start();
  const f32 bfrac = lavd::Config::budget_frac();
  const bool budget = bfrac < 1.0f;
  const char* hs_env = std::getenv("SHINE_LAVD_HOTSET");
  const bool use_indeg = hs_env && std::strcmp(hs_env, "indeg") == 0;
  std::vector<u32> compact_idx;  // [N] -> compact block index or COLD
  u32 H = N;
  if (budget) {
    std::vector<u32> order(N);
    for (u32 i = 0; i < N; ++i) order[i] = i;
    u64 reached = N;
    if (use_indeg) {
      // hubness: count incoming level-0 edges per node (one pass).
      std::vector<u32> indeg(N, 0);
      for (u32 u = 0; u < N; ++u) {
        const byte_t* l0 = scratch + off_by_uid[u] + l0_off;
        u32 cnt = *reinterpret_cast<const u32*>(l0);
        if (cnt > m_max0) cnt = m_max0;
        const u64* np = reinterpret_cast<const u64*>(l0 + sizeof(u32));
        for (u32 i = 0; i < cnt; ++i) {
          const u32 v = *reinterpret_cast<const u32*>(
            scratch + RemotePtr{np[i]}.byte_offset() + Node::HEADER_SIZE);
          if (v < N) ++indeg[v];
        }
      }
      std::sort(order.begin(), order.end(), [&](u32 a, u32 b) {
        return indeg[a] != indeg[b] ? indeg[a] > indeg[b] : a < b;
      });
    } else {
      // hop-distance BFS from the entry point.
      const u64 ep_raw = *reinterpret_cast<u64*>(scratch + 8);
      const u64 ep_off = RemotePtr{ep_raw}.byte_offset();
      const u32 ep_uid = *reinterpret_cast<u32*>(scratch + ep_off + Node::HEADER_SIZE);
      std::vector<u32> hop(N, 0xFFFFFFFFu);
      std::vector<u32> bfs;
      bfs.reserve(N);
      hop[ep_uid] = 0;
      bfs.push_back(ep_uid);
      for (size_t qi = 0; qi < bfs.size(); ++qi) {
        const u32 u = bfs[qi];
        const byte_t* l0 = scratch + off_by_uid[u] + l0_off;
        u32 cnt = *reinterpret_cast<const u32*>(l0);
        if (cnt > m_max0) cnt = m_max0;
        const u64* np = reinterpret_cast<const u64*>(l0 + sizeof(u32));
        for (u32 i = 0; i < cnt; ++i) {
          const u32 v = *reinterpret_cast<const u32*>(
            scratch + RemotePtr{np[i]}.byte_offset() + Node::HEADER_SIZE);
          if (v < N && hop[v] == 0xFFFFFFFFu) {
            hop[v] = hop[u] + 1;
            bfs.push_back(v);
          }
        }
      }
      std::sort(order.begin(), order.end(), [&](u32 a, u32 b) {
        return hop[a] != hop[b] ? hop[a] < hop[b] : a < b;
      });
      reached = bfs.size();
    }
    H = static_cast<u32>(std::llround(static_cast<double>(bfrac) * N));
    if (H < 1) H = 1;
    if (H > N) H = N;
    compact_idx.assign(N, lavd::Config::COLD);
    for (u32 j = 0; j < H; ++j) compact_idx[order[j]] = j;
    std::cerr << "[LAVD][budget] f=" << bfrac << " H=" << H << "/" << N
              << " hotset=" << (use_indeg ? "indeg" : "hop")
              << " compact_region=" << (static_cast<u64>(H) * stride)
              << "B (+map " << (static_cast<u64>(N) * 4) << "B) reached="
              << reached << std::endl;
  }
  t_rank->stop();

  // ---- 4. fit quantizer from a deterministic sample of components ----
  // Always fit the scalar qz (its params go in the region header so every
  // CN can init dim/bits/rerank). When PQ is on, ALSO fit the PQ codebook
  // on the same sample and publish it (single-CN: CN-local, no MN round-
  // trip); assemble_block then writes m-byte PQ codes instead of SQ.
  t_encode->start();
  Quantizer qz;
  {
    const u32 SAMPLE = std::min<u32>(N, 100000);
    vec<element_t> samp(static_cast<size_t>(SAMPLE) * dim);
    for (u32 i = 0; i < SAMPLE; ++i) {
      const u64 o = off_by_uid[i];
      std::memcpy(samp.data() + static_cast<size_t>(i) * dim,
                  scratch + o + comp_off, dim * sizeof(element_t));
    }
    qz.fit(dim, bits, samp.data(), SAMPLE);
    if (rb > 0) {
      RaBitQ r;
      r.fit(dim, rb, samp.data(), SAMPLE, 12345);
      lavd::Config::set_rabitq(rb, r);
      // precompute per-uid RaBitQ codes ONCE (the denormalized layout would
      // otherwise re-rotate each vector ~M times). One-time pass over all N nodes.
      const size_t cb_rb = r.code_bytes();
      g_rabitq_codetab.assign(static_cast<size_t>(N) * cb_rb, 0);
      g_rabitq_normtab.assign(N, 0.f);
      g_rabitq_dottab.assign(N, 0.f);
      const u32 build_workers = lavd::build_worker_count(N);
      lavd::parallel_for_u32(
          N, build_workers, [&](u32 begin, u32 end, u32) {
            std::vector<f32> su(dim), sr(dim);
            for (u32 uid = begin; uid < end; ++uid) {
              const element_t* v = reinterpret_cast<const element_t*>(
                  scratch + off_by_uid[uid] + comp_off);
              r.encode(v,
                       &g_rabitq_codetab[static_cast<size_t>(uid) * cb_rb],
                       &g_rabitq_normtab[uid], &g_rabitq_dottab[uid],
                       su.data(), sr.data());
            }
          });
      const size_t sq_bytes = (bits == 8) ? dim : (dim + 1) / 2;  // scalar code size
      std::cerr << "[LAVD][rabitq] B=" << rb << " code_bytes=" << r.code_bytes()
                << " payload_bytes=" << lavd::qbytes(dim, bits)
                << " (scalar would be " << sq_bytes << " B) stride=" << stride
                << " build_threads=" << build_workers << std::endl;
    }
    if (pq_m > 0 && rb == 0) {
      const bool opq = lavd::Config::opq_from_env();
      PQ pqb;
      pqb.fit(dim, pq_m, samp.data(), SAMPLE, opq);
      lavd::Config::set_pq(pq_m, pqb);  // sets Config::pq + g_pq_m
      const size_t sq_bytes = (bits == 8) ? dim : (dim + 1) / 2;  // scalar code size
      std::cerr << "[LAVD][pq] m=" << pq_m << " code_bytes=" << pqb.code_bytes()
                << " opq=" << (opq ? 1 : 0)
                << " (scalar would be " << sq_bytes << " B) stride=" << stride << std::endl;
    }
  }

  // ---- 4a'. OPQ: rotate every node's component vector IN PLACE in the
  //   bulk-read scratch ONCE (R-preserving L2). assemble_block then
  //   encode_norot's the pre-rotated vectors (no per-neighbor rotation,
  //   avoiding the M x redundancy). The MN's index keeps RAW fp32 (this
  //   is a CN-local copy), so the fp32 rerank is unaffected. Cold-path /
  //   seed encode raw vectors and rotate on the fly (same R) => the codes
  //   match the hot path bit-for-bit.
  if (pq_m > 0 && rb == 0 && lavd::Config::pq.has_rot()) {
    vec<f32> tmp(dim);
    for (u32 uid = 0; uid < N; ++uid) {
      element_t* v = reinterpret_cast<element_t*>(scratch + off_by_uid[uid] + comp_off);
      lavd::Config::pq.rotate_vec(v, tmp.data());
      for (u32 d = 0; d < dim; ++d) v[d] = tmp[d];
    }
    std::cerr << "[LAVD][pq] rotated " << N << " scratch vectors in place (OPQ)" << std::endl;
  }
  t_encode->stop();

  // ---- 4b. write quantizer params to the region header (offset 0) so
  //          EVERY CN can RDMA-read them at startup and Config::init
  //          identically (multi-CN: only the initiator builds). ----
  t_metadata->start();
  {
    const size_t pb = qz.params_bytes();
    lib_assert(pb <= lavd::PARAMS_RESERVE, "LAVD: params exceed PARAMS_RESERVE");
    auto* ph = static_cast<byte_t*>(std::aligned_alloc(64, lavd::PARAMS_RESERVE));
    std::memset(ph, 0, lavd::PARAMS_RESERVE);
    qz.write_params(ph);
    // memory-bounded LAVD: stash N (total) and H (co-located) in the last
    // 8 B of the params reserve so every CN learns the budget at startup.
    // H==N signals OFF (full LAVD, slot-indexed, no map).
    *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF) = N;
    *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF + 4) = H;
    // RaBitQ: ship the data-dependent centroid so non-initiator CNs rebuild the
    // identical encoder (rotation/levels are deterministic from seed+dim/B).
    if (rb > 0) {
      const size_t ro = lavd::rabitq_hdr_off(pb);
      lib_assert(ro + 8 + static_cast<size_t>(dim) * sizeof(f32) <= lavd::HDR_COUNTS_OFF,
                 "LAVD: RaBitQ centroid overflows params header");
      *reinterpret_cast<u32*>(ph + ro) = rb;
      *reinterpret_cast<u32*>(ph + ro + 4) = dim;
      std::memcpy(ph + ro + 8, lavd::Config::rabitq.c.data(),
                  static_cast<size_t>(dim) * sizeof(f32));
    }
    LocalMemoryRegion ph_mr{context, ph, lavd::PARAMS_RESERVE};
    qp->post_send(reinterpret_cast<u64>(ph), static_cast<u32>(lavd::PARAMS_RESERVE),
                  ph_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false, nbh_tok,
                  /*remote_off*/ 0, 0, 0);
    context.poll_send_cq_until_completion();
    std::free(ph);
  }

  // ---- 4c. budget mode: write the slot->compact-index map to the region
  //          (right after the params header). All CNs RDMA-read it. ----
  if (budget) {
    LocalMemoryRegion map_mr{context, compact_idx.data(),
                             static_cast<size_t>(N) * sizeof(u32)};
    const u64 MCHUNK = (1ull << 30) / sizeof(u32);  // <1GB per RDMA
    for (u64 off = 0; off < N; off += MCHUNK) {
      const u32 cnt = static_cast<u32>(std::min<u64>(MCHUNK, N - off));
      qp->post_send(reinterpret_cast<u64>(compact_idx.data() + off),
                    static_cast<u32>(cnt * sizeof(u32)), map_mr.get_lkey(),
                    IBV_WR_RDMA_WRITE, true, false, nbh_tok,
                    lavd::map_region_offset() + off * sizeof(u32), 0, 0);
      context.poll_send_cq_until_completion();
    }
  }

  // ---- 4d. var-block mode: pre-compute prefix-sum offset_table + RDMA-WRITE
  //          to MN. Single-MN only; multi-MN var-block deferred to a
  //          follow-up commit. Mutex with budget for now (compact_idx and
  //          varblock_offset would need to compose). ----
  const bool vb_on = lavd::varblock_on();
  if (vb_on) {
    lib_assert(!budget, "SHINE_LAVD_VARBLOCK incompatible with SHINE_LAVD_BUDGET<1 in this commit");
    lavd::g_varblock_offsets.assign(static_cast<size_t>(N) + 1, 0);
    u64 cursor = lavd::varblock_blocks_base(N);
    for (u32 s = 0; s < N; ++s) {
      lavd::g_varblock_offsets[s] = cursor;
      const byte_t* l0p = scratch + off_by_uid[s] + l0_off;
      u32 cnt_s = *reinterpret_cast<const u32*>(l0p);
      if (cnt_s > m_max0) cnt_s = m_max0;
      cursor += lavd::varblock_stride(cnt_s, dim, bits);
    }
    lavd::g_varblock_offsets[N] = cursor;

    const size_t tbytes = lavd::varblock_table_bytes(N);
    LocalMemoryRegion ot_mr{context, lavd::g_varblock_offsets.data(), tbytes};
    const u64 TCHUNK = 1ull << 30;  // <1 GB per WR
    for (u64 off = 0; off < tbytes; off += TCHUNK) {
      const u32 cb = static_cast<u32>(std::min<u64>(TCHUNK, tbytes - off));
      qp->post_send(reinterpret_cast<u64>(
                        reinterpret_cast<const byte_t*>(lavd::g_varblock_offsets.data()) + off),
                    cb, ot_mr.get_lkey(),
                    IBV_WR_RDMA_WRITE, true, false, nbh_tok,
                    lavd::varblock_table_offset() + off, 0, 0);
      context.poll_send_cq_until_completion();
    }
    std::cerr << "[LAVD][varblock] offset_table written: N=" << N
              << " table_bytes=" << tbytes
              << " blocks_total=" << (cursor - lavd::varblock_blocks_base(N))
              << " avg_stride=" << ((cursor - lavd::varblock_blocks_base(N)) / std::max<u64>(1, N))
              << " fixed_stride=" << stride << std::endl;
  }
  t_metadata->stop();

  // ---- 5. assemble + publish neighborhood blocks ----
  t_materialize->start();
  const size_t block_buffer_capacity =
      lavd::aligned_allocation_bytes(stride, 64u);
  lib_assert(block_buffer_capacity != 0,
             "LAVD: invalid aligned block-buffer capacity");
  size_t total_edges = 0;
  size_t blocks_written = 0;
  u64 record_write_posts = 0;
  const bool staged_fixed_build = !budget && !vb_on && [] {
    const char* env = std::getenv("SHINE_LAVD_STAGED_BUILD");
    return env != nullptr && std::atoi(env) != 0;
  }();
  if (staged_fixed_build) {
    auto* stage = static_cast<byte_t*>(
        std::aligned_alloc(64, lavd::STAGED_IO_BYTES));
    lib_assert(stage != nullptr, "LAVD: staging-buffer allocation failed");
    const u32 build_workers = lavd::build_worker_count(N);
    {
      LocalMemoryRegion stage_mr{context, stage, lavd::STAGED_IO_BYTES};
      u32 cursor = 0;
      while (cursor < N) {
        const auto range = lavd::next_fixed_staged_slot_range(
            N, cursor, stride, lavd::STAGED_IO_BYTES);
        lib_assert(range.valid && range.end_slot > range.begin_slot,
                   "LAVD: invalid fixed-stride staging range");
        const u32 range_slots = range.end_slot - range.begin_slot;
        const u32 range_workers =
            std::min<u32>(build_workers, range_slots);
        std::vector<size_t> worker_edges(range_workers, 0);
        lavd::parallel_for_u32(
            range_slots, range_workers,
            [&](u32 begin, u32 end, u32 worker) {
              for (u32 relative = begin; relative < end; ++relative) {
                const u32 slot = range.begin_slot + relative;
                byte_t* record =
                    stage + static_cast<size_t>(relative) * stride;
                assemble_block(slot, scratch, off_by_uid, qz, dim, bits,
                               m_max0, l0_off, comp_off, stride, record);
                worker_edges[worker] += blk_count(record);
              }
            });
        for (size_t edges : worker_edges) total_edges += edges;
        const size_t remote_offset =
            lavd::region_offset(range.begin_slot, stride);
        lib_assert(
            lavd::region_range_fits(remote_offset, range.bytes,
                                    lavd::Config::region_capacity_bytes()),
            "LAVD: fixed staged publication exceeds registered region");
        qp->post_send(reinterpret_cast<u64>(stage), range.bytes,
                      stage_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                      nbh_tok, remote_offset, 0, 0);
        context.poll_send_cq_until_completion();
        blocks_written += range_slots;
        ++record_write_posts;
        cursor = range.end_slot;
      }
    }
    std::free(stage);
    std::cerr << "LAVD_BUILD_PUBLICATION {\"version\":1,\"mode\":\"staged_fixed\""
              << ",\"workers\":" << build_workers
              << ",\"staging_bytes\":" << lavd::STAGED_IO_BYTES
              << ",\"records\":" << blocks_written
              << ",\"record_write_posts\":" << record_write_posts << "}"
              << std::endl;
  } else {
    auto* blk = static_cast<byte_t*>(
        std::aligned_alloc(64, block_buffer_capacity));
    lib_assert(blk != nullptr, "LAVD: block-buffer allocation failed");
    {
      LocalMemoryRegion blk_mr{context, blk, block_buffer_capacity};
      for (u32 slot = 0; slot < N; ++slot) {
        if (budget && compact_idx[slot] == lavd::Config::COLD) continue;
        assemble_block(slot, scratch, off_by_uid, qz, dim, bits, m_max0,
                       l0_off, comp_off, stride, blk);
        const u32 cnt = blk_count(blk);
        total_edges += cnt;
        const size_t boff = budget
            ? lavd::compact_block_offset(compact_idx[slot], N, stride)
            : (vb_on ? lavd::varblock_offset(slot)
                     : lavd::region_offset(slot, stride));
        const u32 write_bytes = vb_on
            ? static_cast<u32>(lavd::varblock_stride(cnt, dim, bits))
            : static_cast<u32>(stride);
        qp->post_send(reinterpret_cast<u64>(blk), write_bytes,
                      blk_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                      nbh_tok, boff, 0, 0);
        context.poll_send_cq_until_completion();
        ++blocks_written;
        ++record_write_posts;
      }
    }
    std::free(blk);
    std::cerr << "LAVD_BUILD_PUBLICATION {\"version\":1,\"mode\":\"serial\""
              << ",\"workers\":1,\"staging_bytes\":0"
              << ",\"records\":" << blocks_written
              << ",\"record_write_posts\":" << record_write_posts << "}"
              << std::endl;
  }
  t_materialize->stop();

  t_total->stop();
  struct rusage usage {};
  lib_assert(getrusage(RUSAGE_SELF, &usage) == 0, "LAVD: getrusage failed");
  const u64 region_bytes = budget
    ? (static_cast<u64>(N) * 4 + static_cast<u64>(blocks_written) * stride)
    : (static_cast<u64>(N) * stride);
  std::cerr << "[LAVD] build done: N=" << N << " m_max0=" << m_max0
            << " bits=" << bits << " stride=" << stride
            << " budget_f=" << bfrac << " blocks=" << blocks_written << "/" << N
            << " region=" << region_bytes << "B"
            << " avg_deg=" << (double)total_edges / std::max<size_t>(1, blocks_written)
            << std::endl;
  std::cerr << "[LAVD][build-profile] peak_rss_kb=" << usage.ru_maxrss << std::endl;

  // ---- self-check (SHINE_LAVD_SELFTEST=1; slot-indexed mode only — uses
  //      region_offset, which the compact budget layout does not use) ----
  const char* st = std::getenv("SHINE_LAVD_SELFTEST");
  const char* cst = std::getenv("SHINE_LAVD_COLOC_SELFTEST");
  const bool run_selftest = !budget && !vb_on &&
    ((st && std::atoi(st) != 0) ||
     (lavd::layout_coloc_on(m_max0) && cst && std::atoi(cst) != 0));
  if (run_selftest) {
    auto* rb = static_cast<byte_t*>(
        std::aligned_alloc(64, block_buffer_capacity));
    lib_assert(rb != nullptr, "LAVD: selftest-buffer allocation failed");
    LocalMemoryRegion rb_mr{context, rb, block_buffer_capacity};
    u32 checked = 0, fails = 0;
    std::mt19937 rng(12345);
    const u32 K_CHECK = std::min<u32>(N, 64);
    for (u32 c = 0; c < K_CHECK; ++c) {
      const u32 slot = rng() % N;
      qp->post_send(reinterpret_cast<u64>(rb), static_cast<u32>(stride), rb_mr.get_lkey(),
                    IBV_WR_RDMA_READ, true, false, nbh_tok,
                    lavd::region_offset(slot, stride), 0, 0);
      context.poll_send_cq_until_completion();

      const u64 o = off_by_uid[slot];
      const byte_t* l0 = scratch + o + l0_off;
      u32 exp_cnt = *reinterpret_cast<const u32*>(l0);
      if (exp_cnt > m_max0) exp_cnt = m_max0;
      const u64* nptr = reinterpret_cast<const u64*>(l0 + sizeof(u32));
      bool ok = (blk_count(rb) == exp_cnt);
      for (u32 i = 0; ok && i < exp_cnt; ++i) {
        const byte_t* e = blk_entry(rb, i, dim, bits, m_max0);
        const RemotePtr rp{nptr[i]};
        const u32 exp_slot = *reinterpret_cast<const u32*>(scratch + rp.byte_offset() + Node::HEADER_SIZE);
        if (ent_slot(e) != exp_slot || ent_rptr(e) != nptr[i]) { ok = false; break; }
        if (!lavd::entry_has_qvec(i, m_max0)) continue;
        std::vector<u8> expq(qbytes(dim, bits));
        const element_t* ev = reinterpret_cast<const element_t*>(scratch + rp.byte_offset() + comp_off);
        if (lavd::g_rabitq_b) {
          std::vector<f32> su(dim), sr(dim);
          lavd::Config::rabitq.encode(ev, expq.data() + 8,
                                      reinterpret_cast<f32*>(expq.data()),
                                      reinterpret_cast<f32*>(expq.data() + 4),
                                      su.data(), sr.data());
        } else if (lavd::g_pq_m) lavd::Config::pq.encode_norot(ev, expq.data());  // scratch pre-rotated for OPQ
        else qz.encode(ev, expq.data());
        if (std::memcmp(ent_qvec(e), expq.data(), qbytes(dim, bits)) != 0) { ok = false; break; }
      }
      ++checked;
      if (!ok) ++fails;
    }
    std::cerr << "[LAVD][selftest] checked=" << checked << " fails=" << fails
              << " coloc_d=" << lavd::layout_coloc_d(m_max0)
              << (fails == 0 ? "  PASS" : "  FAIL") << std::endl;
    std::free(rb);
  }

  const char* crane_env = std::getenv("SHINE_CRANE");
  const bool retain_authoritative_snapshot =
      crane_env != nullptr && std::atoll(crane_env) != 0;
  if (retain_authoritative_snapshot) {
    std::vector<void*> snapshot_shards;
    snapshot_shards.push_back(scratch);
    std::vector<u64> snapshot_bytes{static_cast<u64>(idx_bytes)};
    const u64 entry_raw = *reinterpret_cast<const u64*>(scratch + 8);
    lavd::publish_authoritative_snapshot(
        std::move(snapshot_shards), std::move(snapshot_bytes), entry_raw);
    scratch = nullptr;
    std::cerr << "[LAVD] retained authoritative snapshot for resident upper graph: shards=1"
              << std::endl;
  } else {
    lavd::clear_authoritative_snapshot();
    std::free(scratch);
  }
  std::free(hdr);
  return qz;
}

// =====================================================================
// build_neighborhood_multi: multi-MN variant of build_neighborhood.
// Per-MN bulk-read into separate scratches; each uid's fat block is written
// to its uid's MN (decoded from the uid's RemotePtr in SHINE's index, which
// already encodes mn_id per node). Search side is UNCHANGED because the fat
// block's nbr_rptr already carries the correct mn (RemotePtr-native), and
// region_offset(uid, stride) addresses each MN's per-MN LAVD region by uid.
// Design A: each MN's LAVD region is uid-indexed (sparse — only ~N/num_mns
// slots used per MN, the rest are zero/unused). Trades some MN memory for
// zero changes on the search hot path.
// NOTE: budget mode is intentionally NOT supported in this initial version
// (asserts no budget). All-hot path; full N blocks distributed across MNs.
// =====================================================================
template <typename Ctx>
inline Quantizer build_neighborhood_multi(Ctx* ctx, u32 num_mns, u32 bits, timing::Timing& timing) {
  Context& context = ctx->context;
  lib_assert(num_mns >= 1, "LAVD-multi: num_mns must be >= 1");

  const u32 dim = Node::DIM;
  const u32 m_max0 = (Node::NEIGHBORLIST_SIZE_ZERO - sizeof(u32)) / sizeof(u64);
  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;
  const size_t l0_off = Node::size_until_components();
  const u32 pq_m = lavd::Config::pq_m_from_env();
  lavd::g_pq_m = pq_m;
  lib_assert(pq_m == 0, "LAVD-multi: PQ not yet supported in multi-MN path");
  const u32 rb = lavd::Config::rabitq_b_from_env();
  lavd::g_rabitq_b = rb;
  lavd::g_rabitq_code = rb ? ((static_cast<size_t>(dim) * rb + 7) / 8) : 0;
  const size_t stride = block_stride(m_max0, dim, bits);
  const bool vb_on = lavd::varblock_on();
  const bool native_packed_write = [] {
    const char* e = std::getenv("SHINE_LAVD_NATIVE_PACKED_WRITE");
    return e && std::atoi(e) != 0;
  }();

  u64 byte_budget = 0;
  lib_assert(lavd::materialization::parse_budget_bytes(
                 std::getenv("SHINE_LAVD_BUDGET_BYTES"), &byte_budget),
             "SHINE_LAVD_BUDGET_BYTES must be an unsigned byte count");
  const bool byte_budget_on = byte_budget > 0;

  // Multi-MN budget<1 (mbLAVD f-knob) is supported when paired with
  // var-block — the offset_table already represents per-slot variable
  // size, so cold slots naturally get a zero-stride entry (no on-MN
  // storage) and the existing global budget_map drives the search-side
  // hot/cold branch. Sparse + budget (no var-block) is a narrower
  // regime we do not wire here.
  const f32 bfrac = lavd::Config::budget_frac();
  const auto byte_budget_config =
      lavd::materialization::validate_byte_budget_configuration(
          byte_budget, bfrac, vb_on, native_packed_write);
  lib_assert(byte_budget_config !=
                 lavd::materialization::ConfigurationError::kAmbiguousBudgets,
             "SHINE_LAVD_BUDGET_BYTES is ambiguous with SHINE_LAVD_BUDGET<1");
  lib_assert(byte_budget_config !=
                 lavd::materialization::ConfigurationError::kRequiresVariableRecords,
             "SHINE_LAVD_BUDGET_BYTES requires SHINE_LAVD_VARBLOCK=1");
  lib_assert(byte_budget_config !=
                 lavd::materialization::ConfigurationError::kRequiresNativePackedLayout,
             "SHINE_LAVD_BUDGET_BYTES requires SHINE_LAVD_NATIVE_PACKED_WRITE=1");
  const bool budget = byte_budget_on || bfrac < 1.0f;
  if (budget) {
    lib_assert(vb_on,
               "LAVD-multi budget<1 requires SHINE_LAVD_VARBLOCK=1");
  }

  auto t = timing.create_enroll("lavd_build_multi");
  t->start();
  std::cerr << "[LAVD][multi] start build, num_mns=" << num_mns
            << " bits=" << bits << " stride=" << stride
            << " rabitq_b=" << rb << std::endl;

  // ---- Step 1+2: per-MN bulk-read INDEX into separate scratches ----
  auto t_fetch = timing.create_enroll("lavd_build_fetch");
  t_fetch->start();
  std::vector<byte_t*> per_mn_scratch(num_mns, nullptr);
  std::vector<u64> per_mn_bytes(num_mns, 0);
  auto* read_stage =
      static_cast<byte_t*>(std::aligned_alloc(64, lavd::STAGED_IO_BYTES));
  lib_assert(read_stage != nullptr,
             "LAVD-multi: staged-read buffer allocation failed");
  {
    LocalMemoryRegion read_stage_mr{context, read_stage,
                                    lavd::STAGED_IO_BYTES};
    for (u32 mn = 0; mn < num_mns; ++mn) {
      const QP& qp = ctx->qps[mn]->qp;
      MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);

      auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
      lib_assert(hdr != nullptr,
                 "LAVD-multi: free-pointer buffer allocation failed");
      u64 free_ptr = 0;
      {
        LocalMemoryRegion hdr_mr{context, hdr, 64};
        qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(),
                      IBV_WR_RDMA_READ, true, false, idx_tok, 0, 0, 0);
        context.poll_send_cq_until_completion();
        free_ptr = *reinterpret_cast<u64*>(hdr);
      }
      std::free(hdr);
      lib_assert(free_ptr > 16 && free_ptr < (1ull << 48),
                 "LAVD-multi: bad free_ptr");
      per_mn_bytes[mn] = free_ptr;

      const size_t scratch_capacity =
          static_cast<size_t>((free_ptr + 63) & ~u64{63});
      per_mn_scratch[mn] = static_cast<byte_t*>(
          std::aligned_alloc(64, scratch_capacity));
      lib_assert(per_mn_scratch[mn] != nullptr,
                 "LAVD-multi: scratch allocation failed");
      for (u64 off = 0; off < free_ptr; off += lavd::STAGED_IO_BYTES) {
        const u32 n = lavd::staged_chunk_bytes(free_ptr, off);
        qp->post_send(reinterpret_cast<u64>(read_stage), n,
                      read_stage_mr.get_lkey(), IBV_WR_RDMA_READ, true,
                      false, idx_tok, off, 0, 0);
        context.poll_send_cq_until_completion();
        std::memcpy(per_mn_scratch[mn] + off, read_stage, n);
      }
      std::cerr << "[LAVD][multi] MN " << mn << " staged-read "
                << free_ptr << "B via " << lavd::STAGED_IO_BYTES << "B MR"
                << std::endl;
    }
  }
  std::free(read_stage);
  t_fetch->stop();

  // ---- Step 3: parse all MN shards, build global off_by_uid + max_uid ----
  auto t_parse = timing.create_enroll("lavd_build_parse");
  t_parse->start();
  static constexpr u32 INVALID_MN = 0xFFFFFFFFu;
  std::vector<UidInfoM> off_by_uid;  // [uid] -> {mn, byte_offset_in_per_mn_scratch[mn]}
  off_by_uid.reserve(1u << 20);
  u32 max_uid = 0;

  for (u32 mn = 0; mn < num_mns; ++mn) {
    u64 walk = 16;
    while (walk < per_mn_bytes[mn]) {
      const u32 uid = *reinterpret_cast<u32*>(per_mn_scratch[mn] + walk + Node::HEADER_SIZE);
      const u32 level = *reinterpret_cast<u32*>(per_mn_scratch[mn] + walk + Node::HEADER_SIZE + sizeof(u32));
      size_t node_total =
        l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
      while (node_total % 8 != 0) node_total += 4;

      if (uid >= off_by_uid.size()) off_by_uid.resize(uid + 1, {INVALID_MN, 0});
      lib_assert(off_by_uid[uid].mn == INVALID_MN,
                 "LAVD-multi: duplicate uid across MN shards (data corruption?)");
      off_by_uid[uid] = {mn, walk};
      max_uid = std::max(max_uid, uid);
      walk += node_total;
    }
    lib_assert(walk == per_mn_bytes[mn],
               "LAVD-multi: walk did not consume MN's index region exactly");
  }
  const u32 N = max_uid + 1;
  u32 missing = 0;
  for (u32 u = 0; u < N; ++u) if (off_by_uid[u].mn == INVALID_MN) ++missing;
  lib_assert(missing == 0, "LAVD-multi: gaps in uid space (some uids missing)");
  std::cerr << "[LAVD][multi] parsed N=" << N << " uids across " << num_mns << " MNs" << std::endl;
  t_parse->stop();

  // ---- Multi-MN budget: compute global hot/cold ranking + compact_idx.
  //      Matches single-MN: SHINE_LAVD_HOTSET=indeg uses hubness (L0
  //      in-degree, default in single-MN per project memory), otherwise
  //      BFS from the real entry point. Indeg is one cross-MN pass over
  //      every L0 neighbor uid (already resolvable via off_by_uid since
  //      every uid lives in some per_mn_scratch[]).
  std::vector<u32> compact_idx;
  std::vector<lavd::materialization::Candidate> byte_candidates;
  lavd::materialization::Policy byte_policy =
      lavd::materialization::Policy::kHop;
  lavd::materialization::Selection byte_selection;
  bool materialization_map_on = false;
  u64 byte_reached = N;
  u32 H = N;
  u32 rank_workers = 1;
  auto t_rank = timing.create_enroll("lavd_build_rank");
  t_rank->start();
  if (byte_budget_on) {
    lib_assert(lavd::materialization::parse_policy(
                   std::getenv("SHINE_LAVD_HOTSET"), &byte_policy),
               "SHINE_LAVD_HOTSET must be benefit, indeg, or hop with a byte budget");
    byte_candidates.resize(N);
    rank_workers = lavd::build_worker_count(N);
    lavd::parallel_accumulate_indegree_u32(
        N, rank_workers, [&](u32 u, const auto& emit) {
          auto& candidate = byte_candidates[u];
          candidate.uid = u;
          candidate.hop = std::numeric_limits<u32>::max();
          const auto& info_u = off_by_uid[u];
          const byte_t* l0p =
              per_mn_scratch[info_u.mn] + info_u.offset + l0_off;
          u32 cnt = *reinterpret_cast<const u32*>(l0p);
          if (cnt > m_max0) cnt = m_max0;
          candidate.live_degree = cnt;
          candidate.record_bytes =
              static_cast<u64>(lavd::varblock_stride(cnt, dim, bits));
          const u64* np = reinterpret_cast<const u64*>(l0p + sizeof(u32));
          for (u32 i = 0; i < cnt; ++i) {
            const RemotePtr nbr_rp{np[i]};
            const u32 v = *reinterpret_cast<const u32*>(
                per_mn_scratch[nbr_rp.memory_node()] +
                nbr_rp.byte_offset() + Node::HEADER_SIZE);
            emit(v);
          }
        },
        [&](u32 uid) -> u64& { return byte_candidates[uid].indegree; });

    if (byte_policy == lavd::materialization::Policy::kHop) {
      const u64 ep_raw =
          *reinterpret_cast<const u64*>(per_mn_scratch[0] + 8);
      const RemotePtr ep_rp{ep_raw};
      const u32 ep_uid_anchor =
          *reinterpret_cast<const u32*>(
              per_mn_scratch[ep_rp.memory_node()] + ep_rp.byte_offset() +
              Node::HEADER_SIZE);
      std::vector<u32> bfs;
      bfs.reserve(N);
      byte_candidates[ep_uid_anchor].hop = 0;
      bfs.push_back(ep_uid_anchor);
      for (size_t qi = 0; qi < bfs.size(); ++qi) {
        const u32 u = bfs[qi];
        const auto& info_u = off_by_uid[u];
        const byte_t* l0p =
            per_mn_scratch[info_u.mn] + info_u.offset + l0_off;
        const u32 cnt = byte_candidates[u].live_degree;
        const u64* np = reinterpret_cast<const u64*>(l0p + sizeof(u32));
        for (u32 i = 0; i < cnt; ++i) {
          const RemotePtr nbr_rp{np[i]};
          const u32 v = *reinterpret_cast<const u32*>(
              per_mn_scratch[nbr_rp.memory_node()] +
              nbr_rp.byte_offset() + Node::HEADER_SIZE);
          if (v < N && byte_candidates[v].hop ==
                           std::numeric_limits<u32>::max()) {
            byte_candidates[v].hop = byte_candidates[u].hop + 1;
            bfs.push_back(v);
          }
        }
      }
      byte_reached = bfs.size();
    }
  } else if (budget) {
    const char* hs_env = std::getenv("SHINE_LAVD_HOTSET");
    const bool use_indeg = hs_env && std::strcmp(hs_env, "indeg") == 0;
    std::vector<u32> order(N);
    for (u32 i = 0; i < N; ++i) order[i] = i;
    u64 reached = N;
    if (use_indeg) {
      // hubness: count incoming L0 edges per node, cross-MN.
      std::vector<u32> indeg(N, 0);
      for (u32 u = 0; u < N; ++u) {
        const auto& info_u = off_by_uid[u];
        const byte_t* l0p = per_mn_scratch[info_u.mn] + info_u.offset + l0_off;
        u32 cnt = *reinterpret_cast<const u32*>(l0p);
        if (cnt > m_max0) cnt = m_max0;
        const u64* np = reinterpret_cast<const u64*>(l0p + sizeof(u32));
        for (u32 i = 0; i < cnt; ++i) {
          const RemotePtr nbr_rp{np[i]};
          const u32 v = *reinterpret_cast<const u32*>(
              per_mn_scratch[nbr_rp.memory_node()] + nbr_rp.byte_offset() + Node::HEADER_SIZE);
          if (v < N) ++indeg[v];
        }
      }
      std::sort(order.begin(), order.end(), [&](u32 a, u32 b) {
        return indeg[a] != indeg[b] ? indeg[a] > indeg[b] : a < b;
      });
    } else {
      // hop-distance BFS from the real entry point (per MN[0] header).
      const u64 ep_raw = *reinterpret_cast<const u64*>(per_mn_scratch[0] + 8);
      const RemotePtr ep_rp{ep_raw};
      const u32 ep_uid_anchor =
          (ep_rp.memory_node() < num_mns)
              ? *reinterpret_cast<const u32*>(
                    per_mn_scratch[ep_rp.memory_node()] + ep_rp.byte_offset() + Node::HEADER_SIZE)
              : 0u;
      std::vector<u32> hop(N, 0xFFFFFFFFu);
      std::vector<u32> bfs;
      bfs.reserve(N);
      hop[ep_uid_anchor] = 0;
      bfs.push_back(ep_uid_anchor);
      for (size_t qi = 0; qi < bfs.size(); ++qi) {
        const u32 u = bfs[qi];
        const auto& info_u = off_by_uid[u];
        const byte_t* l0p = per_mn_scratch[info_u.mn] + info_u.offset + l0_off;
        u32 cnt = *reinterpret_cast<const u32*>(l0p);
        if (cnt > m_max0) cnt = m_max0;
        const u64* np = reinterpret_cast<const u64*>(l0p + sizeof(u32));
        for (u32 i = 0; i < cnt; ++i) {
          const RemotePtr nbr_rp{np[i]};
          const u32 v = *reinterpret_cast<const u32*>(
              per_mn_scratch[nbr_rp.memory_node()] + nbr_rp.byte_offset() + Node::HEADER_SIZE);
          if (v < N && hop[v] == 0xFFFFFFFFu) {
            hop[v] = hop[u] + 1;
            bfs.push_back(v);
          }
        }
      }
      std::sort(order.begin(), order.end(),
                [&](u32 a, u32 b) { return hop[a] != hop[b] ? hop[a] < hop[b] : a < b; });
      reached = bfs.size();
    }
    H = static_cast<u32>(std::llround(static_cast<double>(bfrac) * N));
    if (H < 1) H = 1;
    if (H > N) H = N;
    compact_idx.assign(N, lavd::Config::COLD);
    for (u32 j = 0; j < H; ++j) compact_idx[order[j]] = j;
    std::cerr << "[LAVD][multi][budget] f=" << bfrac << " H=" << H << "/" << N
              << " hotset=" << (use_indeg ? "indeg" : "hop")
              << " reached=" << reached << std::endl;
  }
  t_rank->stop();

  const bool native_plan_on = [] {
    const char* e = std::getenv("SHINE_LAVD_NATIVE_EXPORT_PLAN");
    return e && std::atoi(e) != 0;
  }();
  const char* native_policy_env = std::getenv("SHINE_LAVD_NATIVE_POLICY");
  const lavd::native::ShardPolicy native_policy =
      native_policy_env && std::strcmp(native_policy_env, "range") == 0
          ? lavd::native::ShardPolicy::kContiguousRange
          : lavd::native::ShardPolicy::kBlockCyclic;
  const lavd::native::ScoringCodeKind native_scoring_kind =
      rb > 0 ? lavd::native::ScoringCodeKind::kRaBitQ
             : (pq_m > 0 ? lavd::native::ScoringCodeKind::kProductQuantizer
                         : lavd::native::ScoringCodeKind::kScalarQuantizer);
  const u32 native_scoring_bits = rb > 0 ? rb : (pq_m > 0 ? pq_m : bits);
  lib_assert(stride <= std::numeric_limits<u32>::max(),
             "LAVD-native: record upper bound exceeds descriptor field");
  const u32 native_max_record_bytes = static_cast<u32>(stride);
  const bool native_slot_only = lavd::slot_only_on();
  const u32 native_colocated_degree =
      native_slot_only ? 0u : lavd::layout_coloc_d(m_max0);
  lavd::native::FixedExportPlan native_export_plan;
  lavd::native::PhysicalByteAccounting native_accounting;
  lavd::native::NativeDescriptor native_descriptor;
  std::string selected_uid_hash;
  if (native_plan_on || native_packed_write) {
    native_export_plan = lavd::native::make_fixed_export_plan(
        N, num_mns, static_cast<u32>(stride), lavd::PARAMS_RESERVE,
        native_policy, /*materialize_entries*/ false);
    std::cerr << "[LAVD][native][export] N=" << N
              << " mns=" << num_mns
              << " policy=" << lavd::native::policy_name(native_policy)
              << " mode=" << (native_packed_write ? "packed_write" : "plan_only")
              << " packed_region=" << native_export_plan.packed_region_bytes
              << " sparse_region=" << native_export_plan.sparse_region_bytes
              << " saved=" << native_export_plan.saved_region_bytes();
    for (u32 mn = 0; mn < num_mns; ++mn) {
      std::cerr << " MN[" << mn << "]_blocks=" << native_export_plan.blocks_per_mn[mn]
                << " MN[" << mn << "]_bytes=" << native_export_plan.region_bytes_per_mn[mn];
    }
    std::cerr << std::endl;
  }

  if (byte_budget_on) {
    u64 map_payload_bytes = 0;
    u64 map_shift_bytes = 0;
    lib_assert(lavd::native::detail::checked_mul(
                   static_cast<u64>(N),
                   lavd::native::BUDGET_MAP_ENTRY_BYTES,
                   &map_payload_bytes) &&
                   lavd::native::detail::checked_align_up(
                       map_payload_bytes,
                       lavd::native::OFFSET_TABLE_ENTRY_BYTES,
                       &map_shift_bytes),
               "LAVD exact-byte budget metadata size overflow");
    const std::vector<u64> zero_record_bytes(num_mns, 0u);
    const auto fixed_accounting_without_map =
        lavd::native::make_variable_physical_accounting(
            native_export_plan.layout.resolver, lavd::PARAMS_RESERVE,
            lavd::native::BudgetMapPlacement::kNone,
            /*map_shift_bytes=*/0u, zero_record_bytes);
    const auto fixed_accounting_with_map =
        lavd::native::make_variable_physical_accounting(
            native_export_plan.layout.resolver, lavd::PARAMS_RESERVE,
            lavd::native::BudgetMapPlacement::kMemoryNode0,
            map_shift_bytes, zero_record_bytes);
    lib_assert(fixed_accounting_without_map.valid &&
                   fixed_accounting_with_map.valid,
               "LAVD exact-byte budget fixed metadata accounting failed");
    auto optional_map_selection =
        lavd::materialization::select_records_with_optional_map(
            byte_candidates, byte_policy, byte_budget,
            fixed_accounting_without_map.total_bytes_across_mns,
            fixed_accounting_with_map.total_bytes_across_mns);
    materialization_map_on = optional_map_selection.budget_map_required;
    byte_selection = std::move(optional_map_selection.selection);
    lib_assert(byte_selection.valid,
               "LAVD exact-byte materialization selection failed: error=" +
                   std::to_string(static_cast<u32>(byte_selection.error)));
    compact_idx = std::move(byte_selection.compact_idx);
    H = static_cast<u32>(byte_selection.selected_uids.size());
    std::cerr
        << "LAVD_MATERIALIZATION_POLICY {\"version\":"
        << lavd::materialization::SELECTION_VERSION << ",\"policy\":\""
        << lavd::materialization::policy_name(byte_policy)
        << "\",\"budget_scope\":\"aggregate_high_water_across_mns\""
        << ",\"requested_bytes\":" << byte_selection.requested_bytes
        << ",\"fixed_bytes\":" << byte_selection.fixed_bytes
        << ",\"record_bytes\":" << byte_selection.record_bytes
        << ",\"admitted_bytes\":" << byte_selection.admitted_bytes
        << ",\"unused_bytes\":" << byte_selection.unused_bytes
        << ",\"selected_records\":" << H << ",\"total_records\":" << N
        << ",\"budget_map_required\":"
        << (materialization_map_on ? "true" : "false")
        << ",\"selection_hash\":" << byte_selection.selection_hash
        << ",\"total_benefit\":" << byte_selection.total_benefit
        << ",\"reached\":" << byte_reached
        << ",\"rank_workers\":" << rank_workers << "}" << std::endl;
    std::vector<lavd::materialization::Candidate>().swap(byte_candidates);
  }
  if (!byte_budget_on) {
    materialization_map_on = H < N;
  }

  {
    evidence::Fnv1a64 selected_uid_hasher;
    if (byte_budget_on) {
      lib_assert(byte_selection.selected_uids.size() == H,
                 "LAVD exact-byte selected-UID count mismatch");
      for (const u32 uid : byte_selection.selected_uids) {
        selected_uid_hasher.update_u32_le(uid);
      }
    } else if (materialization_map_on) {
      std::vector<u32> selected_uids(H, lavd::Config::COLD);
      for (u32 uid = 0; uid < N; ++uid) {
        const u32 compact = compact_idx[uid];
        if (compact == lavd::Config::COLD) continue;
        lib_assert(compact < H &&
                       selected_uids[compact] == lavd::Config::COLD,
                   "LAVD selected-UID sequence is not a permutation");
        selected_uids[compact] = uid;
      }
      for (const u32 uid : selected_uids) {
        lib_assert(uid != lavd::Config::COLD,
                   "LAVD selected-UID sequence has a gap");
        selected_uid_hasher.update_u32_le(uid);
      }
    } else {
      lib_assert(H == N, "LAVD full materialization count mismatch");
      for (u32 uid = 0; uid < N; ++uid) {
        selected_uid_hasher.update_u32_le(uid);
      }
    }
    selected_uid_hash = selected_uid_hasher.hex();
  }
  std::vector<u32>().swap(byte_selection.selected_uids);

  const u64 region_capacity = lavd::Config::region_capacity_bytes();
  lib_assert(
      lavd::is_valid_region_capacity_bytes(region_capacity,
        lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES),
      "LAVD-multi: invalid registered neighborhood-region capacity");
  const auto require_region_range = [region_capacity](u32 mn, u64 offset,
                                                       u64 bytes,
                                                       const char* object) {
    lib_assert(lavd::region_range_fits(offset, bytes, region_capacity),
               std::string("LAVD-multi: ") + object + " on MN[" +
                   std::to_string(mn) + "] exceeds registered region: offset=" +
                   std::to_string(offset) + " bytes=" + std::to_string(bytes) +
                   " capacity=" + std::to_string(region_capacity));
  };

  if (native_packed_write && !vb_on) {
    native_accounting =
        lavd::native::make_fixed_physical_accounting(native_export_plan);
    lib_assert(native_accounting.valid,
               "LAVD-native: invalid fixed physical accounting");
    native_descriptor = lavd::native::make_fixed_descriptor(
        native_export_plan, native_scoring_kind, native_scoring_bits,
        m_max0, native_colocated_degree, native_slot_only);
    lib_assert(native_descriptor.valid,
               "LAVD-native: failed to construct fixed v3 descriptor");
    for (u32 mn = 0; mn < num_mns; ++mn) {
      require_region_range(mn, 0,
                           native_accounting.per_mn[mn].total_region_bytes,
                           "fixed packed shard");
    }
  }

  // ---- Step 4: fit quantizer + RaBitQ on a deterministic sample ----
  auto t_encode = timing.create_enroll("lavd_build_encode");
  t_encode->start();
  Quantizer qz;
  {
    const u32 SAMPLE = std::min<u32>(N, 100000);
    vec<element_t> samp(static_cast<size_t>(SAMPLE) * dim);
    for (u32 i = 0; i < SAMPLE; ++i) {
      const auto& info = off_by_uid[i];
      std::memcpy(samp.data() + static_cast<size_t>(i) * dim,
                  per_mn_scratch[info.mn] + info.offset + comp_off,
                  static_cast<size_t>(dim) * sizeof(element_t));
    }
    qz.fit(dim, bits, samp.data(), SAMPLE);
    if (rb > 0) {
      RaBitQ r;
      r.fit(dim, rb, samp.data(), SAMPLE, 12345);
      lavd::Config::set_rabitq(rb, r);
      // Precompute per-uid RaBitQ code (encode-once memoization)
      const size_t cb_rb = r.code_bytes();
      g_rabitq_codetab.assign(static_cast<size_t>(N) * cb_rb, 0);
      g_rabitq_normtab.assign(N, 0.f);
      g_rabitq_dottab.assign(N, 0.f);
      const u32 build_workers = lavd::build_worker_count(N);
      lavd::parallel_for_u32(
          N, build_workers, [&](u32 begin, u32 end, u32) {
            std::vector<f32> su(dim), sr(dim);
            for (u32 uid = begin; uid < end; ++uid) {
              const auto& info = off_by_uid[uid];
              const element_t* v = reinterpret_cast<const element_t*>(
                  per_mn_scratch[info.mn] + info.offset + comp_off);
              r.encode(v,
                       &g_rabitq_codetab[static_cast<size_t>(uid) * cb_rb],
                       &g_rabitq_normtab[uid], &g_rabitq_dottab[uid],
                       su.data(), sr.data());
            }
          });
      const size_t sq_bytes = (bits == 8) ? dim : (dim + 1) / 2;
      std::cerr << "[LAVD][multi][rabitq] B=" << rb << " code_bytes=" << r.code_bytes()
                << " (scalar would be " << sq_bytes << " B)"
                << " build_threads=" << build_workers << std::endl;
    }
  }
  t_encode->stop();

  const size_t params_bytes = qz.params_bytes();
  lib_assert(lavd::native::scalar_params_fit_before_metadata(
                 params_bytes, native_packed_write),
             "LAVD-multi: scalar quantizer params overlap sidecar metadata");
  if (rb > 0) {
    const size_t rb_limit = native_packed_write
                                ? lavd::native::DESCRIPTOR_OFFSET
                                : lavd::HDR_COUNTS_OFF;
    const size_t rb_end = lavd::rabitq_hdr_off(params_bytes) + 8 +
                          static_cast<size_t>(dim) * sizeof(f32);
    lib_assert(rb_end <= rb_limit,
               "LAVD-multi: RaBitQ centroid overlaps sidecar metadata");
  }

  // ---- Step 4b: Multi-MN var-block prefix-sum + per-MN offset_table.
  //                Requires native_packed_write (we need a per-MN owner
  //                resolver to compute local_slot per uid). Combined
  //                mode populates g_varblock_offsets_per_mn on the
  //                initiator + RDMA-WRITEs each MN's table. ----
  auto t_metadata = timing.create_enroll("lavd_build_metadata");
  t_metadata->start();
  if (vb_on) {
    lib_assert(native_packed_write,
               "LAVD-multi varblock requires SHINE_LAVD_NATIVE_PACKED_WRITE=1 (owner resolver)");
    // Combined+budget collision fix: when budget is on, MN[0]'s compact_idx
    // map occupies [PARAMS_RESERVE, PARAMS_RESERVE+N*4). Shift var-block
    // table/blocks past that footprint on EVERY MN (uniform layout, ~0.76 MB
    // wasted on non-MN[0] — negligible vs region size). Must be set BEFORE
    // any varblock_table_offset / varblock_blocks_base call so prefix-sum
    // base offsets bake in the correct absolute byte positions.
    const u64 budget_map_bytes =
        materialization_map_on ? static_cast<u64>(N) * sizeof(u32) : 0;
    u64 aligned_map_shift = 0;
    lib_assert(lavd::native::detail::checked_align_up(
                   budget_map_bytes,
                   lavd::native::OFFSET_TABLE_ENTRY_BYTES,
                   &aligned_map_shift),
               "LAVD-multi: budget-map alignment overflow");
    lavd::g_varblock_map_shift = static_cast<size_t>(aligned_map_shift);
    // Per-MN local-count from resolver, then per-MN tight prefix sum.
    lavd::g_varblock_offsets_per_mn.assign(num_mns, std::vector<u64>{});
    std::vector<u32> per_mn_pos(num_mns, 0);  // running local_slot per MN (= position)
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      const u32 L = native_export_plan.layout.resolver.local_count(mn_i);
      lavd::g_varblock_offsets_per_mn[mn_i].assign(static_cast<size_t>(L) + 1, 0);
      // base = varblock_blocks_base(L) for per-MN compact region
      lavd::g_varblock_offsets_per_mn[mn_i][0] = lavd::varblock_blocks_base(L);
    }
    // First pass: compute per-uid count, accumulate into per-MN prefix.
    for (u32 uid = 0; uid < N; ++uid) {
      const lavd::native::FixedL0ReadPlan rp = native_export_plan.layout.read_plan(uid);
      const u32 owner = rp.owner_mn;
      const u32 local = rp.local_slot;
      const auto& info = off_by_uid[uid];
      const byte_t* l0p = per_mn_scratch[info.mn] + info.offset + l0_off;
      u32 cnt_u = *reinterpret_cast<const u32*>(l0p);
      if (cnt_u > m_max0) cnt_u = m_max0;
      // Multi-MN budget: cold slots get a zero-stride entry — no on-MN
      // bytes, prefix sum just rolls forward. Search side picks them up
      // via Config::budget_map (CN-local mirror of compact_idx) and
      // routes to the existing cold fallback.
      const u32 step =
          (materialization_map_on && compact_idx[uid] == lavd::Config::COLD)
          ? 0u
          : static_cast<u32>(lavd::varblock_stride(cnt_u, dim, bits));
      lavd::g_varblock_offsets_per_mn[owner][local + 1] =
          lavd::g_varblock_offsets_per_mn[owner][local] + step;
      (void)per_mn_pos;  // unused — uid order suffices since resolver maps local in scan order
    }
    const auto map_placement =
        materialization_map_on
            ? lavd::native::BudgetMapPlacement::kMemoryNode0
            : lavd::native::BudgetMapPlacement::kNone;
    native_accounting =
        lavd::native::make_variable_physical_accounting_from_offset_tables(
            native_export_plan.layout.resolver, lavd::PARAMS_RESERVE,
            map_placement, aligned_map_shift,
            lavd::g_varblock_offsets_per_mn);
    lib_assert(native_accounting.valid,
               "LAVD-native: invalid variable-record physical accounting");
    if (byte_budget_on) {
      lib_assert(native_accounting.total_bytes_across_mns ==
                     byte_selection.admitted_bytes,
                 "LAVD exact-byte selection/accounting mismatch");
      lib_assert(native_accounting.total_bytes_across_mns <= byte_budget,
                 "LAVD exact-byte materialization exceeded requested cap");
    }
    native_descriptor = lavd::native::make_variable_descriptor(
        native_accounting, native_max_record_bytes, native_scoring_kind,
        native_scoring_bits, m_max0, native_colocated_degree,
        native_slot_only);
    lib_assert(native_descriptor.valid,
               "LAVD-native: failed to construct variable-record v3 descriptor");
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      require_region_range(mn_i, 0,
                           native_accounting.per_mn[mn_i].total_region_bytes,
                           "variable packed shard");
    }
  }

  // Preflight the legacy global-slot sparse layout as well. It is not the
  // native packed representation, but it must still fail before publishing
  // any sidecar bytes when the configured MR is too small.
  std::vector<u64> legacy_high_water;
  if (!native_packed_write) {
    legacy_high_water.assign(num_mns, lavd::PARAMS_RESERVE);
    for (u32 uid = 0; uid < N; ++uid) {
      u64 record_end = 0;
      const u64 record_offset = lavd::region_offset(uid, stride);
      lib_assert(lavd::native::detail::checked_add(
                     record_offset, static_cast<u64>(stride), &record_end),
                 "LAVD-multi: sparse-layout offset overflow");
      legacy_high_water[off_by_uid[uid].mn] =
          std::max(legacy_high_water[off_by_uid[uid].mn], record_end);
    }
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      require_region_range(mn_i, 0, legacy_high_water[mn_i],
                           "legacy sparse shard");
    }
  }
  t_metadata->stop();

  auto t_materialize = timing.create_enroll("lavd_build_materialize");
  t_materialize->start();
  std::vector<u64> actual_write_bytes_per_mn(num_mns, 0);
  evidence::Fnv1a64 map_hasher;
  std::vector<evidence::Fnv1a64> offset_table_hashers(num_mns);
  std::vector<evidence::Fnv1a64> record_payload_hashers(num_mns);
  std::string header_hash;
  std::string descriptor_hash;

  // Publish metadata payloads only after the entire physical layout has
  // passed its capacity preflight. The descriptor header itself is committed
  // after all tables and records, below.
  if (materialization_map_on) {
    const QP& qp_m = ctx->qps[0]->qp;
    MemoryRegionToken* nbh_tok_m = ctx->get_remote_neighborhood_mrt(0);
    LocalMemoryRegion map_mr{context, compact_idx.data(),
                             static_cast<size_t>(N) * sizeof(u32)};
    const u64 MCHUNK = (1ull << 30) / sizeof(u32);
    for (u64 off = 0; off < N; off += MCHUNK) {
      const u32 cnt = static_cast<u32>(std::min<u64>(MCHUNK, N - off));
      const u64 remote_offset =
          lavd::map_region_offset() + off * sizeof(u32);
      const u64 bytes = static_cast<u64>(cnt) * sizeof(u32);
      require_region_range(0, remote_offset, bytes, "budget map");
      map_hasher.update(compact_idx.data() + off,
                        static_cast<size_t>(bytes));
      qp_m->post_send(reinterpret_cast<u64>(compact_idx.data() + off),
                      static_cast<u32>(bytes), map_mr.get_lkey(),
                      IBV_WR_RDMA_WRITE, true, false, nbh_tok_m,
                      remote_offset, 0, 0);
      context.poll_send_cq_until_completion();
      actual_write_bytes_per_mn[0] += bytes;
    }
    std::cerr << "[LAVD][multi][budget] compact_idx map written to MN[0]: N="
              << N << " H=" << H << std::endl;
  }

  if (vb_on) {
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      const u32 L = native_export_plan.layout.resolver.local_count(mn_i);
      const size_t tbytes = lavd::varblock_table_bytes(L);
      LocalMemoryRegion ot_mr{
          context, lavd::g_varblock_offsets_per_mn[mn_i].data(), tbytes};
      const QP& qp_t = ctx->qps[mn_i]->qp;
      MemoryRegionToken* nbh_tok_t =
          ctx->get_remote_neighborhood_mrt(mn_i);
      const u64 TCHUNK = 1ull << 30;
      for (u64 off = 0; off < tbytes; off += TCHUNK) {
        const u32 cb =
            static_cast<u32>(std::min<u64>(TCHUNK, tbytes - off));
        const u64 remote_offset = lavd::varblock_table_offset() + off;
        require_region_range(mn_i, remote_offset, cb, "offset table");
        const auto* table_bytes = reinterpret_cast<const byte_t*>(
            lavd::g_varblock_offsets_per_mn[mn_i].data()) + off;
        offset_table_hashers[mn_i].update(table_bytes, cb);
        qp_t->post_send(
            reinterpret_cast<u64>(
                reinterpret_cast<byte_t*>(
                    lavd::g_varblock_offsets_per_mn[mn_i].data()) + off),
            cb, ot_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
            nbh_tok_t, remote_offset, 0, 0);
        context.poll_send_cq_until_completion();
        actual_write_bytes_per_mn[mn_i] += cb;
      }
      const u64 total = lavd::g_varblock_offsets_per_mn[mn_i][L];
      const u64 useful = total - lavd::varblock_blocks_base(L);
      std::cerr << "[LAVD][varblock][multi] MN[" << mn_i
                << "] offset_table written: L=" << L
                << " table_bytes=" << tbytes
                << " blocks_total=" << useful
                << " avg_stride=" << (useful / std::max<u64>(1, L))
                << std::endl;
    }
  }

  // ---- Step 5: assemble + publish Slab records. Native variable records
  // can be assembled in parallel into bounded contiguous staging windows and
  // published with one WRITE per window. Other layouts retain the established
  // one-record path until they have an equally direct physical-order mapping.
  size_t total_edges = 0;
  std::vector<size_t> per_mn_blocks(num_mns, 0);
  u64 record_write_posts = 0;
  const bool staged_build = native_packed_write && vb_on && [] {
    const char* env = std::getenv("SHINE_LAVD_STAGED_BUILD");
    return env != nullptr && std::atoi(env) != 0;
  }();
  if (staged_build) {
    auto* stage = static_cast<byte_t*>(
        std::aligned_alloc(64, lavd::STAGED_IO_BYTES));
    lib_assert(stage != nullptr, "LAVD-multi: staging-buffer allocation failed");
    const u32 build_workers = lavd::build_worker_count(N);
    const size_t worker_record_bytes =
        lavd::aligned_allocation_bytes(stride, 64u);
    lib_assert(worker_record_bytes != 0,
               "LAVD-multi: invalid aligned record-scratch size");
    std::vector<byte_t*> worker_record_scratch(build_workers, nullptr);
    for (u32 worker = 0; worker < build_workers; ++worker) {
      worker_record_scratch[worker] = static_cast<byte_t*>(
          std::aligned_alloc(64, worker_record_bytes));
      lib_assert(worker_record_scratch[worker] != nullptr,
                 "LAVD-multi: record-scratch allocation failed");
    }
    auto t_assemble = timing.create_enroll("lavd_build_record_assemble");
    auto t_publish = timing.create_enroll("lavd_build_record_publish");
    {
      LocalMemoryRegion stage_mr{context, stage, lavd::STAGED_IO_BYTES};
      for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
        const auto& offsets = lavd::g_varblock_offsets_per_mn[mn_i];
        const u32 local_slots = native_export_plan.layout.resolver.local_count(mn_i);
        lib_assert(offsets.size() == static_cast<size_t>(local_slots) + 1,
                   "LAVD-multi: invalid staged offset table");
        u32 cursor = 0;
        while (cursor < local_slots) {
          const auto range = lavd::next_staged_slot_range(offsets, cursor);
          lib_assert(range.valid,
                     "LAVD-multi: one variable record exceeds staging capacity");
          if (range.bytes == 0) {
            cursor = range.end_slot;
            break;
          }

          const u32 range_slots = range.end_slot - range.begin_slot;
          const u32 range_workers =
              std::min<u32>(build_workers, std::max<u32>(1, range_slots));
          std::vector<size_t> worker_edges(range_workers, 0);
          std::vector<size_t> worker_blocks(range_workers, 0);
          t_assemble->start();
          lavd::parallel_for_u32(
              range_slots, range_workers,
              [&](u32 begin, u32 end, u32 worker) {
                for (u32 relative = begin; relative < end; ++relative) {
                  const u32 local = range.begin_slot + relative;
                  const u64 record_bytes = offsets[local + 1] - offsets[local];
                  if (record_bytes == 0) continue;
                  lib_assert(record_bytes <= stride,
                             "LAVD-multi: variable record exceeds fixed upper bound");
                  const u32 uid = native_export_plan.layout.resolver.global_slot(
                      lavd::native::SlotRef{mn_i, local});
                  const auto& info = off_by_uid[uid];
                  const size_t stage_offset =
                      static_cast<size_t>(offsets[local] - range.remote_offset);
                  byte_t* record = worker_record_scratch[worker];
                  assemble_block_multi(
                      info, per_mn_scratch, qz, dim, bits, m_max0, l0_off,
                      comp_off, static_cast<size_t>(record_bytes),
                      record);
                  std::memcpy(stage + stage_offset, record,
                              static_cast<size_t>(record_bytes));
                  worker_edges[worker] += blk_count(record);
                  ++worker_blocks[worker];
                }
              });
          t_assemble->stop();
          for (u32 worker = 0; worker < range_workers; ++worker) {
            total_edges += worker_edges[worker];
            per_mn_blocks[mn_i] += worker_blocks[worker];
          }

          require_region_range(mn_i, range.remote_offset, range.bytes,
                               "staged Slab records");
          record_payload_hashers[mn_i].update(stage, range.bytes);
          const QP& qp_mn = ctx->qps[mn_i]->qp;
          MemoryRegionToken* nbh_tok_mn =
              ctx->get_remote_neighborhood_mrt(mn_i);
          t_publish->start();
          qp_mn->post_send(reinterpret_cast<u64>(stage), range.bytes,
                           stage_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                           nbh_tok_mn, range.remote_offset, 0, 0);
          context.poll_send_cq_until_completion();
          t_publish->stop();
          actual_write_bytes_per_mn[mn_i] += range.bytes;
          ++record_write_posts;
          cursor = range.end_slot;
        }
      }
    }
    for (byte_t* record : worker_record_scratch) std::free(record);
    std::free(stage);
    std::cerr << "LAVD_BUILD_PUBLICATION {\"version\":1,\"mode\":\"staged\""
              << ",\"workers\":" << build_workers
              << ",\"staging_bytes\":" << lavd::STAGED_IO_BYTES
              << ",\"records\":" << H
              << ",\"record_write_posts\":" << record_write_posts << "}"
              << std::endl;
  } else {
    const size_t blk_capacity = lavd::aligned_allocation_bytes(stride, 64u);
    lib_assert(blk_capacity != 0,
               "LAVD-multi: invalid aligned block-buffer size");
    auto* blk =
        static_cast<byte_t*>(std::aligned_alloc(64, blk_capacity));
    lib_assert(blk != nullptr, "LAVD-multi: block-buffer allocation failed");
    LocalMemoryRegion blk_mr{context, blk, blk_capacity};
    for (u32 uid = 0; uid < N; ++uid) {
      // Multi-MN budget: cold uids hold no block on MN (offset_table delta
      // = 0 set in the prefix-sum pass above). Skip the write.
      if (materialization_map_on &&
          compact_idx[uid] == lavd::Config::COLD) {
        continue;
      }
      const auto& info = off_by_uid[uid];
      assemble_block_multi(info, per_mn_scratch, qz, dim, bits,
                           m_max0, l0_off, comp_off, stride, blk);
      const u32 cnt = blk_count(blk);
      total_edges += cnt;
      const lavd::native::FixedL0ReadPlan native_rp =
          native_packed_write
              ? native_export_plan.layout.read_plan(uid)
              : lavd::native::FixedL0ReadPlan{
                    info.mn, uid, lavd::region_offset(uid, stride),
                    static_cast<u32>(stride)};
      // Var-block override: write at per-MN var-block offset, tight bytes only.
      const u64 remote_off =
          vb_on ? lavd::varblock_offset_mn(native_rp.owner_mn,
                                           native_rp.local_slot)
                : native_rp.remote_offset;
      const u32 write_bytes =
          vb_on ? static_cast<u32>(lavd::varblock_stride(cnt, dim, bits))
                : static_cast<u32>(stride);
      require_region_range(native_rp.owner_mn, remote_off, write_bytes,
                           "Slab record");
      record_payload_hashers[native_rp.owner_mn].update(blk, write_bytes);
      const QP& qp_mn = ctx->qps[native_rp.owner_mn]->qp;
      MemoryRegionToken* nbh_tok_mn =
          ctx->get_remote_neighborhood_mrt(native_rp.owner_mn);
      qp_mn->post_send(reinterpret_cast<u64>(blk), write_bytes,
                       blk_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                       nbh_tok_mn, remote_off, 0, 0);
      context.poll_send_cq_until_completion();
      ++per_mn_blocks[native_rp.owner_mn];
      actual_write_bytes_per_mn[native_rp.owner_mn] += write_bytes;
      ++record_write_posts;
    }
    std::free(blk);
    std::cerr << "LAVD_BUILD_PUBLICATION {\"version\":1,\"mode\":\"serial\""
              << ",\"workers\":1,\"staging_bytes\":0"
              << ",\"records\":" << H
              << ",\"record_write_posts\":" << record_write_posts << "}"
              << std::endl;
  }

  if (native_packed_write) {
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      const auto& shard = native_accounting.per_mn[mn_i];
      const u64 expected_payload_bytes =
          shard.budget_map_bytes + shard.offset_table_bytes +
          shard.record_bytes;
      lib_assert(actual_write_bytes_per_mn[mn_i] == expected_payload_bytes,
                 "LAVD-native: precommit payload-byte mismatch on MN[" +
                     std::to_string(mn_i) + "]");
    }
  }

  // Header publication is the sidecar commit point. A reader cannot observe a
  // valid v3 descriptor until all map/table/record writes have completed.
  {
    auto* ph =
        static_cast<byte_t*>(std::aligned_alloc(64, lavd::PARAMS_RESERVE));
    lib_assert(ph != nullptr, "LAVD-multi: params-header allocation failed");
    std::memset(ph, 0, lavd::PARAMS_RESERVE);
    qz.write_params(ph);
    *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF) = N;
    *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF + 4) = H;
    if (rb > 0) {
      const size_t ro = lavd::rabitq_hdr_off(params_bytes);
      *reinterpret_cast<u32*>(ph + ro) = rb;
      *reinterpret_cast<u32*>(ph + ro + 4) = dim;
      std::memcpy(ph + ro + 8, lavd::Config::rabitq.c.data(),
                  static_cast<size_t>(dim) * sizeof(f32));
    }
    if (native_packed_write) {
      lib_assert(native_descriptor.valid,
                 "LAVD-native: descriptor missing at commit");
      lib_assert(lavd::native::write_descriptor(ph, native_descriptor),
                 "LAVD-native: descriptor/header mismatch at commit");
      evidence::Fnv1a64 header_hasher;
      header_hasher.update(ph, lavd::PARAMS_RESERVE);
      header_hash = header_hasher.hex();
      evidence::Fnv1a64 descriptor_hasher;
      descriptor_hasher.update(ph + lavd::native::DESCRIPTOR_OFFSET,
                               lavd::native::DESCRIPTOR_BYTES);
      descriptor_hash = descriptor_hasher.hex();
    }
    {
      LocalMemoryRegion ph_mr{context, ph, lavd::PARAMS_RESERVE};
      const u32 header_mns = native_packed_write ? num_mns : 1;
      for (u32 hmn = 0; hmn < header_mns; ++hmn) {
        require_region_range(hmn, 0, lavd::PARAMS_RESERVE,
                             "committed header");
        const QP& qp_h = ctx->qps[hmn]->qp;
        MemoryRegionToken* nbh_tok_h =
            ctx->get_remote_neighborhood_mrt(hmn);
        qp_h->post_send(reinterpret_cast<u64>(ph),
                        static_cast<u32>(lavd::PARAMS_RESERVE),
                        ph_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                        nbh_tok_h, 0, 0, 0);
        context.poll_send_cq_until_completion();
        actual_write_bytes_per_mn[hmn] += lavd::PARAMS_RESERVE;
      }
    }
    std::free(ph);
  }

  if (native_packed_write) {
    const char* record_layout = vb_on ? "variable" : "fixed";
    const char* scoring_code =
        native_scoring_kind == lavd::native::ScoringCodeKind::kRaBitQ
            ? "rabitq"
            : (native_scoring_kind ==
                       lavd::native::ScoringCodeKind::kProductQuantizer
                   ? "pq"
                   : "scalar");
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      const auto& shard = native_accounting.per_mn[mn_i];
      std::cerr
          << "LAVD_PHYSICAL_ACCOUNTING {\"descriptor_version\":"
          << native_descriptor.version << ",\"policy\":\""
          << lavd::native::policy_name(native_policy)
          << "\",\"record_layout\":\"" << record_layout
          << "\",\"scoring_code\":\"" << scoring_code
          << "\",\"scoring_bits\":" << native_scoring_bits
          << ",\"max_record_bytes\":" << native_descriptor.block_stride
          << ",\"max_degree\":" << native_descriptor.max_degree
          << ",\"colocated_degree\":"
          << native_descriptor.colocated_degree
          << ",\"slot_only\":"
          << (native_descriptor.slot_only ? "true" : "false")
          << ",\"budget_map_required\":"
          << (materialization_map_on ? "true" : "false")
          << ",\"total_slots\":" << N << ",\"num_mns\":" << num_mns
          << ",\"mn\":" << mn_i
          << ",\"local_slots\":" << shard.local_slot_count
          << ",\"header_bytes\":" << shard.header_bytes
          << ",\"budget_map_bytes\":" << shard.budget_map_bytes
          << ",\"placement_padding_bytes\":"
          << shard.placement_padding_bytes
          << ",\"offset_table_bytes\":" << shard.offset_table_bytes
          << ",\"record_bytes\":" << shard.record_bytes
          << ",\"materialized_bytes\":" << shard.total_region_bytes
          << ",\"registered_bytes\":" << region_capacity
          << ",\"hash_version\":" << evidence::PHYSICAL_HASH_VERSION
          << ",\"hash_algorithm\":\""
          << evidence::PHYSICAL_HASH_ALGORITHM << "\""
          << ",\"hash_scope\":\"" << evidence::PHYSICAL_HASH_SCOPE << "\""
          << ",\"header_hash_scope\":\""
          << evidence::PHYSICAL_HEADER_HASH_SCOPE << "\""
          << ",\"descriptor_hash_scope\":\""
          << evidence::PHYSICAL_DESCRIPTOR_HASH_SCOPE << "\""
          << ",\"map_hash_scope\":\""
          << evidence::PHYSICAL_MAP_HASH_SCOPE << "\""
          << ",\"offset_table_hash_scope\":\""
          << evidence::PHYSICAL_OFFSET_TABLE_HASH_SCOPE << "\""
          << ",\"record_payload_hash_scope\":\""
          << evidence::PHYSICAL_RECORD_PAYLOAD_HASH_SCOPE << "\""
          << ",\"selected_uid_hash_scope\":\""
          << evidence::PHYSICAL_SELECTED_UID_HASH_SCOPE << "\""
          << ",\"budget_map_owner_mn\":"
          << evidence::PHYSICAL_BUDGET_MAP_OWNER_MN
          << ",\"header_hash\":\"" << header_hash << "\""
          << ",\"descriptor_hash\":\"" << descriptor_hash << "\""
          << ",\"map_hash\":\"" << map_hasher.hex() << "\""
          << ",\"offset_table_hash\":\""
          << offset_table_hashers[mn_i].hex() << "\""
          << ",\"record_payload_hash\":\""
          << record_payload_hashers[mn_i].hex() << "\""
          << ",\"selected_uid_hash\":\"" << selected_uid_hash << "\""
          << ",\"actual_write_bytes\":"
          << actual_write_bytes_per_mn[mn_i] << "}" << std::endl;
    }
  } else {
    const char* scoring_code =
        native_scoring_kind == lavd::native::ScoringCodeKind::kRaBitQ
            ? "rabitq"
            : (native_scoring_kind ==
                       lavd::native::ScoringCodeKind::kProductQuantizer
                   ? "pq"
                   : "scalar");
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) {
      const u64 record_bytes =
          static_cast<u64>(per_mn_blocks[mn_i]) * stride;
      const u64 placement_padding =
          legacy_high_water[mn_i] - lavd::PARAMS_RESERVE - record_bytes;
      std::cerr
          << "LAVD_PHYSICAL_ACCOUNTING {\"descriptor_version\":0"
          << ",\"policy\":\"authoritative_owner_global_slot\""
          << ",\"record_layout\":\"legacy_sparse_fixed\""
          << ",\"scoring_code\":\"" << scoring_code << "\""
          << ",\"scoring_bits\":" << native_scoring_bits
          << ",\"total_slots\":" << N << ",\"num_mns\":" << num_mns
          << ",\"mn\":" << mn_i
          << ",\"local_slots\":" << per_mn_blocks[mn_i]
          << ",\"header_bytes\":"
          << (mn_i == 0 ? lavd::PARAMS_RESERVE : 0)
          << ",\"budget_map_bytes\":0"
          << ",\"placement_padding_bytes\":" << placement_padding
          << ",\"offset_table_bytes\":0"
          << ",\"record_bytes\":" << record_bytes
          << ",\"materialized_bytes\":" << legacy_high_water[mn_i]
          << ",\"registered_bytes\":" << region_capacity
          << ",\"actual_write_bytes\":"
          << actual_write_bytes_per_mn[mn_i] << "}" << std::endl;
    }
  }
  t_materialize->stop();
  t->stop();

  std::cerr << "[LAVD][multi] build done: N=" << N << " edges=" << total_edges
            << " avg_deg=" << (double)total_edges / N;
  for (u32 mn = 0; mn < num_mns; ++mn) {
    std::cerr << " MN[" << mn << "]=" << per_mn_blocks[mn];
  }
  std::cerr << std::endl;

  // mbLAVD cold-path pre-filter: build the per-MN RemotePtr->uid reverse
  // map from the parsed off_by_uid. Lets the cold fallback dedup against
  // the slot bitmap *before* issuing the per-neighbor read+encode (cold
  // CPU dominates at small f). Only the initiator owns this map (clients
  // would have to walk the INDEX themselves); search code falls back to
  // the post-read dedup when rev_index_ready() is false.
  g_rev_index_per_mn.clear();
  if (materialization_map_on) {
    g_rev_index_per_mn.assign(num_mns, {});
    for (u32 mn_i = 0; mn_i < num_mns; ++mn_i) g_rev_index_per_mn[mn_i].reserve(N / num_mns + 16);
    for (u32 uid = 0; uid < N; ++uid) {
      const auto& info = off_by_uid[uid];
      g_rev_index_per_mn[info.mn].emplace(info.offset, uid);
    }
    std::cerr << "[LAVD][multi][prefilter] rev_index built: total="
              << N << " entries across " << num_mns << " MNs" << std::endl;
  }

  // SHC (Structural Hub Cache) inline build: when SHINE_LAVD_HUB_CACHE_K
  // is set AND budget mode is on (indeg-ranked compact_idx exists), pin
  // the top-K hot uids' fat blocks into the CN-resident hub cache. The
  // hub set is the head of compact_idx ordering -- it's the same indeg
  // top-K already paid for by the build pass. Under slot_only the block
  // is ~69 B, so K=50000 fits in 3.5 MB CN; under sq8 K=10000 fits in
  // ~46 MB. Search path's existing g_hub_cache.is_hub check turns each
  // hit into a memcpy (no RDMA).
  if (budget) {
    const char* hk = std::getenv("SHINE_LAVD_HUB_CACHE_K");
    const u32 K_req = hk ? static_cast<u32>(std::atoll(hk)) : 0u;
    if (K_req > 0) {
      const u32 K = std::min(K_req, H);
      // Invert compact_idx: hot_at_rank[rank] = uid such that compact_idx[uid] == rank.
      std::vector<u32> hot_at_rank(H, 0xFFFFFFFFu);
      for (u32 uid = 0; uid < N; ++uid) {
        if (compact_idx[uid] != lavd::Config::COLD && compact_idx[uid] < H) {
          hot_at_rank[compact_idx[uid]] = uid;
        }
      }
      // Use the build-time `stride` variable -- Config is not yet
      // initialized when the build runs (Config::init happens later in
      // read_params_init), so Config::stride() returns garbage here.
      const size_t shc_stride = stride;
      lavd::g_hub_cache.hub_map.assign(N, lavd::HubCacheS::INVALID);
      lavd::g_hub_cache.stride = shc_stride;
      lavd::g_hub_cache.n_hubs = K;
      lavd::g_hub_cache.blocks.assign(static_cast<size_t>(K) * shc_stride, 0);
      // Config not yet initialized here; use the build-time / env flags.
      const bool np_on = native_packed_write;
      const bool vb_on_local = lavd::varblock_on();
      u32 actually_cached = 0;
      for (u32 i = 0; i < K; ++i) {
        const u32 slot = hot_at_rank[i];
        if (slot >= N) continue;
        u32 read_mn = 0;
        size_t remote_off = lavd::region_offset(slot, stride);
        size_t read_bytes = stride;
        if (np_on) {
          const auto rp = native_export_plan.layout.read_plan(slot);
          read_mn = rp.owner_mn;
          if (vb_on_local) {
            remote_off = lavd::varblock_offset_mn(rp.owner_mn, rp.local_slot);
            read_bytes = lavd::varblock_size_mn(rp.owner_mn, rp.local_slot);
          } else {
            remote_off = rp.remote_offset;
            read_bytes = rp.read_bytes;
          }
        } else if (vb_on_local) {
          remote_off = lavd::varblock_offset(slot);
          read_bytes = lavd::varblock_size(slot);
        }
        if (read_bytes == 0) continue;
        lavd::g_hub_cache.hub_map[slot] = i;
        const QP& qp_h = ctx->qps[read_mn]->qp;
        MemoryRegionToken* tok_h = ctx->get_remote_neighborhood_mrt(read_mn);
        LocalMemoryRegion mr{context,
                             lavd::g_hub_cache.blocks.data() + static_cast<size_t>(i) * stride,
                             stride};
        qp_h->post_send(
            reinterpret_cast<u64>(lavd::g_hub_cache.blocks.data() + static_cast<size_t>(i) * stride),
            static_cast<u32>(read_bytes), mr.get_lkey(),
            IBV_WR_RDMA_READ, true, false, tok_h, remote_off, 0, 0);
        context.poll_send_cq_until_completion();
        ++actually_cached;
      }
      lavd::g_hub_cache.n_hubs = actually_cached;
      std::cerr << "[LAVD][multi][shc] K=" << K << " cached=" << actually_cached
                << " stride=" << stride
                << " bytes=" << (static_cast<u64>(actually_cached) * stride / 1024) << " KB"
                << std::endl;
    }
  }

  // Slot-only entry layout: build the CN-side qvec lookup table.
  // Two modes:
  //   tiered=off (default): packed table indexed by uid, size N * pqs.
  //     Cold neighbors of hot centers still get a valid qvec entry;
  //     has_qvec() always returns true, the in-block uses slot only.
  //   tiered=on (SHINE_LAVD_TIERED_CACHE=1) AND budget<1: packed table
  //     indexed by compact_idx[uid], size H * pqs. Cold uids have no
  //     entry; the search-side has_qvec() reports false for them and
  //     the cold-style read_node+encode path runs. This is the path
  //     that fits N=100M (f=0.05 -> 1.24 GB) and N=1B (f=0.001 ->
  //     248 MB) into the 10 GB CN budget; the full-table mode bursts
  //     at N=10M+.
  if (lavd::slot_only_on()) {
    const size_t pqs = lavd::slot_packed_qvec_size();
    lib_assert(pqs > 0, "SHINE_LAVD_SLOT_ONLY requires a quantizer with packed_qvec_size>0");
    // H==N is reader-visible as budget OFF, so the CN table must remain
    // uid-indexed even when an explicit byte cap happened to admit every row.
    const bool tiered = lavd::tiered_cache_on() && H < N;
    const size_t packed_n = tiered ? static_cast<size_t>(H) : static_cast<size_t>(N);
    lavd::g_uid_qvec_packed.assign(packed_n * pqs, 0);
    lavd::g_uid_to_rptr.assign(N, 0);
    for (u32 uid = 0; uid < N; ++uid) {
      const auto& info = off_by_uid[uid];
      const RemotePtr rp{info.mn, info.offset};
      lavd::g_uid_to_rptr[uid] = rp.raw_address;
      if (lavd::g_rabitq_b && uid < lavd::g_rabitq_normtab.size()) {
        size_t idx;
        if (tiered) {
          // Skip cold uids entirely: their qvec is never read on the
          // hot path (has_qvec=false -> read_node + encode fallback).
          if (compact_idx[uid] == lavd::Config::COLD) continue;
          idx = compact_idx[uid];
        } else {
          idx = uid;
        }
        u8* qv = lavd::g_uid_qvec_packed.data() + idx * pqs;
        *reinterpret_cast<f32*>(qv) = lavd::g_rabitq_normtab[uid];
        *reinterpret_cast<f32*>(qv + 4) = lavd::g_rabitq_dottab[uid];
        std::memcpy(qv + 8, &lavd::g_rabitq_codetab[static_cast<size_t>(uid) * lavd::g_rabitq_code],
                    lavd::g_rabitq_code);
      }
    }
    std::cerr << "[LAVD][multi][slot_only] CN tables built: N=" << N
              << " H=" << H << " tiered=" << tiered
              << " packed_qvec_bytes=" << (packed_n * pqs)
              << " rptr_bytes=" << (N * 8) << std::endl;
  }

  const char* crane_env = std::getenv("SHINE_CRANE");
  const bool retain_authoritative_snapshot =
      crane_env != nullptr && std::atoll(crane_env) != 0;
  if (retain_authoritative_snapshot) {
    std::vector<void*> snapshot_shards;
    snapshot_shards.reserve(num_mns);
    for (u32 mn = 0; mn < num_mns; ++mn) {
      snapshot_shards.push_back(per_mn_scratch[mn]);
      per_mn_scratch[mn] = nullptr;
    }
    const u64 entry_raw =
        *reinterpret_cast<const u64*>(
            static_cast<const byte_t*>(snapshot_shards[0]) + 8);
    lavd::publish_authoritative_snapshot(
        std::move(snapshot_shards), std::move(per_mn_bytes), entry_raw);
    std::cerr << "[LAVD][multi] retained authoritative snapshot for resident upper graph: shards="
              << num_mns << std::endl;
  } else {
    lavd::clear_authoritative_snapshot();
    for (u32 mn = 0; mn < num_mns; ++mn) {
      std::free(per_mn_scratch[mn]);
    }
  }
  return qz;
}

// Offline touched-record rematerialization control. The final authoritative
// HNSW is replayed as if uids [n_before, N) formed one insert batch. Before
// query admission, only {new uids} U {their final L0 neighbors} is rewritten
// in the already materialized fixed-stride sidecar. This measures affected
// records and validates byte identity; it is not concurrent insert handling,
// epoch publication, or crash-safe refresh.
template <typename Ctx>
inline u32 offline_rematerialize_control(Ctx* ctx, u32 mn, u32 bits,
                                         const Quantizer& qz, u32 n_before,
                                         timing::Timing& timing,
                                         bool release_mirror) {
  // Degree changes can shift every subsequent variable record, so this
  // control is intentionally fixed-stride only.
  lib_assert(!lavd::varblock_on(),
             "LAVD offline rematerialization: variable layout requires a full rebuild");
  Context& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);
  MemoryRegionToken* nbh_tok = ctx->get_remote_neighborhood_mrt(mn);

  const u32 dim = Node::DIM;
  const u32 m_max0 = (Node::NEIGHBORLIST_SIZE_ZERO - sizeof(u32)) / sizeof(u64);
  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;
  const size_t l0_off = Node::size_until_components();
  const size_t stride = block_stride(m_max0, dim, bits);

  // Optional persistent CN mirror: the first replay scans the index; later
  // replays fetch only the appended tail and touched node records. This is a
  // measurement optimization for repeated offline controls.
  static byte_t* M_s = nullptr;
  static size_t M_cap = 0;
  static LocalMemoryRegion* M_mr = nullptr;  // persistent registration
  static vec<u64> M_off;
  static u32 M_N = 0;
  static u64 M_idxb = 0;
  static bool M_have = false;
  const bool v2 = []{ const char* e = std::getenv("SHINE_LAVD_MAINT_V2");
                      return e && std::atoi(e) != 0; }();

  auto t = timing.create_enroll("lavd_maintain");
  t->start();

  auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
  lib_assert(hdr != nullptr, "LAVD rematerialization: header allocation failed");
  {
    LocalMemoryRegion hdr_mr{context, hdr, 64};
    qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(),
                  IBV_WR_RDMA_READ, true, false, idx_tok, 0, 0, 0);
    context.poll_send_cq_until_completion();
  }
  const u64 idx_bytes = *reinterpret_cast<u64*>(hdr);
  const size_t CHUNK = 64ull * 1024 * 1024;
  u64 read_bytes = 0;
  const char* mode = "init";

  if (!v2 || !M_have) {
    // ---- INIT (one-time; == the build cost): full read + parse ----
    mode = M_have ? "reinit" : "init";
    if (idx_bytes + 64 > M_cap) {
      delete M_mr;
      M_mr = nullptr;
      std::free(M_s);
      M_s = nullptr;
      M_cap = static_cast<size_t>((idx_bytes + 127) & ~u64{63});
      M_s = static_cast<byte_t*>(std::aligned_alloc(64, M_cap));
      lib_assert(M_s != nullptr,
                 "LAVD rematerialization: mirror allocation failed");
      M_mr = new LocalMemoryRegion{context, M_s, M_cap};
    }
    for (size_t off = 0; off < idx_bytes; off += CHUNK) {
      const u32 n = static_cast<u32>(std::min(CHUNK, idx_bytes - off));
      qp->post_send(reinterpret_cast<u64>(M_s + off), n, M_mr->get_lkey(),
                    IBV_WR_RDMA_READ, true, false, idx_tok, off, 0, 0);
      context.poll_send_cq_until_completion();
    }
    read_bytes = idx_bytes;
    M_off.assign(0, 0);
    M_off.reserve(1u << 20);
    u64 walk = 16;
    u32 max_uid = 0;
    while (walk < idx_bytes) {
      const u32 uid = *reinterpret_cast<u32*>(M_s + walk + Node::HEADER_SIZE);
      const u32 level = *reinterpret_cast<u32*>(M_s + walk + Node::HEADER_SIZE + sizeof(u32));
      size_t node_total =
        l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
      while (node_total % 8 != 0) node_total += 4;
      if (uid >= M_off.size()) M_off.resize(uid + 1, 0);
      M_off[uid] = walk;
      max_uid = std::max(max_uid, uid);
      walk += node_total;
    }
    M_N = max_uid + 1;
    M_idxb = idx_bytes;
    M_have = true;
    lib_assert(walk == idx_bytes, "LAVD maintain: node walk mismatch");
  } else {
    // ---- DIFF (per tick): only the grown tail (existing offsets are
    //      stable; HNSW appends) ----
    mode = "diff";
    const std::string_view growth_rejection =
      lavd::offline_mirror_growth_rejection(M_idxb, idx_bytes, M_cap);
    lib_assert(
      growth_rejection.empty(),
      "LAVD offline rematerialization rejected: " << growth_rejection);
    if (idx_bytes > M_idxb) {
      for (u64 off = M_idxb; off < idx_bytes; off += CHUNK) {
        const u32 n = static_cast<u32>(std::min<u64>(CHUNK, idx_bytes - off));
        qp->post_send(reinterpret_cast<u64>(M_s + off), n, M_mr->get_lkey(),
                      IBV_WR_RDMA_READ, true, false, idx_tok, off, 0, 0);
        context.poll_send_cq_until_completion();
      }
      read_bytes += idx_bytes - M_idxb;
      u64 walk = M_idxb;
      u32 max_uid = M_N - 1;
      while (walk < idx_bytes) {
        const u32 uid = *reinterpret_cast<u32*>(M_s + walk + Node::HEADER_SIZE);
        const u32 level = *reinterpret_cast<u32*>(M_s + walk + Node::HEADER_SIZE + sizeof(u32));
        size_t node_total =
          l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
        while (node_total % 8 != 0) node_total += 4;
        if (uid >= M_off.size()) M_off.resize(uid + 1, 0);
        M_off[uid] = walk;
        max_uid = std::max(max_uid, uid);
        walk += node_total;
      }
      M_N = max_uid + 1;
      M_idxb = idx_bytes;
    }
  }

  byte_t* scratch = M_s;
  vec<u64>& off_by_uid = M_off;
  const u32 N = M_N;
  lib_assert(N >= n_before, "LAVD maintain: index shrank?");

  // 4. touched = {new uids} INTO {their L0 neighbors}
  vec<u8> touched(N, 0);
  for (u32 x = n_before; x < N; ++x) {
    touched[x] = 1;
    const byte_t* l0 = scratch + off_by_uid[x] + l0_off;
    u32 cnt = *reinterpret_cast<const u32*>(l0);
    if (cnt > m_max0) cnt = m_max0;
    const u64* nptr = reinterpret_cast<const u64*>(l0 + sizeof(u32));
    for (u32 i = 0; i < cnt; ++i) {
      const u32 nb_uid =
        *reinterpret_cast<const u32*>(scratch + RemotePtr{nptr[i]}.byte_offset() + Node::HEADER_SIZE);
      if (nb_uid < N) touched[nb_uid] = 1;
    }
  }
  size_t n_touched = 0;
  for (u32 s = 0; s < N; ++s) n_touched += touched[s];

  // v2 DIFF: refresh ONLY the touched nodes' regions from the wire
  // (production: HNSW may have rewritten their L0 lists in place;
  // their neighbors' *components* are immutable and already mirrored).
  // Read volume = tail + Sum touched node sizes = O(K*M*node),
  // bounded by inserts -- NOT O(N).
  if (v2 && std::strcmp(mode, "diff") == 0) {
    for (u32 s = 0; s < N; ++s) {
      if (!touched[s]) continue;
      const u64 o = off_by_uid[s];
      const u32 level = *reinterpret_cast<u32*>(M_s + o + Node::HEADER_SIZE + sizeof(u32));
      size_t node_total =
        l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
      while (node_total % 8 != 0) node_total += 4;
      for (size_t off = 0; off < node_total; off += CHUNK) {
        const u32 n = static_cast<u32>(std::min<size_t>(CHUNK, node_total - off));
        qp->post_send(reinterpret_cast<u64>(M_s + o + off), n, M_mr->get_lkey(),
                      IBV_WR_RDMA_READ, true, false, idx_tok, o + off, 0, 0);
        context.poll_send_cq_until_completion();
      }
      read_bytes += node_total;
    }
  }

  const size_t blk_capacity = (stride + 127) & ~size_t{63};
  auto* blk = static_cast<byte_t*>(std::aligned_alloc(64, blk_capacity));
  lib_assert(blk != nullptr, "LAVD rematerialization: block allocation failed");

  {
    LocalMemoryRegion blk_mr{context, blk, blk_capacity};

    // The self-test invalidates each selected record before reconstruction,
    // preventing an unchanged full-build byte range from masking a bad write.
    if (const char* st = std::getenv("SHINE_LAVD_MAINTAIN_SELFTEST");
        st && std::atoi(st) != 0) {
      std::memset(blk, 0, stride);
      for (u32 slot = 0; slot < N; ++slot) {
        if (!touched[slot]) continue;
        qp->post_send(reinterpret_cast<u64>(blk), static_cast<u32>(stride),
                      blk_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                      nbh_tok, lavd::region_offset(slot, stride), 0, 0);
        context.poll_send_cq_until_completion();
      }
    }

    // Reassemble and overwrite only the selected fixed-stride records.
    for (u32 slot = 0; slot < N; ++slot) {
      if (!touched[slot]) continue;
      assemble_block(slot, scratch, off_by_uid, qz, dim, bits, m_max0,
                     l0_off, comp_off, stride, blk);
      qp->post_send(reinterpret_cast<u64>(blk), static_cast<u32>(stride),
                    blk_mr.get_lkey(), IBV_WR_RDMA_WRITE, true, false,
                    nbh_tok, lavd::region_offset(slot, stride), 0, 0);
      context.poll_send_cq_until_completion();
    }
  }
  t->stop();

  const u32 k_ins = N - n_before;
  const double rd_mb = read_bytes / 1e6;
  const double full_mb = idx_bytes / 1e6;
  std::cerr << "[LAVD][offline-rematerialize] mode=" << mode
            << " replayed_inserts=" << k_ins
            << " touched=" << n_touched
            << " blocks/insert=" << (k_ins ? (double)n_touched / k_ins : 0.0)
            << " write_amp=" << (k_ins ? (double)n_touched / k_ins : 0.0)
            << " read_MB=" << rd_mb << " full_idx_MB=" << full_mb
            << " read_frac=" << (full_mb ? rd_mb / full_mb : 0.0)
            << " (m_max0=" << m_max0 << ")" << std::endl;

  // Compare the entire sidecar with fresh assembly under the same quantizer.
  if (const char* st = std::getenv("SHINE_LAVD_MAINTAIN_SELFTEST"); st && std::atoi(st) != 0) {
    auto* rb = static_cast<byte_t*>(std::aligned_alloc(64, blk_capacity));
    auto* ex = static_cast<byte_t*>(std::aligned_alloc(64, blk_capacity));
    lib_assert(rb != nullptr && ex != nullptr,
               "LAVD rematerialization: verification allocation failed");
    u64 fails = 0;
    {
      LocalMemoryRegion rb_mr{context, rb, blk_capacity};
      for (u32 slot = 0; slot < N; ++slot) {
        qp->post_send(reinterpret_cast<u64>(rb), static_cast<u32>(stride),
                      rb_mr.get_lkey(), IBV_WR_RDMA_READ, true, false,
                      nbh_tok, lavd::region_offset(slot, stride), 0, 0);
        context.poll_send_cq_until_completion();
        assemble_block(slot, scratch, off_by_uid, qz, dim, bits, m_max0,
                       l0_off, comp_off, stride, ex);
        if (std::memcmp(rb, ex, stride) != 0) ++fails;
      }
    }
    std::cerr << "[LAVD][offline-rematerialize][selftest] slots=" << N
              << " mismatches=" << fails
              << (fails == 0 ? "  PASS (rewrite==full-materialization)"
                             : "  FAIL")
              << std::endl;
    std::free(rb);
    std::free(ex);
  }

  std::free(blk);
  std::free(hdr);
  if (release_mirror) {
    delete M_mr;
    M_mr = nullptr;
    std::free(M_s);
    M_s = nullptr;
    M_cap = 0;
    M_off.clear();
    M_off.shrink_to_fit();
    M_N = 0;
    M_idxb = 0;
    M_have = false;
  }
  return static_cast<u32>(n_touched);
}

// Called by EVERY CN (initiator + clients) AFTER the post-build barrier:
// RDMA-read the quantizer params the initiator wrote to the region
// header, then init lavd::Config. No CN-to-CN messaging — all CNs hold
// the 2nd-region token (Phase C). Makes multi-CN correct (every CN takes
// the LAVD path with identical params).
template <typename Ctx>
inline void read_params_init(Ctx* ctx, u32 mn, u32 bits, u32 m_max0, u32 rerank) {
  Context& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* nbh_tok = ctx->get_remote_neighborhood_mrt(mn);

  auto* ph = static_cast<byte_t*>(std::aligned_alloc(64, lavd::PARAMS_RESERVE));
  lib_assert(ph != nullptr, "LAVD: params-header allocation failed");
  {
    LocalMemoryRegion ph_mr{context, ph, lavd::PARAMS_RESERVE};
    qp->post_send(reinterpret_cast<u64>(ph),
                  static_cast<u32>(lavd::PARAMS_RESERVE), ph_mr.get_lkey(),
                  IBV_WR_RDMA_READ, true, false, nbh_tok,
                  /*remote_off*/ 0, 0, 0);
    context.poll_send_cq_until_completion();
  }

  Quantizer qz;
  qz.read_params(ph);  // sets dim+bits from the header
  lib_assert(qz.dim() == Node::DIM && qz.bits() == bits, "LAVD: param header mismatch");
  lavd::Config::init(bits, Node::DIM, m_max0, rerank, qz);

  // RaBitQ (multi-CN): only the initiator ran fit(); a querying CN reconstructs
  // the identical encoder from the shipped centroid (rotation deterministic
  // from seed, levels from dim/B). Driven off the header B field so a client
  // need not even pass the env -- the region itself says RaBitQ is active.
  {
    const size_t ro = lavd::rabitq_hdr_off(qz.params_bytes());
    const u32 hb = *reinterpret_cast<u32*>(ph + ro);
    if (hb == 1 || hb == 2 || hb == 3 || hb == 4) {
      const u32 hd = *reinterpret_cast<u32*>(ph + ro + 4);
      lib_assert(hd == qz.dim(), "LAVD: RaBitQ header dim mismatch");
      lavd::RaBitQ r;
      const bool rotation_reused = r.init_shared_reusing_rotation(
          hd, hb, reinterpret_cast<const f32*>(ph + ro + 8),
          lavd::Config::rabitq);
      lavd::Config::set_rabitq(hb, r);
      std::cerr << "[LAVD][rabitq] CN reconstructed encoder from header: B=" << hb
                << " dim=" << hd << " code_bytes=" << r.code_bytes()
                << " rotation_reused=" << (rotation_reused ? "true" : "false")
                << std::endl;
    }
  }

  // memory-bounded LAVD: read N/H from the header tail; if a budget is
  // active (H<N) RDMA-read the slot->compact-index map into a persistent
  // CN-resident array and wire Config::compact_idx. H==N => OFF.
  const u32 N = *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF);
  const u32 Hh = *reinterpret_cast<u32*>(ph + lavd::HDR_COUNTS_OFF + 4);
  // Always publish N to Config (used by SHC and other CN-side modules even
  // when budget mode is OFF; set_budget_map only fires for budget mode).
  lavd::Config::total_n = N;
  const u64 region_capacity = lavd::Config::region_capacity_bytes();
  lib_assert(
      lavd::is_valid_region_capacity_bytes(region_capacity,
        lavd::REGION_CAPACITY_EXPLICIT_MAX_BYTES),
      "LAVD: invalid registered neighborhood-region capacity at read init");
  const auto require_region_range = [region_capacity](u32 read_mn, u64 offset,
                                                       u64 bytes,
                                                       const char* object) {
    lib_assert(lavd::region_range_fits(offset, bytes, region_capacity),
               std::string("LAVD: ") + object + " on MN[" +
                   std::to_string(read_mn) + "] exceeds registered region");
  };

  // A valid native descriptor is authoritative. It activates packed addressing
  // on every CN and restores the variable/fixed layout without requiring an
  // out-of-band read-path environment variable.
  lavd::native::NativeDescriptor native_desc;
  bool descriptor_active = false;
  const bool descriptor_present = lavd::native::descriptor_present(ph);
  if (descriptor_present) {
    native_desc = lavd::native::read_descriptor(ph);
    lib_assert(native_desc.valid,
               "LAVD-native: descriptor present but invalid");
    lib_assert(native_desc.total_slots == N,
               "LAVD-native: descriptor N mismatch");
    lib_assert(native_desc.header_bytes == lavd::PARAMS_RESERVE,
               "LAVD-native: descriptor header-size mismatch");
    const u32 connected_mns = static_cast<u32>(ctx->qps.size());
    lib_assert(native_desc.num_mns == connected_mns,
               "LAVD-native: descriptor/connection MN-count mismatch");

    if (connected_mns > 1) {
      auto* replica = static_cast<byte_t*>(
          std::aligned_alloc(64, lavd::PARAMS_RESERVE));
      lib_assert(replica != nullptr,
                 "LAVD-native: replicated-header allocation failed");
      {
        LocalMemoryRegion replica_mr{context, replica, lavd::PARAMS_RESERVE};
        for (u32 mn_i = 0; mn_i < connected_mns; ++mn_i) {
          if (mn_i == mn) continue;
          const QP& replica_qp = ctx->qps[mn_i]->qp;
          MemoryRegionToken* replica_tok =
              ctx->get_remote_neighborhood_mrt(mn_i);
          replica_qp->post_send(
              reinterpret_cast<u64>(replica),
              static_cast<u32>(lavd::PARAMS_RESERVE),
              replica_mr.get_lkey(), IBV_WR_RDMA_READ, true, false,
              replica_tok, /*remote_off*/ 0, 0, 0);
          context.poll_send_cq_until_completion();
          lib_assert(
              lavd::native::replicated_header_matches(ph, replica),
              "LAVD-native: replicated MN headers do not match");
        }
      }
      std::free(replica);
    }

    if (native_desc.version == lavd::native::DESCRIPTOR_VERSION) {
      const lavd::native::ScoringCodeKind expected_kind =
          lavd::Config::rabitq_on()
              ? lavd::native::ScoringCodeKind::kRaBitQ
              : (lavd::Config::pq_on()
                     ? lavd::native::ScoringCodeKind::kProductQuantizer
                     : lavd::native::ScoringCodeKind::kScalarQuantizer);
      const u32 expected_bits =
          lavd::Config::rabitq_on()
              ? lavd::Config::rabitq_b
              : (lavd::Config::pq_on() ? lavd::Config::pq_m : bits);
      lib_assert(native_desc.scoring_code_kind == expected_kind &&
                     native_desc.scoring_code_bits == expected_bits,
                 "LAVD-native: descriptor scoring-code mismatch");
      const bool expected_slot_only = lavd::slot_only_on();
      const u32 expected_colocated_degree =
          expected_slot_only ? 0u : lavd::layout_coloc_d(m_max0);
      const size_t configured_stride = lavd::Config::stride();
      lib_assert(configured_stride <= std::numeric_limits<u32>::max(),
                 "LAVD-native: descriptor ABI cannot represent the configured stride");
      lib_assert(lavd::native::descriptor_record_abi_matches(
                     native_desc, static_cast<u32>(configured_stride),
                     m_max0, expected_colocated_degree, expected_slot_only),
                 "LAVD-native: descriptor record-ABI mismatch");
    }

    const auto resolver = lavd::native::make_resolver(
        N, native_desc.num_mns, native_desc.policy);
    lavd::native::PackedL0Layout layout;
    if (native_desc.record_layout ==
        lavd::native::RecordLayout::kFixedStride) {
      lib_assert(lavd::native::try_layout_from_descriptor(native_desc, &layout),
                 "LAVD-native: cannot restore fixed layout");
      lavd::set_varblock_on(false);
      lavd::g_varblock_map_shift = 0;
      for (u32 mn_i = 0; mn_i < native_desc.num_mns; ++mn_i) {
        require_region_range(mn_i, 0, layout.shard_bytes(mn_i),
                             "fixed packed shard");
      }
    } else {
      lib_assert(native_desc.version == lavd::native::DESCRIPTOR_VERSION,
                 "LAVD-native: variable layout requires a v3 descriptor");
      layout = lavd::native::PackedL0Layout::fixed_stride(
          resolver, native_desc.block_stride,
          native_desc.header_bytes);
      lavd::set_varblock_on(true);
      lavd::g_varblock_map_shift =
          static_cast<size_t>(native_desc.map_shift_bytes);
    }
    lavd::Config::set_native_packed_l0(layout);
    descriptor_active = true;
    std::cerr << "[LAVD][native] packed addressing restored from descriptor: N="
              << N << " mns=" << native_desc.num_mns
              << " policy=" << lavd::native::policy_name(native_desc.policy)
              << " layout="
              << (native_desc.record_layout ==
                          lavd::native::RecordLayout::kVariableRecords
                      ? "variable"
                      : "fixed")
              << " descriptor_version=" << native_desc.version
              << " packed_bytes=" << native_desc.packed_region_bytes
              << " sparse_bytes=" << native_desc.sparse_region_bytes
              << std::endl;
  } else {
    const char* np_read_env =
        std::getenv("SHINE_LAVD_NATIVE_PACKED_READ");
    const bool env_native =
        np_read_env != nullptr && std::atoi(np_read_env) != 0;
    if (N > 0 && env_native) {
      u32 native_mns = 1;
      if (const char* ms = std::getenv("SHINE_LAVD_NATIVE_MNS")) {
        const long parsed = std::atol(ms);
        if (parsed > 0) native_mns = static_cast<u32>(parsed);
      }
      lib_assert(native_mns == static_cast<u32>(ctx->qps.size()),
                 "LAVD-native: env MN-count/connection mismatch");
      const char* policy_env = std::getenv("SHINE_LAVD_NATIVE_POLICY");
      const bool range_policy =
          policy_env && std::strcmp(policy_env, "range") == 0;
      const auto resolver =
          range_policy
              ? lavd::native::OwnerResolver::contiguous_range(N, native_mns)
              : lavd::native::OwnerResolver::block_cyclic(N, native_mns);
      const auto layout = lavd::native::PackedL0Layout::fixed_stride(
          resolver, static_cast<u32>(lavd::Config::stride()),
          lavd::PARAMS_RESERVE);
      lavd::Config::set_native_packed_l0(layout);
      for (u32 mn_i = 0; mn_i < native_mns; ++mn_i) {
        require_region_range(mn_i, 0, layout.shard_bytes(mn_i),
                             "env fixed packed shard");
      }
      std::cerr << "[LAVD][native] packed addressing enabled by legacy env: N="
                << N << " mns=" << native_mns
                << " policy=" << lavd::native::policy_name(resolver.policy)
                << std::endl;
    }

    // Legacy layouts have no descriptor-carried map shift. Retain their
    // historical exact N*4 convention for compatibility.
    lavd::g_varblock_map_shift =
        (N > 0 && Hh < N && lavd::varblock_on())
            ? static_cast<size_t>(N) * sizeof(u32)
            : 0;
  }

  if (N > 0 && Hh < N) {
    g_budget_map.assign(N, lavd::Config::COLD);
    LocalMemoryRegion m_mr{context, g_budget_map.data(),
                           static_cast<size_t>(N) * sizeof(u32)};
    const u64 MCHUNK = (1ull << 30) / sizeof(u32);
    for (u64 off = 0; off < N; off += MCHUNK) {
      const u32 cnt = static_cast<u32>(std::min<u64>(MCHUNK, N - off));
      const u64 remote_offset =
          lavd::map_region_offset() + off * sizeof(u32);
      const u64 bytes = static_cast<u64>(cnt) * sizeof(u32);
      require_region_range(0, remote_offset, bytes, "budget map");
      qp->post_send(reinterpret_cast<u64>(g_budget_map.data() + off),
                    static_cast<u32>(bytes), m_mr.get_lkey(),
                    IBV_WR_RDMA_READ, true, false, nbh_tok,
                    remote_offset, 0, 0);
      context.poll_send_cq_until_completion();
    }
    lavd::Config::set_budget_map(N, Hh, g_budget_map.data());
    std::cerr << "[LAVD][budget] CN loaded map: N=" << N << " H=" << Hh
              << " (" << (100.0 * Hh / N) << "% co-located)" << std::endl;
  }

  // var-block: RDMA-READ per-slot offset_table into CN-local cache so the
  // search-side read_neighborhood can compute fat-read offset/size without
  // a per-hop metadata round-trip. Idempotent: on the initiator CN the
  // table was already populated by build_neighborhood (commit 2); the
  // empty() check skips the re-read. On non-initiator CNs the table is
  // empty after construction => fetch from MN @ varblock_table_offset().
  // Size: (N+1) * 8 bytes; 8 MB for 1 M nodes -- chunked at <1 GB.
  // Single-MN path only: combined native mode is handled below.
  if (N > 0 && lavd::varblock_on() && !lavd::Config::native_packed_on()
      && lavd::g_varblock_offsets.empty()) {
    lavd::g_varblock_offsets.assign(static_cast<size_t>(N) + 1, 0);
    const size_t tbytes = lavd::varblock_table_bytes(N);
    LocalMemoryRegion ot_mr{context, lavd::g_varblock_offsets.data(), tbytes};
    const u64 TCHUNK = 1ull << 30;
    for (u64 off = 0; off < tbytes; off += TCHUNK) {
      const u32 cb = static_cast<u32>(std::min<u64>(TCHUNK, tbytes - off));
      const u64 remote_offset = lavd::varblock_table_offset() + off;
      require_region_range(mn, remote_offset, cb, "offset table");
      qp->post_send(reinterpret_cast<u64>(
                        reinterpret_cast<byte_t*>(lavd::g_varblock_offsets.data()) + off),
                    cb, ot_mr.get_lkey(),
                    IBV_WR_RDMA_READ, true, false, nbh_tok,
                    remote_offset, 0, 0);
      context.poll_send_cq_until_completion();
    }
    lib_assert(lavd::g_varblock_offsets.front() ==
                   lavd::varblock_blocks_base(N),
               "LAVD: single-MN offset-table base mismatch");
    for (u32 slot = 0; slot < N; ++slot) {
      lib_assert(lavd::g_varblock_offsets[slot + 1] >=
                     lavd::g_varblock_offsets[slot],
                 "LAVD: non-monotonic single-MN offset table");
      const u64 delta = lavd::g_varblock_offsets[slot + 1] -
                        lavd::g_varblock_offsets[slot];
      lib_assert(delta > 0 && delta <= lavd::Config::stride(),
                 "LAVD: invalid single-MN variable-record length");
    }
    require_region_range(mn, 0, lavd::g_varblock_offsets.back(),
                         "variable-record shard");
    const u64 blocks_total = lavd::g_varblock_offsets[N] - lavd::varblock_blocks_base(N);
    std::cerr << "[LAVD][varblock] CN loaded offset_table: N=" << N
              << " table_bytes=" << tbytes
              << " blocks_total=" << blocks_total
              << " avg_stride=" << (blocks_total / std::max<u64>(1, N)) << std::endl;
  }

  (void)descriptor_active;
  std::free(ph);

  // Multi-MN combined var-block: pull each MN's per-MN offset_table when
  // (varblock_on && native_packed_on). Idempotent on initiator
  // (g_varblock_offsets_per_mn already populated by
  // build_neighborhood_multi); on non-initiator CNs the table is empty
  // -> RDMA-READ from every MN. Each MN's offset_table is local-slot-
  // indexed, size (L_mn + 1) * 8 bytes.
  if (N > 0 && lavd::varblock_on() && lavd::Config::native_packed_on()
      && lavd::g_varblock_offsets_per_mn.empty()) {
    const auto& resolver = lavd::Config::native_l0.resolver;
    const u32 nm = resolver.num_mns;
    lavd::g_varblock_offsets_per_mn.assign(nm, std::vector<u64>{});
    for (u32 mn_i = 0; mn_i < nm; ++mn_i) {
      const u32 L = resolver.local_count(mn_i);
      lavd::g_varblock_offsets_per_mn[mn_i].assign(static_cast<size_t>(L) + 1, 0);
      const size_t tbytes = lavd::varblock_table_bytes(L);
      require_region_range(mn_i, lavd::varblock_table_offset(), tbytes,
                           "offset table");
      LocalMemoryRegion ot_mr{context, lavd::g_varblock_offsets_per_mn[mn_i].data(), tbytes};
      const QP& qp_t = ctx->qps[mn_i]->qp;
      MemoryRegionToken* nbh_tok_t = ctx->get_remote_neighborhood_mrt(mn_i);
      const u64 TCHUNK = 1ull << 30;
      for (u64 off = 0; off < tbytes; off += TCHUNK) {
        const u32 cb = static_cast<u32>(std::min<u64>(TCHUNK, tbytes - off));
        const u64 remote_offset = lavd::varblock_table_offset() + off;
        require_region_range(mn_i, remote_offset, cb, "offset table");
        qp_t->post_send(reinterpret_cast<u64>(
                            reinterpret_cast<byte_t*>(lavd::g_varblock_offsets_per_mn[mn_i].data()) + off),
                        cb, ot_mr.get_lkey(),
                        IBV_WR_RDMA_READ, true, false, nbh_tok_t,
                        remote_offset, 0, 0);
        context.poll_send_cq_until_completion();
      }
      const u64 useful = lavd::g_varblock_offsets_per_mn[mn_i][L] - lavd::varblock_blocks_base(L);
      std::cerr << "[LAVD][varblock][multi] CN loaded MN[" << mn_i << "] offset_table: L=" << L
                << " table_bytes=" << tbytes
                << " blocks_total=" << useful
                << " avg_stride=" << (useful / std::max<u64>(1, L)) << std::endl;
    }
  }
  if (N > 0 && lavd::varblock_on() && lavd::Config::native_packed_on()) {
    const auto& resolver = lavd::Config::native_l0.resolver;
    const u64 descriptor_record_bound =
        native_desc.valid &&
                native_desc.version == lavd::native::DESCRIPTOR_VERSION
            ? native_desc.block_stride
            : lavd::Config::stride();
    lib_assert(lavd::g_varblock_offsets_per_mn.size() == resolver.num_mns,
               "LAVD-native: missing per-MN offset tables");
    for (u32 mn_i = 0; mn_i < resolver.num_mns; ++mn_i) {
      const u32 L = resolver.local_count(mn_i);
      const auto& offsets = lavd::g_varblock_offsets_per_mn[mn_i];
      lib_assert(offsets.size() == static_cast<size_t>(L) + 1,
                 "LAVD-native: offset-table length mismatch");
      const u64 expected_base =
          native_desc.valid &&
                  native_desc.record_layout ==
                      lavd::native::RecordLayout::kVariableRecords
              ? lavd::native::descriptor_record_region_offset(native_desc,
                                                               mn_i)
              : lavd::varblock_blocks_base(L);
      lib_assert(offsets.front() == expected_base,
                 "LAVD-native: offset-table base mismatch");
      for (u32 local = 0; local < L; ++local) {
        lib_assert(offsets[local + 1] >= offsets[local],
                   "LAVD-native: non-monotonic offset table");
        const u64 delta = offsets[local + 1] - offsets[local];
        lib_assert(delta <= descriptor_record_bound,
                   "LAVD-native: variable record exceeds descriptor bound");
        if (delta == 0) {
          const u32 global_slot = resolver.global_slot(
              lavd::native::SlotRef{mn_i, local});
          lib_assert(lavd::Config::budget_on() &&
                         !lavd::Config::is_hot(global_slot),
                     "LAVD-native: zero record for a non-cold slot");
        }
      }
      require_region_range(mn_i, 0, offsets.back(),
                           "variable packed shard");
    }
  }
}

// PATCH 4 (cold-code-table fix, Track A): populate g_rabitq_codetab on a
// non-initiator CN so the cold-fallback fast path in hnsw.hh can lookup
// per-uid RaBitQ codes instead of re-rotating each fp32 vector at query time.
// On the initiator the codetab is already filled inside build_neighborhood
// (single-MN) / build_neighborhood_multi (multi-MN); this helper is a no-op
// in that case (early-return on non-empty codetab). For non-initiators with
// RaBitQ active and an empty codetab, this is the entry point for a future
// multi-CN bulk-read implementation; for the single-CN test matrix the
// non-initiator path is never exercised (num_clients=1 ⇒ initiator only).
template <typename Ctx>
inline void fill_rabitq_codetab(Ctx* ctx, u32 mn) {
  (void)ctx; (void)mn;
  if (lavd::g_rabitq_b == 0) return;                        // RaBitQ off ⇒ nothing to fill
  if (!lavd::g_rabitq_codetab.empty()) return;              // initiator already filled ⇒ no-op
  // multi-CN non-initiator path: would RDMA-read all node components and
  // re-encode locally. Not exercised by the single-CN test matrix; leaving
  // codetab empty here keeps the fast path silently fall back to rb.encode()
  // via the (nb_uid < normtab.size()) guard in enc lambda.
  std::cerr << "[LAVD][rabitq] non-initiator CN: codetab not pre-populated; "
               "cold-fallback fast path inactive (using rb.encode())" << std::endl;
}

// Structural Hub Cache loader (env GB_HUB_CACHE_LIST=<path>).
// Reads a binary hub list file (u32 n + u32[n] slots), allocates a CN-local
// buffer, and RDMA-reads each hub's fat block from MN 0 into it. Searches
// then bypass RDMA for these slots (lookup in g_hub_cache). Static and
// workload-agnostic at runtime; the hub list is chosen offline from a
// HUB_PROFILE access histogram, so the cache is "workload-aware by design"
// while the runtime path stays zero-overhead (dense array + memcpy).
template <typename Ctx>
inline void load_hub_cache_init(Ctx* ctx, u32 mn, u32 N, size_t stride) {
  const char* path = std::getenv("GB_HUB_CACHE_LIST");
  if (!path || !path[0]) return;
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    std::cerr << "[HUB_CACHE] WARN: cannot open " << path << ", skip" << std::endl;
    return;
  }
  u32 n_hubs = 0;
  f.read(reinterpret_cast<char*>(&n_hubs), sizeof(u32));
  if (!f || n_hubs == 0 || n_hubs > N) {
    std::cerr << "[HUB_CACHE] WARN: bad n_hubs=" << n_hubs << " (N=" << N << "), skip" << std::endl;
    return;
  }
  std::vector<u32> hub_slots(n_hubs);
  f.read(reinterpret_cast<char*>(hub_slots.data()), n_hubs * sizeof(u32));
  if (!f) {
    std::cerr << "[HUB_CACHE] WARN: read error, skip" << std::endl;
    return;
  }
  g_hub_cache.hub_map.assign(N, HubCacheS::INVALID);
  g_hub_cache.stride = stride;
  g_hub_cache.n_hubs = n_hubs;
  g_hub_cache.blocks.assign(static_cast<size_t>(n_hubs) * stride, 0);
  Context& context = ctx->context;
  // Updated SHC loader: route each hub slot through the active LAVD
  // address mechanism. Three cases composed:
  //   1) native_packed_on (multi-MN combined): slot -> (owner_mn,
  //      local_slot) via Config::native_l0.read_plan(slot).
  //   2) varblock_on: byte offset comes from g_varblock_offsets_per_mn
  //      (combined) or g_varblock_offsets (single-MN), with actual size.
  //   3) Otherwise the original fixed-stride single-MN sparse path.
  // The hub_cache.blocks slot buffer is sized to the full fixed stride
  // upper bound; the var-block read fills only count*entry bytes, the
  // rest stays zero. Neighborhood::count() reads from the block header,
  // so the slack tail is harmless.
  const bool np_on = lavd::Config::native_packed_on();
  const bool vb_on = lavd::varblock_on();
  for (u32 i = 0; i < n_hubs; ++i) {
    const u32 slot = hub_slots[i];
    if (slot >= N) continue;
    g_hub_cache.hub_map[slot] = i;

    u32 read_mn = mn;
    size_t remote_off = lavd::region_offset(slot, stride);
    size_t read_bytes = stride;

    if (np_on) {
      const auto rp = lavd::Config::native_l0.read_plan(slot);
      read_mn = rp.owner_mn;
      if (vb_on) {
        remote_off = lavd::varblock_offset_mn(rp.owner_mn, rp.local_slot);
        read_bytes = lavd::varblock_size_mn(rp.owner_mn, rp.local_slot);
      } else {
        remote_off = rp.remote_offset;
        read_bytes = rp.read_bytes;
      }
    } else if (vb_on) {
      remote_off = lavd::varblock_offset(slot);
      read_bytes = lavd::varblock_size(slot);
    }

    if (read_bytes == 0) {
      // Cold slot under budget — no block to cache. Mark INVALID and
      // skip the RDMA; the search path will fall back to the cold path.
      g_hub_cache.hub_map[slot] = HubCacheS::INVALID;
      continue;
    }

    const QP& qp_h = ctx->qps[read_mn]->qp;
    MemoryRegionToken* tok_h = ctx->get_remote_neighborhood_mrt(read_mn);
    LocalMemoryRegion mr{context,
                         g_hub_cache.blocks.data() + static_cast<size_t>(i) * stride,
                         stride};
    qp_h->post_send(
        reinterpret_cast<u64>(g_hub_cache.blocks.data() + static_cast<size_t>(i) * stride),
        static_cast<u32>(read_bytes), mr.get_lkey(),
        IBV_WR_RDMA_READ, true, false, tok_h, remote_off, 0, 0);
    context.poll_send_cq_until_completion();
  }
  std::cerr << "[HUB_CACHE] loaded " << n_hubs << " hub blocks ("
            << (static_cast<u64>(n_hubs) * stride / 1024) << " KB) "
            << (np_on ? "native_packed " : "") << (vb_on ? "varblock " : "") << std::endl;
}

}  // namespace lavd
