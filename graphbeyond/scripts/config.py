from datasets import Datasets

PUSH_BENCHMARKS = True
COMPUTE_RECALL = False  # can only be true if a ground truth file for the respective query file exists
COROUTINES = 4

# hnsw parameters
EF_CONSTRUCTION = 500
M = 32

# SKV cluster: 5 physical nodes.
# node1-4 are Ubuntu 20.04 (homogeneous); node5 is Ubuntu 22.04 (ABI differs).
# Initial config: 3 CN (node1-3) + 1 MN (node4). Excludes node5 due to glibc mismatch.
ALL_COMPUTE_NODES = ["skv-node1", "skv-node2", "skv-node3"]
ALL_MEMORY_NODES = ["skv-node4"]
INITIATOR = ALL_COMPUTE_NODES[0]

# paths
DATASETS_PATH = "/home/kvgroup/chaomei/hnsw-data"
REPOSITORY_PATH = "/home/kvgroup/chaomei/shine-hnsw-index"
EXECUTABLE = f"{REPOSITORY_PATH}/build/shine"
RUNNER_SCRIPT = f"{REPOSITORY_PATH}/scripts/run_node.py"


def to_path(dataset: Datasets) -> str:
    return f"{DATASETS_PATH}/{dataset.value.name}/"


def get_cache_parameters(label: str, cache_size_ratio: int) -> str:
    return f"--cache --cache-ratio {cache_size_ratio} --routing" if "routing" in label else f"--cache --cache-ratio {cache_size_ratio}" if "cache" in label else ""


# SKV cluster: SSH uses intra-cluster 192.168.1.x; RDMA uses 10.0.0.x
# (handled separately in rdma-library/library/utils.cc)
DNS = {"skv-node1": "192.168.1.116",
       "skv-node2": "192.168.1.102",
       "skv-node3": "192.168.1.103",
       "skv-node4": "192.168.1.117",
       "skv-node5": "192.168.1.141"}
