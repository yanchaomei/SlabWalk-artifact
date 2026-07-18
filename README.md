# SlabWalk artifact

This repository accompanies *SlabWalk: An Expansion-Oriented Physical Access
Structure for Disaggregated Vector Search*. SlabWalk derives a remote access
structure from an authoritative HNSW index and retrieves the data required for
one level-0 graph expansion with one remote read.

The artifact contains the implementation, experiment harnesses, promoted raw
measurements, aggregate tables, query-pool fingerprints, generated claim
macros, and the nine figures bound to the manuscript release. Datasets and
d-HNSW are not redistributed; their preparation and pinned baseline workflow
are documented in [`experiments/README.md`](experiments/README.md).

## Integrity check

Every payload file is recorded in `artifact_manifest.json` and `SHA256SUMS`.
From the repository root, verify the deposit before using it:

```bash
shasum -a 256 -c SHA256SUMS
```

`results/vldb_final_evidence/release_bundle.json` is the publication marker.
It binds the evidence gate, generated claims, and all manuscript figures by
SHA-256. Failed and superseded campaigns are retained outside the promoted
tree and cannot enter a figure through this marker.

## Fast validation

The fail-closed harness tests run without an RDMA cluster:

```bash
python3 -m unittest discover -s experiments/sigmetrics -p 'test_*.py'
```

The tests cover query-pool identity, parser cardinality, exact repeat counts,
campaign ownership, statistical gates, atomic promotion, figure contracts,
and deterministic packaging. Some plotting tests require the Python packages
listed in `requirements.txt`; PDF checks additionally use Poppler and
Ghostscript command-line tools.

The promoted evidence can be revalidated and the numerical figures regenerated
with:

```bash
bash experiments/sigmetrics/generate_vldb_final_figures.sh
```

This command recomputes the evidence gate and manuscript claims before
rendering. It fails on a missing raw source, changed executable or runner hash,
incomplete matrix, query-pool mismatch, unverified PDF, or stale publication
input. See [`ARTIFACT.md`](ARTIFACT.md) for the expanded command, source
ownership rules, and the distinction between a completed run and promoted
paper evidence.

## Building SlabWalk

The implementation targets Linux with GCC 12, CMake, a C++20 standard library,
IB verbs development headers, Boost program-options, pthreads, and oneTBB:

```bash
cmake -S graphbeyond -B graphbeyond/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-12
cmake --build graphbeyond/build -j 16
```

Rebuilding validates source portability but does not recreate the exact frozen
measurement binary. The certified binary SHA-256 and every campaign-specific
input hash are recorded in the evidence manifests. A rebuilt binary must use a
new campaign root and pass the complete promotion gate before replacing paper
evidence.

## Running experiments

The full benchmark requires passive-memory and compute hosts connected by
one-sided RDMA. Dataset locations, host roles, executable paths, worker counts,
query widths, warmups, repetitions, and output roots are explicit environment
variables in the scripts under `experiments/sigmetrics/`. Start with a dry run,
then use a fresh campaign ID:

```bash
DRY_RUN=1 DATASETS=DEEP1M METHODS=slabwalk \
  bash experiments/sigmetrics/run_frontier_repeated.sh
```

Each retained campaign uses a fixed 10,000-query pool, records complete input
and binary fingerprints, and separates warmups from five measured repeats.
The graph-preserving comparator is labeled SHINE-derived because it uses the
shared SlabWalk substrate rather than claiming to reproduce every component of
the released SHINE system. The d-HNSW scripts preserve its released
partition-fetch path and record the compatibility patch and runtime bundle.

## Scope

- SlabWalk preserves the authoritative global HNSW graph.
- Derived-state refresh is evaluated offline with serving paused.
- Compact scoring can alter beam order; the retained TTI experiment is a
  representation boundary, not a compression-only causal claim.
- Multi-host measurements report aggregate throughput and per-CN fairness;
  latency is reported only where the protocol measures a complete logical
  query distribution.

## License

The packaged implementation is distributed under the MIT License in
[`LICENSE`](LICENSE). Dataset, baseline, and bundled third-party licenses remain
with their respective authors.
