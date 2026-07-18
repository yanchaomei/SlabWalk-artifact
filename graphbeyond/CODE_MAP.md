# SlabWalk Code Map

This directory contains the SlabWalk implementation on the shared
SHINE-derived substrate. The active paper name is SlabWalk; historical source
identifiers such as `lavd` and `crane` remain to avoid an unrelated rename in
performance-sensitive code.

## Physical Access Structure

| Path | Responsibility |
|---|---|
| `src/lavd/layout.hh` | Slab record layout and compact scoring payload |
| `src/lavd/native_descriptor.hh` | descriptor-driven physical-layout discovery |
| `src/lavd/native_owner.hh` | formulaic block-cyclic/range ownership |
| `src/lavd/native_export.hh` | fixed/variable packed placement and accounting |
| `src/lavd/build.hh` | authoritative scan, selection, encoding, bounded staged publication, and snapshot handoff |
| `src/lavd/staged_io.hh` | fixed-stride 64 MiB publication-window planner and range guards |
| `src/lavd/build_snapshot.hh` | ownership of the authoritative build snapshot until resident navigation consumes it |
| `src/node/neighborhood.hh` | local view and decoder for one fetched Slab record |

## Query Processing

| Path | Responsibility |
|---|---|
| `src/hnsw/hnsw.hh` | resident upper descent, Slab level-0 beam, and bounded exact rerank |
| `src/rdma/rdma_reads.hh` | Slab and authoritative-node RDMA primitives |
| `src/coroutine.hh` | per-coroutine visited/search state |
| `src/compute_thread.hh` | QP/CQ ownership, completion accounting, and queue budget |
| `src/crane/crane.hh` | exact resident upper graph; historical code name only |

## Physical Guards

| Path | Responsibility |
|---|---|
| `src/lavd/config.hh` | scoring, budget, packed-layout, and experimental gates |
| `src/lavd/varblock.hh` | live-prefix variable record sizing |
| `src/lavd/rabitq.hh` | compact RaBitQ scoring code |
| `src/lavd/parallel_build.hh` | deterministic build-worker partitioning |
| `src/lavd/parallel_indegree.hh` | deterministic parallel in-degree accounting |
| `src/lavd/materialization_policy.hh` | exact-byte record selection policies and accounting |
| `src/lavd/queue_budget.hh` | safe exact-rerank WR chunk bound |
| `src/lavd/region_capacity.hh` | registered-region capacity contract |

## Boundaries

- `--lavd 0` is the shared-substrate object-native comparator path.
- The manuscript calls it SHINE-derived, not an official SHINE reproduction.
- Compact-code search may change beam order; final reranking is exact only over
  the bounded candidate set.
- Current refresh evidence is offline rematerialization with serving paused.
- Fixed full-materialization builds use bounded staged publication. Variable
  and exact-budget layouts retain record-granular publication.
- The initiating CN reuses its validated HNSW snapshot for resident upper
  navigation; other CNs still obtain the authoritative state independently.
- Counterfactual code paths remain gated and are not active contributions.

See [`../docs/story.md`](../docs/story.md) for contribution ownership,
[`../progress.md`](../progress.md) for current work, and
[`../experiments/README.md`](../experiments/README.md) for measured protocols.
