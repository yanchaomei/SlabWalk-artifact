// Phase A acceptance test for lavd::Quantizer.
// Build (on cluster, has full include tree + g++-12):
//   g++-12 -std=c++20 -O2 -I src -I rdma-library \
//       src_test/test_quant.cc -o /tmp/test_quant && /tmp/test_quant
//
// Checks: encode/decode round-trip + approx_d2 vs true squared-L2
// relative error within expected bounds (int8 tight, int4 looser),
// and params write/read round-trip.

#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

#include "common/quant.hh"

static double true_d2(const std::vector<float>& a, const std::vector<float>& b) {
  double s = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = static_cast<double>(a[i]) - b[i];
    s += d * d;
  }
  return s;
}

int main() {
  const u32 dim = 128;
  const size_t n = 5000;
  std::mt19937 rng(42);
  std::normal_distribution<float> g(0.f, 1.f);  // continuous float (not uint8)

  std::vector<float> data(n * dim);
  for (auto& v : data) v = g(rng);

  int failures = 0;
  for (u32 bits : {8u, 4u}) {
    lavd::Quantizer q;
    q.fit(dim, bits, data.data(), n);

    // params round-trip
    std::vector<byte_t> pbuf(q.params_bytes());
    q.write_params(pbuf.data());
    lavd::Quantizer q2;
    q2.read_params(pbuf.data());
    if (q2.dim() != dim || q2.bits() != bits) {
      printf("[FAIL] params round-trip bits=%u\n", bits);
      ++failures;
    }

    // approx_d2 vs true_d2 over random pairs
    std::vector<u8> ca(q.code_bytes()), cb(q.code_bytes());
    double sum_rel = 0;
    double max_rel = 0;
    const int trials = 2000;
    for (int t = 0; t < trials; ++t) {
      std::uniform_int_distribution<size_t> pick(0, n - 1);
      size_t i = pick(rng), j = pick(rng);
      std::vector<float> a(data.begin() + i * dim, data.begin() + (i + 1) * dim);
      std::vector<float> b(data.begin() + j * dim, data.begin() + (j + 1) * dim);
      q.encode(a.data(), ca.data());
      q.encode(b.data(), cb.data());
      // q2 (deserialized params) must give identical result
      const double da = q.approx_d2(ca.data(), cb.data());
      const double da2 = q2.approx_d2(ca.data(), cb.data());
      if (std::abs(da - da2) > 1e-3 * std::max(1.0, da)) {
        printf("[FAIL] q2 mismatch bits=%u: %.3f vs %.3f\n", bits, da, da2);
        ++failures;
        break;
      }
      const double dt = true_d2(a, b);
      const double rel = std::abs(da - dt) / std::max(1.0, dt);
      sum_rel += rel;
      max_rel = std::max(max_rel, rel);
    }
    const double avg_rel = sum_rel / trials;
    // Expected: int8 mean rel error < 2%, int4 < 12% on N(0,1) data.
    const double bound = (bits == 8) ? 0.02 : 0.12;
    const bool ok = avg_rel < bound;
    printf("bits=%u code_bytes=%zu  approx_d2 mean_rel=%.4f max_rel=%.4f  bound=%.2f  %s\n",
           bits, q.code_bytes(), avg_rel, max_rel, bound, ok ? "OK" : "FAIL");
    if (!ok) ++failures;
  }

  printf(failures == 0 ? "\nPhase A PASS\n" : "\nPhase A FAIL (%d)\n", failures);
  return failures == 0 ? 0 : 1;
}
