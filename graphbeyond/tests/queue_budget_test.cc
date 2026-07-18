#include <cassert>
#include <iostream>

#include "lavd/queue_budget.hh"

int main() {
  assert(lavd::queue_safe_rerank_chunk(1024, 2) == 256);
  assert(lavd::queue_safe_rerank_chunk(1024, 6) == 85);
  assert(lavd::queue_safe_rerank_chunk(1024, 8) == 64);
  assert(lavd::queue_safe_rerank_chunk(1024, 16) == 32);
  assert(lavd::queue_safe_rerank_chunk(8, 8) == 1);
  assert(lavd::queue_safe_rerank_chunk(7, 8) == 0);
  assert(lavd::queue_safe_rerank_chunk(1024, 0) == 0);
  assert(lavd::queue_safe_rerank_chunk(0, 1) == 0);

  std::cout << "queue_budget_test PASS" << std::endl;
  return 0;
}
