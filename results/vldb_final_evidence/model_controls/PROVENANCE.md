# Model-control provenance

- Campaign: `vldb-rdma-tau-5x-v1`
- Completion time: 2026-07-13 17:48:25 +0800
- Raw matrix: 25 controlled cells x 5 measured repetitions = 125 rows
- Initiator CN: `skv-node6`
- Passive MN: `skv-node4` (`10.0.0.64`)
- RDMA device / GID: `mlx5_1` / `3`
- CPU on both hosts: Intel Xeon Gold 5218R, two NUMA nodes
- HCA firmware on both hosts: `22.31.2006`
- perftest: `ib_read_lat` and `ib_read_bw`, version `5.99`
- Workload: one-sided RDMA READ; payload, MTU, NUMA placement,
  outstanding reads, QP count, and CQ moderation are varied independently.
- Runner: `experiments/sigmetrics/rdma_tau_microbench.py`
- Runner SHA-256: `712a391b79719c8b53283b79b0b63a52cf3de51475d64bd4d108cfc5fac6fad2`
- Raw CSV SHA-256: `978db5366252d1f0e2136283dae1136fa2909d4b6d095c4290fe4c10bae527bf`
- Campaign log SHA-256: `4e0b8efc5ffbf9f858156d16f7ba10c4711ca6fc191839e0539d6d3e3b0cfcc9`

The final evidence gate re-parses the CSV, requires the exact 25-cell matrix,
requires repetitions 1 through 5 for every cell, checks unique perftest ports,
compares requested and reported knobs, and rejects host, device, or GID drift.
The peak-bandwidth field is zero because perftest did not report a peak sample;
average bandwidth and message rate are the validated bandwidth outputs.
