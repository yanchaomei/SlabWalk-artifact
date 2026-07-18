#!/usr/bin/env bash
# Build SIFT10M on node4/node6 and atomically publish the dump to node2.
set -euo pipefail

CLOSURE=${CLOSURE:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$CLOSURE/parallel_sift10m_node4_snapshot_20260713}
OUT=${OUT:-$CLOSURE/evidence/frontier_index_sift10m_parallel_node4_v1_20260713}
GB_BIN=${GB_BIN:-$CLOSURE/build-final-v5/shine}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
SOURCE_HOST=${SOURCE_HOST:-skv-node1}
MEMORY_NODE=${MEMORY_NODE:-skv-node6}
DESTINATION_NODE=${DESTINATION_NODE:-skv-node2}
PORT=${PORT:-1490}
DRY_RUN=${DRY_RUN:-0}

DATA_DIR=$GB_DATA/sift10m
DUMP_REL=sift10m/dump/index_m16_efc100_node1_of1.dat
REMOTE_DUMP=$GB_DATA/$DUMP_REL
PARTIAL_DUMP=${REMOTE_DUMP}.parallel-partial

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'source=%s cn=%s mn=%s destination=%s port=%s out=%s\n' \
    "$SOURCE_HOST" "$(hostname)" "$MEMORY_NODE" "$DESTINATION_NODE" "$PORT" "$OUT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing frozen SlabWalk binary: $GB_BIN" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_SHA" ]] || {
  echo "Frozen SlabWalk binary SHA mismatch" >&2; exit 2;
}
[[ -x "$SNAPSHOT/experiments/sigmetrics/build_frontier_indexes_12h.sh" ]] || {
  echo "Missing frozen index-build runner" >&2; exit 2;
}

mkdir -p "$DATA_DIR"
rsync -a "$SOURCE_HOST:$DATA_DIR/" "$DATA_DIR/"
for relative in base.fbin queries/query-uniform.fbin queries/groundtruth-uniform.bin; do
  source_sha=$(ssh -o LogLevel=ERROR "$SOURCE_HOST" \
    "sha256sum '$DATA_DIR/$relative' | awk '{print \$1}'")
  local_sha=$(sha256sum "$DATA_DIR/$relative" | awk '{print $1}')
  [[ "$source_sha" == "$local_sha" ]] || {
    echo "source input SHA mismatch: $relative" >&2; exit 2;
  }
done

ssh -o LogLevel=ERROR "$MEMORY_NODE" "mkdir -p '$GB_DATA/sift10m/dump'"
DATASETS=SIFT10M SIFT10_MN="$MEMORY_NODE" OUT="$OUT" \
GB_BIN="$GB_BIN" GB_DATA="$GB_DATA" PORT="$PORT" TIMEOUT_S=43200 \
bash "$SNAPSHOT/experiments/sigmetrics/build_frontier_indexes_12h.sh"

ssh -o LogLevel=ERROR "$MEMORY_NODE" "test -s '$REMOTE_DUMP'"
source_dump_sha=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
  "sha256sum '$REMOTE_DUMP' | awk '{print \$1}'")
ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
  "mkdir -p '$GB_DATA/sift10m/dump'; rm -f '$PARTIAL_DUMP'"
ssh -o LogLevel=ERROR "$MEMORY_NODE" \
  "rsync -a '$REMOTE_DUMP' '$DESTINATION_NODE:$PARTIAL_DUMP'"
destination_partial_sha=$(ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
  "sha256sum '$PARTIAL_DUMP' | awk '{print \$1}'")
[[ "$source_dump_sha" == "$destination_partial_sha" ]] || {
  echo "destination dump SHA mismatch" >&2; exit 2;
}

if ssh -o LogLevel=ERROR "$DESTINATION_NODE" "test -e '$REMOTE_DUMP'"; then
  destination_sha=$(ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
    "sha256sum '$REMOTE_DUMP' | awk '{print \$1}'")
  [[ "$source_dump_sha" == "$destination_sha" ]] || {
    echo "destination dump SHA mismatch: concurrent build owns final path" >&2
    exit 2
  }
  ssh -o LogLevel=ERROR "$DESTINATION_NODE" "rm -f '$PARTIAL_DUMP'"
else
  ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
    "mv '$PARTIAL_DUMP' '$REMOTE_DUMP'"
fi

printf 'SIFT10M dump published: sha256=%s destination=%s:%s\n' \
  "$source_dump_sha" "$DESTINATION_NODE" "$REMOTE_DUMP"
