#pragma once

// GraphBeyond LAVD — Phase A: scalar quantizer (QTF enabler).
//
// Symmetric per-dimension affine quantization (matches the offline
// `experiments/qtf_feasibility.py` sq8/sq4 paths whose recall was
// validated, incl. the rotation float-native control):
//
//   lo[d]    = min over sample of x[d]
//   scale[d] = (max[d] - lo[d]) / (2^bits - 1)
//   code     = clip(round((x[d]-lo[d]) / scale[d]), 0, 2^bits-1)
//   approx d^2(a,b) = sum_d ( (code_a[d]-code_b[d]) * scale[d] )^2
//
// The `lo` offset cancels in the code difference, so distance only
// needs `scale`. bits in {8,4}. int4 packs two codes per byte.

#include <algorithm>
#include <cmath>
#include <cstring>

#include <library/types.hh>  // u8,u16,u32,f32,vec,span,byte_t

#include "common/types.hh"  // element_t, distance_t

namespace lavd {

class Quantizer {
public:
  Quantizer() = default;

  // Fit lo_/scale_ from a flat row-major buffer data[n*dim].
  void fit(u32 dim, u32 bits, const element_t* data, size_t n) {
    init(dim, bits);
    vec<f32> hi(dim, -1e30f);
    lo_.assign(dim, 1e30f);
    for (size_t i = 0; i < n; ++i) {
      const element_t* row = data + i * dim;
      for (u32 d = 0; d < dim; ++d) {
        lo_[d] = std::min(lo_[d], static_cast<f32>(row[d]));
        hi[d] = std::max(hi[d], static_cast<f32>(row[d]));
      }
    }
    scale_.assign(dim, 0.f);
    for (u32 d = 0; d < dim; ++d) {
      const f32 rng = std::max(hi[d] - lo_[d], 1e-6f);
      scale_[d] = rng / static_cast<f32>(levels_ - 1);
    }
  }

  u32 dim() const { return dim_; }
  u32 bits() const { return bits_; }

  // Bytes one encoded vector occupies (int8: dim, int4: ceil(dim/2)).
  size_t code_bytes() const { return bits_ == 8 ? dim_ : (dim_ + 1) / 2; }

  // Encode x[dim_] into out[code_bytes()].
  void encode(const element_t* x, u8* out) const {
    if (bits_ == 8) {
      for (u32 d = 0; d < dim_; ++d) out[d] = static_cast<u8>(quant(x[d], d));
    } else {  // int4 packed: even dim -> low nibble, odd dim -> high nibble
      std::memset(out, 0, code_bytes());
      for (u32 d = 0; d < dim_; ++d) {
        const u8 q = static_cast<u8>(quant(x[d], d)) & 0x0F;
        if (d & 1u)
          out[d >> 1] |= static_cast<u8>(q << 4);
        else
          out[d >> 1] |= q;
      }
    }
  }

  // Approx squared-L2 between two code buffers of THIS quantizer.
  distance_t approx_d2(const u8* a, const u8* b) const {
    f32 acc = 0.f;
    if (bits_ == 8) {
      for (u32 d = 0; d < dim_; ++d) {
        const f32 diff = (static_cast<f32>(a[d]) - static_cast<f32>(b[d])) * scale_[d];
        acc += diff * diff;
      }
    } else {
      for (u32 d = 0; d < dim_; ++d) {
        const u8 ca = (d & 1u) ? static_cast<u8>(a[d >> 1] >> 4) : static_cast<u8>(a[d >> 1] & 0x0F);
        const u8 cb = (d & 1u) ? static_cast<u8>(b[d >> 1] >> 4) : static_cast<u8>(b[d >> 1] & 0x0F);
        const f32 diff = (static_cast<f32>(ca) - static_cast<f32>(cb)) * scale_[d];
        acc += diff * diff;
      }
    }
    return acc;
  }

  // --- params (lo_/scale_) serialization for CN<->MN token exchange ---
  size_t params_bytes() const { return sizeof(u32) * 2 + 2u * dim_ * sizeof(f32); }

  void write_params(byte_t* dst) const {
    auto* p = reinterpret_cast<u32*>(dst);
    p[0] = dim_;
    p[1] = bits_;
    auto* f = reinterpret_cast<f32*>(dst + sizeof(u32) * 2);
    std::memcpy(f, lo_.data(), dim_ * sizeof(f32));
    std::memcpy(f + dim_, scale_.data(), dim_ * sizeof(f32));
  }

  void read_params(const byte_t* src) {
    const auto* p = reinterpret_cast<const u32*>(src);
    init(p[0], p[1]);
    const auto* f = reinterpret_cast<const f32*>(src + sizeof(u32) * 2);
    lo_.assign(f, f + dim_);
    scale_.assign(f + dim_, f + 2 * dim_);
  }

private:
  u32 dim_{}, bits_{}, levels_{};
  vec<f32> lo_, scale_;

  void init(u32 dim, u32 bits) {
    dim_ = dim;
    bits_ = bits;
    levels_ = 1u << bits;  // 256 for int8, 16 for int4
  }

  u32 quant(f32 v, u32 d) const {
    const f32 q = std::round((v - lo_[d]) / scale_[d]);
    const f32 c = std::min(std::max(q, 0.f), static_cast<f32>(levels_ - 1));
    return static_cast<u32>(c);
  }
};

}  // namespace lavd
