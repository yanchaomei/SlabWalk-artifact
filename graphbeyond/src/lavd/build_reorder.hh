#pragma once

// GraphBeyond reorder-not-replicate — CN-side build pass.
//
// Mirrors build_neighborhood steps 1-3 (read free_ptr, bulk-RDMA-READ the
// index region into a CN scratch, parse -> off_by_uid + L0 lists). Then reads
// a precomputed vector-cluster block_of map (Python k-means, balanced sub-
// blocks) and assembles VARIABLE-SIZE blocks: each node is stored EXACTLY ONCE
// (uid, l0_count, rptr into the INDEX region for the fp32 rerank, its vector
// fp32|sq8, and its level-0 neighbor uids). The initiator RDMA-WRITES the
// block_of map, the absolute offset table, and every block into the MN's 2nd
// region (passive MN). Every CN then loads the map + offtab back (reorder_load,
// mirrors read_params_init). Memory == 1x (no LAVD replication). v1: single MN.
//
// Gated by SHINE_LAVD_REORDER_BLOCKOF=<file>; SHINE_LAVD_RB_SQ8=1 stores sq8
// block vectors (4x smaller; fp32 rerank is the recall safety net).

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <random>
#include <vector>

#include <library/memory_region.hh>

#include "common/quant.hh"
#include "common/timing.hh"
#include "lavd/config.hh"
#include "lavd/layout.hh"
#include "lavd/reorder_layout.hh"
#include "lavd/staged_io.hh"
#include "node/node.hh"
#include "remote_pointer.hh"
#include "shared_context.hh"

namespace lavd {

// Persistent CN-resident reorder maps (mirror g_budget_map). Lifetime =
// process; pointed to by Config::block_of / Config::block_off.
inline std::vector<u32> g_block_of;
inline std::vector<u64> g_block_off;

// block_of file: [u32 N][u32 n_blocks] then N x u32 block id (uid order).
inline void read_blockof_file(const char* path, std::vector<u32>& bo, u32& N, u32& nb) {
  FILE* f = std::fopen(path, "rb");
  lib_assert(f != nullptr, "reorder: cannot open block_of file");
  u32 hdr[2];
  lib_assert(std::fread(hdr, 4, 2, f) == 2, "reorder: bad block_of header");
  N = hdr[0];
  nb = hdr[1];
  bo.resize(N);
  lib_assert(std::fread(bo.data(), 4, N, f) == N, "reorder: short block_of body");
  std::fclose(f);
}

// Returns the fitted quantizer (the search path encodes the query with the
// IDENTICAL params for the sq8 approx; fp32 mode keeps it for header parity).
template <typename Ctx>
inline Quantizer build_reorder_region(Ctx* ctx, u32 mn, u32 bits, timing::Timing& timing) {
  Context& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);
  MemoryRegionToken* nbh_tok = ctx->get_remote_neighborhood_mrt(mn);

  const u32 dim = Node::DIM;
  const u32 m_max0 = (Node::NEIGHBORLIST_SIZE_ZERO - sizeof(u32)) / sizeof(u64);
  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;  // 16
  const size_t l0_off = Node::size_until_components();          // hdr+meta+DIM*4
  const bool sq8 = Config::rb_sq8_from_env();
  const size_t vbytes = sq8 ? static_cast<size_t>(dim) : static_cast<size_t>(dim) * 4;

  auto t = timing.create_enroll("lavd_reorder_build");
  t->start();

  // ---- 1. read free_ptr ----
  auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
  LocalMemoryRegion hdr_mr{context, hdr, 64};
  qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(), IBV_WR_RDMA_READ,
                true, false, idx_tok, 0, 0, 0);
  context.poll_send_cq_until_completion();
  const u64 free_ptr = *reinterpret_cast<u64*>(hdr);
  lib_assert(free_ptr > 16 && free_ptr < (1ull << 48), "reorder: bad free_ptr");

  // ---- 2. bulk-read the whole index region into a registered scratch ----
  const size_t idx_bytes = free_ptr;
  const size_t scratch_capacity = lavd::aligned_allocation_bytes(idx_bytes, 64u);
  lib_assert(scratch_capacity != 0, "reorder: invalid scratch capacity");
  auto* scratch =
      static_cast<byte_t*>(std::aligned_alloc(64, scratch_capacity));
  lib_assert(scratch != nullptr, "reorder: scratch alloc failed");
  LocalMemoryRegion scratch_mr{context, scratch, scratch_capacity};
  const size_t CHUNK = 64ull * 1024 * 1024;
  for (size_t off = 0; off < idx_bytes; off += CHUNK) {
    const u32 n = static_cast<u32>(std::min(CHUNK, idx_bytes - off));
    qp->post_send(reinterpret_cast<u64>(scratch + off), n, scratch_mr.get_lkey(),
                  IBV_WR_RDMA_READ, true, false, idx_tok, off, 0, 0);
    context.poll_send_cq_until_completion();
  }

  // ---- 3. parse: walk nodes, record uid -> byte offset (== build.hh) ----
  vec<u64> off_by_uid;
  off_by_uid.reserve(1u << 20);
  u64 walk = 16;
  u32 max_uid = 0;
  while (walk < idx_bytes) {
    const u32 uid = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE);
    const u32 level = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE + sizeof(u32));
    size_t node_total =
      l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(level) * Node::NEIGHBORLIST_SIZE;
    while (node_total % 8 != 0) node_total += 4;
    if (uid >= off_by_uid.size()) off_by_uid.resize(uid + 1, 0);
    off_by_uid[uid] = walk;
    max_uid = std::max(max_uid, uid);
    walk += node_total;
  }
  const u32 N = max_uid + 1;
  lib_assert(walk == idx_bytes, "reorder: node walk mismatch");

  // ---- 4. read the precomputed block_of map; build per-block membership ----
  const char* bo_path = Config::reorder_blockof_path();
  lib_assert(bo_path != nullptr, "reorder: SHINE_LAVD_REORDER_BLOCKOF unset");
  std::vector<u32> block_of;
  u32 bo_N = 0, n_blocks = 0;
  read_blockof_file(bo_path, block_of, bo_N, n_blocks);
  lib_assert(bo_N == N, "reorder: block_of N != index N");

  // membership in uid order (deterministic) so the load path's offset table
  // (recomputed-free: we RDMA it back) and the assembled blocks agree.
  std::vector<std::vector<u32>> members(n_blocks);
  std::vector<u32> bcnt(n_blocks, 0);
  for (u32 u = 0; u < N; ++u) ++bcnt[block_of[u]];
  for (u32 b = 0; b < n_blocks; ++b) members[b].reserve(bcnt[b]);
  for (u32 u = 0; u < N; ++u) members[block_of[u]].push_back(u);

  // ---- 5. absolute offset table; blocks start after [params][map][offtab] ----
  std::vector<u64> block_off(n_blocks + 1);
  const u64 base = rb_blocks_base(N, n_blocks);
  block_off[0] = base;
  for (u32 b = 0; b < n_blocks; ++b)
    block_off[b + 1] = block_off[b] + rb_block_bytes(static_cast<u32>(members[b].size()), m_max0, vbytes);
  const u64 region_bytes = block_off[n_blocks];
  lib_assert(region_bytes <= Config::region_capacity_bytes(),
             "reorder: region exceeds registered MN neighborhood capacity");
  size_t max_block_bytes = 0;
  for (u32 b = 0; b < n_blocks; ++b)
    max_block_bytes = std::max<size_t>(max_block_bytes, block_off[b + 1] - block_off[b]);

  // ---- 6. fit quantizer + write params header (offset 0). N at HDR_COUNTS_OFF
  //          with H=N (budget OFF); n_blocks + sq8 at RB_HDR_OFF. ----
  Quantizer qz;
  {
    const u32 SAMPLE = std::min<u32>(N, 100000);
    vec<element_t> samp(static_cast<size_t>(SAMPLE) * dim);
    for (u32 i = 0; i < SAMPLE; ++i)
      std::memcpy(samp.data() + static_cast<size_t>(i) * dim, scratch + off_by_uid[i] + comp_off,
                  dim * sizeof(element_t));
    qz.fit(dim, bits, samp.data(), SAMPLE);
  }
  {
    const size_t pb = qz.params_bytes();
    lib_assert(pb <= PARAMS_RESERVE - 16, "reorder: params exceed reserve");
    auto* ph = static_cast<byte_t*>(std::aligned_alloc(64, PARAMS_RESERVE));
    std::memset(ph, 0, PARAMS_RESERVE);
    qz.write_params(ph);
    *reinterpret_cast<u32*>(ph + HDR_COUNTS_OFF) = N;      // budget OFF (H==N)
    *reinterpret_cast<u32*>(ph + HDR_COUNTS_OFF + 4) = N;
    *reinterpret_cast<u32*>(ph + RB_HDR_OFF) = n_blocks;
    *reinterpret_cast<u32*>(ph + RB_HDR_OFF + 4) = sq8 ? 1u : 0u;
    LocalMemoryRegion ph_mr{context, ph, PARAMS_RESERVE};
    qp->post_send(reinterpret_cast<u64>(ph), static_cast<u32>(PARAMS_RESERVE), ph_mr.get_lkey(),
                  IBV_WR_RDMA_WRITE, true, false, nbh_tok, 0, 0, 0);
    context.poll_send_cq_until_completion();
    std::free(ph);
  }

  // ---- 7. RDMA-write block_of map (N u32) + offset table (n_blocks+1 u64) ----
  {
    LocalMemoryRegion bo_mr{context, block_of.data(), static_cast<size_t>(N) * sizeof(u32)};
    const u64 MCHUNK = (1ull << 30) / sizeof(u32);
    for (u64 off = 0; off < N; off += MCHUNK) {
      const u32 cnt = static_cast<u32>(std::min<u64>(MCHUNK, N - off));
      qp->post_send(reinterpret_cast<u64>(block_of.data() + off),
                    static_cast<u32>(cnt * sizeof(u32)), bo_mr.get_lkey(), IBV_WR_RDMA_WRITE,
                    true, false, nbh_tok, rb_map_off() + off * sizeof(u32), 0, 0);
      context.poll_send_cq_until_completion();
    }
  }
  {
    LocalMemoryRegion ot_mr{context, block_off.data(), (n_blocks + 1) * sizeof(u64)};
    qp->post_send(reinterpret_cast<u64>(block_off.data()),
                  static_cast<u32>((n_blocks + 1) * sizeof(u64)), ot_mr.get_lkey(),
                  IBV_WR_RDMA_WRITE, true, false, nbh_tok, rb_offtab_off(N), 0, 0);
    context.poll_send_cq_until_completion();
  }

  // ---- 8. assemble + RDMA-WRITE each block (max-sized reusable buffer) ----
  const size_t block_capacity =
      lavd::aligned_allocation_bytes(max_block_bytes, 64u);
  lib_assert(block_capacity != 0, "reorder: invalid block capacity");
  auto* blk = static_cast<byte_t*>(std::aligned_alloc(64, block_capacity));
  lib_assert(blk != nullptr, "reorder: block alloc failed");
  LocalMemoryRegion blk_mr{context, blk, block_capacity};
  u64 total_edges = 0;
  for (u32 b = 0; b < n_blocks; ++b) {
    const u32 nn = static_cast<u32>(members[b].size());
    const size_t bsz = block_off[b + 1] - block_off[b];
    std::memset(blk, 0, bsz);
    rb_n_nodes(blk) = nn;
    for (u32 i = 0; i < nn; ++i) {
      const u32 uid = members[b][i];
      byte_t* e = rb_entry(blk, i, m_max0, vbytes);
      re_uid(e) = uid;
      re_rptr(e) = RemotePtr{mn, off_by_uid[uid]}.raw_address;
      // vector: fp32 copy | sq8 encode (same qz as the query encode)
      const element_t* v = reinterpret_cast<const element_t*>(scratch + off_by_uid[uid] + comp_off);
      if (sq8) qz.encode(v, reinterpret_cast<u8*>(re_vec(e)));
      else std::memcpy(re_vec(e), v, static_cast<size_t>(dim) * sizeof(element_t));
      // L0 neighbor uids (resolve each stored RemotePtr -> uid)
      const byte_t* l0 = scratch + off_by_uid[uid] + l0_off;
      u32 cnt = *reinterpret_cast<const u32*>(l0);
      if (cnt > m_max0) cnt = m_max0;
      re_l0count(e) = cnt;
      const u64* np = reinterpret_cast<const u64*>(l0 + sizeof(u32));
      u32* dst = re_nbr(e, vbytes);
      for (u32 j = 0; j < cnt; ++j)
        dst[j] = *reinterpret_cast<const u32*>(scratch + RemotePtr{np[j]}.byte_offset() + Node::HEADER_SIZE);
      total_edges += cnt;
    }
    qp->post_send(reinterpret_cast<u64>(blk), static_cast<u32>(bsz), blk_mr.get_lkey(),
                  IBV_WR_RDMA_WRITE, true, false, nbh_tok, block_off[b], 0, 0);
    context.poll_send_cq_until_completion();
  }
  t->stop();

  std::cerr << "[REORDER] build done: N=" << N << " n_blocks=" << n_blocks
            << " m_max0=" << m_max0 << " vbytes=" << vbytes << (sq8 ? " (sq8)" : " (fp32)")
            << " region=" << region_bytes << "B (1x, no replication)"
            << " max_block=" << max_block_bytes << "B"
            << " avg_deg=" << (double)total_edges / std::max<u32>(1, N) << std::endl;

  // ---- selftest (SHINE_LAVD_REORDER_SELFTEST=1): read back random blocks,
  //      assert n_nodes + per-node uid/rptr/neighbor-uids match. ----
  if (const char* st = std::getenv("SHINE_LAVD_REORDER_SELFTEST"); st && std::atoi(st) != 0) {
    auto* rb = static_cast<byte_t*>(std::aligned_alloc(64, block_capacity));
    lib_assert(rb != nullptr, "reorder: selftest block alloc failed");
    LocalMemoryRegion rb_mr{context, rb, block_capacity};
    u32 checked = 0, fails = 0;
    std::mt19937 rng(2026);
    for (u32 c = 0; c < std::min<u32>(n_blocks, 64); ++c) {
      const u32 b = rng() % n_blocks;
      const size_t bsz = block_off[b + 1] - block_off[b];
      qp->post_send(reinterpret_cast<u64>(rb), static_cast<u32>(bsz), rb_mr.get_lkey(),
                    IBV_WR_RDMA_READ, true, false, nbh_tok, block_off[b], 0, 0);
      context.poll_send_cq_until_completion();
      bool ok = (rb_n_nodes(rb) == members[b].size());
      for (u32 i = 0; ok && i < members[b].size(); ++i) {
        const byte_t* e = rb_entry(rb, i, m_max0, vbytes);
        if (re_uid(e) != members[b][i]) { ok = false; break; }
        if (re_rptr(e) != RemotePtr{mn, off_by_uid[members[b][i]]}.raw_address) { ok = false; break; }
      }
      ++checked;
      if (!ok) ++fails;
    }
    std::cerr << "[REORDER][selftest] checked=" << checked << " fails=" << fails
              << (fails == 0 ? "  PASS" : "  FAIL") << std::endl;
    std::free(rb);
  }

  std::free(blk);
  std::free(scratch);
  std::free(hdr);
  return qz;
}

// Called by EVERY CN after the post-build barrier: RDMA-read n_blocks/sq8 from
// the params header, then the block_of map + offset table into persistent CN
// arrays, and wire Config::set_reorder. Mirrors read_params_init.
template <typename Ctx>
inline void reorder_load(Ctx* ctx, u32 mn) {
  Context& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* nbh_tok = ctx->get_remote_neighborhood_mrt(mn);

  auto* ph = static_cast<byte_t*>(std::aligned_alloc(64, PARAMS_RESERVE));
  LocalMemoryRegion ph_mr{context, ph, PARAMS_RESERVE};
  qp->post_send(reinterpret_cast<u64>(ph), static_cast<u32>(PARAMS_RESERVE), ph_mr.get_lkey(),
                IBV_WR_RDMA_READ, true, false, nbh_tok, 0, 0, 0);
  context.poll_send_cq_until_completion();
  const u32 N = *reinterpret_cast<u32*>(ph + HDR_COUNTS_OFF);
  const u32 n_blocks = *reinterpret_cast<u32*>(ph + RB_HDR_OFF);
  const bool sq8 = *reinterpret_cast<u32*>(ph + RB_HDR_OFF + 4) != 0;
  std::free(ph);

  g_block_of.assign(N, 0);
  g_block_off.assign(n_blocks + 1, 0);
  {
    LocalMemoryRegion bo_mr{context, g_block_of.data(), static_cast<size_t>(N) * sizeof(u32)};
    const u64 MCHUNK = (1ull << 30) / sizeof(u32);
    for (u64 off = 0; off < N; off += MCHUNK) {
      const u32 cnt = static_cast<u32>(std::min<u64>(MCHUNK, N - off));
      qp->post_send(reinterpret_cast<u64>(g_block_of.data() + off),
                    static_cast<u32>(cnt * sizeof(u32)), bo_mr.get_lkey(), IBV_WR_RDMA_READ,
                    true, false, nbh_tok, rb_map_off() + off * sizeof(u32), 0, 0);
      context.poll_send_cq_until_completion();
    }
  }
  {
    LocalMemoryRegion ot_mr{context, g_block_off.data(), (n_blocks + 1) * sizeof(u64)};
    qp->post_send(reinterpret_cast<u64>(g_block_off.data()),
                  static_cast<u32>((n_blocks + 1) * sizeof(u64)), ot_mr.get_lkey(),
                  IBV_WR_RDMA_READ, true, false, nbh_tok, rb_offtab_off(N), 0, 0);
    context.poll_send_cq_until_completion();
  }

  size_t maxbb = 0;
  for (u32 b = 0; b < n_blocks; ++b)
    maxbb = std::max<size_t>(maxbb, g_block_off[b + 1] - g_block_off[b]);
  // mean block size for the eps record (informational)
  const u32 eps = n_blocks ? static_cast<u32>(N / n_blocks) : 0;
  Config::rb_sq8 = sq8;  // set before set_reorder so rb_vbytes() is correct
  Config::set_reorder(eps, n_blocks, sq8, g_block_of.data(), g_block_off.data(), maxbb);
  std::cerr << "[REORDER] CN loaded map: N=" << N << " n_blocks=" << n_blocks
            << " eps~" << eps << (sq8 ? " sq8" : " fp32")
            << " max_block=" << maxbb << "B" << std::endl;
}

}  // namespace lavd
