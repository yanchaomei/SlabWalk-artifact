# SlabWalk PVLDB artifact workflow

This document describes the auditable path from source campaigns to paper
figures.  It separates four states that must not be conflated:

1. a benchmark process completed;
2. its raw sources and protocol metadata passed a campaign validator;
3. the campaign was atomically promoted into `results/vldb_final_evidence`;
4. the complete evidence gate generated the figures used by the manuscript.

The current repository is an internally certified closure workspace. Every
required campaign has been promoted, the final evidence gate passes, and the
current PDF and Overleaf archive are content-bound to that gate. They remain an
internal preview rather than an upload-ready submission until the authors,
affiliations, public artifact URL, disclosure sign-off, and CMT declarations in
`paper_vldb/SUBMISSION_PREFLIGHT.md` are completed.

The post-closure optimization route is tracked separately in
[`progress.md`](progress.md), [`docs/story.md`](docs/story.md), and
[`experiments/README.md`](experiments/README.md). Development binaries and
campaigns from that route must not replace the certified binary or any
directory under `results/vldb_final_evidence/` in place. A replacement paper
release requires a new complete promotion, claim assembly, figure build, and
manifest.

## 1. Environment

The measured deployment uses seven SKV hosts with 100 Gbps RoCE v2,
ConnectX-6 DX NICs, dual Xeon Gold 5218R CPUs, and passive memory nodes.  The
paper records the exact host roles for each campaign.  The validated software
build uses:

- GCC 12.5 with C++20;
- CMake 3.22.6;
- oneTBB with `oneapi/tbb` headers;
- IB verbs, Boost program-options, and pthreads.

Build the two system executables with:

```bash
cmake -S graphbeyond -B graphbeyond/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=g++-12
cmake --build graphbeyond/build -j 16
```

The formal measurements use the frozen SlabWalk binary SHA-256:

```text
2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
```

Rebuilding the source validates buildability; it does not recreate that exact
binary and must not be substituted into an existing campaign.

## 2. Fast local checks

```bash
python3 -m unittest discover -s experiments/sigmetrics -p 'test_*.py'
git diff --check
```

The Python suite tests parsers, fixed-query-pool fingerprints, run ownership,
cardinality and repeat checks, atomic assemblers, exclusion records, figure
contracts, publication fonts, and the final evidence gate.  Independent C++
tests live under `graphbeyond/tests/`; their verified compiler command is
documented in `paper_vldb/SUBMISSION_PREFLIGHT.md`. The certified local run on
2026-07-14 completed 412 Python tests with `OK`. After the fail-closed
post-closure harness additions, the complete suite on 2026-07-16 completed 526
tests in 65.518 seconds with `OK`. After adding the fixed-stride publication,
snapshot-reuse, rotation-reuse, and formal-frontier launcher contracts, the
complete suite on 2026-07-17 completed 538 tests in 68.782 seconds with `OK`.
The final 2026-07-19 closure run completed 689 tests in 79.363 seconds with
`OK`; all 47 experiment shell scripts passed `bash -n`, and `git diff --check`
passed. These counts describe named tree snapshots and must be refreshed after
source changes.

### Query-path optimization decisions

The frozen binary incorporates only candidates that passed an alternating
AB/BA experiment with five paired DEEP1M repetitions and unchanged recall and
bytes/query. Inline neighborhood-entry decoding increases mean throughput from
9,582.2 to 9,897.8 QPS (paired delta +315.6 QPS; two-sided 95% Student-t
half-width 314.3 QPS). Assigning one QP/CQ query context per worker increases
mean throughput from 9,928.4 to 11,754.2 QPS (paired speedup 1.1839x; 95%
half-width 0.0231x) and reduces `pthread_spin_lock` samples from 39.53% to
15.90%. A candidate that polled the completion queue once per scheduler sweep
showed no supported improvement and was reverted before the binary was frozen.

The run identities, binary hashes, raw evidence directories, and confidence
calculations are recorded in
`results/vldb_acceptance_closure_20260713/OPTIMIZATION_DECISIONS.md`. These
experiments justify implementation choices; they are not additional frontier
points and do not enter the paper figures independently.

## 3. Evidence ownership

Only directories under `results/vldb_final_evidence/` may feed the final
figures.  Each promoted directory retains or hashes its raw sources and records
the scripts that derived its summaries.  The gate expects these components:

| Evidence directory | Required content |
|---|---|
| `frontier/` | Three 10M datasets, three systems, five repeats per point, fixed 10K query pools |
| `query_pools/` | Nine cross-format manifests plus the exact-IP TTI spot check |
| `robustness/` | 17 five-repeat query-control cells |
| `worker_scaling/` | Three systems by four worker counts by five repeats |
| `topology_control/` | d-HNSW loopback and separated-host cells, five repeats each |
| `model_controls/` | 25 RDMA control cells, five repeats each |
| `resource_ledger/` | Three layouts by three stripe counts, five repeats each |
| `cache_control/` | Four cache budgets, five measured runs each |
| `query_profile/` | Frozen-binary query-only profile and retained `perf.data` |
| `colocation_control/` | Six expansion-completeness cells, five repeats each |
| `mechanism_controls/` | Six materialization budgets plus resident-navigation controls |
| `build_cost/` | SIFT, DEEP, and GIST derived builds, five repeats each |
| `build_scaling_10m/` | One canonical packed-variable frontier startup per 10M dataset and repeat |
| `index_construction/` | Graph-preserving SIFT10M and TTI10M construction records |
| `lifecycle_controls/` | Offline replay and TTI representation-boundary sources |
| `physical_design_advisor/` | Sealed 162-row policy source plus a fixed 0--2 training / 3--5 held-out advisor validation |

Warmups, partial runs, failed builders, OOM campaigns, and superseded parser
outputs remain retained under `results/vldb_acceptance_closure_20260713/`, but
their exclusion manifests prevent them from entering a promoted aggregate.

### Post-closure builder bundles

The candidate staged-builder campaign is self-contained but remains outside
`results/vldb_final_evidence/` until the complete replacement gate passes.
After transferring one of these bundles, validate both byte integrity and
meaning:

```bash
python3 experiments/sigmetrics/vldb_evidence_bundle.py verify \
  --root "$BUILDER_BUNDLE"
python3 experiments/sigmetrics/summarize_vldb_build_pipeline.py verify \
  --bundle "$BUILDER_BUNDLE" \
  --expected-sha bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-compute-host skv-node3
```

The second command reparses every sealed child and recomputes the run table,
confidence summary, and comparison record. It accepts relocation of a bundle
between machines only when each retained absolute path still identifies the
same hash-bound artifact by its complete bundle-relative suffix. A valid
development bundle is evidence for promotion review; it does not authorize an
in-place overwrite of the certified release.

The serial/staged A/B gate has an equivalent raw-artifact verifier:

```bash
python3 experiments/sigmetrics/vldb_evidence_bundle.py verify \
  --root "$BINARY_AB_BUNDLE"
python3 experiments/sigmetrics/verify_vldb_binary_ab.py \
  --root "$BINARY_AB_BUNDLE" \
  --expected-sha-a bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-sha-b bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-compute-host skv-node3
```

This second pass does not trust the sealed CSV/JSON derivatives. It reparses
each child result and stderr artifact, rechecks executable and input
provenance, and recomputes the paired and order-stratified A/B statistics.

If a `/proc` RSS sidecar accompanies the campaign, run
`summarize_vldb_rss_sidecar.py` before sealing that sidecar. It joins samples
to child processes by the recorded process start time, binds the parent root
manifest and sampler-source SHA, and rejects unknown or configuration-drifted
processes. RSS remains diagnostic unless the promoted release gate explicitly
names the sidecar and its minimum per-variant coverage.

The matched-byte materialization campaign uses the same two-stage rule:

```bash
python3 experiments/sigmetrics/vldb_evidence_bundle.py verify \
  --root "$POLICY_BUNDLE"
python3 experiments/sigmetrics/summarize_vldb_materialization_policy.py verify \
  --bundle "$POLICY_BUNDLE" \
  --expected-sha bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-compute-host skv-node3
```

Its semantic pass reconstructs the cyclic policy schedule, verifies every
cell's executable, input, and raw-artifact provenance, and then regenerates
both the per-run and confidence-summary tables. Hash validity alone is not a
promotion condition.

### Physical-design advisor validation

The advisor is derived from the same sealed 162-row matched-byte campaign, but
its source and validation are independently sealed under
`results/vldb_final_evidence/physical_design_advisor/`. Build or reproduce the
validation with:

```bash
python3 experiments/sigmetrics/validate_vldb_physical_design_advisor.py build \
  --bundle results/vldb_v3_evidence_v2_node3_materialization_policy_formal_20260716T204600Z \
  --out results/vldb_physical_design_advisor_20260719 \
  --expected-sha bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-compute-host skv-node3
python3 experiments/sigmetrics/validate_vldb_physical_design_advisor.py verify \
  --bundle results/vldb_v3_evidence_v2_node3_materialization_policy_formal_20260716T204600Z \
  --validation results/vldb_physical_design_advisor_20260719 \
  --expected-sha bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8 \
  --expected-compute-host skv-node3
```

The builder fixes repeats 0--2 as the selection sample and repeats 3--5 as the
held-out sample before applying the gate. For each dataset and byte budget, it
rejects candidates below Recall@10 0.90 or above the requested MN bytes, then
maximizes the two-sided Student-t 95% lower confidence bound on QPS. Promotion
requires every selected layout to remain feasible, every held-out QPS ratio to
the feasible oracle to be at least 0.98, and the nine-cell geometric mean to be
at least 0.99. This is a strict post-hoc split over an existing campaign, not a
prospective predictor or online reconfiguration mechanism.

### Multi-CN negative diagnostic

`results/vldb_multicn_formal_20260719f/` is deliberately outside
`results/vldb_final_evidence/`. It contains the complete 135-row fixed-pool
matrix for SHINE-derived, SlabWalk, and d-HNSW at one, two, and three CNs. Its
promotion thresholds were fixed before aggregation and were not changed after
measurement. Verify both the complete-file inventory and semantic matrix with:

```bash
python3 experiments/sigmetrics/seal_vldb_multicn_campaign.py verify \
  --root results/vldb_multicn_formal_20260719f
```

The campaign has protocol fingerprint
`3d307e42c0d412aafe221f2973d2a55b78b46ea919fe76421e3080367d3b17ec`
and `MULTICN_SEALED.json` SHA-256
`88ecc25d1228af5f6331b728c1084b6d8d1f13db4f960040f0334a3d20d61356`.
SlabWalk preserves recall, but its 3CN/1CN QPS CI-lows are 1.87270, 2.07652,
and 1.65983 for SIFT, DEEP, and GIST, all below 2.30. The 3CN fairness CI-low
also falls below 0.95 on SIFT (0.94980) and GIST (0.93164). The seal therefore
records `promotion_ready=false`. The public artifact manifest lists this tree
as a diagnostic campaign; the publication release manifest does not admit it.

## 4. Query-pool and frontier promotion

The query-pool bundle is assembled independently of performance results:

```bash
python3 experiments/sigmetrics/assemble_vldb_query_pools.py \
  --base-dir results/vldb_acceptance_closure_20260713/query_pools_base_six_20260714 \
  --tti-graph-dir results/vldb_acceptance_closure_20260713/query_pools_text_sift_recovery_v3_20260714 \
  --tti-dhnsw-manifest results/vldb_acceptance_closure_20260713/dhnsw_text_sift_node7_final_v3_20260713/provenance/tti10m_dhnsw.json \
  --out-dir results/vldb_final_evidence/query_pools
```

After all five SlabWalk/SHINE-derived SIFT10M and TTI10M repeats complete, the
three-dataset frontier is published atomically:

```bash
python3 experiments/sigmetrics/assemble_vldb_frontier_bundle.py \
  --deep-bundle results/vldb_acceptance_closure_20260713/deep10_frontier_validated_v1 \
  --deep-sw-campaign results/vldb_acceptance_closure_20260713/frontier_deep10_sw_final_v1_20260713 \
  --deep-dhnsw-campaign results/vldb_acceptance_closure_20260713/dhnsw_deep10m_exactgt_formal_v2_20260713 \
  --sw-campaign results/vldb_acceptance_closure_20260713/frontier_text_sift_sw_final_v3_20260714 \
  --dhnsw-campaign results/vldb_acceptance_closure_20260713/dhnsw_text_sift_node7_final_v3_20260713 \
  --query-pools results/vldb_final_evidence/query_pools \
  --out-dir results/vldb_final_evidence/frontier
```

Both assemblers stage into a temporary directory and rename only after all
matrix, hash, query-count, repeat, and provenance checks pass. The 10M bundle
retains all four source campaign manifests and maps every dataset/method cell
to exactly one source; a dataset-level merge is not represented as one
synthetic campaign.

The same completed frontier campaigns also close the 10M derivation-cost
control.  Exactly one fixed-`ef` Slab startup is selected per dataset and repeat;
the other frontier widths are deliberately not counted as independent builds:

```bash
python3 experiments/sigmetrics/assemble_vldb_10m_build_scaling.py \
  --deep-campaign results/vldb_acceptance_closure_20260713/frontier_deep10_sw_final_v1_20260713 \
  --text-sift-campaign results/vldb_acceptance_closure_20260713/frontier_text_sift_sw_final_v3_20260714 \
  --expected-binary-sha 2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6 \
  --out-dir results/vldb_final_evidence/build_scaling_10m
```

The 10M assembler pins its own SHA-256, reparses every retained measurement,
physical-accounting log, and frontier row, and recomputes the summaries.  Its
signed inventory is closed: the 45 selected run sources, two source campaign
manifests, and prescribed top-level files are the only admitted members.

## 5. Final evidence gate

Run from the repository root:

```bash
python3 experiments/sigmetrics/validate_vldb_final_evidence.py \
  --frontier results/vldb_final_evidence/frontier \
  --robustness results/vldb_final_evidence/robustness \
  --worker-scaling results/vldb_final_evidence/worker_scaling \
  --topology-control results/vldb_final_evidence/topology_control \
  --build-cost results/vldb_final_evidence/build_cost \
  --build-scaling-10m results/vldb_final_evidence/build_scaling_10m \
  --index-construction results/vldb_final_evidence/index_construction \
  --lifecycle-controls results/vldb_final_evidence/lifecycle_controls \
  --cache-control results/vldb_final_evidence/cache_control \
  --colocation-control results/vldb_final_evidence/colocation_control \
  --mechanism-controls results/vldb_final_evidence/mechanism_controls \
  --query-profile results/vldb_final_evidence/query_profile \
  --resource-ledger results/vldb_final_evidence/resource_ledger \
  --model-controls results/vldb_final_evidence/model_controls \
  --query-pools results/vldb_final_evidence/query_pools \
  --expected-slabwalk-sha 2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6 \
  --expected-profile-runner-sha 3d20f0968654a3ad27fa1b4624c5425e1844258a1a75cc905232ac78e68bcf6e \
  --expected-colocation-campaign-id vldb-colocation-deep1-final-v4-20260715 \
  --expected-colocation-protocol-fingerprint d99de8f75c7153f0182b21a9804e050a45c8c4e60c04ceac37e7071afa4c8bb5 \
  --expected-mechanism-campaign-id vldb-mechanism-controls-final-v6-20260715 \
  --expected-mechanism-protocol-fingerprint d60f2d12f0f23c2bbccecb65db1a2fe074ba819ef11567887c6734768129e31a \
  --out results/vldb_final_evidence/evidence_gate.json
```

This command must fail if any source, matrix cell, repeat, binary hash,
query-pool identity, profile, or retained raw file is missing or inconsistent.

## 6. Figures and manuscript

Only after the gate succeeds:

```bash
bash experiments/sigmetrics/generate_vldb_final_figures.sh

cd paper_vldb
SOURCE_DATE_EPOCH=946684800 FORCE_SOURCE_DATE=1 \
  latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

The figure pipeline regenerates all numerical evaluation figures and the
cache/profile motivation inset in staging.  Each output must be a nonempty,
single-page, landscape, unencrypted PDF with embedded non-Type-3 fonts before
it can replace a manuscript figure.

The minimal Overleaf source archive can be built before submission to certify
that the current internal preview is self-contained. It is upload-ready only
after all submission blockers are resolved:

```bash
python3 experiments/sigmetrics/package_vldb_overleaf.py \
  --paper-dir paper_vldb \
  --release-manifest results/vldb_final_evidence/release_bundle.json \
  --out paper_vldb/SlabWalk_PVLDB20_Overleaf.zip \
  --force
```

The packager admits exactly the class, bibliography style, `main.tex`,
`refs.bib`, generated claim macros, and nine PDF figures referenced by
`main.tex`. It verifies all generated inputs against the release manifest,
normalizes ZIP metadata, and checks every one of the 14 members against the
source tree before publication.

Before release, complete every open item in
[`paper_vldb/SUBMISSION_PREFLIGHT.md`](paper_vldb/SUBMISSION_PREFLIGHT.md),
including author metadata, artifact URL, AI-tool disclosure sign-off, final
claim generation from `manuscript_claims.json`, a fresh compile whose
non-reference content ends by page 12, and a visual audit of every page.

## 7. Interpretation boundaries

- SHINE-derived and SlabWalk share one authoritative graph and substrate.
- d-HNSW uses its own partition index and released routing path; it is not a
  same-graph ablation.
- The primary d-HNSW frontier keeps its favorable released loopback topology;
  the separated-host experiment is reported as sensitivity evidence.
- The TTI compact-code result is a representation boundary.  There is no
  fp32-in-Slab control, so it is not a compression-only causal claim.
- Offline replay measures record-selection and rewrite amplification with
  serving paused.  It is not an online-update or fault-recovery result.
