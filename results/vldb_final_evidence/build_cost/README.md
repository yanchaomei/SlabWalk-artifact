# Slab construction cost

Each row below summarizes 5 independent construction runs.
The campaign manifest records query and builder parallelism separately.
Intervals are reported with a two-sided 95% Student-t confidence interval.
Build-end RSS is sampled before query state allocation; process peak RSS from
`/usr/bin/time -v` is retained as a cross-check.

| Dataset | Code | Records | f | Build mean (s) | 95% CI half-width (s) | Build RSS mean (GiB) | Slab region (GB) | Amp. vs. estimated HNSW |
|---|---|---|---:|---:|---:|---:|---:|---:|
| SIFT1M | sq8 | fixed | 1.00 | 20.62 | 0.07 | 4.83 | 4.62 | 5.79x |
| DEEP1M | sq8 | fixed | 1.00 | 18.49 | 0.05 | 4.71 | 3.59 | 5.37x |
| GIST1M | RaBitQ-2 | fixed | 1.00 | 25.48 | 0.05 | 8.51 | 8.46 | 2.05x |

Generated with:

```bash
python3 experiments/sigmetrics/summarize_slab_build_cost.py \
  --raw results/vldb_build_cost/raw --out results/vldb_build_cost
```
Final promotion is performed by `assemble_vldb_build_cost.py`; `PROVENANCE.json` records the assembler, summarizer, source campaigns, exclusions, and every retained run file.
