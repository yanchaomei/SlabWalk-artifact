#include <cassert>
#include <cstdint>
#include <vector>

#include "lavd/layout.hh"

int main() {
  constexpr u32 dim = 8;
  constexpr u32 bits = 8;
  constexpr u32 m_max0 = 4;

  lavd::g_rabitq_b = 0;
  lavd::g_rabitq_code = 0;
  lavd::g_pq_m = 0;

  lavd::set_layout_coloc_degree(0);
  std::vector<byte_t> full(lavd::block_stride(m_max0, dim, bits), 0);
  lavd::blk_count(full.data()) = 2;
  byte_t* full_entry = lavd::blk_entry(full.data(), 1, dim, bits, m_max0);
  lavd::ent_slot(full_entry) = 42;
  lavd::ent_rptr(full_entry) = 0x0102030405060708ULL;
  lavd::ent_qvec(full_entry)[0] = 17;

  const lavd::BlockEntryDecoder full_decoder(
      full.data(), dim, bits, m_max0);
  const auto decoded_full = full_decoder.decode(1);
  assert(decoded_full.slot == 42);
  assert(decoded_full.rptr == 0x0102030405060708ULL);
  assert(decoded_full.has_qvec);
  assert(decoded_full.qvec == lavd::ent_qvec(full_entry));
  assert(decoded_full.qvec[0] == 17);

  lavd::set_layout_coloc_degree(1);
  std::vector<byte_t> bounded(lavd::block_stride(m_max0, dim, bits), 0);
  lavd::blk_count(bounded.data()) = 2;
  byte_t* fixed_entry = lavd::blk_entry(bounded.data(), 1, dim, bits, m_max0);
  lavd::ent_slot(fixed_entry) = 77;
  lavd::ent_rptr(fixed_entry) = 0x1112131415161718ULL;

  const lavd::BlockEntryDecoder bounded_decoder(
      bounded.data(), dim, bits, m_max0);
  const auto decoded_fixed = bounded_decoder.decode(1);
  assert(decoded_fixed.slot == 77);
  assert(decoded_fixed.rptr == 0x1112131415161718ULL);
  assert(!decoded_fixed.has_qvec);
  assert(decoded_fixed.qvec == nullptr);

  return 0;
}
