#!/usr/bin/env python3
"""Exact brute-force ground truth for a BigANN-format base + query set.

No faiss dependency — chunked numpy (BLAS) brute force, memory-bounded.
Writes c1's GT format:  [u32 n_queries][u32 k] + n_queries*k * u32 neighbor-ids
(IDs only, no distances — matches existing groundtruth-uniform.bin).

Distance:
  L2 (default): neighbors = k smallest ||q-b||^2
  IP (--ip):    neighbors = k largest dot(q,b)   (c1 IPDistance = 1 - dot,
                so smaller-is-better => GT is argmax dot, matching HNSW)

Reads .fbin/.u8bin/.i8bin (header [u32 n][u32 dim] + n*dim*sizeof(type)).
All vectors are promoted to float32 for the distance math (matches the
binary's element_t = f32 conversion in io/read_data.hh).

Usage:
  gt_bruteforce.py -b base.fbin -q query-uniform.fbin -o groundtruth-uniform.bin \
      [--ip] [-k 100] [--base-chunk 25000] [--query-chunk 10000]
"""
import argparse
import struct
import sys
import numpy as np


def read_vecs(path):
    ext = path.rsplit(".", 1)[-1]
    if ext == "fbin":
        dt = np.float32
    elif ext == "u8bin":
        dt = np.uint8
    elif ext == "i8bin":
        dt = np.int8
    else:
        sys.exit(f"unknown extension: {path}")
    with open(path, "rb") as f:
        n, dim = struct.unpack("<II", f.read(8))
        a = np.fromfile(f, dtype=dt, count=n * dim)
    a = a.reshape(n, dim).astype(np.float32, copy=False)
    return a, n, dim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-b", "--base", required=True)
    ap.add_argument("-q", "--query", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--ip", action="store_true", help="inner-product (argmax dot); default L2")
    ap.add_argument("-k", type=int, default=100)
    ap.add_argument("--base-chunk", type=int, default=25000)
    ap.add_argument("--query-chunk", type=int, default=10000)
    args = ap.parse_args()

    base, N, dim = read_vecs(args.base)
    query, Q, qdim = read_vecs(args.query)
    assert dim == qdim, f"dim mismatch base={dim} query={qdim}"
    k = min(args.k, N)
    print(f"[gt] base N={N} dim={dim} | query Q={Q} | k={k} | metric={'IP' if args.ip else 'L2'}",
          flush=True)

    if not args.ip:
        base_sq = np.einsum("ij,ij->i", base, base)  # ||b||^2, [N]

    out_ids = np.empty((Q, k), dtype=np.uint32)

    for qs in range(0, Q, args.query_chunk):
        qe = min(qs + args.query_chunk, Q)
        qb = query[qs:qe]                       # [q, dim]
        nq = qe - qs
        # running top-k (vals + global ids) for this query block
        best_val = None                          # [q, k]
        best_idx = None                          # [q, k] global ids
        if not args.ip:
            q_sq = np.einsum("ij,ij->i", qb, qb)  # [q]
        for bs in range(0, N, args.base_chunk):
            be = min(bs + args.base_chunk, N)
            bb = base[bs:be]                     # [c, dim]
            dots = qb @ bb.T                     # [q, c]
            kk = min(k, be - bs)
            if args.ip:
                score = dots                     # larger = better
                part = np.argpartition(-score, kk - 1, axis=1)[:, :kk]
            else:
                # ||q||^2 + ||b||^2 - 2 q.b ; smaller = better.
                # in-place on the dots buffer to avoid allocating big [q,c] temps
                dots *= -2.0
                dots += base_sq[bs:be][None, :]
                dots += q_sq[:, None]
                score = dots
                part = np.argpartition(score, kk - 1, axis=1)[:, :kk]
            rows = np.arange(nq)[:, None]
            cand_val = score[rows, part]          # [q, kk]
            cand_idx = (part + bs).astype(np.uint32)
            if best_val is None:
                best_val, best_idx = cand_val, cand_idx
            else:
                best_val = np.concatenate([best_val, cand_val], axis=1)
                best_idx = np.concatenate([best_idx, cand_idx], axis=1)
            # shrink back to top-k (IP: largest; L2: smallest)
            if best_val.shape[1] > k:
                if args.ip:
                    sel = np.argpartition(-best_val, k - 1, axis=1)[:, :k]
                else:
                    sel = np.argpartition(best_val, k - 1, axis=1)[:, :k]
                best_val = best_val[rows, sel]
                best_idx = best_idx[rows, sel]
        # final exact sort of the k survivors (ascending distance / descending score)
        order = np.argsort(-best_val if args.ip else best_val, axis=1)
        out_ids[qs:qe] = best_idx[np.arange(nq)[:, None], order]
        print(f"[gt]   queries {qe}/{Q}", flush=True)

    with open(args.out, "wb") as f:
        f.write(struct.pack("<II", Q, k))
        out_ids.tofile(f)
    print(f"[gt] wrote {args.out}  ({Q} x {k} ids)", flush=True)


if __name__ == "__main__":
    main()
