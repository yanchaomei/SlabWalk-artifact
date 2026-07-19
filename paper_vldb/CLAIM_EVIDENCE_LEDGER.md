# SlabWalk PVLDB Claim-to-Evidence Ledger

This ledger records the final, admitted evidence behind the PVLDB manuscript.
It is deliberately narrower than a lab notebook: failed, partial, warmup, and
superseded campaigns remain in provenance, but no such row can feed a figure or
a manuscript number.

## Release Contract

- Frozen SlabWalk/SHINE-derived binary SHA-256:
  `2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6`.
- Final evidence gate:
  `results/vldb_final_evidence/evidence_gate.json`, SHA-256
  `899493c0dd96cbf6aecc26945622e872bc142dbc4d46812d58cf920cfd0555ce`.
  It reports `ready_for_plotting=true` and verifies all 28 files in the
  composite frontier's exact SHA-256 inventory.
- Gated manuscript claims:
  `results/vldb_final_evidence/manuscript_claims.json`, SHA-256
  `96ae31cfdbf1dad311ae6bab45a0e23127a2cc6dcb9299fce11ec1d150a5c3ea`.
- Atomic release manifest:
  `results/vldb_final_evidence/release_bundle.json`, SHA-256
  `3162239595c0b30176a5844221a3dc018e92841c0ef9a581577dadecc196599a`.
  It binds 20 installed files, including the official PVLDB style, and exactly
  nine publication PDFs.
- `paper_vldb/generated_claims.tex` is rendered from the gated JSON. Numeric
  prose in `main.tex` consumes those macros rather than copying remote logs or
  intermediate CSV values. Its SHA-256 is
  `883976d98dd0185a7a080a3df4c41d56203778e90b9ccb35e0b517b0f6ee0618`.
- Primary frontier protocol: Recall@10, ten workers, fixed 10K logical query and
  ground-truth pools, one excluded warmup, and five measured repeats.
- Primary datasets: DEEP10M, SIFT10M, and TTI10M.
- Primary systems: SHINE-derived, d-HNSW, and SlabWalk. A native configuration
  is never interpolated when absent.

## Admitted Matrix

| Evidence | Final admitted content | Gate-enforced boundary |
|---|---|---|
| Three-system frontier | 245 measured rows; three 10M datasets; three systems; five repeats per point | All 245 raw-source and query-pool links resolve and reproduce recorded hashes |
| Query pools | Nine dataset/system manifests, 10K queries per cell, plus a three-query exact-IP TTI check | Canonical query and top-10 ground-truth hashes agree across file formats within each dataset |
| RDMA model controls | 25 cells and 125 measured rows | Requested and reported payload, QP/CQ, MTU, outstanding depth, NIC, NUMA, device, and GID settings agree |
| Query robustness | 17 cells and 85 measured rows | Five repeats per result-width, coroutine, skew, instrumentation, or fixed-code boundary cell |
| Worker scaling | Three systems by workers 1/8/16/40; 12 cells and 60 measured rows | Fixed DEEP1M query pools and explicit per-system recall; not labeled matched-recall |
| d-HNSW topology | Loopback and separated-host cells; ten measured rows | Same binary, base data, query pool, and server binary across the sensitivity pair |
| Resource ledger | Three layouts by one/three/five memory nodes; nine cells and 45 measured rows | All 45 retained source cells rederive the runs, per-MN, and summary tables |
| Cache control | Cache 0/5/20/50%; 20 measured rows and 120 retained source files | The 50% point cuts posts by 83.2% while QPS falls 23.2%; this motivates access-unit redesign, not a universal cache claim |
| Query profile | 200K completed queries, 3,000 samples, zero lost samples | Retained `perf.data`, runner hash, frozen binary, and machine-readable report reproduce 5.16% distance-scoring self-time |
| Expansion completeness | Six DEEP1M cells, 30 measured rows, 180 retained sources | HNSW, SQ8, beam, rerank, workers, and query pool are fixed while inline neighbor scores vary |
| Mechanism controls | Six materialization budgets plus remote/resident upper descent; 12 cells, 60 measured rows, 360 sources | Physical accounting, descriptor readback, budget-map readback, and query identity pass for every admitted cell |
| 1M build cost | SIFT1M, DEEP1M, GIST1M; 15 measured runs | 45 provenance run files, three source campaigns, and one excluded campaign are retained and reparsed |
| 10M build scaling | DEEP10M, SIFT10M, TTI10M; 15 runs and 45 sources | Exactly one canonical packed-variable startup per dataset and repeat; search widths are not pseudo-replicates |
| Graph construction | SIFT10M and TTI10M hnswlib-0.8.0 build/conversion records | Full graph-preserving checks cover vectors, levels, neighbor IDs, pointers, padding, hashes, time, RSS, and return codes |
| Lifecycle boundaries | Four offline replay cells and eight TTI representation cells | Each row is rederived from one retained source; no online-update or crash-recovery claim is admitted |
| Physical-design advisor | 162 source rows; three datasets by three byte budgets by three policies by six repeats; nine fixed held-out selections | Repeats 0--2 select among feasible measured candidates; repeats 3--5 must retain feasibility, per-cell QPS/oracle ratio at least 0.98, and nine-cell geometric mean at least 0.99 |

The two final causal campaign identities are:

- expansion completeness: `vldb-colocation-deep1-final-v4-20260715`, protocol
  fingerprint
  `d99de8f75c7153f0182b21a9804e050a45c8c4e60c04ceac37e7071afa4c8bb5`;
- budget/resident controls: `vldb-mechanism-controls-final-v6-20260715`,
  protocol fingerprint
  `d60f2d12f0f23c2bbccecb65db1a2fe074ba819ef11567887c6734768129e31a`.

## Manuscript Claims

| Manuscript surface | Generated source | Evidence meaning and permitted wording |
|---|---|---|
| Abstract and Q1 high-recall result | `frontier.high_recall_matched_pairs`, `high_recall_qps_speedup_*`, `high_recall_post_reduction_*`, `recall_floor`, `recall_tolerance` | Pair SHINE-derived and SlabWalk only at the same search width, with Recall@10 at least 0.90 and absolute recall delta at most 0.002 |
| Q1 d-HNSW endpoint | `frontier.dhnsw_max_recall` | Report maximum observed recall per dataset; do not form an unmatched throughput ratio or claim an intrinsic ceiling for every partition design |
| Introduction/Figure 1 cache motivation | `cache_control.cache_50`, `query_profile` | A same-path cache can remove posts yet reduce QPS; profile evidence shows useful distance scoring is a small self-time share |
| Q2 fixed-operation model | `rdma_controls`, `robustness_controls` | Report payload, QP/CQ, MTU, outstanding-depth, NUMA, coroutine, tail-latency, skew, and instrumentation controls only from the gated repeated cells |
| Q2 expansion completeness | `colocation_control.full`, `degree_1`, `recall_mean_span` | Reducing an expansion to one inline score preserves measured recall in this control but increases posts/bytes and reduces QPS; this isolates object completeness, not a new baseline |
| Q2 worker scaling | `worker_scaling.slabwalk`, `worker_scaling.cells` | Report scaling shape and each system's observed recall; do not call the panel matched-recall |
| Q2 d-HNSW deployment sensitivity | `topology_control.loopback`, `separated` | Report the released loopback and separated CN--MN deployments as a topology control, not as a replacement primary frontier or an intrinsic partition limit |
| Q3 degree budget | `materialization_budget.fraction_05`, `full`, `recall_mean_span` | Describe the measured storage/quality guardrail under the fixed campaign; do not reuse historical one-run sweeps |
| Q3 resident upper graph | `resident_upper_graph.ef50`, `ef100`, `ef200`, `cells` | Attribute only upper-descent post removal and its measured QPS change to residency |
| Q3 physical accounting and striping | `resource_ledger.variable_scale`, `five_mn` | Report measured storage amplification, sidecar bytes, CN/MN RSS, QPS, and read-byte balance; never substitute nominal region-size arithmetic |
| Q3 offline physical-design advisor | `physical_design_advisor` | Report nine fixed 0--2 training / 3--5 held-out selections, minimum and geometric-mean held-out ratios, and policy counts; state that it selects only among measured candidates and that its separate builder-policy binary contributes no absolute QPS to the frontier |
| Q4 1M derivation cost | `build_cost.SIFT1M`, `DEEP1M`, `GIST1M` | Report five-repeat means and 95% intervals from the promoted fixed-layout build bundle |
| Q4 10M derivation scaling | `build_scaling_10m.DEEP10M`, `SIFT10M`, `TTI10M` | Report five independent canonical startups per dataset, separate from the 1M fixed-layout experiment |
| Q4 offline replay | `lifecycle_refresh.cells` | Report fixed-stride selected records, authoritative bytes read, and rewrite amplification under paused serving; do not imply online insertion or atomic publication |
| Q4 TTI representation boundary | `lifecycle_tti.cells` | Report transferred bytes, posts, QPS, and recall for retained compact-code controls; do not infer compression-only causality without an fp32-in-Slab path |

The generated macros consumed by `main.tex` cover every value in the rows above.
The TeX source must not redefine a `Claim...` macro or hard-code a replacement
value after `\input{generated_claims.tex}`.

## Figure Dependencies

| Figure | Data dependencies | Interpretation boundary |
|---|---|---|
| Figure 1, `fig_physical_units.pdf` | Final cache summary and frozen-binary CPU profile | Contrasts node/vector, partition, and expansion objects; cache evidence is a motivation control |
| Figure 6, `eval_frontier_curves.pdf` | Final 1M/10M frontier tables, query manifests, and source provenance | Ten panels contain all three systems and 95% intervals; DEEP-10M and SIFT-10M annotate eligible matched-high-recall posts/query reductions; TTI exposes a compact-code representation boundary |
| Figure 7, `eval_access_scaling.pdf` | Model controls, robustness, worker scaling, topology sensitivity, and expansion completeness | Causal controls and scaling panels are not rebranded as additional end-to-end datasets |
| Figure 8, `eval_index_cost.pdf` | Mechanism controls and resource ledger | Budget/residency panels use 60 final mechanism rows; layout/striping panels use 45 final resource rows |
| Figure 9, `eval_lifecycle_boundaries.pdf` | 1M/10M construction evidence and lifecycle controls | Build panels have repeated intervals; refresh and TTI panels remain explicitly scoped boundary controls |

Figures 2--5 are explanatory design drawings. Figure 1 additionally contains
the gated cache and CPU-profile motivation inset. All four evaluation PDFs are
installed only by
`experiments/sigmetrics/generate_vldb_final_figures.sh` after the complete gate
passes; direct manual replacement is outside the release contract.

## Baseline Fairness

- `SHINE-derived` is a graph-preserving node/vector access path implemented on
  the same frozen substrate as SlabWalk. It is not represented as an official
  reproduction of every component in the released SHINE system.
- SlabWalk and SHINE-derived load the same converted HNSW dump for SIFT10M and
  TTI10M. d-HNSW uses its released partition/routing path and its own native
  index; it is therefore a system baseline, not a same-graph ablation.
- The primary d-HNSW frontier retains its favorable released loopback topology.
  The separated-host result is disclosed only as deployment sensitivity.
- All primary methods receive the same 10K logical query and exact top-10
  ground-truth rows. Cross-format canonical hashes establish equality even when
  the on-disk encodings differ.
- The TTI outcome is described as a compact-code representation boundary. The
  system has no fp32-in-Slab control, so the paper does not claim isolated
  compression causality.
- Queue-safe rerank is a correctness and completion boundary, not a claimed
  throughput optimization.
- The physical-design advisor is a strict post-hoc split over one pre-existing
  sealed campaign. It supports auditable offline configuration among measured
  candidates, not unseen-layout prediction, online learning, or reconfiguration.
- All 162 advisor-source rows use candidate binary
  `bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8`.
  No absolute QPS from that source is mixed into the frozen system frontier.

## Lifecycle and Exclusions

- The implemented refresh path is an offline fixed-stride differential
  refresh with serving paused. The manuscript does not claim live
  insertion, reader epochs, failure-atomic publication, or crash recovery.
- Warmups, partial repeats, OOM attempts, timeout builders, premeasurement build
  failures, superseded parsers, and repaired-but-excluded raw runs remain under
  `results/vldb_acceptance_closure_20260713/` with manifests. They are retained
  for auditability and barred from final aggregates.
- The failed SIFT10M/TTI10M node2 campaign is an OOM exclusion. The complete
  SlabWalk/SHINE-derived frontier comes from the validated node1-to-node3
  recovery campaign; no partial node2 row is admitted.
- d-HNSW node7 attempts with obsolete embedded paths or an incompatible
  Protobuf build are premeasurement exclusions. Only the source-matched final
  campaign enters the atomic frontier bundle.
- The worker-scaling pre-fix `w40/r0` tree is retained with a SHA inventory but
  excluded. The corrected point was rerun under the same protocol.
- The complete fixed-pool multi-CN campaign is sealed at
  `results/vldb_multicn_formal_20260719f/` (seal SHA-256
  `88ecc25d1228af5f6331b728c1084b6d8d1f13db4f960040f0334a3d20d61356`).
  It failed its preregistered SlabWalk scaling gate on all three datasets and
  its fairness gate on SIFT and GIST. It supports the manuscript's explicit
  no-scale-out boundary, but no row enters a publication figure, generated
  numeric macro, or `release_bundle.json`.

## Reproduction Gate

Run the publication pipeline from the repository root:

```bash
bash experiments/sigmetrics/generate_vldb_final_figures.sh
```

The script revalidates every admitted component, derives headline candidates,
assembles `manuscript_claims.json`, renders `generated_claims.tex`, stages all
data-driven figures, verifies publication-PDF properties, and writes the
content-bound release manifest. Paper compilation and Overleaf packaging are
separate checks against that manifest. Any missing cell, source/hash mismatch,
query-pool drift, binary drift, unadmitted figure, or claim-binding mismatch
must fail before manuscript installation.

The current PDF and Overleaf archive are internally certified against this
contract. Author metadata, the artifact URL, and its anonymously accessible
audited package are present. Disclosure sign-off and CMT declarations remain
external submission blockers listed only in `SUBMISSION_PREFLIGHT.md`; they are
not experimental evidence states.
