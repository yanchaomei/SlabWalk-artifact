#pragma once

// GraphBeyond LAVD — PQ / OPQ fanout codes (dimension-INDEPENDENT
// co-location).
//
// Replaces the scalar quantizer's dim-proportional code (int8: dim bytes)
// with a Product-Quantization code of m bytes (m subquantizers, k=256
// centroids each). The co-located block stores m bytes/neighbor; one fat
// read still serves all M neighbors (op-collapse intact) and the existing
// fp32 rerank recovers final accuracy.
//
// Distance is ADC (asymmetric): per query build LUT[m][256] =
// ||q_subj - centroid[j][c]||^2, neighbor approx = sum_j LUT[j][code[j]].
//
// OPQ option (random rotation): plain PQ on skewed-variance data (e.g.
// GIST) wastes bits — variance concentrates in a few dims, so some
// subquantizers see almost no signal. A random ORTHOGONAL rotation R
// spreads variance evenly across subspaces (||Rx||=||x|| so L2/ADC is
// unchanged), recovering recall at small m. Codes are PQ(R·x); the query
// LUT is built from R·q. The rerank reads RAW fp32 from the MN (rotation-
// independent true L2), so rotation only affects the approx ranking.
// dim must be divisible by m. k-means + rotation are seeded → reproducible.

#include <algorithm>
#include <cmath>
#include <cstring>
#include <random>

#include <library/types.hh>
#include "common/types.hh"

namespace lavd {

class PQ {
public:
  static constexpr u32 K = 256;
  static constexpr u32 MAXDIM = 2048;  // stack scratch for the rotated vec

  PQ() = default;

  u32 dim() const { return dim_; }
  u32 m() const { return m_; }
  size_t code_bytes() const { return m_; }
  bool has_rot() const { return !R_.empty(); }

  // Fit m per-subspace codebooks via k-means on data[n*dim]. If rotate,
  // first build a random orthogonal R and fit on the rotated sample
  // (RR-PQ). max_iter k-means iters; fixed seed → reproducible.
  void fit(u32 dim, u32 m, const element_t* data, size_t n,
           bool rotate = false, u32 max_iter = 20, u32 seed = 12345) {
    init(dim, m);
    if (rotate) gen_rotation(seed ^ 0x9e3779b9u);

    // working sample (rotated if R present)
    vec<f32> work(static_cast<size_t>(n) * dim_);
    for (size_t i = 0; i < n; ++i) {
      if (has_rot()) rotate_vec(data + i * dim_, &work[i * dim_]);
      else for (u32 d = 0; d < dim_; ++d) work[i * dim_ + d] = static_cast<f32>(data[i * dim_ + d]);
    }

    std::mt19937 rng(seed);
    std::uniform_int_distribution<size_t> pick(0, n - 1);
    for (u32 j = 0; j < m_; ++j) {
      const u32 s = sub_off_[j], sd = sub_dim_[j];
      f32* cb = centroid_ptr(j);
      for (u32 c = 0; c < K; ++c) {
        const f32* row = &work[pick(rng) * dim_];
        for (u32 d = 0; d < sd; ++d) cb[c * sd + d] = row[s + d];
      }
      vec<u32> assign(n, 0);
      vec<f64> sum(static_cast<size_t>(K) * sd, 0.0);
      vec<u32> cnt(K, 0);
      for (u32 it = 0; it < max_iter; ++it) {
        bool changed = false;
        for (size_t i = 0; i < n; ++i) {
          const f32* sub = &work[i * dim_ + s];
          u32 best = 0; f32 bestd = 1e30f;
          for (u32 c = 0; c < K; ++c) {
            const f32* ce = cb + c * sd;
            f32 acc = 0.f;
            for (u32 d = 0; d < sd; ++d) { const f32 t = sub[d] - ce[d]; acc += t * t; }
            if (acc < bestd) { bestd = acc; best = c; }
          }
          if (assign[i] != best) { assign[i] = best; changed = true; }
        }
        if (it > 0 && !changed) break;
        std::fill(sum.begin(), sum.end(), 0.0);
        std::fill(cnt.begin(), cnt.end(), 0u);
        for (size_t i = 0; i < n; ++i) {
          const f32* sub = &work[i * dim_ + s];
          const u32 c = assign[i]; ++cnt[c];
          f64* acc = &sum[static_cast<size_t>(c) * sd];
          for (u32 d = 0; d < sd; ++d) acc[d] += sub[d];
        }
        for (u32 c = 0; c < K; ++c) {
          if (cnt[c] == 0) { const f32* row = &work[pick(rng) * dim_]; for (u32 d = 0; d < sd; ++d) cb[c * sd + d] = row[s + d]; }
          else { const f64* acc = &sum[static_cast<size_t>(c) * sd]; for (u32 d = 0; d < sd; ++d) cb[c * sd + d] = static_cast<f32>(acc[d] / cnt[c]); }
        }
      }
    }
  }

  // y = R·x (or copy when no rotation). y must hold dim_ floats.
  void rotate_vec(const element_t* x, f32* y) const {
    if (!has_rot()) { for (u32 i = 0; i < dim_; ++i) y[i] = static_cast<f32>(x[i]); return; }
    for (u32 i = 0; i < dim_; ++i) {
      const f32* r = &R_[static_cast<size_t>(i) * dim_];
      f32 acc = 0.f;
      for (u32 j = 0; j < dim_; ++j) acc += r[j] * static_cast<f32>(x[j]);
      y[i] = acc;
    }
  }

  // Encode an ALREADY-IN-PQ-SPACE vector (rotated, or raw if no rotation).
  void encode_norot(const f32* y, u8* out) const {
    for (u32 j = 0; j < m_; ++j) {
      const u32 s = sub_off_[j], sd = sub_dim_[j];
      const f32* cb = centroid_ptr(j); const f32* sub = y + s;
      u32 best = 0; f32 bestd = 1e30f;
      for (u32 c = 0; c < K; ++c) {
        const f32* ce = cb + c * sd; f32 acc = 0.f;
        for (u32 d = 0; d < sd; ++d) { const f32 t = sub[d] - ce[d]; acc += t * t; }
        if (acc < bestd) { bestd = acc; best = c; }
      }
      out[j] = static_cast<u8>(best);
    }
  }

  // Encode a RAW vector (rotates first if OPQ). Used by cold/seed paths.
  void encode(const element_t* x, u8* out) const {
    if (!has_rot()) { encode_norot(reinterpret_cast<const f32*>(x), out); return; }
    f32 y[MAXDIM]; rotate_vec(x, y); encode_norot(y, out);
  }

  // Per-query ADC LUT from a RAW query (rotates first if OPQ).
  void build_lut(const element_t* q, f32* lut) const {
    const f32* qq;
    f32 y[MAXDIM];
    if (has_rot()) { rotate_vec(q, y); qq = y; } else { qq = reinterpret_cast<const f32*>(q); }
    for (u32 j = 0; j < m_; ++j) {
      const u32 s = sub_off_[j], sd = sub_dim_[j];
      const f32* cb = centroid_ptr(j); const f32* sub = qq + s;
      f32* row = lut + static_cast<size_t>(j) * K;
      for (u32 c = 0; c < K; ++c) {
        const f32* ce = cb + c * sd; f32 acc = 0.f;
        for (u32 d = 0; d < sd; ++d) { const f32 t = sub[d] - ce[d]; acc += t * t; }
        row[c] = acc;
      }
    }
  }
  size_t lut_size() const { return static_cast<size_t>(m_) * K; }

  distance_t adc(const f32* lut, const u8* code) const {
    f32 acc = 0.f;
    for (u32 j = 0; j < m_; ++j) acc += lut[static_cast<size_t>(j) * K + code[j]];
    return acc;
  }

  // --- serialization (codebook + optional rotation; CN-local single-CN) ---
  size_t params_bytes() const {
    return sizeof(u32) * 3 + (codebook_.size() + R_.size()) * sizeof(f32);
  }
  void write_params(byte_t* dst) const {
    auto* p = reinterpret_cast<u32*>(dst);
    p[0] = dim_; p[1] = m_; p[2] = has_rot() ? 1u : 0u;
    auto* f = reinterpret_cast<f32*>(dst + sizeof(u32) * 3);
    std::memcpy(f, codebook_.data(), codebook_.size() * sizeof(f32));
    if (has_rot()) std::memcpy(f + codebook_.size(), R_.data(), R_.size() * sizeof(f32));
  }
  void read_params(const byte_t* src) {
    const auto* p = reinterpret_cast<const u32*>(src);
    init(p[0], p[1]);
    const auto* f = reinterpret_cast<const f32*>(src + sizeof(u32) * 3);
    std::memcpy(codebook_.data(), f, codebook_.size() * sizeof(f32));
    if (p[2]) { R_.assign(static_cast<size_t>(dim_) * dim_, 0.f); std::memcpy(R_.data(), f + codebook_.size(), R_.size() * sizeof(f32)); }
  }

private:
  u32 dim_{}, m_{};
  vec<u32> sub_off_, sub_dim_;
  vec<f32> codebook_;
  vec<size_t> cb_off_;
  vec<f32> R_;  // dim*dim row-major orthogonal rotation (empty = none)

  void init(u32 dim, u32 m) {
    dim_ = dim; m_ = m; R_.clear();
    sub_off_.assign(m_, 0); sub_dim_.assign(m_, 0); cb_off_.assign(m_, 0);
    size_t off = 0;
    for (u32 j = 0; j < m_; ++j) {
      const u32 s = static_cast<u32>(static_cast<u64>(j) * dim_ / m_);
      const u32 e = static_cast<u32>(static_cast<u64>(j + 1) * dim_ / m_);
      sub_off_[j] = s; sub_dim_[j] = e - s; cb_off_[j] = off;
      off += static_cast<size_t>(K) * sub_dim_[j];
    }
    codebook_.assign(off, 0.f);
  }
  f32* centroid_ptr(u32 j) { return codebook_.data() + cb_off_[j]; }
  const f32* centroid_ptr(u32 j) const { return codebook_.data() + cb_off_[j]; }

  // random orthogonal R via modified Gram-Schmidt on a Gaussian matrix
  void gen_rotation(u32 seed) {
    std::mt19937 rng(seed);
    std::normal_distribution<f32> g(0.f, 1.f);
    R_.assign(static_cast<size_t>(dim_) * dim_, 0.f);
    vec<f32> col(dim_);
    for (u32 row = 0; row < dim_; ++row) {
      for (u32 i = 0; i < dim_; ++i) col[i] = g(rng);
      for (u32 p = 0; p < row; ++p) {
        const f32* q = &R_[static_cast<size_t>(p) * dim_];
        f32 dot = 0.f; for (u32 i = 0; i < dim_; ++i) dot += col[i] * q[i];
        for (u32 i = 0; i < dim_; ++i) col[i] -= dot * q[i];
      }
      f32 nrm = 0.f; for (u32 i = 0; i < dim_; ++i) nrm += col[i] * col[i];
      nrm = std::sqrt(std::max(nrm, 1e-12f));
      f32* dst = &R_[static_cast<size_t>(row) * dim_];
      for (u32 i = 0; i < dim_; ++i) dst[i] = col[i] / nrm;
    }
  }
};

}  // namespace lavd
