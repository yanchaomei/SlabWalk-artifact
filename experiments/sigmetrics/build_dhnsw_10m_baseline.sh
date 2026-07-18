#!/usr/bin/env bash
# Build the patched d-HNSW 10M baseline on a host with the matching toolchain.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BUILD_ROOT=${BUILD_ROOT:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713/dhnsw-node7-v3-build}
SOURCE_DROOT=${SOURCE_DROOT:-/home/kvgroup/chaomei/d-HNSW}
RUNNER=${RUNNER:-$SCRIPT_DIR/run_dhnsw_frontier.sh}
EXPECTED_SOURCE_COMMIT=d6f275732275e6009a542a7066d7f695036daaf6
BUILD_JOBS=${BUILD_JOBS:-8}
DRY_RUN=${DRY_RUN:-0}

DROOT=$BUILD_ROOT/source
PROVENANCE=$BUILD_ROOT/provenance

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'build_host=%s source=%s output=%s jobs=%s\n' \
    "$(hostname)" "$SOURCE_DROOT" "$BUILD_ROOT" "$BUILD_JOBS"
  exit 0
fi

[[ ! -e "$BUILD_ROOT" ]] || { echo "Build root already exists: $BUILD_ROOT" >&2; exit 2; }
[[ -s "$RUNNER" ]] || { echo "Missing fixed-pool patch runner: $RUNNER" >&2; exit 2; }
[[ "$BUILD_JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid BUILD_JOBS: $BUILD_JOBS" >&2; exit 2; }

source_commit=$(git -C "$SOURCE_DROOT" rev-parse HEAD)
[[ "$source_commit" == "$EXPECTED_SOURCE_COMMIT" ]] || {
  echo "d-HNSW source commit drift: $source_commit" >&2
  exit 2
}
mkdir -p "$DROOT" "$PROVENANCE" "$BUILD_ROOT/patch-out" "$BUILD_ROOT/work"
git -C "$SOURCE_DROOT" archive "$source_commit" | tar -x -C "$DROOT"
printf '%s\n' "$source_commit" > "$PROVENANCE/source_commit.txt"
cp -p "$RUNNER" "$PROVENANCE/run_dhnsw_frontier.sh"

DROOT="$DROOT" OUT="$BUILD_ROOT/patch-out" WORK="$BUILD_ROOT/work" \
  EF_LIST='48 64 96 128 200' DATASETS='tti10M sift10M' PATCH_ONLY=1 \
  bash "$RUNNER" > "$PROVENANCE/patch.log" 2>&1

sha256sum "$DROOT/src/dhnsw/data_config.hh" \
  "$DROOT/src/bench/search_client_pipelined_reuse_thread.cc" \
  "$DROOT/src/bench/search_server.cc" > "$PROVENANCE/patched-source.sha256"
{
  cmake --version
  c++ --version
  protoc --version
} > "$PROVENANCE/toolchain.txt" 2>&1

cmake -S "$DROOT" -B "$DROOT/build" -DCMAKE_BUILD_TYPE=Release \
  > "$PROVENANCE/configure.log" 2>&1
cmake --build "$DROOT/build" -j"$BUILD_JOBS" --target run_client run_server \
  > "$PROVENANCE/build.log" 2>&1

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
ldd "$DROOT/build/run_client" "$DROOT/build/run_server" \
  > "$PROVENANCE/ldd.txt" 2>&1
if grep -q "not found" "$PROVENANCE/ldd.txt"; then
  echo "source-matched d-HNSW binary has unresolved runtime libraries" >&2
  exit 2
fi

python3 - "$BUILD_ROOT" "$source_commit" "$RUNNER" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
source_commit = sys.argv[2]
runner = Path(sys.argv[3])
droot = root / "source"
provenance = root / "provenance"

def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

sources = [
    droot / "src/dhnsw/data_config.hh",
    droot / "src/bench/search_client_pipelined_reuse_thread.cc",
    droot / "src/bench/search_server.cc",
]
binaries = {
    name: droot / "build" / name for name in ("run_client", "run_server")
}
record = {
    "kind": "dhnsw_source_matched_build",
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "source_commit": source_commit,
    "patch_runner_sha256": sha(runner),
    "patched_source_sha256": {
        str(path.relative_to(droot)): sha(path) for path in sources
    },
    "binary_sha256": {name: sha(path) for name, path in binaries.items()},
    "binary_size_bytes": {name: path.stat().st_size for name, path in binaries.items()},
    "dataset_paths": (provenance / "binary-dataset-paths.txt").read_text().splitlines(),
    "toolchain_sha256": sha(provenance / "toolchain.txt"),
    "configure_log_sha256": sha(provenance / "configure.log"),
    "build_log_sha256": sha(provenance / "build.log"),
    "ldd_sha256": sha(provenance / "ldd.txt"),
}
(provenance / "build_manifest.json").write_text(
    json.dumps(record, indent=2, sort_keys=True) + "\n"
)
print(json.dumps(record, indent=2, sort_keys=True))
PY

echo "Source-matched d-HNSW baseline built under $BUILD_ROOT"
