#include <cassert>
#include <iostream>

#include "common/query_contexts.hh"

int main() {
  using query_contexts::is_valid_request;
  using query_contexts::max_threads_per_context;
  using query_contexts::resolve;

  assert(resolve(1, 0) == 1);
  assert(resolve(10, 0) == 4);
  assert(resolve(40, 0) == 4);
  assert(resolve(10, 1) == 1);
  assert(resolve(10, 10) == 10);
  assert(resolve(40, 40) == 40);

  assert(is_valid_request(10, 0));
  assert(is_valid_request(10, 10));
  assert(!is_valid_request(0, 0));
  assert(!is_valid_request(10, 11));
  assert(!is_valid_request(80, 41));

  assert(max_threads_per_context(10, 4) == 3);
  assert(max_threads_per_context(10, 10) == 1);
  assert(max_threads_per_context(40, 8) == 5);

  std::cout << "query_contexts_test PASS" << std::endl;
  return 0;
}
