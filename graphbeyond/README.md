# SlabWalk Implementation

This directory contains SlabWalk, an expansion-oriented physical access
structure derived from an authoritative HNSW index. It is implemented on a
SHINE-derived passive-RDMA substrate; upstream identifiers such as `shine`,
`lavd`, and `crane` remain in source paths to avoid an unrelated rename of
performance-sensitive code. The object-native path remains available as the
shared-substrate comparator.

The active component map is in [`CODE_MAP.md`](CODE_MAP.md). Current evidence
status and exact binary identities are recorded in
[`../progress.md`](../progress.md), not in this build README.

## Setup

### C++ Libraries and Unix Packages

The following C++ libraries and Unix packages are required to compile the code.
Note that `ibverbs` (the RDMA library) is Linux-only. 
The code also compiles without InfiniBand network cards.

* [ibverbs](https://github.com/linux-rdma/rdma-core/tree/master)
* [boost](https://www.boost.org/doc/libs/1_83_0/doc/html/program_options.html) (to support `boost::program_options` for
  CLI parsing)
* pthreads (for multithreading)
* [oneTBB](https://github.com/oneapi-src/oneTBB) 2021 or newer (for concurrent
  data structures; the legacy `tbb/...` header layout is unsupported)
* a C++ compiler that supports C++20 (we have used `g++-12`)
* cmake
* numactl
* vmtouch (to map index files into main memory)
* axel (a download accelerator for the datasets)

For instance, on Ubuntu 22.04 or a recent Debian release whose `libtbb-dev`
package provides `oneapi/tbb/...` headers, install:
```
apt-get -y install g++-12 libboost-all-dev libibverbs1 libibverbs-dev numactl cmake libtbb-dev git python3-venv vmtouch axel
```

### Cluster Nodes Configuration

Adjust the IP addresses of the cluster nodes accordingly in `rdma-library/library/utils.cc`:
https://frosch.cosy.sbg.ac.at/mwidmoser/shine-hnsw-index/-/blob/main/rdma-library/library/utils.cc?ref_type=heads#L14-L23

### Compilation

Build one immutable candidate on the audited Linux builder, run all CTest
contracts, and deploy the resulting bytes unchanged to the measurement nodes:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER=/usr/bin/g++-12
cmake --build build -j 20
ctest --test-dir build --output-on-failure
sha256sum build/shine-opt
```

Configuration compiles and links probes for both
`std::atomic<std::shared_ptr<T>>` and the selected oneTBB header/library pair.
This catches mixed installations in which oneTBB headers are paired with the
ABI-incompatible legacy `libtbb.so.2`. If multiple TBB installations exist,
select a matching pair explicitly:

```bash
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=g++-12 \
  -DGB_TBB_INCLUDE_DIR=/path/to/onetbb/include \
  -DGB_TBB_LIBRARY=/path/to/onetbb/lib/libtbb.so ..
```

## Download the Data

First, install all Python requirements:
```
cd scripts
python3 -m pip install -r requirements.txt
```

Then, run the following script to download the data. 
This may take a while, we recommend to run the script within a `tmux` session.
Also make sure that `axel` (a download accelerator) is installed.
```
cd data
bash download.sh
```

Finally, create the queries (adjust the `DATASET_PATH` in `create_queries.py`):
```
python3 create_queries.py
```

Now all the data is available in `data/datasets`, move them to a location where all cluster nodes have access to (e.g., to an NFS).
Then, adjust the path in `config.py`.

## Run the Experiments

The fail-closed RDMA runners, fixed-query-pool protocol, source and binary
identity checks, evidence sealing, and promotion flow are documented in
[`../experiments/README.md`](../experiments/README.md). Do not use an ad hoc
binary or a manually edited CSV for paper evidence.
