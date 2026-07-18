#pragma once

// CRANE — Compute-Resident Approximate Navigation.
//
// Caches HNSW's upper-level (levels >=1, ~6% of nodes) subgraph
// VERBATIM in CN-local DRAM so the upper-level greedy descent runs
// entirely locally (zero remote ops), leaving only LAVD's level-0
// fat-block beam + rerank remote. The ROLEX/XStore "tiny cached
// navigation + ~1 remote refinement" pattern transplanted from
// ordered KV to graph-ANN.
//
// Red line: SHINE_CRANE unset/0 => OFF => the published LAVD path is
// taken unchanged (byte-identical). v1 = STATIC read path. Because
// the cached subgraph is the EXACT same nodes/edges/fp32-vectors as
// the remote graph, the local descent visits the identical nodes in
// identical order and yields the identical level-0 seed => recall
// byte-identical by construction.

#include <cstdlib>
#include <unordered_map>

#include <library/memory_region.hh>

#include "common/timing.hh"
#include "lavd/build_snapshot.hh"
#include "lavd/staged_io.hh"
#include "node/node.hh"
#include "remote_pointer.hh"
#include "shared_context.hh"

namespace crane {

struct Cfg {
  inline static bool enabled = false;
  static bool on() { return enabled; }
  static void init_from_env() {
    if (const char* e = std::getenv("SHINE_CRANE")) {
      enabled = (std::atoll(e) != 0);
    }
  }
};

// CN-resident upper-level subgraph over a compact upper-id space.
struct Cache {
  u32 dim = 0;
  u32 entry_uid = 0;
  vec<u64> rptr_raw;        // [uid] -> own RemotePtr raw (level-0 seed handoff)
  vec<u32> slot;            // [uid] -> dense node id
  vec<u32> top_level;       // [uid] -> node top level
  vec<element_t> comp;      // [uid*dim ..] fp32 components (descent distance)
  // per-uid, per-level (1..top_level) neighbor uid lists
  vec<vec<vec<u32>>> adj;   // adj[uid][L-1] = neighbor uids at level L
  bool ready = false;

  const element_t* vec_of(u32 uid) const {
    return comp.data() + static_cast<size_t>(uid) * dim;
  }
};

// One CN-resident cache (namespace-scope singleton; declared after the
// struct is complete).
inline Cache CACHE;

// One-time builder: bulk-read the whole index region into a
// registered scratch (same proven path as lavd::build_neighborhood
// steps 1-2), walk every node, extract the level>=1 subgraph into
// Cache::I. Runs on each CN at init, before queries. Off the query
// path; amortized over the workload (cf. LAVD region build).
template <typename Ctx>
inline void build_upper_cache(Ctx* ctx, u32 mn, timing::Timing& timing) {
  auto t = timing.create_enroll("crane_build");
  t->start();
  auto& context = ctx->context;
  const QP& qp = ctx->qps[mn]->qp;
  MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);

  // ---- 1. read free_ptr (used bytes of the index region) ----
  auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
  LocalMemoryRegion hdr_mr{context, hdr, 64};
  qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(), IBV_WR_RDMA_READ,
                true, false, idx_tok, /*remote_off*/ 0, 0, 0);
  context.poll_send_cq_until_completion();
  const u64 idx_bytes = *reinterpret_cast<u64*>(hdr);
  const u64 ep_raw = *reinterpret_cast<u64*>(hdr + 8);  // entry-point RemotePtr raw
  std::free(hdr);

  // ---- 2. bulk-read the whole index region into scratch ----
  const size_t scratch_capacity =
      lavd::aligned_allocation_bytes(static_cast<size_t>(idx_bytes), 64u);
  lib_assert(scratch_capacity != 0, "CRANE: invalid scratch capacity");
  auto* scratch =
      static_cast<byte_t*>(std::aligned_alloc(64, scratch_capacity));
  lib_assert(scratch != nullptr, "CRANE: scratch allocation failed");
  LocalMemoryRegion scratch_mr{context, scratch, scratch_capacity};
  {
    const u64 CHUNK = 1ull << 30;  // 1 GB max RDMA message
    for (u64 off = 0; off < idx_bytes; off += CHUNK) {
      const u32 n = static_cast<u32>(std::min<u64>(CHUNK, idx_bytes - off));
      qp->post_send(reinterpret_cast<u64>(scratch + off), n, scratch_mr.get_lkey(),
                    IBV_WR_RDMA_READ, true, false, idx_tok, off, 0, 0);
      context.poll_send_cq_until_completion();
    }
  }

  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;
  const size_t l0_off = Node::size_until_components();
  const u32 dim = Node::DIM;

  // ---- 3. pass A: walk nodes, assign upper-id to every level>=1
  //         node, record rawptr -> upper-id. ----
  Cache& C = CACHE;
  C = Cache{};  // reset
  C.dim = dim;
  std::unordered_map<u64, u32> raw2uid;
  raw2uid.reserve(1u << 18);
  vec<u64> off_by_uid_full;  // dense-uid -> region offset (all nodes, for pass B)
  off_by_uid_full.reserve(1u << 20);

  u64 walk = 16;  // skip free_ptr(8) + ep_ptr(8)
  while (walk < idx_bytes) {
    const u32 nid = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE);
    const u32 lvl = *reinterpret_cast<u32*>(scratch + walk + Node::HEADER_SIZE + sizeof(u32));
    size_t node_total =
      l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(lvl) * Node::NEIGHBORLIST_SIZE;
    while (node_total % 8 != 0) node_total += 4;
    if (nid >= off_by_uid_full.size()) off_by_uid_full.resize(nid + 1, 0);
    off_by_uid_full[nid] = walk;

    if (lvl >= 1) {
      const u64 own_raw = RemotePtr{mn, walk}.raw_address;
      const u32 uid = static_cast<u32>(C.slot.size());
      raw2uid.emplace(own_raw, uid);
      C.rptr_raw.push_back(own_raw);
      C.slot.push_back(nid);
      C.top_level.push_back(lvl);
      const element_t* cp = reinterpret_cast<const element_t*>(scratch + walk + comp_off);
      C.comp.insert(C.comp.end(), cp, cp + dim);
    }
    walk += node_total;
  }
  const u32 U = static_cast<u32>(C.slot.size());

  // ---- 4. pass B: per upper node, parse its level 1..top
  //         neighborlists, map neighbor raws -> upper-ids. ----
  C.adj.resize(U);
  for (u32 uid = 0; uid < U; ++uid) {
    const u64 o = off_by_uid_full[C.slot[uid]];
    const u32 lvl = C.top_level[uid];
    C.adj[uid].resize(lvl);  // levels 1..lvl
    for (u32 L = 1; L <= lvl; ++L) {
      const byte_t* nl = scratch + o + l0_off + Node::NEIGHBORLIST_SIZE_ZERO +
                         static_cast<size_t>(L - 1) * Node::NEIGHBORLIST_SIZE;
      const u32 cnt = *reinterpret_cast<const u32*>(nl);
      const u64* np = reinterpret_cast<const u64*>(nl + sizeof(u32));
      auto& dst = C.adj[uid][L - 1];
      dst.reserve(cnt);
      for (u32 i = 0; i < cnt; ++i) {
        auto it = raw2uid.find(np[i]);
        // HNSW invariant: a level-L neighbor has level>=L>=1 => present.
        if (it != raw2uid.end()) dst.push_back(it->second);
      }
    }
  }

  auto ite = raw2uid.find(ep_raw);
  lib_assert(ite != raw2uid.end(), "CRANE: entry point not in upper set");
  C.entry_uid = ite->second;
  C.ready = true;

  std::free(scratch);
  t->stop();
  std::cerr << "[CRANE] upper cache built: U=" << U << " (" << dim << "-d) "
            << "comp=" << (static_cast<u64>(U) * dim * sizeof(element_t)) << "B "
            << "entry_uid=" << C.entry_uid << std::endl;
}

// === Multi-MN variant: bulk-reads INDEX from each MN, then walks the union
// === to build the upper subgraph cache. Same Cache schema and same descent
// === code apply (uid is dense within the upper set; rptr_raw carries the
// === correct mn natively). Entry point is read from MN 0's header (global).
template <typename Ctx>
inline void build_upper_cache_multi(Ctx* ctx, u32 num_mns, timing::Timing& timing) {
  auto t = timing.create_enroll("crane_build_multi");
  t->start();
  auto& context = ctx->context;
  lib_assert(num_mns >= 1, "CRANE-multi: num_mns must be >= 1");

  // Step 1+2: per-MN bulk-read INDEX
  std::vector<byte_t*> per_mn_scratch(num_mns, nullptr);
  std::vector<u64> per_mn_bytes(num_mns, 0);
  u64 ep_raw = 0;
  const bool reuse_snapshot =
      lavd::authoritative_snapshot_available(num_mns);
  if (reuse_snapshot) {
    const auto& snapshot = lavd::authoritative_snapshot();
    ep_raw = snapshot.entry_raw;
    for (u32 mn = 0; mn < num_mns; ++mn) {
      per_mn_scratch[mn] =
          static_cast<byte_t*>(snapshot.shards[mn]);
      per_mn_bytes[mn] = snapshot.bytes[mn];
    }
    std::cerr << "[CRANE][multi] reused authoritative build snapshot: shards="
              << num_mns << std::endl;
  } else {
    auto* read_stage = static_cast<byte_t*>(
        std::aligned_alloc(64, lavd::STAGED_IO_BYTES));
    lib_assert(read_stage != nullptr,
               "CRANE-multi: staged-read buffer allocation failed");
    {
      LocalMemoryRegion read_stage_mr{context, read_stage,
                                      lavd::STAGED_IO_BYTES};
      for (u32 mn = 0; mn < num_mns; ++mn) {
        const QP& qp = ctx->qps[mn]->qp;
        MemoryRegionToken* idx_tok = ctx->get_remote_mrt(mn);
        auto* hdr = static_cast<byte_t*>(std::aligned_alloc(64, 64));
        lib_assert(hdr != nullptr, "CRANE-multi: header allocation failed");
        u64 idx_bytes = 0;
        {
          LocalMemoryRegion hdr_mr{context, hdr, 64};
          qp->post_send(reinterpret_cast<u64>(hdr), 16, hdr_mr.get_lkey(),
                        IBV_WR_RDMA_READ, true, false, idx_tok, 0, 0, 0);
          context.poll_send_cq_until_completion();
          idx_bytes = *reinterpret_cast<u64*>(hdr);
          if (mn == 0) ep_raw = *reinterpret_cast<u64*>(hdr + 8);
        }
        std::free(hdr);
        per_mn_bytes[mn] = idx_bytes;
        const size_t scratch_capacity =
            static_cast<size_t>((idx_bytes + 63) & ~u64{63});
        per_mn_scratch[mn] = static_cast<byte_t*>(
            std::aligned_alloc(64, scratch_capacity));
        lib_assert(per_mn_scratch[mn] != nullptr,
                   "CRANE-multi: scratch allocation failed");
        for (u64 off = 0; off < idx_bytes; off += lavd::STAGED_IO_BYTES) {
          const u32 n = lavd::staged_chunk_bytes(idx_bytes, off);
          qp->post_send(reinterpret_cast<u64>(read_stage), n,
                        read_stage_mr.get_lkey(), IBV_WR_RDMA_READ, true,
                        false, idx_tok, off, 0, 0);
          context.poll_send_cq_until_completion();
          std::memcpy(per_mn_scratch[mn] + off, read_stage, n);
        }
        std::cerr << "[CRANE][multi] MN " << mn << " staged-read "
                  << idx_bytes << "B via " << lavd::STAGED_IO_BYTES << "B MR"
                  << std::endl;
      }
    }
    std::free(read_stage);
  }

  const size_t comp_off = Node::HEADER_SIZE + Node::META_SIZE;
  const size_t l0_off = Node::size_until_components();
  const u32 dim = Node::DIM;

  // Pass A: walk EACH MN's scratch, build upper subgraph + raw2uid map
  Cache& C = CACHE;
  C = Cache{};
  C.dim = dim;
  std::unordered_map<u64, u32> raw2uid;
  raw2uid.reserve(1u << 18);
  // For pass B we need: nid -> (mn, byte_offset_in_per_mn_scratch[mn])
  std::vector<u64> off_by_nid_offset;
  std::vector<u32> off_by_nid_mn;
  off_by_nid_offset.reserve(1u << 20);
  off_by_nid_mn.reserve(1u << 20);
  for (u32 mn = 0; mn < num_mns; ++mn) {
    u64 walk = 16;
    while (walk < per_mn_bytes[mn]) {
      const u32 nid = *reinterpret_cast<u32*>(per_mn_scratch[mn] + walk + Node::HEADER_SIZE);
      const u32 lvl = *reinterpret_cast<u32*>(per_mn_scratch[mn] + walk + Node::HEADER_SIZE + sizeof(u32));
      size_t node_total =
        l0_off + Node::NEIGHBORLIST_SIZE_ZERO + static_cast<size_t>(lvl) * Node::NEIGHBORLIST_SIZE;
      while (node_total % 8 != 0) node_total += 4;
      if (nid >= off_by_nid_offset.size()) {
        off_by_nid_offset.resize(nid + 1, 0);
        off_by_nid_mn.resize(nid + 1, 0xFFFFFFFFu);
      }
      off_by_nid_offset[nid] = walk;
      off_by_nid_mn[nid] = mn;
      if (lvl >= 1) {
        const u64 own_raw = RemotePtr{mn, walk}.raw_address;
        const u32 uid = static_cast<u32>(C.slot.size());
        raw2uid.emplace(own_raw, uid);
        C.rptr_raw.push_back(own_raw);
        C.slot.push_back(nid);
        C.top_level.push_back(lvl);
        const element_t* cp = reinterpret_cast<const element_t*>(per_mn_scratch[mn] + walk + comp_off);
        C.comp.insert(C.comp.end(), cp, cp + dim);
      }
      walk += node_total;
    }
  }
  const u32 U = static_cast<u32>(C.slot.size());

  // Pass B: build adjacency per upper-uid; neighbor rptr -> uid via raw2uid
  C.adj.resize(U);
  for (u32 uid = 0; uid < U; ++uid) {
    const u32 nid = C.slot[uid];
    const u32 mn = off_by_nid_mn[nid];
    const u64 o = off_by_nid_offset[nid];
    const byte_t* my_scratch = per_mn_scratch[mn];
    const u32 lvl = C.top_level[uid];
    C.adj[uid].resize(lvl);
    for (u32 L = 1; L <= lvl; ++L) {
      const byte_t* nl = my_scratch + o + l0_off + Node::NEIGHBORLIST_SIZE_ZERO +
                         static_cast<size_t>(L - 1) * Node::NEIGHBORLIST_SIZE;
      const u32 cnt = *reinterpret_cast<const u32*>(nl);
      const u64* np = reinterpret_cast<const u64*>(nl + sizeof(u32));
      auto& dst = C.adj[uid][L - 1];
      dst.reserve(cnt);
      for (u32 i = 0; i < cnt; ++i) {
        auto it = raw2uid.find(np[i]);
        if (it != raw2uid.end()) dst.push_back(it->second);
      }
    }
  }

  auto ite = raw2uid.find(ep_raw);
  lib_assert(ite != raw2uid.end(), "CRANE-multi: entry point not in upper set");
  C.entry_uid = ite->second;
  C.ready = true;

  if (reuse_snapshot) {
    lavd::clear_authoritative_snapshot();
  } else {
    for (u32 mn = 0; mn < num_mns; ++mn) {
      std::free(per_mn_scratch[mn]);
    }
  }
  t->stop();
  std::cerr << "[CRANE][multi] upper cache built: U=" << U << " (" << dim << "-d) "
            << "comp=" << (static_cast<u64>(U) * dim * sizeof(element_t)) << "B "
            << "entry_uid=" << C.entry_uid << " num_mns=" << num_mns << std::endl;
}

// CN-local greedy descent over the cached upper subgraph. Mirrors
// hnsw::search_for_one<without_lock> EXACTLY (same greedy rule, same
// per-level loop, same Distance) but on local memory => identical
// level-0 seed, zero remote ops. Returns the seed upper-id; the
// caller hands {slot, rptr_raw, comp} to search_level_lavd.
template <class Distance>
inline u32 descend(const span<element_t> q) {
  const Cache& C = CACHE;
  u32 cur = C.entry_uid;
  f32 closest = Distance::dist(q, span<element_t>{const_cast<element_t*>(C.vec_of(cur)), C.dim}, C.dim);
  for (u32 level = C.top_level[cur]; level >= 1; --level) {
    bool changed = true;
    while (changed) {
      changed = false;
      // a node only has adjacency for levels <= its top_level
      if (level > C.top_level[cur]) continue;
      const auto& nbrs = C.adj[cur][level - 1];
      u32 best = cur;
      for (u32 nb : nbrs) {
        const f32 d = Distance::dist(
          q, span<element_t>{const_cast<element_t*>(C.vec_of(nb)), C.dim}, C.dim);
        if (d < closest) { closest = d; best = nb; changed = true; }
      }
      cur = best;
    }
    if (level == 1) break;  // avoid u32 underflow at level-- past 1
  }
  return cur;
}

}  // namespace crane
