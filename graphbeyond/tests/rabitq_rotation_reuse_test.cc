#include <cassert>
#include <cstring>
#include <vector>

#include "lavd/rabitq.hh"

int main() {
  constexpr u32 dim = 8;
  constexpr u32 bits = 2;
  constexpr u32 sample_count = 4;
  constexpr u32 seed = 12345;

  std::vector<f32> sample(static_cast<size_t>(sample_count) * dim);
  for (size_t i = 0; i < sample.size(); ++i) {
    sample[i] = static_cast<f32>((static_cast<int>(i % 11) - 5) * 0.125);
  }

  lavd::RaBitQ built;
  built.fit(dim, bits, sample.data(), sample_count, seed);

  lavd::RaBitQ regenerated;
  regenerated.init_shared(dim, bits, built.c.data(), seed);

  lavd::RaBitQ reused;
  assert(reused.init_shared_reusing_rotation(
      dim, bits, built.c.data(), built, seed));
  assert(reused.rotation_seed == seed);
  assert(reused.P.size() == regenerated.P.size());
  assert(std::memcmp(reused.P.data(), regenerated.P.data(),
                     reused.P.size() * sizeof(f32)) == 0);
  assert(reused.lvl == regenerated.lvl);
  assert(reused.thr == regenerated.thr);

  std::vector<byte_t> regenerated_code(regenerated.code_bytes());
  std::vector<byte_t> reused_code(reused.code_bytes());
  std::vector<f32> regenerated_u(dim), regenerated_rot(dim);
  std::vector<f32> reused_u(dim), reused_rot(dim);
  f32 regenerated_norm = 0;
  f32 regenerated_dot = 0;
  f32 reused_norm = 0;
  f32 reused_dot = 0;
  regenerated.encode(sample.data(), regenerated_code.data(),
                     &regenerated_norm, &regenerated_dot,
                     regenerated_u.data(), regenerated_rot.data());
  reused.encode(sample.data(), reused_code.data(), &reused_norm, &reused_dot,
                reused_u.data(), reused_rot.data());
  assert(regenerated_code == reused_code);
  assert(regenerated_norm == reused_norm);
  assert(regenerated_dot == reused_dot);

  lavd::RaBitQ wrong_seed;
  wrong_seed.fit(dim, bits, sample.data(), sample_count, seed + 1);
  lavd::RaBitQ fallback;
  assert(!fallback.init_shared_reusing_rotation(
      dim, bits, built.c.data(), wrong_seed, seed));
  assert(fallback.rotation_seed == seed);
  assert(fallback.P == regenerated.P);

  return 0;
}
