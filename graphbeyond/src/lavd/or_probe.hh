#pragma once

// GraphBeyond — Phase-0 OR (Overlap Ratio) feasibility probe for the
// "reorder-not-replicate" block layout (Starling-adapted for RDMA).
//
// THE GATE: before writing any reorder build/search code, measure whether
// the HNSW level-0 graph admits high-OR blocking. OR = fraction of each
// node's L0 neighbors that land in the SAME block after a locality reorder.
// High OR => one block read per hop serves the fanout WITHOUT replication
// (op-collapse at 1x memory); low OR => cross-block reads kill the op-
// collapse and the whole idea is infeasible (a publishable negative result).
//
// Reuses the EXACT parsed graph from build_neighborhood (scratch +
// off_by_uid + L0 lists) so there is zero format drift. Gated by
// SHINE_LAVD_OR_PROBE=1; prints the OR table for eps in {16,32,64,128}
// under BNP (greedy frontier-fill) and BNP+BNF (frequency refinement),
// then std::exit(0) (probe-only run; the region is never built).

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <vector>

#include <library/types.hh>
#include "node/node.hh"
#include "remote_pointer.hh"

namespace lavd {

// Build the level-0 adjacency (uid -> neighbor uids), resolving each
// neighbor RemotePtr -> uid exactly as assemble_block does.
inline std::vector<std::vector<u32>> or_build_adj(const byte_t* scratch,
                                                  const std::vector<u64>& off_by_uid,
                                                  u32 N, size_t l0_off, u32 m_max0) {
  std::vector<std::vector<u32>> adj(N);
  for (u32 u = 0; u < N; ++u) {
    const byte_t* l0 = scratch + off_by_uid[u] + l0_off;
    u32 cnt = *reinterpret_cast<const u32*>(l0);
    if (cnt > m_max0) cnt = m_max0;
    const u64* np = reinterpret_cast<const u64*>(l0 + sizeof(u32));
    adj[u].reserve(cnt);
    for (u32 i = 0; i < cnt; ++i) {
      const u32 v = *reinterpret_cast<const u32*>(
        scratch + RemotePtr{np[i]}.byte_offset() + Node::HEADER_SIZE);
      if (v < N) adj[u].push_back(v);
    }
  }
  return adj;
}

inline double or_compute(const std::vector<std::vector<u32>>& adj,
                         const std::vector<u32>& block_of) {
  u64 in = 0, tot = 0;
  for (u32 u = 0; u < adj.size(); ++u)
    for (u32 v : adj[u]) { ++tot; if (block_of[v] == block_of[u]) ++in; }
  return tot ? static_cast<double>(in) / static_cast<double>(tot) : 0.0;
}

// BNP: deterministic global BFS seed order from the entry point; open a
// block, frontier-BFS-fill it with unassigned graph-local nodes until eps.
inline std::vector<u32> or_bnp(const std::vector<std::vector<u32>>& adj, u32 N,
                               u32 ep_uid, u32 eps, u32& n_blocks) {
  constexpr u32 UNSET = 0xFFFFFFFFu;
  std::vector<u32> block_of(N, UNSET);
  // global BFS order (deterministic) so seeds are graph-ordered
  std::vector<u32> order; order.reserve(N);
  std::vector<char> seen(N, 0);
  { std::vector<u32> q; q.push_back(ep_uid); seen[ep_uid] = 1; size_t h = 0;
    while (h < q.size()) { u32 x = q[h++]; order.push_back(x);
      for (u32 w : adj[x]) if (!seen[w]) { seen[w] = 1; q.push_back(w); } }
    for (u32 u = 0; u < N; ++u) if (!seen[u]) order.push_back(u); }
  u32 b = 0;
  for (u32 s : order) {
    if (block_of[s] != UNSET) continue;
    std::vector<u32> fq; fq.push_back(s); size_t fh = 0; u32 fill = 0;
    while (fh < fq.size() && fill < eps) {
      u32 x = fq[fh++];
      if (block_of[x] != UNSET) continue;
      block_of[x] = b; ++fill;
      for (u32 w : adj[x]) if (block_of[w] == UNSET) fq.push_back(w);
    }
    ++b;
  }
  n_blocks = b;
  return block_of;
}

// BNF: reassign each node to the block holding the most of its neighbors,
// subject to a soft capacity cap (eps + slack); a few deterministic sweeps.
inline void or_bnf(const std::vector<std::vector<u32>>& adj, u32 N, u32 eps,
                   u32 n_blocks, std::vector<u32>& block_of, u32 sweeps = 3) {
  const u32 cap = eps + eps / 4;  // 25% slack to allow moves
  std::vector<u32> bcount(n_blocks, 0);
  for (u32 u = 0; u < N; ++u) ++bcount[block_of[u]];
  std::vector<u32> tally(n_blocks, 0);
  for (u32 sw = 0; sw < sweeps; ++sw) {
    bool moved = false;
    for (u32 u = 0; u < N; ++u) {
      if (adj[u].empty()) continue;
      // count neighbors per block (reset only touched entries)
      std::vector<u32> touched;
      u32 best = block_of[u], bestc = 0;
      for (u32 v : adj[u]) { u32 bv = block_of[v]; if (tally[bv]++ == 0) touched.push_back(bv); }
      for (u32 bv : touched) if (tally[bv] > bestc && (bv == block_of[u] || bcount[bv] < cap)) { bestc = tally[bv]; best = bv; }
      for (u32 bv : touched) tally[bv] = 0;
      if (best != block_of[u] && bcount[best] < cap) {
        --bcount[block_of[u]]; ++bcount[best]; block_of[u] = best; moved = true;
      }
    }
    if (!moved) break;
  }
}

// Dump the L0 adjacency + entry uid to a binary file for offline
// block-search simulation (the decisive reads+recall gate before the C++
// build). Format: [u32 N][u32 entry_uid] then per uid: [u32 deg][u32 nbr...].
// uid == base.fbin row (HNSW insertion order), so Python pairs adj[uid]
// with vector[uid] directly.
inline void run_adj_dump(const byte_t* scratch, const std::vector<u64>& off_by_uid,
                         u32 N, size_t l0_off, u32 m_max0, const char* path) {
  auto adj = or_build_adj(scratch, off_by_uid, N, l0_off, m_max0);
  const u64 ep_raw = *reinterpret_cast<const u64*>(scratch + 8);
  const u32 ep_uid = *reinterpret_cast<const u32*>(
    scratch + RemotePtr{ep_raw}.byte_offset() + Node::HEADER_SIZE);
  FILE* f = std::fopen(path, "wb");
  if (!f) { std::cerr << "[ADJ_DUMP] cannot open " << path << std::endl; std::exit(1); }
  std::fwrite(&N, 4, 1, f); std::fwrite(&ep_uid, 4, 1, f);
  for (u32 u = 0; u < N; ++u) {
    u32 deg = static_cast<u32>(adj[u].size());
    std::fwrite(&deg, 4, 1, f);
    if (deg) std::fwrite(adj[u].data(), 4, deg, f);
  }
  std::fclose(f);
  std::cerr << "[ADJ_DUMP] wrote " << path << " N=" << N << " entry=" << ep_uid << std::endl;
  std::exit(0);
}

inline void run_or_probe(const byte_t* scratch, const std::vector<u64>& off_by_uid,
                         u32 N, size_t l0_off, u32 m_max0, u32 dim) {
  std::cerr << "[OR_PROBE] N=" << N << " m_max0=" << m_max0 << " dim=" << dim << std::endl;
  auto adj = or_build_adj(scratch, off_by_uid, N, l0_off, m_max0);
  u64 tot_edges = 0; for (auto& a : adj) tot_edges += a.size();
  const u64 ep_raw = *reinterpret_cast<const u64*>(scratch + 8);
  const u32 ep_uid = *reinterpret_cast<const u32*>(
    scratch + RemotePtr{ep_raw}.byte_offset() + Node::HEADER_SIZE);
  std::cerr << "[OR_PROBE] tot_l0_edges=" << tot_edges
            << " avg_deg=" << (double)tot_edges / std::max<u32>(1, N) << std::endl;

  // block byte sizes for the memory estimate (per the plan layout)
  const size_t node_entry_fp32 = 16 + (size_t)dim * 4 + (size_t)m_max0 * 4;
  const size_t node_entry_sq8  = 16 + (size_t)dim     + (size_t)m_max0 * 4;
  const double mem_fp32_gb = ((double)N * node_entry_fp32 + (double)N * 4) / 1e9;
  const double mem_sq8_gb  = ((double)N * node_entry_sq8  + (double)N * 4) / 1e9;
  std::cerr << "[OR_PROBE] reorder region mem: fp32=" << mem_fp32_gb
            << "GB sq8=" << mem_sq8_gb << "GB (1x, no replication)" << std::endl;

  for (u32 eps : {16u, 32u, 64u, 128u}) {
    u32 nb = 0;
    auto bo = or_bnp(adj, N, ep_uid, eps, nb);
    double or_bnp_v = or_compute(adj, bo);
    auto bo2 = bo;
    or_bnf(adj, N, eps, nb, bo2);
    double or_bnf_v = or_compute(adj, bo2);
    // predicted per-hop block reads: 1 (own block) + cross-block neighbors followed.
    // crude: a hop reads its own block + (1-OR)*deg distinct cross blocks (upper bound deg).
    const size_t blk_bytes_fp32 = 8 + (size_t)eps * node_entry_fp32;
    std::cerr << "[OR_PROBE] eps=" << eps
              << " blocks=" << nb
              << " OR_bnp=" << or_bnp_v
              << " OR_bnf=" << or_bnf_v
              << " est_extra_reads/hop=" << (1.0 - or_bnf_v) * ((double)tot_edges / std::max<u32>(1, N))
              << " blk_fp32=" << (blk_bytes_fp32 / 1024) << "KB"
              << std::endl;
  }
  std::cerr << "[OR_PROBE] DONE — go/no-go: proceed if some eps gives OR_bnf >= ~0.6" << std::endl;
  std::exit(0);
}

}  // namespace lavd
