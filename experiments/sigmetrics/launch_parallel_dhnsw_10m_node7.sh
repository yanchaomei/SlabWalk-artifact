#!/usr/bin/env bash
# Stage and run the formal d-HNSW TTI10M/SIFT10M frontier on node7.
set -euo pipefail

CLOSURE=${CLOSURE:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$CLOSURE/dhnsw_10m_node7_snapshot_v3_20260713}
OUT=${OUT:-$CLOSURE/evidence/dhnsw_text_sift_node7_final_v3_20260713}
DROOT=${DROOT:-$CLOSURE/dhnsw-text-sift-node7-final-v3}
LOCAL_INPUT_CACHE=${LOCAL_INPUT_CACHE:-$CLOSURE/dhnsw-text-sift-node7-final-v1}
SOURCE_NODE=${SOURCE_NODE:-skv-node1}
RUNTIME_NODE=${RUNTIME_NODE:-skv-node3}
SOURCE_BUILD_ROOT=${SOURCE_BUILD_ROOT:-$CLOSURE/dhnsw-node7-v3-build}
SOURCE_DROOT=${SOURCE_DROOT:-/home/kvgroup/chaomei/d-HNSW}
SOURCE_RUNTIME=${SOURCE_RUNTIME:-/home/kvgroup/chaomei/dhnsw-pilot-20260713/runtime-lib}
SOURCE_GB_DATA=${SOURCE_GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
EXPECTED_SOURCE_COMMIT=d6f275732275e6009a542a7066d7f695036daaf6
EXPECTED_BUILD_MANIFEST_SHA=a5a8bf71e66bd1de1bd31bd607d82459da929e2dde35cfc6ae40754ecaab51e4
PORT=${PORT:-50371}
RDMA_PORT=${RDMA_PORT:-8978}
DRY_RUN=${DRY_RUN:-0}

PROVENANCE=$OUT/provenance
INPUTS=$DROOT/input-sources
RUNTIME=$DROOT/runtime-lib
RUNNER=$SNAPSHOT/experiments/sigmetrics/run_dhnsw_frontier.sh
REPEATED=$SNAPSHOT/experiments/sigmetrics/run_dhnsw_repeated.sh
PARSER=$SNAPSHOT/experiments/sigmetrics/parse_dhnsw_frontier.py
VALIDATOR=$SNAPSHOT/experiments/sigmetrics/validate_dhnsw_dataset.py
PREPARER=$SNAPSHOT/experiments/sigmetrics/prepare_fixed_query_pool.py
FINGERPRINTER=$SNAPSHOT/experiments/sigmetrics/fingerprint_query_pool.py

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'cn=%s source=%s runtime=%s out=%s droot=%s ports=%s/%s\n' \
    "$(hostname)" "$SOURCE_NODE" "$RUNTIME_NODE" "$OUT" "$DROOT" "$PORT" "$RDMA_PORT"
  exit 0
fi

[[ ! -e "$OUT" ]] || { echo "Output already exists: $OUT" >&2; exit 2; }
[[ ! -e "$DROOT" ]] || { echo "Staging root already exists: $DROOT" >&2; exit 2; }
for required in "$RUNNER" "$REPEATED" "$PARSER" "$VALIDATOR" "$PREPARER" "$FINGERPRINTER"; do
  [[ -s "$required" ]] || { echo "Missing frozen campaign input: $required" >&2; exit 2; }
done

mkdir -p "$PROVENANCE/source-build" "$DROOT/build" "$DROOT/datasets/text10M" \
  "$DROOT/datasets/sift10M/gnd" "$INPUTS/tti10M" "$RUNTIME"

remote_build_manifest_sha=$(ssh -o LogLevel=ERROR "$SOURCE_NODE" \
  "sha256sum '$SOURCE_BUILD_ROOT/provenance/build_manifest.json' | awk '{print \$1}'")
[[ "$remote_build_manifest_sha" == "$EXPECTED_BUILD_MANIFEST_SHA" ]] || {
  echo "source-build manifest drift: $remote_build_manifest_sha" >&2; exit 2;
}
rsync -a --exclude=build/ "$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/" "$DROOT/"
rsync -a \
  "$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/build/run_client" \
  "$SOURCE_NODE:$SOURCE_BUILD_ROOT/source/build/run_server" \
  "$DROOT/build/"
rsync -a "$SOURCE_NODE:$SOURCE_BUILD_ROOT/provenance/" \
  "$PROVENANCE/source-build/"

python3 - "$PROVENANCE/source-build/build_manifest.json" "$DROOT" "$RUNNER" \
  "$EXPECTED_BUILD_MANIFEST_SHA" "$EXPECTED_SOURCE_COMMIT" \
  "$PROVENANCE/staged-build-verification.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_path, droot, runner, expected_manifest, expected_commit, output = (
    Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]),
    sys.argv[4], sys.argv[5], Path(sys.argv[6])
)

def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

manifest = json.loads(manifest_path.read_text())
checks = {
    "build_manifest_sha256": sha(manifest_path),
    "patch_runner_sha256": sha(runner),
    "run_client_sha256": sha(droot / "build/run_client"),
    "run_server_sha256": sha(droot / "build/run_server"),
}
if (
    manifest.get("kind") != "dhnsw_source_matched_build"
    or manifest.get("source_commit") != expected_commit
    or checks["build_manifest_sha256"] != expected_manifest
    or manifest.get("patch_runner_sha256") != checks["patch_runner_sha256"]
    or manifest.get("binary_sha256", {}).get("run_client")
        != checks["run_client_sha256"]
    or manifest.get("binary_sha256", {}).get("run_server")
        != checks["run_server_sha256"]
):
    raise SystemExit("staged d-HNSW source-build identity mismatch")
for relative, expected in manifest.get("patched_source_sha256", {}).items():
    if sha(droot / relative) != expected:
        raise SystemExit(f"staged patched-source mismatch: {relative}")
output.write_text(json.dumps({
    "kind": "staged_dhnsw_source_build_verification",
    "source_commit": expected_commit,
    "checks": checks,
}, indent=2, sort_keys=True) + "\n")
PY

rsync -a --delete "$RUNTIME_NODE:$SOURCE_RUNTIME/" "$RUNTIME/"
ssh -o LogLevel=ERROR "$RUNTIME_NODE" \
  "cd '$SOURCE_RUNTIME' && find . -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum" \
  > "$PROVENANCE/runtime-source.sha256"
(
  cd "$RUNTIME"
  find . -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum
) > "$PROVENANCE/runtime-local.sha256"
diff -u "$PROVENANCE/runtime-source.sha256" "$PROVENANCE/runtime-local.sha256"

sync_and_verify() {
  local remote_path=$1 local_path=$2 cache_path=${3:-} source_sha local_sha transfer
  mkdir -p "$(dirname "$local_path")"
  if [[ -n "$cache_path" && -s "$cache_path" ]]; then
    ln "$cache_path" "$local_path"
    transfer=hardlink-cache
  else
    rsync -a --partial "$SOURCE_NODE:$remote_path" "$local_path"
    transfer=rsync-source
  fi
  source_sha=$(ssh -o LogLevel=ERROR "$SOURCE_NODE" \
    "sha256sum '$remote_path' | awk '{print \$1}'")
  local_sha=$(sha256sum "$local_path" | awk '{print $1}')
  [[ "$source_sha" == "$local_sha" ]] || {
    echo "source input SHA mismatch: $remote_path" >&2; exit 2;
  }
  printf '%s  %s\n' "$local_sha" "$local_path" >> "$PROVENANCE/inputs.sha256"
  printf '%s\t%s\t%s\t%s\n' "$transfer" "$source_sha" "$remote_path" "$local_path" \
    >> "$PROVENANCE/input-transfer.tsv"
}

: > "$PROVENANCE/inputs.sha256"
: > "$PROVENANCE/input-transfer.tsv"
sync_and_verify "$SOURCE_DROOT/datasets/text10M/text10M_base.fvecs" \
  "$DROOT/datasets/text10M/text10M_base.fvecs" \
  "$LOCAL_INPUT_CACHE/datasets/text10M/text10M_base.fvecs"
sync_and_verify "$SOURCE_GB_DATA/tti-10m/queries/query-uniform.fbin" \
  "$INPUTS/tti10M/query-uniform.fbin" \
  "$LOCAL_INPUT_CACHE/input-sources/tti10M/query-uniform.fbin"
sync_and_verify "$SOURCE_GB_DATA/tti-10m/queries/groundtruth-uniform.bin" \
  "$INPUTS/tti10M/groundtruth-uniform.bin" \
  "$LOCAL_INPUT_CACHE/input-sources/tti10M/groundtruth-uniform.bin"
sync_and_verify "$SOURCE_DROOT/datasets/sift10M/bigann_base.fvecs" \
  "$DROOT/datasets/sift10M/bigann_base.fvecs" \
  "$LOCAL_INPUT_CACHE/datasets/sift10M/bigann_base.fvecs"
sync_and_verify "$SOURCE_DROOT/datasets/sift10M/bigann_query.fvecs" \
  "$DROOT/datasets/sift10M/bigann_query.fvecs" \
  "$LOCAL_INPUT_CACHE/datasets/sift10M/bigann_query.fvecs"
sync_and_verify "$SOURCE_DROOT/datasets/sift10M/gnd/idx_10M.ivecs" \
  "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" \
  "$LOCAL_INPUT_CACHE/datasets/sift10M/gnd/idx_10M.ivecs"

python3 "$PREPARER" \
  --query "$INPUTS/tti10M/query-uniform.fbin" \
  --groundtruth "$INPUTS/tti10M/groundtruth-uniform.bin" \
  --limit 10000 \
  --query-fvecs "$DROOT/datasets/text10M/query-u10k.fvecs" \
  --groundtruth-ivecs "$DROOT/datasets/text10M/groundtruth-u10k.ivecs" \
  --manifest "$PROVENANCE/tti10m-query-pool.json"

python3 "$VALIDATOR" \
  --base "$DROOT/datasets/text10M/text10M_base.fvecs" \
  --query "$DROOT/datasets/text10M/query-u10k.fvecs" \
  --groundtruth "$DROOT/datasets/text10M/groundtruth-u10k.ivecs" \
  --expected-queries 10000 --min-k 10 \
  --out "$PROVENANCE/tti10m-dataset-validation.json"
python3 "$VALIDATOR" \
  --base "$DROOT/datasets/sift10M/bigann_base.fvecs" \
  --query "$DROOT/datasets/sift10M/bigann_query.fvecs" \
  --groundtruth "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" \
  --expected-queries 10000 --min-k 10 \
  --out "$PROVENANCE/sift10m-dataset-validation.json"

python3 "$FINGERPRINTER" \
  --query "$DROOT/datasets/text10M/query-u10k.fvecs" \
  --groundtruth "$DROOT/datasets/text10M/groundtruth-u10k.ivecs" \
  --dataset TTI10M --method d-HNSW --metric ip \
  --out "$PROVENANCE/tti10m_dhnsw.json"
python3 "$FINGERPRINTER" \
  --query "$DROOT/datasets/sift10M/bigann_query.fvecs" \
  --groundtruth "$DROOT/datasets/sift10M/gnd/idx_10M.ivecs" \
  --dataset SIFT10M --method d-HNSW --metric l2 \
  --out "$PROVENANCE/sift10m_dhnsw.json"

sha256sum "$DROOT/src/dhnsw/data_config.hh" \
  "$DROOT/src/bench/search_client_pipelined_reuse_thread.cc" \
  "$DROOT/src/bench/search_server.cc" > "$PROVENANCE/staged-patched-source.sha256"
sha256sum "$DROOT/build/run_client" "$DROOT/build/run_server" \
  > "$PROVENANCE/binary.sha256"
strings "$DROOT/build/run_client" | \
  grep -E 'datasets/(text10M|sift10M)/.*(fvecs|ivecs)' | sort -u \
  > "$PROVENANCE/binary-dataset-paths.txt"
if ! grep -q 'query-u10k.fvecs' "$PROVENANCE/binary-dataset-paths.txt" ||
   ! grep -q 'groundtruth-u10k.ivecs' "$PROVENANCE/binary-dataset-paths.txt" ||
   grep -q 'text10M_query.fvecs' "$PROVENANCE/binary-dataset-paths.txt"; then
  echo "compiled client/source dataset-path mismatch" >&2
  exit 2
fi
if ! env LD_LIBRARY_PATH="$RUNTIME" ldd \
    "$DROOT/build/run_client" "$DROOT/build/run_server" \
    > "$PROVENANCE/ldd.txt" 2>&1; then
  echo "Unable to resolve staged source-matched d-HNSW runtime libraries" >&2
  exit 2
fi
if grep -q "not found" "$PROVENANCE/ldd.txt"; then
  echo "Staged source-matched d-HNSW binary has unresolved runtime libraries" >&2
  exit 2
fi

env \
  DROOT="$DROOT" OUT_ROOT="$OUT" WORK="$DROOT/work" \
  DATASETS='tti10M sift10M' EF_LIST='48 64 96 128 200' \
  THREADS=10 REPEATS=5 BENCHMARK_DURATION=20 \
  BUILD_DHNSW=0 PREPARE_DATASETS=0 \
  DHNSW_LD_LIBRARY_PATH="$RUNTIME" \
  RUNNER="$RUNNER" PARSER="$PARSER" VALIDATOR="$VALIDATOR" \
  SERVER_IP=10.0.0.67 RDMA_IP=10.0.0.67 PORT="$PORT" RDMA_PORT="$RDMA_PORT" \
  NIC_IDX=1 TIMEOUT_SERVER_S=21600 TIMEOUT_CLIENT_S=1800 SERVER_READY_WAIT_S=7200 \
  CAMPAIGN_ID=vldb-dhnsw-text-sift-node7-final-v3-20260713 RESUME=0 \
  bash "$REPEATED"

printf 'd-HNSW TTI10M/SIFT10M final campaign completed: %s\n' "$OUT"
