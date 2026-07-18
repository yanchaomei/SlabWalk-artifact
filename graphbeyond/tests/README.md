# LAVD standalone unit tests

## Runtime index-region capacity

Validates the bounded deployment knob for the authoritative HNSW memory
region. A zero request preserves the 4 GiB legacy default, while explicit
aligned capacities support larger index dumps without requiring a second
binary.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/index_region_capacity_test.cc -o /tmp/index_region_capacity_test && /tmp/index_region_capacity_test
# expect: index_region_capacity_test PASS
```

## Single-pass neighborhood entry decoding

Validates that one packed-entry decode returns a consistent slot, remote
pointer, compact-code pointer, and compact-code presence bit for both the full
layout and a degree-bounded fixed-only tail entry.

```bash
c++ -O2 -std=c++20 -Wall -Wextra -Werror -Isrc -Irdma-library tests/neighborhood_entry_decode_test.cc -o /tmp/neighborhood_entry_decode_test && /tmp/neighborhood_entry_decode_test
# expect: exit status 0
```

## Authoritative snapshot handoff

Validates the move-only ownership handoff that lets resident-upper-graph
construction reuse the Slab builder's authoritative scan exactly once.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/build_snapshot_test.cc -o /tmp/build_snapshot_test && /tmp/build_snapshot_test
# expect: exit status 0
```

## RaBitQ rotation reuse

Validates that post-build parameter initialization can reuse the initiator's
already-fitted random rotation while preserving the byte-exact code and
distance-estimator state produced by deterministic reconstruction. The test
also exercises the mismatched-seed fallback. It requires the same C++20 and
oneTBB contract as the main GraphBeyond binary.

```bash
g++-12 -O2 -std=c++20 -Wall -Wextra -Werror \
  -Isrc -Irdma-library tests/rabitq_rotation_reuse_test.cc \
  -ltbb -o /tmp/rabitq_rotation_reuse_test && \
  /tmp/rabitq_rotation_reuse_test
# expect: exit status 0
```

## Parallel build partitioning

Validates the bounded build-worker policy and proves that deterministic
contiguous partitions visit every uid exactly once.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -pthread -Isrc tests/parallel_build_test.cc -o /tmp/parallel_build_test && /tmp/parallel_build_test
# expect: exit status 0
```

## Deterministic parallel in-degree scan

Validates the exact-byte ranker's parallel graph scan. Repeated edges are
counted, out-of-range neighbor IDs retain the legacy ignored-edge behavior,
and 1/2/3/6 workers must produce the same in-degree vector, admitted UID order,
and selection hash.

```bash
c++ -O2 -std=c++20 -Wall -Wextra -Werror -pthread -Isrc tests/parallel_indegree_test.cc -o /tmp/parallel_indegree_test && /tmp/parallel_indegree_test
# expect: exit status 0
```

## Bounded staged I/O

Validates that large authoritative-index transfers are split into exact,
bounded 64 MiB chunks instead of one oversized registered buffer or WR. It
also validates overflow-safe 64-byte allocation rounding used by all staged
record scratch buffers before they are registered with verbs.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/staged_io_test.cc -o /tmp/staged_io_test && /tmp/staged_io_test
# expect: exit status 0
```

## Native region capacity

Validates the pure per-MN neighborhood-region capacity contract: the legacy
zero sentinel, exact nonzero requests, the params/descriptor lower bound, and
the legacy maximum upper bound. Exact requests must be 64-byte aligned for the
registered allocator. This test has no RDMA or cluster dependency.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/native_region_capacity_test.cc -o /tmp/native_region_capacity_test && /tmp/native_region_capacity_test
# expect: native_region_capacity_test PASS
```

## Query QP/CQ context policy

Validates the legacy four-context default, explicit per-worker context fanout,
the 40-context resource ceiling, and the threads-per-context calculation used
by send-queue validation.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/query_contexts_test.cc -o /tmp/query_contexts_test && /tmp/query_contexts_test
# expect: query_contexts_test PASS
```

## Startup wire contract

Locks the original 12-byte CN-to-MN parameter frame, including every field
offset. The formerly implicit two-byte gap now carries the resolved query
QP/CQ-context count. The test also checks the version-2, 24-byte compute-node
agreement frame used before LAVD token reception.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/startup_wire_test.cc -o /tmp/startup_wire_test && /tmp/startup_wire_test
# expect: startup_wire_test PASS
```

## Native owner resolver

Validates the MB-LAVD / LAVD-native formulaic owner resolver and packed
fixed-stride L0 read-plan helper without RDMA dependencies. This is the
base invariant for replacing the current multi-MN global-uid sparse
region with per-MN compact sidecars. The same test also covers the
legacy sparse fixed-stride read plan used by CWC fallback requests.

```bash
g++ -O2 -std=c++17 -Isrc tests/native_owner_test.cc -o /tmp/native_owner_test && /tmp/native_owner_test
# expect: native_owner_test PASS
```

## Queue-safe rerank budget

Checks the worst-case per-coroutine rerank chunk derived from the QP send-queue
depth and coroutine count. The aggregate chunk occupancy stays within half of
the SQ when possible, and invalid `coroutines > SQ` configurations are rejected.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/queue_budget_test.cc -o /tmp/queue_budget_test && /tmp/queue_budget_test
# expect: queue_budget_test PASS
```

## Native export plan

Validates the fixed-stride packed sidecar export plan and RDMA-free physical
byte accounting. Coverage includes uneven block-cyclic ownership, exact
agreement with `FixedExportPlan`, variable-record header/map/table/record
bytes per MN, aggregate bytes, vector-length checks, and overflow rejection.

```bash
g++ -O2 -std=c++17 -Isrc tests/native_export_test.cc -o /tmp/native_export_test && /tmp/native_export_test
# expect: native_export_test PASS
```

## Native descriptor

Validates the 64 B native sidecar descriptor stored immediately before the
legacy N/H tail. Coverage includes v1 fixed-layout compatibility, v3 fixed
and variable layouts, scoring-code kind/bits, budget-map placement and shift,
the record ABI, per-MN offset-table discovery, feature compatibility, rejection
of unsafe v2 descriptors, and malformed metadata.

```bash
g++ -O2 -std=c++17 -Isrc tests/native_descriptor_test.cc -o /tmp/native_descriptor_test && /tmp/native_descriptor_test
# expect: native_descriptor_test PASS
```

## Exact-byte materialization policy

Validates strict byte-budget and policy parsing, exact-cap accounting,
benefit-per-byte ordering without floating-point comparisons, deterministic
tie-breaking and selection hashes, compact-map ranks, and rejection of
malformed, zero-sized, or overflowing candidates. Full-budget selections must
materialize all records without charging or publishing a compact map; partial
selections include the map in their exact-byte accounting.

```bash
g++-12 -O2 -std=c++20 -Wall -Wextra -Werror -Isrc tests/materialization_policy_test.cc -o /tmp/materialization_policy_test && /tmp/materialization_policy_test
# expect: materialization_policy_test PASS
```

## Offline maintenance guards

Rejects offline differential rematerialization for multi-MN, packed,
reordered, and variable-record layouts, and prevents a persistent mirror from
reading a shrinking or over-capacity authoritative tail.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/maintenance_guard_test.cc -o /tmp/maintenance_guard_test && /tmp/maintenance_guard_test
# expect: exit status 0
```

## Query-latency quantiles

Validates the nearest-rank p50/p95/p99 helper used after thread-local query
latency samples are merged. Query execution records into pre-reserved
per-thread buffers; sorting happens only after the measured query pool ends.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/latency_quantile_test.cc -o /tmp/latency_quantile_test && /tmp/latency_quantile_test
# expect: exit status 0
```

## Query-result fingerprint

Validates the deterministic final-result evidence hash used by formal binary
A/B campaigns. The hash is invariant to thread/container iteration order,
but remains sensitive to the ordered top-k IDs within every query and rejects
duplicate query IDs.

```bash
c++ -O2 -std=c++17 -Wall -Wextra -Werror -Isrc tests/query_result_fingerprint_test.cc -o /tmp/query_result_fingerprint_test && /tmp/query_result_fingerprint_test
# expect: exit status 0
```

## PQ-LAVD standalone unit test

Validates the PQ primitive (`src/lavd/pq.hh`) without the cluster:
encode determinism, codebook serialize roundtrip (recall-neutral
precondition), and ADC ranking quality (true top-10 ∈ ADC top-100).

```bash
mkdir -p /tmp/pqstub/library /tmp/pqstub/common
printf '#pragma once\n#include <cstdint>\n#include <vector>\nusing u8=uint8_t;using u16=uint16_t;using u32=uint32_t;using u64=uint64_t;\nusing f32=float;using f64=double;using byte_t=unsigned char;\ntemplate<class T>using vec=std::vector<T>;\n' > /tmp/pqstub/library/types.hh
printf '#pragma once\n#include <library/types.hh>\nusing element_t=f32;using distance_t=f32;\n' > /tmp/pqstub/common/types.hh
g++ -O2 -std=c++17 -I/tmp/pqstub -Isrc tests/pq_test.cc -o /tmp/pq_test && /tmp/pq_test
# expect: determinism=1 roundtrip=1 recall@10-in-top100~1.0 code_bytes=16 (8x smaller)
```
