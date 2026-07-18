#!/usr/bin/env bash
# Hedge the long TTI10M HNSW build on node3/node5, then publish only after the
# primary node1 build has released the canonical node2 path.
set -euo pipefail

CLOSURE=${CLOSURE:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SNAPSHOT=${SNAPSHOT:-$CLOSURE/parallel_tti10m_node3_snapshot_v1_20260714}
OUT=${OUT:-$CLOSURE/evidence/frontier_index_tti10m_parallel_node3_v1_20260714}
GB_BIN=${GB_BIN:-$CLOSURE/build-final-v5/shine}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
EXPECTED_SHA=2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6
SOURCE_HOST=${SOURCE_HOST:-skv-node1}
MEMORY_NODE=${MEMORY_NODE:-skv-node5}
DESTINATION_NODE=${DESTINATION_NODE:-skv-node2}
WAIT_SESSION_HOST=${WAIT_SESSION_HOST:-skv-node1}
WAIT_SESSION=${WAIT_SESSION:-vldb-index-build-12h-retry-v3}
THREADS=${THREADS:-40}
PORT=${PORT:-1492}
TIMEOUT_S=${TIMEOUT_S:-43200}
DRY_RUN=${DRY_RUN:-0}

DATA_DIR=$GB_DATA/tti-10m
DUMP_REL=tti-10m/dump/index_m16_efc100_node1_of1.dat
REMOTE_DUMP=$GB_DATA/$DUMP_REL
PARTIAL_DUMP=${REMOTE_DUMP}.parallel-partial
RUNNER=$SNAPSHOT/experiments/sigmetrics/build_frontier_indexes.sh

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'source=%s cn=%s mn=%s destination=%s wait=%s:%s threads=%s port=%s out=%s\n' \
    "$SOURCE_HOST" "$(hostname)" "$MEMORY_NODE" "$DESTINATION_NODE" \
    "$WAIT_SESSION_HOST" "$WAIT_SESSION" "$THREADS" "$PORT" "$OUT"
  exit 0
fi

[[ -x "$GB_BIN" ]] || { echo "Missing frozen SlabWalk binary: $GB_BIN" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$EXPECTED_SHA" ]] || {
  echo "Frozen SlabWalk binary SHA mismatch" >&2; exit 2;
}
[[ -x "$RUNNER" ]] || { echo "Missing frozen index-build runner: $RUNNER" >&2; exit 2; }
[[ "$THREADS" =~ ^[1-9][0-9]*$ ]] || { echo "THREADS must be positive" >&2; exit 2; }

mkdir -p "$OUT" "$DATA_DIR"
rsync -a --partial "$SOURCE_HOST:$DATA_DIR/" "$DATA_DIR/"
: > "$OUT/source_inputs.sha256"
for relative in base.fbin queries/query-uniform.fbin queries/groundtruth-uniform.bin; do
  source_sha=$(ssh -o LogLevel=ERROR "$SOURCE_HOST" \
    "sha256sum '$DATA_DIR/$relative' | awk '{print \$1}'")
  local_sha=$(sha256sum "$DATA_DIR/$relative" | awk '{print $1}')
  [[ "$source_sha" == "$local_sha" ]] || {
    echo "source input SHA mismatch: $relative" >&2; exit 2;
  }
  printf '%s  %s\n' "$local_sha" "$relative" >> "$OUT/source_inputs.sha256"
done

sha256sum "$0" "$RUNNER" > "$OUT/launcher_sources.sha256"
ssh -o LogLevel=ERROR "$MEMORY_NODE" "mkdir -p '$GB_DATA/tti-10m/dump'"
DATASETS=TEXT10M TEXT_MN="$MEMORY_NODE" OUT="$OUT" \
GB_BIN="$GB_BIN" GB_BIN_R="$GB_BIN" GB_DATA="$GB_DATA" \
THREADS="$THREADS" COROS=2 PORT="$PORT" TIMEOUT_S="$TIMEOUT_S" \
bash "$RUNNER"

ssh -o LogLevel=ERROR "$MEMORY_NODE" "test -s '$REMOTE_DUMP'"
source_dump_sha=$(ssh -o LogLevel=ERROR "$MEMORY_NODE" \
  "sha256sum '$REMOTE_DUMP' | awk '{print \$1}'")
printf '%s  %s:%s\n' "$source_dump_sha" "$MEMORY_NODE" "$REMOTE_DUMP" \
  > "$OUT/built_dump.sha256"

# The primary build writes directly to the canonical node2 path. Waiting here
# prevents a race between that store and this hedge's atomic copy.
while ssh -o LogLevel=ERROR "$WAIT_SESSION_HOST" \
  "tmux has-session -t '$WAIT_SESSION' 2>/dev/null"; do
  sleep 30
done

ssh -o LogLevel=ERROR "$DESTINATION_NODE" "mkdir -p '$GB_DATA/tti-10m/dump'"
if ssh -o LogLevel=ERROR "$DESTINATION_NODE" "test -s '$REMOTE_DUMP'"; then
  destination_sha=$(ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
    "sha256sum '$REMOTE_DUMP' | awk '{print \$1}'")
  printf 'winner=primary-existing sha256=%s hedge_sha256=%s destination=%s:%s\n' \
    "$destination_sha" "$source_dump_sha" "$DESTINATION_NODE" "$REMOTE_DUMP" \
    | tee "$OUT/publication.txt"
  exit 0
fi

ssh -o LogLevel=ERROR "$DESTINATION_NODE" "rm -f '$PARTIAL_DUMP'"
ssh -o LogLevel=ERROR "$MEMORY_NODE" \
  "rsync -a '$REMOTE_DUMP' '$DESTINATION_NODE:$PARTIAL_DUMP'"
destination_partial_sha=$(ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
  "sha256sum '$PARTIAL_DUMP' | awk '{print \$1}'")
[[ "$source_dump_sha" == "$destination_partial_sha" ]] || {
  echo "destination dump SHA mismatch" >&2; exit 2;
}
ssh -o LogLevel=ERROR "$DESTINATION_NODE" \
  "test ! -e '$REMOTE_DUMP' && mv '$PARTIAL_DUMP' '$REMOTE_DUMP'"

printf 'winner=parallel-hedge sha256=%s destination=%s:%s\n' \
  "$source_dump_sha" "$DESTINATION_NODE" "$REMOTE_DUMP" \
  | tee "$OUT/publication.txt"
