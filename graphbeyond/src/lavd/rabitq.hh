#pragma once

// GraphBeyond — RaBitQ co-located code (alternative to scalar SQ in LAVD blocks).
//
// Random-rotation + B-bit/dim quantization with an UNBIASED inner-product
// estimator (RaBitQ, SIGMOD'24/'25): a theoretical error bound makes B-bit
// codes navigable where aggressive PQ craters. Replaces qz.encode / approx in
// the LAVD fanout: the per-neighbor code shrinks to dim*B/8 bytes (vs sq8's
// dim bytes) -> attacks BOTH MN memory (M) and per-hop bandwidth (B), the two
// horns the budget alone cannot fully close at high dimension.
//
// Verified navigable on gist200k (Python probe docs/.. /tmp/rabitq_probe*.py):
//   sq8 0.8778(960B) | RaBitQ-1bit 0.8600(120B,8x) | RaBitQ-2bit 0.8722(240B,4x).
// PQ at the same compression cratered to 0.61 -> RaBitQ's bound+bitwise is the fix.
//
// Gated by SHINE_LAVD_RABITQ_B (1/2/3/4; 0 = OFF = scalar SQ, byte-identical).
//
// Math (L2), all vectors centered by the data mean c and unit-normalized:
//   ||q-o||^2 = ||q-c||^2 + ||o-c||^2 - 2 <q-c,o-c>
//             = nq^2 + no^2 - 2 nq*no <q_unit,o_unit>,   <q_unit,o_unit>=<q_rot,o_rot>
//   RaBitQ estimates <q_rot,o_rot> ~= <q_rot, o_bar> / <o_rot, o_bar>
//   where o_bar_i = lvl[code_i] is the B-bit reconstruction of o_rot.
// Stored per neighbor: the B-bit code + two f32 scalars (no = ||o-c||,
//   dot_oo = <o_rot,o_bar>). Query prep rotates q once; estimate is a dot product.

#include <cmath>
#include <cstdint>
#include <cstring>
#include <random>
#include <vector>

#include <library/types.hh>

#if defined(__AVX512F__)
#include <immintrin.h>
#endif

namespace lavd {

struct RaBitQ {
  u32 dim = 0;
  u32 B = 0;                  // bits per dim
  std::vector<f32> c;         // centroid (dim)
  std::vector<f32> P;         // rotation, row-major dim*dim (orthogonal)
  std::vector<f32> lvl;       // 2^B reconstruction levels (already /sqrt(dim))
  std::vector<f32> thr;       // 2^B-1 thresholds (already /sqrt(dim))
  u32 rotation_seed = 0;

  u32 nlvl() const { return 1u << B; }
  // BIT-SLICED (planar) storage: B contiguous planes, plane b = bit b of every
  // dim's index. This lets the hot estimate read 16-dim masks with one load and
  // accumulate via AVX512 masked-add (no per-dim gather/branch).
  size_t plane_bytes() const { return (static_cast<size_t>(dim) + 7) / 8; }
  size_t code_bytes() const { return static_cast<size_t>(B) * plane_bytes(); }

  // ---- fit: centroid + seeded rotation + Lloyd-Max levels for N(0,1/dim) ----
  void fit(u32 d, u32 b, const f32* sample, u32 n, u32 seed = 12345u) {
    dim = d; B = b;
    c.assign(d, 0.f);
    for (u32 i = 0; i < n; ++i)
      for (u32 j = 0; j < d; ++j) c[j] += sample[static_cast<size_t>(i) * d + j];
    for (u32 j = 0; j < d; ++j) c[j] /= static_cast<f32>(std::max(1u, n));
    gen_rotation(seed);
    set_levels();
  }

  // ---- multi-CN reconstruction (no data sample needed) ----
  // Only the initiator runs fit() (it builds the MN region). Every other CN
  // rebuilds the IDENTICAL encoder from the shared header: the rotation P is
  // deterministic from `seed`, the levels depend only on (dim, B), so the ONLY
  // data-dependent state is the centroid `c` -- which the initiator ships in
  // the params header. Result is byte-identical to the initiator's instance,
  // so co-located codes decode the same on every CN (recall byte-neutral).
  void init_shared(u32 d, u32 b, const f32* centroid, u32 seed = 12345u) {
    dim = d; B = b;
    c.assign(centroid, centroid + d);
    gen_rotation(seed);
    set_levels();
  }

  bool init_shared_reusing_rotation(u32 d, u32 b, const f32* centroid,
                                    const RaBitQ& reusable,
                                    u32 seed = 12345u) {
    dim = d;
    B = b;
    c.assign(centroid, centroid + d);
    const size_t expected = static_cast<size_t>(d) * d;
    const bool reuse = reusable.dim == d &&
                       reusable.rotation_seed == seed &&
                       reusable.P.size() == expected;
    if (reuse) {
      P = reusable.P;
      rotation_seed = seed;
    } else {
      gen_rotation(seed);
    }
    set_levels();
    return reuse;
  }

  // seeded random orthogonal P via Gaussian + modified Gram-Schmidt (rows
  // orthonormal). Deterministic from seed so every CN regenerates the same P
  // (no need to ship the 3.7MB matrix; just persist `seed`). One-time O(d^3).
  void gen_rotation(u32 seed) {
    const u32 d = dim;
    rotation_seed = seed;
    P.assign(static_cast<size_t>(d) * d, 0.f);
    std::mt19937 rng(seed);
    std::normal_distribution<f32> g(0.f, 1.f);
    for (auto& x : P) x = g(rng);
    for (u32 i = 0; i < d; ++i) {
      f32* ri = &P[static_cast<size_t>(i) * d];
      for (u32 k = 0; k < i; ++k) {                 // subtract projections on prior rows
        const f32* rk = &P[static_cast<size_t>(k) * d];
        f64 dot = 0; for (u32 j = 0; j < d; ++j) dot += static_cast<f64>(ri[j]) * rk[j];
        const f32 fd = static_cast<f32>(dot);
        for (u32 j = 0; j < d; ++j) ri[j] -= fd * rk[j];
      }
      f64 nn = 0; for (u32 j = 0; j < d; ++j) nn += static_cast<f64>(ri[j]) * ri[j];
      const f32 inv = static_cast<f32>(1.0 / std::sqrt(std::max(1e-30, nn)));
      for (u32 j = 0; j < d; ++j) ri[j] *= inv;
    }
  }

  // Lloyd-Max optimal levels/thresholds for a unit-variance Gaussian, scaled
  // by sigma = 1/sqrt(dim) (the std of a rotated unit vector's components).
  // Computed numerically (deterministic Lloyd iteration on a fine grid) so any
  // B works without hardcoded tables.
  void set_levels() {
    const u32 L = nlvl();
    const f32 sigma = 1.f / std::sqrt(static_cast<f32>(dim));
    // grid of a standard normal in [-5,5]
    const int G = 20001; const double gmin = -5.0, gstep = 10.0 / (G - 1);
    std::vector<double> x(G), p(G);
    double psum = 0;
    for (int i = 0; i < G; ++i) { x[i] = gmin + i * gstep; p[i] = std::exp(-0.5 * x[i] * x[i]); psum += p[i]; }
    for (int i = 0; i < G; ++i) p[i] /= psum;
    std::vector<double> rec(L);                       // init: uniform quantiles
    for (u32 q = 0; q < L; ++q) rec[q] = -2.5 + 5.0 * (q + 0.5) / L;
    std::vector<double> t(L + 1);
    for (int it = 0; it < 50; ++it) {
      t[0] = -1e9; t[L] = 1e9;
      for (u32 q = 1; q < L; ++q) t[q] = 0.5 * (rec[q - 1] + rec[q]);
      for (u32 q = 0; q < L; ++q) {                   // centroid of each cell
        double num = 0, den = 0;
        for (int i = 0; i < G; ++i) if (x[i] >= t[q] && x[i] < t[q + 1]) { num += x[i] * p[i]; den += p[i]; }
        if (den > 0) rec[q] = num / den;
      }
    }
    lvl.resize(L); thr.resize(L - 1);
    for (u32 q = 0; q < L; ++q) lvl[q] = static_cast<f32>(rec[q]) * sigma;
    for (u32 q = 1; q < L; ++q) thr[q - 1] = static_cast<f32>(0.5 * (rec[q - 1] + rec[q])) * sigma;
  }

  // rotate a centered/normalized vector into `out` (out[i] = P_i . u). The D^2
  // matvec is the per-query cost at low concurrency; AVX512-FMA the inner dot.
  void rotate(const f32* u, f32* out) const {
    const u32 d = dim;
#if defined(__AVX512F__)
    if ((d & 15u) == 0) {
      for (u32 i = 0; i < d; ++i) {
        const f32* ri = &P[static_cast<size_t>(i) * d];
        __m512 acc = _mm512_setzero_ps();
        for (u32 j = 0; j < d; j += 16)
          acc = _mm512_fmadd_ps(_mm512_loadu_ps(ri + j), _mm512_loadu_ps(u + j), acc);
        out[i] = _mm512_reduce_add_ps(acc);
      }
      return;
    }
#endif
    for (u32 i = 0; i < d; ++i) {
      const f32* ri = &P[static_cast<size_t>(i) * d];
      f32 s = 0; for (u32 j = 0; j < d; ++j) s += ri[j] * u[j];
      out[i] = s;
    }
  }
  // quantize one rotated component to its level index (0..L-1)
  u32 quant(f32 v) const {
    u32 q = 0; while (q < thr.size() && v >= thr[q]) ++q; return q;
  }
  void pack(u32* idx, byte_t* out) const {            // planar: plane b, byte i>>3, bit i&7
    const size_t pb = plane_bytes();
    std::memset(out, 0, code_bytes());
    for (u32 i = 0; i < dim; ++i)
      for (u32 b = 0; b < B; ++b)
        if (idx[i] & (1u << b)) out[b * pb + (i >> 3)] |= (1u << (i & 7));
  }
  u32 unpack(const byte_t* code, u32 i) const {
    const size_t pb = plane_bytes(); u32 v = 0;
    for (u32 b = 0; b < B; ++b)
      if (code[b * pb + (i >> 3)] & (1u << (i & 7))) v |= (1u << b);
    return v;
  }

  // ---- encode a raw vector into a co-located neighbor code ----
  // writes code bytes to `out`; returns no=||v-c|| via *norm, dot_oo via *dot.
  void encode(const f32* v, byte_t* out, f32* norm, f32* dot, f32* scratch_u, f32* scratch_rot) const {
    f64 nn = 0; for (u32 j = 0; j < dim; ++j) { scratch_u[j] = v[j] - c[j]; nn += static_cast<f64>(scratch_u[j]) * scratch_u[j]; }
    const f32 no = static_cast<f32>(std::sqrt(std::max(1e-30, nn))); *norm = no;
    const f32 inv = 1.f / no; for (u32 j = 0; j < dim; ++j) scratch_u[j] *= inv;
    rotate(scratch_u, scratch_rot);
    std::vector<u32> idx(dim); f64 d = 0;
    for (u32 j = 0; j < dim; ++j) { idx[j] = quant(scratch_rot[j]); d += static_cast<f64>(scratch_rot[j]) * lvl[idx[j]]; }
    pack(idx.data(), out);
    *dot = static_cast<f32>(d);                       // <o_rot, o_bar>
  }

  // ---- query prep: rotate query once; returns nq=||q-c||, fills q_rot ----
  f32 query_prep(const f32* q, f32* q_rot, f32* scratch_u) const {
    f64 nn = 0; for (u32 j = 0; j < dim; ++j) { scratch_u[j] = q[j] - c[j]; nn += static_cast<f64>(scratch_u[j]) * scratch_u[j]; }
    const f32 nq = static_cast<f32>(std::sqrt(std::max(1e-30, nn)));
    const f32 inv = 1.f / nq; for (u32 j = 0; j < dim; ++j) scratch_u[j] *= inv;
    rotate(scratch_u, q_rot);
    return nq;
  }

  // ---- estimate squared L2 from a neighbor's code + scalars + prepared query ----
  // num = <q_rot, o_bar> = sum_j q_rot[j]*lvl[code_j]. Regrouped by code value:
  // num = sum_v lvl[v] * (sum_{j: code_j==v} q_rot[j]) -- mathematically IDENTICAL
  // (recall byte-exact), but the group-sums are computed with AVX512 masked-add
  // over the bit-sliced planes (no per-dim gather/branch). B=2 fast path; scalar
  // planar fallback otherwise (and on non-AVX512 builds).
  f32 estimate(const byte_t* code, f32 no, f32 dot_oo, const f32* q_rot, f32 nq) const {
    float num = 0.f;
#if defined(__AVX512F__)
    if (B == 2 && (dim & 15u) == 0) {
      const size_t pb = plane_bytes();
      const byte_t* p0 = code; const byte_t* p1 = code + pb;
      __m512 a0 = _mm512_setzero_ps(), a1 = _mm512_setzero_ps();
      __m512 a2 = _mm512_setzero_ps(), a3 = _mm512_setzero_ps();
      for (u32 cc = 0; cc < dim; cc += 16) {
        const __m512 v = _mm512_loadu_ps(q_rot + cc);
        const uint16_t m0 = *reinterpret_cast<const uint16_t*>(p0 + (cc >> 3));
        const uint16_t m1 = *reinterpret_cast<const uint16_t*>(p1 + (cc >> 3));
        a3 = _mm512_mask_add_ps(a3, static_cast<__mmask16>(m0 & m1), a3, v);                  // code 11
        a1 = _mm512_mask_add_ps(a1, static_cast<__mmask16>(m0 & static_cast<uint16_t>(~m1)), a1, v); // 01
        a2 = _mm512_mask_add_ps(a2, static_cast<__mmask16>(static_cast<uint16_t>(~m0) & m1), a2, v); // 10
        a0 = _mm512_mask_add_ps(a0, static_cast<__mmask16>(static_cast<uint16_t>(~m0) & static_cast<uint16_t>(~m1)), a0, v); // 00
      }
      num = lvl[0] * _mm512_reduce_add_ps(a0) + lvl[1] * _mm512_reduce_add_ps(a1)
          + lvl[2] * _mm512_reduce_add_ps(a2) + lvl[3] * _mm512_reduce_add_ps(a3);
    } else
#endif
    {
      for (u32 j = 0; j < dim; ++j) num += q_rot[j] * lvl[unpack(code, j)];  // scalar planar
    }
    const float est = num / (dot_oo != 0.f ? dot_oo : 1e-30f);  // <q_rot,o_rot> est
    return nq * nq + no * no - 2.f * no * nq * est;
  }

  static u32 b_from_env() {
    const char* e = std::getenv("SHINE_LAVD_RABITQ_B");
    const u32 b = e ? static_cast<u32>(std::atoi(e)) : 0;
    return (b == 1 || b == 2 || b == 3 || b == 4) ? b : 0;
  }
};

}  // namespace lavd
