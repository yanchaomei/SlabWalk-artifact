# SHINE source vendored for GraphBeyond C1 work

Upstream: https://github.com/DatabaseGroup/shine-hnsw-index
Snapshot date: 2026-05-15 (from `shine-hnsw-index-main.zip` distributed by authors)

This directory is a **vendored copy** of SHINE with our SKV-cluster RoCE patches
already applied. Use this as the working tree for GraphBeyond C1 (Speculative
Wide-Beam Navigation) modifications. All C1 changes go here, NOT in the
`baselines/shine/patched_files/` mirror (which is the historical patch record
on `main`).

## Patches applied (on top of upstream)

- `rdma-library/library/utils.cc`: SKV cluster IPs (`skv-node1..5` → 10.0.0.61..65)
- `rdma-library/library/queue_pair.{hh,cc}`: RoCE GRH-based addressing in `transition_to_rtr`
- `rdma-library/library/context.{hh,cc}`: auto-select active port + IPv4-mapped GID scan
- `rdma-library/library/detached_qp.hh`: fill GID in `DetachedQP::connect` (worker-thread QP path)
- `rdma-library/CMakeLists.txt` + `FindIBVerbs.cmake`: provide missing FindIBVerbs module
- `src/common/constants.hh`: 4 GB COMPUTE/MEMORY budget (down from 35/44 GB)
- `src/common/timing.cc`: portable `std::stringstream` + `put_time`
- `CMakeLists.txt`: `-DNOHUGEPAGES` enabled
- `scripts/config.py`: SKV-node names + paths

## How to (re)build on the SKV cluster

```bash
# On node1
cd ~/chaomei/graphbeyond-c1/graphbeyond
mkdir -p build && cd build
CC=/usr/bin/gcc-12 CXX=/usr/bin/g++-12 \
  cmake -DCMAKE_BUILD_TYPE=Release -DTBB_DIR=/usr/local/lib/cmake/tbb ..
make -j16
# Distribute `build/shine` and oneTBB libs to node2..4 (script in repo root)
```

## Where C1 changes go

- `rdma-library/library/rdma_reads.hh`: add `read_neighborlists_batch`
- `src/hnsw/hnsw.hh`: extend `search_level` with K-speculative variant (compile-time `--spec-k`)
- `src/common/configuration.hh`: add `--spec-k` CLI flag (default 1 = baseline behavior)

See `design/C1_speculative_wide_beam.md` in the repo root for the full plan.
