#!/usr/bin/env bash
# Launch the formal SlabWalk/SHINE replacement frontier. d-HNSW is omitted
# here because its unchanged five-repeat evidence is merged downstream.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GB_BIN=${GB_BIN:?set GB_BIN to the immutable candidate executable}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA to its SHA-256}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to a fresh campaign directory}
CAMPAIGN_ID=${CAMPAIGN_ID:?set CAMPAIGN_ID explicitly}
SW_PORT=${SW_PORT:-17980}

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "Formal 1M frontier must run on the dedicated skv-node1 CN" >&2
  exit 2
}
[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "EXPECTED_BINARY_SHA must contain 64 lowercase hex digits" >&2
  exit 2
}
[[ -x "$GB_BIN" ]] || { echo "Missing candidate executable: $GB_BIN" >&2; exit 2; }
ACTUAL_BINARY_SHA=$(sha256sum "$GB_BIN" | awk '{print $1}')
[[ "$ACTUAL_BINARY_SHA" == "$EXPECTED_BINARY_SHA" ]] || {
  echo "Candidate SHA mismatch: $ACTUAL_BINARY_SHA" >&2
  exit 2
}
[[ ! -e "$OUT_ROOT" ]] || {
  echo "Refusing existing formal frontier root: $OUT_ROOT" >&2
  exit 2
}
mkdir -p "$(dirname "$OUT_ROOT")"

export PHASES=sw
export REPEATS=5
export THREADS=10
export QUERY_CONTEXTS=10
export COROS=2
export RESUME=0
export GB_BIN EXPECTED_BINARY_SHA OUT_ROOT CAMPAIGN_ID SW_PORT
export DATASETS_SW="SIFT1M GIST1M DEEP1M BIGANN1M SPACEV1M TURING1M TEXT1M"
export EXPECTED_DATASETS="SIFT1M,GIST1M,DEEP1M,BIGANN1M,SPACEV1M,TURING1M,TEXT1M"
export SIFT1_EFS="48 64 80 100 150"
export GIST1_EFS="100 200 300 400 600"
export DEEP1_EFS="30 50 80 100 150"
export BIGANN1_EFS="48 64 80 100 150"
export SPACEV1_EFS="100 200 300 400 600"
export TURING1_EFS="200 400 600 900 1200"
export TEXT1_EFS="100 150 200 300 500"

exec bash "$SCRIPT_DIR/run_frontier_repeated.sh"
