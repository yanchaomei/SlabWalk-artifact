# DEEP1M Robustness Evidence

- Campaign: `vldb-robustness-deep1-v2-final-20260713`.
- SlabWalk binary SHA-256:
  `2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6`.
- Protocol: one warmup and five measured fixed-query-pool runs per cell.
- Matrix: 4 worker counts, 5 coroutine counts, 4 top-k values, 2 query
  distributions, and 2 latency-instrumentation controls (17 cells total).
- Raw campaign:
  `results/vldb_acceptance_closure_20260713/robustness_deep1_v2_final_20260713/`.
- Raw rows: 102 runs plus header; 85 rows are measured and 17 are warmups.
- `runs.csv` SHA-256:
  `ec2dd90ad377d8e98274631db7d159b0b6c6c3a1582057f541555edd42ef33be`.
- `summary.csv` SHA-256:
  `bc9592b6b1d5fcdb9f28939741d8fca6434633d04c7529bd094a0ebd3b17a4f5`.
- Original `robustness_raw.csv` SHA-256:
  `85bf6d5ea68ac41b1475276acd7d0c4fff2fe4f7ca3a0a6c50da1f1fe55393c4`.

## Measured Reading

- Scaling from 1 to 40 workers raises mean throughput from 1,347.8 to
  20,861.6 QPS (`15.48x`), while mean P99 latency rises from 1.573 to
  4.260 ms.
- Four coroutines reach 17,867.4 QPS.  Increasing to 16 adds only 2.98% QPS
  but raises mean P99 latency by `4.89x` (2.697 to 13.201 ms), exposing a
  clear throughput-tail knee.
- Across `k={1,10,50,100}`, the mean-QPS span is 1.90%; measured posts/query
  and bytes/query are unchanged.  Result width is not the binding resource in
  this range.
- Zipf-1.0 changes mean QPS by +1.34% and mean P99 by -0.29% relative to the
  uniform slice.  Both changes are small relative to the measured intervals.
- Enabling local latency collection changes mean QPS by +1.99%; the sign and
  confidence intervals do not indicate measurable instrumentation overhead.

Validation:

```bash
cd experiments/sigmetrics
python3 - <<'PY'
from pathlib import Path
import validate_vldb_final_evidence as evidence

print(evidence.validate_robustness(
    Path("../../results/vldb_final_evidence/robustness").resolve(),
    "2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6",
))
PY
```
