# VLDB Resource Ledger

All resource values are measured. Authoritative HNSW bytes are the exact staged-read extents consumed by the builder; intervals are two-sided 95% Student-t confidence intervals.

| Layout | MNs | n | Recall | QPS | MiB/query | WR/query | Sidecar GiB | Build s | CN RSS GiB | Max MN RSS GiB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed | 1 | 5 | 0.9257 +/- 0.0000 | 2518 +/- 5 | 3.966 | 600.6 | 7.88 | 26.52 | 8.52 | 13.01 |
| fixed | 3 | 5 | 0.9257 +/- 0.0000 | 2639 +/- 2 | 3.966 | 600.6 | 7.88 | 26.51 | 8.53 | 7.01 |
| fixed | 5 | 5 | 0.9257 +/- 0.0000 | 2644 +/- 1 | 3.966 | 600.6 | 7.88 | 26.50 | 8.54 | 6.01 |
| legacy | 1 | 5 | 0.9257 +/- 0.0000 | 2521 +/- 8 | 3.966 | 600.6 | 7.88 | 26.39 | 8.52 | 13.01 |
| legacy | 3 | 5 | 0.9257 +/- 0.0000 | 2632 +/- 3 | 3.966 | 600.6 | 23.63 | 26.55 | 8.53 | 13.01 |
| legacy | 5 | 5 | 0.9257 +/- 0.0000 | 2637 +/- 4 | 3.966 | 600.6 | 39.38 | 26.66 | 8.54 | 13.01 |
| variable | 1 | 5 | 0.9257 +/- 0.0000 | 2960 +/- 10 | 2.787 | 600.6 | 2.42 | 25.51 | 8.52 | 10.01 |
| variable | 3 | 5 | 0.9257 +/- 0.0000 | 2984 +/- 11 | 2.787 | 600.6 | 2.42 | 25.56 | 8.53 | 6.01 |
| variable | 5 | 5 | 0.9257 +/- 0.0000 | 2969 +/- 5 | 2.787 | 600.6 | 2.42 | 25.48 | 8.54 | 5.51 |
