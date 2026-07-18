# RDMA tau microbenchmark summary

Source: `ib_read_lat` / `ib_read_bw` from one initiator CN to one passive MN over a ConnectX-6 DX RoCEv2 link; host aliases and device identifiers remain in the raw CSV.

- 64B--4KB payload average latency range: 4.23--5.56 us (1.31x).
- 64B--4KB P99 latency range: 4.38--5.77 us (1.32x).
- Single-QP/CQ1 256B read rate: 3.83 Mops/s; best QP/CQ point: 7.22 Mops/s.
- MTU 1024--4096 average-latency spread: 1.01x.
- Same-node NUMA placement average-latency spread: 1.00x.
- Outstanding-read sweep Mops/s: outs=1: 0.26, outs=4: 1.05, outs=16: 3.86.
