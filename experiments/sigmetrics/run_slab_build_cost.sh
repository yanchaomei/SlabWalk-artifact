#!/usr/bin/env bash
# Measure derived Slab construction on a fixed 1M-vector matrix.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
GB_BIN=${GB_BIN:-$REPO_ROOT/build/shine}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
BUILD_MN=${BUILD_MN:-skv-node4}
DATASETS=${DATASETS:-"SIFT1M DEEP1M GIST1M"}
REPEATS=${REPEATS:-5}
OUT=${OUT:-$REPO_ROOT/results/vldb_build_cost/raw}
TIMEOUT_S=${TIMEOUT_S:-3600}
PORT=${PORT:-1248}
INDEX_REGION_BYTES=${INDEX_REGION_BYTES:-4294967296}
DRY_RUN=${DRY_RUN:-0}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:-}
SOURCE_ROOT=${SOURCE_ROOT:-$REPO_ROOT}
EXPECTED_SOURCE_TREE_SHA=${EXPECTED_SOURCE_TREE_SHA:-}
ADMISSION_GATE=${ADMISSION_GATE:-}
EXPECTED_ADMISSION_GATE_SHA=${EXPECTED_ADMISSION_GATE_SHA:-}
ADMISSION_SCOPE=${ADMISSION_SCOPE:-}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-build-cost-$(date -u +%Y%m%dT%H%M%SZ)}
ACTIVE_MN=""
ACTIVE_REMOTE_DIR=""

BUILD_ENV="SHINE_CRANE=1 GB_BITMAP_DEDUP=1 SHINE_LAVD_BUILD_THREADS=20 SHINE_LAVD_BUILD_CPU_BASE=1 SHINE_LAVD_BUILD_CPU_STRIDE=2 SHINE_LAVD_STAGED_BUILD=1 SHINE_LAVD_SELFTEST=1"
SIFT1M_SPEC="$BUILD_MN|$GB_DATA/sift1m/|uniform|100|5368709120|$BUILD_ENV"
DEEP1M_SPEC="$BUILD_MN|$GB_DATA/deep1m/|uniform|100|4294967296|$BUILD_ENV"
GIST1M_SPEC="$BUILD_MN|$GB_DATA/gist1m/|u10k|400|9663676416|$BUILD_ENV SHINE_LAVD_RABITQ_B=2"

dataset_spec() {
  case "$1" in
    SIFT1M) printf '%s\n' "$SIFT1M_SPEC" ;;
    DEEP1M) printf '%s\n' "$DEEP1M_SPEC" ;;
    GIST1M) printf '%s\n' "$GIST1M_SPEC" ;;
    *) printf 'Unknown build-cost dataset: %s\n' "$1" >&2; return 2 ;;
  esac
}

verify_remote_pid() {
  local host=$1 remote_dir=$2
  ssh -o LogLevel=ERROR "$host" \
    "test -s '$remote_dir/server.pid' -a -s '$remote_dir/server.exe'; \
     pid=\$(cat '$remote_dir/server.pid'); expected=\$(cat '$remote_dir/server.exe'); \
     actual=\$(readlink -f /proc/\$pid/exe 2>/dev/null); \
     test -n \"\$actual\" -a \"\$actual\" = \"\$expected\"" 2>/dev/null
}

stop_active_mn() {
  if [[ -n "$ACTIVE_MN" && -n "$ACTIVE_REMOTE_DIR" ]]; then
    if verify_remote_pid "$ACTIVE_MN" "$ACTIVE_REMOTE_DIR"; then
      ssh -o LogLevel=ERROR "$ACTIVE_MN" \
        "pid=\$(cat '$ACTIVE_REMOTE_DIR/server.pid'); kill \$pid 2>/dev/null || true" || true
    fi
    ACTIVE_MN=""
    ACTIVE_REMOTE_DIR=""
  fi
}

start_mn() {
  local host=$1 remote_dir=$2
  ssh -o LogLevel=ERROR "$host" \
    "rm -rf '$remote_dir'; mkdir -p '$remote_dir'; \
     realpath '$GB_BIN_R' > '$remote_dir/server.exe'; \
     nohup env LD_LIBRARY_PATH='$GB_LIB' numactl --preferred=1 '$GB_BIN_R' \
       --is-server --num-clients 1 --port '$PORT' \
       --index-region-bytes '$INDEX_REGION_BYTES' \
       > '$remote_dir/mn.out' 2> '$remote_dir/mn.err' < /dev/null & \
     echo \$! > '$remote_dir/server.pid'"
  ACTIVE_MN=$host
  ACTIVE_REMOTE_DIR=$remote_dir
  for _ in $(seq 1 100); do
    verify_remote_pid "$host" "$remote_dir" && return 0
    sleep 0.1
  done
  printf 'Memory-node process failed ownership verification on %s\n' "$host" >&2
  return 1
}

print_command() {
  local dataset=$1 rep=$2 mn=$3 data=$4 query_suffix=$5 ef=$6 capacity=$7 extra=$8
  printf 'dataset=%s repeat=%s mn=%s port=%s data=%s query=%s ef=%s capacity=%s env=%s\n' \
    "$dataset" "$rep" "$mn" "$PORT" "$data" "$query_suffix" "$ef" "$capacity" "$extra"
}

verify_dataset_inputs() {
  local dataset=$1 mn=$2 data=$3 query_suffix=$4
  local path
  for path in \
    "$data/base.fbin" \
    "$data/queries/query-${query_suffix}.fbin" \
    "$data/queries/warmup-${query_suffix}.fbin" \
    "$data/queries/groundtruth-${query_suffix}.bin"; do
    [[ -s "$path" ]] || {
      printf 'Build-cost preflight failed: dataset=%s missing CN input %s\n' \
        "$dataset" "$path" >&2
      return 2
    }
  done
  local dump="$data/dump/index_m16_efc100_node1_of1.dat"
  ssh -o LogLevel=ERROR "$mn" "test -s '$dump'" || {
    printf 'Build-cost preflight failed: dataset=%s missing MN dump %s:%s\n' \
      "$dataset" "$mn" "$dump" >&2
    return 2
  }
}

run_one() {
  local dataset=$1 rep=$2 mn=$3 data=$4 query_suffix=$5 ef=$6 capacity=$7 extra=$8
  local tag="vldb_build_${dataset}_r${rep}"
  local json="$OUT/${dataset}_r${rep}.json"
  local err="$OUT/${dataset}_r${rep}.err"
  local remote_dir="/tmp/${tag}_p${PORT}"
  local -a extra_env=()
  read -r -a extra_env <<< "$extra"

  if [[ "$DRY_RUN" == "1" ]]; then
    print_command "$dataset" "$rep" "$mn" "$data" "$query_suffix" "$ef" "$capacity" "$extra"
    return 0
  fi

  verify_dataset_inputs "$dataset" "$mn" "$data" "$query_suffix"
  printf '=== %s repeat %s/%s on %s ===\n' "$dataset" "$((rep + 1))" "$REPEATS" "$mn"
  start_mn "$mn" "$remote_dir"
  set +e
  timeout "$TIMEOUT_S" /usr/bin/time -v env "${extra_env[@]}" \
    LD_LIBRARY_PATH="$GB_LIB" numactl --preferred=1 "$GB_BIN" \
    --servers "$mn" --initiator --threads 1 --coroutines 1 --port "$PORT" \
    --index-region-bytes "$INDEX_REGION_BYTES" \
    --data-path "$data" --query-suffix "$query_suffix" \
    --ef-search "$ef" --ef-construction 100 --m 16 --k 10 \
    --label "$tag" --spec-k 1 --load-index --lavd 8 \
    --lavd-region-bytes "$capacity" > "$json" 2> "$err"
  local rc=$?
  set -e
  stop_active_mn
  ssh -o LogLevel=ERROR "$mn" "cat '$remote_dir/mn.err'" \
    > "$OUT/${dataset}_r${rep}.mn.err" 2>/dev/null || true

  if [[ $rc -ne 0 || ! -s "$json" ]]; then
    printf 'Build-cost run failed: dataset=%s repeat=%s rc=%s\n' "$dataset" "$rep" "$rc" >&2
    tail -60 "$err" >&2 || true
    [[ $rc -ne 0 ]] && return "$rc"
    return 1
  fi
  python3 - "$json" "$err" "$dataset" <<'PY'
import json, pathlib, re, sys
obj = json.load(open(sys.argv[1]))
stderr = pathlib.Path(sys.argv[2]).read_text(errors="replace")
dataset = sys.argv[3]
assert obj["queries"]["processed"] == obj["num_queries"] > 0
assert obj["timings"]["lavd_build"] > 0
assert obj["timings"]["crane_build_multi"] >= 0
publication = re.findall(r"^LAVD_BUILD_PUBLICATION (\{.*\})$", stderr, re.MULTILINE)
assert len(publication) == 1
publication = json.loads(publication[0])
assert publication["mode"] == "staged_fixed"
assert publication["workers"] == 20
assert publication["records"] == obj["num_vectors"]
assert re.search(r"\[LAVD\]\[selftest\] checked=64 fails=0 .* PASS", stderr)
assert "retained authoritative snapshot for resident upper graph" in stderr
assert "reused authoritative build snapshot" in stderr
if dataset == "GIST1M":
    assert re.search(r"\[LAVD\]\[rabitq\].*B=2.*rotation_reused=true", stderr)
PY
}

trap stop_active_mn EXIT INT TERM

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || {
  printf 'REPEATS must be a positive integer, got %s\n' "$REPEATS" >&2; exit 2;
}
[[ "$PORT" =~ ^[1-9][0-9]*$ ]] && ((PORT <= 65535)) || {
  printf 'PORT must be in 1..65535\n' >&2; exit 2;
}
if [[ -n "$EXPECTED_BINARY_SHA" && ! "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'EXPECTED_BINARY_SHA must be a lowercase SHA-256, got %s\n' \
    "$EXPECTED_BINARY_SHA" >&2
  exit 2
fi
if [[ -n "$EXPECTED_SOURCE_TREE_SHA" && ! "$EXPECTED_SOURCE_TREE_SHA" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'EXPECTED_SOURCE_TREE_SHA must be a lowercase SHA-256, got %s\n' \
    "$EXPECTED_SOURCE_TREE_SHA" >&2
  exit 2
fi
if [[ -n "$EXPECTED_ADMISSION_GATE_SHA" && ! "$EXPECTED_ADMISSION_GATE_SHA" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'EXPECTED_ADMISSION_GATE_SHA must be a lowercase SHA-256, got %s\n' \
    "$EXPECTED_ADMISSION_GATE_SHA" >&2
  exit 2
fi
if [[ -n "$ADMISSION_GATE" || -n "$EXPECTED_ADMISSION_GATE_SHA" || -n "$ADMISSION_SCOPE" ]]; then
  [[ -n "$ADMISSION_GATE" && -n "$EXPECTED_ADMISSION_GATE_SHA" && -n "$ADMISSION_SCOPE" ]] || {
    printf 'ADMISSION_GATE, EXPECTED_ADMISSION_GATE_SHA, and ADMISSION_SCOPE must be set together\n' >&2
    exit 2
  }
fi
if [[ -n "$ADMISSION_SCOPE" && "$ADMISSION_SCOPE" != "construction_measurements_only" ]]; then
  printf 'unsupported ADMISSION_SCOPE: %s\n' "$ADMISSION_SCOPE" >&2
  exit 2
fi

if [[ "$DRY_RUN" != "1" ]]; then
  if [[ -n "$ADMISSION_GATE" ]]; then
    [[ -s "$ADMISSION_GATE" ]] || {
      printf 'Admission gate is missing: %s\n' "$ADMISSION_GATE" >&2
      exit 2
    }
    admission_sha=$(sha256sum "$ADMISSION_GATE" | awk '{print $1}')
    [[ "$admission_sha" == "$EXPECTED_ADMISSION_GATE_SHA" ]] || {
      printf 'Admission gate SHA mismatch: expected %s, got %s\n' \
        "$EXPECTED_ADMISSION_GATE_SHA" "$admission_sha" >&2
      exit 2
    }
    python3 - "$ADMISSION_GATE" "$ADMISSION_SCOPE" <<'PY'
import json
import sys

gate = json.load(open(sys.argv[1]))
scope = sys.argv[2]
if gate.get("kind") != "vldb_construction_candidate_gate_v1":
    raise SystemExit("unsupported construction admission gate")
if gate.get("construction_ready") is not True or gate.get("failures") != []:
    raise SystemExit("construction admission gate is not ready")
if gate.get("general_promotion_ready") is not False or gate.get("scope") != scope:
    raise SystemExit("construction admission scope mismatch")
PY
  fi
  mkdir -p "$OUT"
  [[ -x "$GB_BIN" ]] || { printf 'CN binary is not executable: %s\n' "$GB_BIN" >&2; exit 2; }
  cn_sha=$(sha256sum "$GB_BIN" | awk '{print $1}')
  mn_sha=$(ssh -o LogLevel=ERROR "$BUILD_MN" \
    "test -x '$GB_BIN_R' && sha256sum '$GB_BIN_R'" | awk '{print $1}')
  [[ "$cn_sha" == "$mn_sha" ]] || {
    printf 'CN/MN binary SHA mismatch: %s != %s\n' "$cn_sha" "$mn_sha" >&2
    exit 2
  }
  if [[ -n "$EXPECTED_BINARY_SHA" && "$cn_sha" != "$EXPECTED_BINARY_SHA" ]]; then
    printf 'Frozen binary SHA mismatch: expected %s, got %s\n' \
      "$EXPECTED_BINARY_SHA" "$cn_sha" >&2
    exit 2
  fi
  script_sha=$(sha256sum "$0" | awk '{print $1}')
  python3 - "$OUT/campaign.json" "$CAMPAIGN_ID" "$cn_sha" "$script_sha" \
    "$PORT" "$REPEATS" "$INDEX_REGION_BYTES" "$BUILD_MN" \
    "$(hostname)" "$DATASETS" "$SOURCE_ROOT" \
    "$EXPECTED_SOURCE_TREE_SHA" "$ADMISSION_GATE" \
    "$EXPECTED_ADMISSION_GATE_SHA" "$ADMISSION_SCOPE" <<'PY'
import hashlib
import json
import pathlib
import sys
(
    path,
    campaign_id,
    binary_sha,
    script_sha,
    port,
    repeats,
    index_region_bytes,
    memory_node,
    compute_node,
    datasets,
    source_root_raw,
    expected_source_tree_sha,
    admission_gate_raw,
    expected_admission_gate_sha,
    admission_scope,
) = sys.argv[1:]
source_root = pathlib.Path(source_root_raw).resolve()
source_scopes = {
    "repository": [
        "graphbeyond/CMakeLists.txt",
        "graphbeyond/src",
        "graphbeyond/rdma-library/CMakeLists.txt",
        "graphbeyond/rdma-library/FindIBVerbs.cmake",
        "graphbeyond/rdma-library/library",
        "graphbeyond/thirdparty",
    ],
    "graphbeyond_project": [
        "CMakeLists.txt",
        "src",
        "rdma-library/CMakeLists.txt",
        "rdma-library/FindIBVerbs.cmake",
        "rdma-library/library",
        "thirdparty",
    ],
}
if (source_root / "graphbeyond/CMakeLists.txt").is_file():
    source_layout = "repository"
elif (source_root / "CMakeLists.txt").is_file() and (source_root / "src").is_dir():
    source_layout = "graphbeyond_project"
else:
    raise SystemExit(f"cannot identify build source layout: {source_root}")
scope = source_scopes[source_layout]
source_files = []
for relative in scope:
    path = source_root / relative
    if path.is_file():
        source_files.append(path)
    elif path.is_dir():
        source_files.extend(candidate for candidate in path.rglob("*") if candidate.is_file())
if not source_files:
    raise SystemExit("build source scope is empty")
hasher = hashlib.sha256()
for path in sorted(source_files, key=lambda item: item.relative_to(source_root).as_posix()):
    relative = path.relative_to(source_root).as_posix().encode()
    payload = path.read_bytes()
    hasher.update(len(relative).to_bytes(8, "little"))
    hasher.update(relative)
    hasher.update(len(payload).to_bytes(8, "little"))
    hasher.update(payload)
source_tree_sha = hasher.hexdigest()
if expected_source_tree_sha and source_tree_sha != expected_source_tree_sha:
    raise SystemExit(
        f"build source tree SHA mismatch: {source_tree_sha} != {expected_source_tree_sha}"
    )
admission = None
if admission_gate_raw:
    admission_path = pathlib.Path(admission_gate_raw).resolve()
    admission_sha = hashlib.sha256(admission_path.read_bytes()).hexdigest()
    if admission_sha != expected_admission_gate_sha:
        raise SystemExit(
            f"admission gate SHA mismatch: {admission_sha} != {expected_admission_gate_sha}"
        )
    admission_gate = json.loads(admission_path.read_text())
    if (
        admission_gate.get("kind") != "vldb_construction_candidate_gate_v1"
        or admission_gate.get("construction_ready") is not True
        or admission_gate.get("general_promotion_ready") is not False
        or admission_gate.get("scope") != admission_scope
        or admission_gate.get("failures") != []
    ):
        raise SystemExit("invalid construction admission gate")
    admission = {
        "kind": admission_gate["kind"],
        "path": str(admission_path),
        "sha256": admission_sha,
        "scope": admission_scope,
        "construction_ready": True,
        "general_promotion_ready": False,
    }
json.dump({"campaign_id": campaign_id,
           "binary_sha256": binary_sha,
           "script_sha256": script_sha,
           "tcp_port": int(port), "repeats": int(repeats),
           "index_region_bytes": int(index_region_bytes),
           "compute_node": compute_node, "memory_node": memory_node,
           "datasets": datasets.split(), "builder_threads": 20,
           "query_threads": 1, "query_coroutines": 1,
           "layout": "packed_fixed", "measurement": "derived_build_only",
           "build_path": "single_mn_staged_fixed_v1",
           "publication_workers": 20, "staging_bytes": 67108864,
           "selftest_records": 64, "resident_upper_graph": True,
           "authoritative_snapshot_reuse": True,
           "admission": admission,
           "source": {
               "root": str(source_root),
               "layout": source_layout,
               "tree_sha256": source_tree_sha,
               "tree_scope": scope,
               "file_count": len(source_files),
           }},
          open(path, "w"), indent=2, sort_keys=True)
PY
fi

for dataset in $DATASETS; do
  IFS='|' read -r mn data query_suffix ef capacity extra <<< "$(dataset_spec "$dataset")"
  for ((rep = 0; rep < REPEATS; ++rep)); do
    run_one "$dataset" "$rep" "$mn" "$data" "$query_suffix" "$ef" "$capacity" "$extra"
  done
done

[[ "$DRY_RUN" == "1" ]] || printf 'Raw construction measurements written to %s\n' "$OUT"
