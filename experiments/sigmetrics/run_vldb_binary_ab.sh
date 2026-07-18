#!/usr/bin/env bash
# Alternate two GraphBeyond binaries for one fixed access method.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BINARY_AB_SOURCE_SCRIPT_DIR=${BINARY_AB_SOURCE_SCRIPT_DIR:-$SCRIPT_DIR}
BINARY_AB_SCRIPT_PATH=$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")
REPO_ROOT=${REPO_ROOT:-$(cd -- "$BINARY_AB_SOURCE_SCRIPT_DIR/../.." && pwd)}
SOURCE_ROOT_A=${SOURCE_ROOT_A:-$REPO_ROOT}
SOURCE_ROOT_B=${SOURCE_ROOT_B:-$REPO_ROOT}
EVIDENCE_TOOL=${EVIDENCE_TOOL:-$BINARY_AB_SOURCE_SCRIPT_DIR/vldb_evidence_bundle.py}
BIN_A=${BIN_A:?set BIN_A to the baseline binary}
BIN_B=${BIN_B:?set BIN_B to the candidate binary}
LABEL_A=${LABEL_A:-baseline}
LABEL_B=${LABEL_B:-optimized}
RUNNER=${RUNNER:-$BINARY_AB_SOURCE_SCRIPT_DIR/run_vldb_query_profile.sh}
OUT_ROOT=${OUT_ROOT:-$PWD/results/vldb_binary_ab_$(date -u +%Y%m%dT%H%M%SZ)}
REPEATS=${REPEATS:-6}
CAMPAIGN_KIND=${CAMPAIGN_KIND:-formal}
ALLOW_IDENTICAL_BINARY_SHA=${ALLOW_IDENTICAL_BINARY_SHA:-0}
DATASET=${DATASET:-DEEP1M}
METHOD=${METHOD:-slabwalk}
THREADS=${THREADS:-10}
COROUTINES=${COROUTINES:-2}
EF=${EF:-200}
TOP_K=${TOP_K:-10}
PORT=${PORT:-1290}
TIMEOUT_S=${TIMEOUT_S:-1200}
QUERY_CONTEXTS_A=${QUERY_CONTEXTS_A:-0}
QUERY_CONTEXTS_B=${QUERY_CONTEXTS_B:-0}
COMPUTE_RECALL=${COMPUTE_RECALL:-1}
QUERY_TILE=${QUERY_TILE:-1}
VARIANT_ENV_A=${VARIANT_ENV_A:-}
VARIANT_ENV_B=${VARIANT_ENV_B:-}
CAPTURE_BUILD_METRICS=${CAPTURE_BUILD_METRICS:-1}
REQUIRE_QUERY_INVARIANTS=${REQUIRE_QUERY_INVARIANTS:-1}
EXPECTED_BUILD_MODE_A=${EXPECTED_BUILD_MODE_A:-serial}
EXPECTED_BUILD_MODE_B=${EXPECTED_BUILD_MODE_B:-staged}
EXPECTED_BUILD_WORKERS_A=${EXPECTED_BUILD_WORKERS_A:-1}
EXPECTED_BUILD_WORKERS_B=${EXPECTED_BUILD_WORKERS_B:-20}

[[ "$REPEATS" =~ ^[1-9][0-9]*$ ]] || { echo "REPEATS must be positive" >&2; exit 2; }
[[ "$CAMPAIGN_KIND" == "formal" || "$CAMPAIGN_KIND" == "smoke" ]] || {
  echo "CAMPAIGN_KIND must be formal or smoke" >&2; exit 2;
}
if [[ "$CAMPAIGN_KIND" == "formal" ]] && (( REPEATS % 2 != 0 )); then
  echo "Formal A/B evidence requires an even REPEATS value" >&2
  exit 2
fi
[[ "$ALLOW_IDENTICAL_BINARY_SHA" == "0" || "$ALLOW_IDENTICAL_BINARY_SHA" == "1" ]] || {
  echo "ALLOW_IDENTICAL_BINARY_SHA must be 0 or 1" >&2; exit 2;
}
[[ "$DATASET" =~ ^[A-Za-z0-9_]+$ ]] || { echo "DATASET contains unsupported characters" >&2; exit 2; }
[[ "$METHOD" == "slabwalk" || "$METHOD" == "shine" ]] || {
  echo "METHOD must be slabwalk or shine" >&2; exit 2;
}
for contexts in "$QUERY_CONTEXTS_A" "$QUERY_CONTEXTS_B"; do
  [[ "$contexts" =~ ^[0-9]+$ ]] || { echo "query-context values must be non-negative" >&2; exit 2; }
  (( contexts == 0 || contexts <= THREADS )) || { echo "query-context values must not exceed THREADS" >&2; exit 2; }
done
[[ "$COMPUTE_RECALL" == "0" || "$COMPUTE_RECALL" == "1" ]] || { echo "COMPUTE_RECALL must be 0 or 1" >&2; exit 2; }
[[ "$CAPTURE_BUILD_METRICS" == "0" || "$CAPTURE_BUILD_METRICS" == "1" ]] || { echo "CAPTURE_BUILD_METRICS must be 0 or 1" >&2; exit 2; }
[[ "$REQUIRE_QUERY_INVARIANTS" == "0" || "$REQUIRE_QUERY_INVARIANTS" == "1" ]] || { echo "REQUIRE_QUERY_INVARIANTS must be 0 or 1" >&2; exit 2; }
[[ "$QUERY_TILE" =~ ^[1-9][0-9]*$ ]] || { echo "QUERY_TILE must be positive" >&2; exit 2; }
for mode in "$EXPECTED_BUILD_MODE_A" "$EXPECTED_BUILD_MODE_B"; do
  [[ "$mode" == "serial" || "$mode" == "staged" ]] || {
    echo "expected build modes must be serial or staged" >&2; exit 2;
  }
done
for workers in "$EXPECTED_BUILD_WORKERS_A" "$EXPECTED_BUILD_WORKERS_B"; do
  [[ "$workers" =~ ^[1-9][0-9]*$ ]] || {
    echo "expected build workers must be positive" >&2; exit 2;
  }
done
if [[ "$COMPUTE_RECALL" == "1" && "$QUERY_TILE" != "1" ]]; then
  echo "QUERY_TILE must be 1 when COMPUTE_RECALL=1" >&2
  exit 2
fi
for path in "$BIN_A" "$BIN_B" "$RUNNER"; do
  [[ -x "$path" ]] || { echo "Missing executable: $path" >&2; exit 2; }
done
for path in "$SOURCE_ROOT_A" "$SOURCE_ROOT_B"; do
  [[ -d "$path" ]] || { echo "Missing variant source root: $path" >&2; exit 2; }
done
[[ -f "$EVIDENCE_TOOL" ]] || { echo "Missing evidence tool: $EVIDENCE_TOOL" >&2; exit 2; }
validate_variant_env() {
  local raw=$1 assignment
  local -a assignments=()
  [[ -n "$raw" ]] || return 0
  read -r -a assignments <<< "$raw"
  for assignment in "${assignments[@]}"; do
    [[ "$assignment" =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]] || {
      echo "Invalid variant environment assignment: $assignment" >&2
      exit 2
    }
  done
}
validate_variant_env "$VARIANT_ENV_A"
validate_variant_env "$VARIANT_ENV_B"

SHA_A=$(sha256sum "$BIN_A" | awk '{print $1}')
SHA_B=$(sha256sum "$BIN_B" | awk '{print $1}')
COMPUTE_HOST=$(hostname)
COMPARISON_KIND=binary
if [[ "$SHA_A" == "$SHA_B" ]]; then
  [[ "$ALLOW_IDENTICAL_BINARY_SHA" == "1" ]] || {
    echo "Refusing identical binary SHA without explicit configuration A/B mode" >&2
    exit 2
  }
  if [[ "$VARIANT_ENV_A" == "$VARIANT_ENV_B" \
        && "$QUERY_CONTEXTS_A" == "$QUERY_CONTEXTS_B" \
        && "$EXPECTED_BUILD_MODE_A" == "$EXPECTED_BUILD_MODE_B" \
        && "$EXPECTED_BUILD_WORKERS_A" == "$EXPECTED_BUILD_WORKERS_B" ]]; then
    echo "Identical binary configuration A/B requires a declared configuration difference" >&2
    exit 2
  fi
  COMPARISON_KIND=same_binary_configuration
fi

if [[ "${VLDB_BINARY_AB_HARNESS_FROZEN:-0}" != "1" ]]; then
  if [[ -e "$OUT_ROOT" ]]; then
    echo "Refusing existing OUT_ROOT: $OUT_ROOT" >&2
    exit 2
  fi
  mkdir -p "$OUT_ROOT"
  snapshot_json=$(python3 "$EVIDENCE_TOOL" snapshot \
    --out-dir "$OUT_ROOT/harness" \
    --entry runner="$BINARY_AB_SCRIPT_PATH" \
    --entry query_runner="$RUNNER" \
    --entry evidence_tool="$EVIDENCE_TOOL")
  read -r frozen_runner frozen_query_runner frozen_tool harness_sha <<< "$(
    python3 - "$OUT_ROOT/harness" "$snapshot_json" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
payload = json.loads(sys.argv[2])
print(
    root / payload["entries"]["runner"]["path"],
    root / payload["entries"]["query_runner"]["path"],
    root / payload["entries"]["evidence_tool"]["path"],
    payload["manifest_sha256"],
)
PY
  )"
  exec env \
    VLDB_BINARY_AB_HARNESS_FROZEN=1 \
    BINARY_AB_SOURCE_SCRIPT_DIR="$BINARY_AB_SOURCE_SCRIPT_DIR" \
    REPO_ROOT="$REPO_ROOT" SOURCE_ROOT_A="$SOURCE_ROOT_A" \
    SOURCE_ROOT_B="$SOURCE_ROOT_B" OUT_ROOT="$OUT_ROOT" \
    RUNNER="$frozen_query_runner" EVIDENCE_TOOL="$frozen_tool" \
    HARNESS_MANIFEST="$OUT_ROOT/harness/harness.json" \
    HARNESS_MANIFEST_SHA256="$harness_sha" \
    bash "$frozen_runner"
fi

HARNESS_MANIFEST=${HARNESS_MANIFEST:?frozen harness manifest is required}
HARNESS_MANIFEST_SHA256=${HARNESS_MANIFEST_SHA256:?frozen harness SHA is required}
verify_harness() {
  python3 "$EVIDENCE_TOOL" verify-harness \
    --manifest "$HARNESS_MANIFEST" \
    --expected-manifest-sha "$HARNESS_MANIFEST_SHA256" >/dev/null
}
verify_harness
[[ -d "$OUT_ROOT/harness" && ! -e "$OUT_ROOT/campaign.json" ]] || {
  echo "Invalid frozen binary A/B output root" >&2
  exit 2
}
RUNNER_SHA=$(sha256sum "$RUNNER" | awk '{print $1}')
AB_RUNNER_SHA=$(sha256sum "$BINARY_AB_SCRIPT_PATH" | awk '{print $1}')
python3 - "$OUT_ROOT/campaign.json" "$LABEL_A" "$LABEL_B" "$BIN_A" "$BIN_B" \
  "$SHA_A" "$SHA_B" "$RUNNER" "$RUNNER_SHA" "$DATASET" "$METHOD" "$REPEATS" "$THREADS" \
  "$COROUTINES" "$EF" "$TOP_K" "$PORT" "$QUERY_CONTEXTS_A" \
  "$QUERY_CONTEXTS_B" "$COMPUTE_RECALL" "$QUERY_TILE" \
  "$VARIANT_ENV_A" "$VARIANT_ENV_B" "$CAPTURE_BUILD_METRICS" \
  "$REQUIRE_QUERY_INVARIANTS" "$EXPECTED_BUILD_MODE_A" \
  "$EXPECTED_BUILD_MODE_B" "$EXPECTED_BUILD_WORKERS_A" \
  "$EXPECTED_BUILD_WORKERS_B" "$BINARY_AB_SCRIPT_PATH" "$AB_RUNNER_SHA" \
  "$REPO_ROOT" "$SOURCE_ROOT_A" "$SOURCE_ROOT_B" \
  "$COMPUTE_HOST" "$CAMPAIGN_KIND" "$COMPARISON_KIND" \
  "$ALLOW_IDENTICAL_BINARY_SHA" "$HARNESS_MANIFEST" \
  "$HARNESS_MANIFEST_SHA256" <<'PY'
import hashlib, json, pathlib, subprocess, sys, uuid
from datetime import datetime, timezone

(output_path, label_a, label_b, bin_a, bin_b, sha_a, sha_b, runner, runner_sha,
 dataset, method, repeats, threads, coroutines, ef, top_k, port, query_contexts_a,
 query_contexts_b, compute_recall, query_tile, variant_env_a,
 variant_env_b, capture_build_metrics, require_query_invariants,
 expected_mode_a, expected_mode_b, expected_workers_a, expected_workers_b,
 ab_runner, ab_runner_sha, repo_root, source_root_a, source_root_b,
 compute_host, campaign_kind, comparison_kind,
 allow_identical_binary_sha, harness_manifest_path,
 harness_manifest_sha256) = sys.argv[1:]
repeats = int(repeats)
compute_recall = bool(int(compute_recall))
query_tile = int(query_tile)
harness_path = pathlib.Path(harness_manifest_path)
harness_bytes = harness_path.read_bytes()
if hashlib.sha256(harness_bytes).hexdigest() != harness_manifest_sha256:
    raise SystemExit("binary A/B harness manifest drifted before campaign creation")
harness = json.loads(harness_bytes)

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
def source_record(raw_root):
    source_root = pathlib.Path(raw_root).resolve()
    if (source_root / "CMakeLists.txt").is_file() and (
        source_root / "src"
    ).is_dir():
        source_layout = "graphbeyond_project"
    elif (source_root / "graphbeyond" / "CMakeLists.txt").is_file() and (
        source_root / "graphbeyond" / "src"
    ).is_dir():
        source_layout = "repository"
    else:
        raise SystemExit(f"cannot identify variant source layout: {source_root}")
    source_scope = source_scopes[source_layout]
    source_files = []
    for relative in source_scope:
        source_path = source_root / relative
        if source_path.is_file():
            source_files.append(source_path)
        elif source_path.is_dir():
            source_files.extend(
                candidate
                for candidate in source_path.rglob("*")
                if candidate.is_file()
            )
    if not source_files:
        raise SystemExit(f"variant source scope is empty: {source_root}")
    source_hasher = hashlib.sha256()
    for source_path in sorted(
        source_files, key=lambda item: item.relative_to(source_root).as_posix()
    ):
        relative = source_path.relative_to(source_root).as_posix().encode()
        payload = source_path.read_bytes()
        source_hasher.update(len(relative).to_bytes(8, "little"))
        source_hasher.update(relative)
        source_hasher.update(len(payload).to_bytes(8, "little"))
        source_hasher.update(payload)
    git_record = {
        "git_available": False,
        "git_head": None,
        "git_dirty": None,
    }
    probe = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        status = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=normal"],
            check=True,
            capture_output=True,
            text=True,
        )
        git_record = {
            "git_available": True,
            "git_head": probe.stdout.strip(),
            "git_dirty": bool(status.stdout.strip()),
        }
    return {
        "root": str(source_root),
        "layout": source_layout,
        "tree_sha256": source_hasher.hexdigest(),
        "tree_scope": source_scope,
        "file_count": len(source_files),
        **git_record,
    }
variant_sources = {
    "A": source_record(source_root_a),
    "B": source_record(source_root_b),
}
def parse_env(raw):
    parsed = {}
    for assignment in raw.split():
        key, value = assignment.split("=", 1)
        parsed[key] = value
    return dict(sorted(parsed.items()))
protocol = {
    "compute_host": compute_host,
    "campaign_kind": campaign_kind,
    "comparison_kind": comparison_kind,
    "allow_identical_binary_sha": bool(int(allow_identical_binary_sha)),
    "variants": {
        "A": {"label": label_a, "path": bin_a, "sha256": sha_a,
              "query_contexts": int(query_contexts_a),
              "environment": parse_env(variant_env_a),
              "expected_build_mode": expected_mode_a,
              "expected_build_workers": int(expected_workers_a),
              "source": variant_sources["A"]},
        "B": {"label": label_b, "path": bin_b, "sha256": sha_b,
              "query_contexts": int(query_contexts_b),
              "environment": parse_env(variant_env_b),
              "expected_build_mode": expected_mode_b,
              "expected_build_workers": int(expected_workers_b),
              "source": variant_sources["B"]},
    },
    "runner_path": runner,
    "runner_sha256": runner_sha,
    "ab_runner_path": ab_runner,
    "ab_runner_sha256": ab_runner_sha,
    "source": variant_sources["A"],
    "source_identity_version": 2,
    "dataset": dataset,
    "method": method,
    "measurement_mode": "complete_fixed_query_pool",
    "compute_recall": compute_recall,
    "query_tile": query_tile,
    "query_pool_size": 10_000 if compute_recall else 10_000 * query_tile,
    "capture_perf": False,
    "capture_build_metrics": bool(int(capture_build_metrics)),
    "require_query_invariants": bool(int(require_query_invariants)),
    "repeats": repeats,
    "order": ["AB" if rep % 2 == 0 else "BA" for rep in range(repeats)],
    "threads": int(threads),
    "coroutines": int(coroutines),
    "ef": int(ef),
    "top_k": int(top_k),
    "tcp_port": int(port),
    "harness": {
        "manifest": "harness/harness.json",
        "manifest_sha256": harness_manifest_sha256,
        "entries": harness["entries"],
    },
}
fingerprint = hashlib.sha256(
    json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
json.dump({
    "campaign_id": f"vldb-binary-ab-{uuid.uuid4()}",
    "campaign_uuid": str(uuid.uuid4()),
    "created_utc": datetime.now(timezone.utc).isoformat(),
    "protocol_fingerprint": fingerprint,
    "protocol": protocol,
}, open(output_path, "w"), indent=2, sort_keys=True)
PY

RUNS=$OUT_ROOT/runs.csv
printf 'repeat,dataset,position,variant,label,compute_host,binary_sha256,mn_binary_sha256,input_signature,result_hash_version,execution_provenance,execution_provenance_sha256,variant_env,query_contexts,compute_recall,query_tile,qps,recall,result_hash,p50_us,p95_us,p99_us,sq8_prefix_rejections_per_query,posts_per_query,bytes_per_query,processed,build_mode,build_workers,rank_workers,rank_workers_recorded,staging_bytes,record_write_posts,selection_hash,descriptor_version,physical_signature,budget_map_required,physical_bytes,build_total_ms,build_rank_ms,build_materialize_ms,build_assemble_ms,build_publish_ms,json,campaign,status\n' > "$RUNS"

run_one() {
  local rep=$1 position=$2 variant=$3
  local binary label expected_sha query_contexts variant_env
  local expected_build_mode expected_build_workers
  if [[ "$variant" == "A" ]]; then
    binary=$BIN_A; label=$LABEL_A; expected_sha=$SHA_A
    query_contexts=$QUERY_CONTEXTS_A
    variant_env=$VARIANT_ENV_A
    expected_build_mode=$EXPECTED_BUILD_MODE_A
    expected_build_workers=$EXPECTED_BUILD_WORKERS_A
  else
    binary=$BIN_B; label=$LABEL_B; expected_sha=$SHA_B
    query_contexts=$QUERY_CONTEXTS_B
    variant_env=$VARIANT_ENV_B
    expected_build_mode=$EXPECTED_BUILD_MODE_B
    expected_build_workers=$EXPECTED_BUILD_WORKERS_B
  fi
  local run_out="$OUT_ROOT/r${rep}_${position}_${variant}"
  local run_campaign="binary-ab-r${rep}-${position}-${variant}"
  local -a variant_env_args=()
  [[ -z "$variant_env" ]] || read -r -a variant_env_args <<< "$variant_env"
  local -a run_env=(
    "GB_BIN=$binary" "GB_BIN_R=$binary" "OUT=$run_out"
    "CAMPAIGN_ID=$run_campaign" "DATASETS=$DATASET" "METHODS=$METHOD"
    "THREADS=$THREADS" "COROUTINES=$COROUTINES" "EF=$EF" "TOP_K=$TOP_K"
    "TILE=$QUERY_TILE" "CAPTURE_PERF=0" "COMPUTE_RECALL=$COMPUTE_RECALL"
    "QUERY_CONTEXTS=$query_contexts" "TIMEOUT_S=$TIMEOUT_S" "PORT=$PORT"
    "EVIDENCE_TOOL=$EVIDENCE_TOOL" "REPO_ROOT=$REPO_ROOT"
  )
  if [[ -n "$variant_env" ]]; then
    env "${variant_env_args[@]}" "${run_env[@]}" "$RUNNER"
  else
    env "${run_env[@]}" "$RUNNER"
  fi
  verify_harness
  python3 "$EVIDENCE_TOOL" verify --root "$run_out" >/dev/null
  python3 - "$RUNS" "$run_out" "$rep" "$position" "$variant" "$label" \
    "$expected_sha" "$query_contexts" "$THREADS" "$COROUTINES" "$EF" \
    "$COMPUTE_RECALL" "$QUERY_TILE" "$variant_env" \
    "$CAPTURE_BUILD_METRICS" "$REQUIRE_QUERY_INVARIANTS" "$DATASET" "$METHOD" \
    "$expected_build_mode" "$expected_build_workers" "$COMPUTE_HOST" <<'PY'
import csv, hashlib, json, math, re, sys
from pathlib import Path

PHYSICAL_HASH_VERSION = 2
PHYSICAL_HASH_ALGORITHM = "fnv1a64"
PHYSICAL_HASH_SCOPE = "field_scoped_physical_artifacts"
PHYSICAL_HASH_SCOPES = {
    "header_hash_scope": "replicated_header_source_bytes",
    "descriptor_hash_scope": "descriptor_slice_of_replicated_header",
    "map_hash_scope": "global_budget_map_source_bytes",
    "offset_table_hash_scope": "per_mn_offset_table_source_bytes",
    "record_payload_hash_scope": "per_mn_record_payload_source_bytes",
    "selected_uid_hash_scope": "global_selected_uid_u32le_sequence",
}
PHYSICAL_BUDGET_MAP_OWNER_MN = 0

(runs_s, out_s, rep, position, variant, label, expected_sha, query_contexts,
 threads, coroutines, ef, compute_recall_s, query_tile_s,
 variant_env, capture_build_metrics_s, require_query_invariants_s,
 dataset, method, expected_build_mode, expected_build_workers_s,
 expected_compute_host) = sys.argv[1:]
compute_recall = bool(int(compute_recall_s))
capture_build_metrics = bool(int(capture_build_metrics_s))
require_query_invariants = bool(int(require_query_invariants_s))
query_tile = int(query_tile_s)
expected_build_workers = int(expected_build_workers_s)
out = Path(out_s)
campaign_path = out / "campaign.json"
result_path = out / f"{dataset}_{method}_T{threads}_C{coroutines}_ef{ef}.json"
provenance_path = result_path.with_suffix(".provenance.json")
if not campaign_path.is_file() or not result_path.is_file():
    raise SystemExit("A/B runner output is incomplete")
campaign = json.loads(campaign_path.read_text())
if campaign["protocol"]["binary_sha256"] != expected_sha:
    raise SystemExit("A/B runner binary SHA drift")
child_compute_host = str(campaign["protocol"].get("compute_host", "")).strip()
if child_compute_host != expected_compute_host:
    raise SystemExit("A/B child campaign compute host drift")
if not provenance_path.is_file():
    raise SystemExit("A/B run is missing execution provenance")
provenance = json.loads(provenance_path.read_text())
if provenance.get("schema_version") != 1:
    raise SystemExit("A/B execution provenance schema is unsupported")
if provenance.get("dataset") != dataset or provenance.get("method") != method:
    raise SystemExit("A/B execution provenance identity drift")
campaign_binding = provenance.get("campaign", {})
for field in ("campaign_id", "campaign_uuid", "protocol_fingerprint"):
    if campaign_binding.get(field) != campaign.get(field):
        raise SystemExit(f"A/B execution provenance campaign binding drift: {field}")

sha_re = re.compile(r"[0-9a-f]{64}")
def require_sha(value, label):
    value = str(value)
    if sha_re.fullmatch(value) is None:
        raise SystemExit(f"A/B {label} is not a SHA-256 digest")
    return value

executables = provenance.get("executables", {})
compute_node = executables.get("compute_node", {})
compute_host = str(compute_node.get("host", "")).strip()
if not compute_host:
    raise SystemExit("A/B execution provenance is missing compute host")
if compute_host != expected_compute_host:
    raise SystemExit("A/B execution provenance compute host drift")
cn_binary_sha = require_sha(compute_node.get("sha256", ""),
                               "compute-node binary SHA")
if cn_binary_sha != expected_sha:
    raise SystemExit("A/B compute-node binary SHA drift")
memory_nodes = executables.get("memory_nodes", [])
if not isinstance(memory_nodes, list) or not memory_nodes:
    raise SystemExit("A/B execution provenance has no memory node")
mn_binary_shas = {
    require_sha(node.get("sha256", ""), "memory-node binary SHA")
    for node in memory_nodes
}
if mn_binary_shas != {expected_sha}:
    raise SystemExit("A/B memory-node binary SHA drift")
mn_binary_sha = next(iter(mn_binary_shas))

inputs = provenance.get("inputs")
if not isinstance(inputs, dict):
    raise SystemExit("A/B execution provenance has no input manifest")
computed_input_signature = hashlib.sha256(
    json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
input_signature = require_sha(provenance.get("input_signature", ""),
                              "input signature")
if input_signature != computed_input_signature:
    raise SystemExit("A/B execution provenance input signature is invalid")
checks = provenance.get("input_verification", {})
if set(checks) != {"pre_run", "post_run"}:
    raise SystemExit("A/B execution provenance input verification is incomplete")
expected_checks = {
    "query_sha256": inputs["query"]["sha256"],
    "ground_truth_sha256": (
        inputs["ground_truth"]["sha256"] if inputs.get("ground_truth") else None
    ),
    "index_sha256": inputs["index"][0]["sha256"],
}
if checks["pre_run"] != expected_checks or checks["post_run"] != expected_checks:
    raise SystemExit("A/B execution provenance pre/post inputs drifted")

artifacts = provenance.get("artifacts", {})
required_artifacts = {
    "compute_stdout", "compute_stderr",
    "memory_node_stdout", "memory_node_stderr",
}
if set(artifacts) != required_artifacts:
    raise SystemExit("A/B execution provenance has incomplete run artifacts")
resolved_out = out.resolve()
for label_name, artifact in artifacts.items():
    relative = Path(str(artifact.get("path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"A/B provenance artifact path escapes run root: {label_name}")
    artifact_path = (out / relative).resolve()
    if artifact_path.parent != resolved_out or not artifact_path.is_file():
        raise SystemExit(f"A/B provenance artifact is missing: {label_name}")
    expected_artifact_sha = require_sha(
        artifact.get("sha256", ""), f"{label_name} artifact SHA"
    )
    actual_artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    if expected_artifact_sha != actual_artifact_sha:
        raise SystemExit(f"A/B provenance artifact SHA drift: {label_name}")
provenance_sha = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
obj = json.loads(result_path.read_text())
queries = obj["queries"]
processed = int(queries["processed"])
expected_queries = 10_000 if compute_recall else 10_000 * query_tile
if processed != int(obj["num_queries"]) or processed != expected_queries:
    raise SystemExit("A/B fixed query pool is incomplete")
qps = float(queries["queries_per_sec"])
recall = float(queries["recall"])
if not math.isfinite(qps) or qps <= 0:
    raise SystemExit("A/B result requires finite QPS")
if not math.isfinite(recall) or (compute_recall and not 0.0 < recall <= 1.0):
    raise SystemExit("A/B result has invalid QPS or recall")
if not compute_recall and recall != 0.0:
    raise SystemExit("no-recall A/B result must not report recall")
result_hash = int(queries.get("local_result_hash", 0))
if require_query_invariants and result_hash == 0:
    raise SystemExit("A/B result is missing the final query-result hash")
result_hash_version = int(queries.get("local_result_hash_version", 0))
if result_hash_version <= 0:
    raise SystemExit("A/B result is missing the query-result hash version")

build = {
    "build_mode": "",
    "build_workers": 0,
    "rank_workers": 0,
    "rank_workers_recorded": 0,
    "staging_bytes": 0,
    "record_write_posts": 0,
    "selection_hash": 0,
    "descriptor_version": 0,
    "physical_signature": "",
    "budget_map_required": 0,
    "physical_bytes": 0,
    "build_total_ms": 0.0,
    "build_rank_ms": 0.0,
    "build_materialize_ms": 0.0,
    "build_assemble_ms": 0.0,
    "build_publish_ms": 0.0,
}
if capture_build_metrics:
    stderr_path = result_path.with_suffix(".err")
    if not stderr_path.is_file():
        raise SystemExit("A/B build capture is missing stderr")
    lines = stderr_path.read_text().splitlines()
    def payloads(prefix):
        return [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
    policies = payloads("LAVD_MATERIALIZATION_POLICY ")
    publications = payloads("LAVD_BUILD_PUBLICATION ")
    physical = payloads("LAVD_PHYSICAL_ACCOUNTING ")
    if len(policies) != 1 or len(publications) != 1 or not physical:
        raise SystemExit("A/B build capture has incomplete machine records")
    policy = policies[0]
    publication = publications[0]
    rank_workers_recorded = int("rank_workers" in policy)
    rank_workers = int(policy.get("rank_workers", 0))
    if rank_workers_recorded != 1 or rank_workers <= 0:
        raise SystemExit("A/B build capture is missing rank-worker provenance")
    if "budget_map_required" not in policy:
        raise SystemExit("A/B build capture is missing budget-map provenance")
    required_physical = {
        "descriptor_version", "mn", "num_mns", "max_record_bytes",
        "max_degree", "colocated_degree", "slot_only",
        "budget_map_required", "record_layout", "scoring_code", "scoring_bits",
        "header_bytes", "budget_map_bytes", "placement_padding_bytes",
        "offset_table_bytes", "record_bytes", "materialized_bytes",
        "actual_write_bytes",
        "hash_version", "hash_algorithm", "hash_scope",
        *PHYSICAL_HASH_SCOPES,
        "budget_map_owner_mn",
        "header_hash", "descriptor_hash", "map_hash", "offset_table_hash",
        "record_payload_hash", "selected_uid_hash",
    }
    for shard in physical:
        missing = sorted(required_physical - set(shard))
        if missing:
            raise SystemExit(
                "A/B physical accounting is missing fields: " + ",".join(missing)
            )
    physical = sorted(physical, key=lambda shard: int(shard["mn"]))
    num_mns_values = {int(shard["num_mns"]) for shard in physical}
    mn_ids = [int(shard["mn"]) for shard in physical]
    if len(num_mns_values) != 1 or mn_ids != list(range(len(physical))):
        raise SystemExit("A/B physical accounting has an invalid MN set")
    if num_mns_values != {len(physical)}:
        raise SystemExit("A/B physical accounting MN count does not close")
    descriptor_versions = {int(shard["descriptor_version"]) for shard in physical}
    if descriptor_versions != {3}:
        raise SystemExit("A/B build capture requires descriptor version 3")
    if {int(shard["hash_version"]) for shard in physical} != {
        PHYSICAL_HASH_VERSION
    }:
        raise SystemExit(
            f"A/B physical accounting requires hash version {PHYSICAL_HASH_VERSION}"
        )
    if {str(shard["hash_algorithm"]) for shard in physical} != {
        PHYSICAL_HASH_ALGORITHM
    }:
        raise SystemExit(
            f"A/B physical accounting requires {PHYSICAL_HASH_ALGORITHM}"
        )
    if {str(shard["hash_scope"]) for shard in physical} != {PHYSICAL_HASH_SCOPE}:
        raise SystemExit("A/B physical accounting hash scope drift")
    for field, expected in PHYSICAL_HASH_SCOPES.items():
        if {str(shard[field]) for shard in physical} != {expected}:
            raise SystemExit(f"A/B physical accounting {field} drift")
    if {int(shard["budget_map_owner_mn"]) for shard in physical} != {
        PHYSICAL_BUDGET_MAP_OWNER_MN
    }:
        raise SystemExit("A/B physical accounting budget_map_owner_mn drift")
    for shard in physical:
        for field in (
            "header_hash", "descriptor_hash", "map_hash", "offset_table_hash",
            "record_payload_hash", "selected_uid_hash",
        ):
            if re.fullmatch(r"[0-9a-f]{16}", str(shard[field])) is None:
                raise SystemExit(
                    f"A/B physical accounting has invalid {field}"
                )
    for replicated_field in (
        "header_hash", "descriptor_hash", "map_hash", "selected_uid_hash",
    ):
        if len({str(shard[replicated_field]) for shard in physical}) != 1:
            raise SystemExit(
                f"A/B physical accounting has cross-MN {replicated_field} drift"
            )
    layout_abi = {
        (
            int(shard["max_record_bytes"]),
            int(shard["max_degree"]),
            int(shard["colocated_degree"]),
            bool(shard["slot_only"]),
            bool(shard["budget_map_required"]),
            str(shard["record_layout"]),
            str(shard["scoring_code"]),
            int(shard["scoring_bits"]),
        )
        for shard in physical
    }
    if len(layout_abi) != 1:
        raise SystemExit("A/B physical accounting has cross-MN ABI drift")
    budget_map_required = int(bool(policy["budget_map_required"]))
    if {int(bool(shard["budget_map_required"])) for shard in physical} != {
        budget_map_required
    }:
        raise SystemExit("A/B policy/physical budget-map state does not close")
    budget_map_bytes = [int(shard["budget_map_bytes"]) for shard in physical]
    if budget_map_required:
        if budget_map_bytes[PHYSICAL_BUDGET_MAP_OWNER_MN] <= 0 or any(
            value != 0
            for mn, value in enumerate(budget_map_bytes)
            if mn != PHYSICAL_BUDGET_MAP_OWNER_MN
        ):
            raise SystemExit(
                "A/B physical budget map is not confined to its declared owner MN"
            )
    elif any(value != 0 for value in budget_map_bytes):
        raise SystemExit("A/B physical budget map exists while disabled")
    physical_bytes = sum(int(shard["materialized_bytes"]) for shard in physical)
    if physical_bytes != int(policy["admitted_bytes"]):
        raise SystemExit("A/B build planner/physical bytes do not close")
    for shard in physical:
        resident_bytes = (
            int(shard["header_bytes"])
            + int(shard["budget_map_bytes"])
            + int(shard["placement_padding_bytes"])
            + int(shard["offset_table_bytes"])
            + int(shard["record_bytes"])
        )
        writer_bytes = (
            int(shard["header_bytes"])
            + int(shard["budget_map_bytes"])
            + int(shard["offset_table_bytes"])
            + int(shard["record_bytes"])
        )
        if int(shard["materialized_bytes"]) != resident_bytes:
            raise SystemExit("A/B physical resident-byte ledger does not close")
        if int(shard["actual_write_bytes"]) != writer_bytes:
            raise SystemExit("A/B physical writer-byte ledger does not close")
    if int(publication["records"]) != int(policy["selected_records"]):
        raise SystemExit("A/B build selection/publication count mismatch")
    publication_mode = str(publication["mode"])
    publication_workers = int(publication["workers"])
    if publication_mode != expected_build_mode:
        raise SystemExit(
            f"A/B expected build mode {expected_build_mode}, got {publication_mode}"
        )
    if publication_workers != expected_build_workers:
        raise SystemExit(
            "A/B expected build workers "
            f"{expected_build_workers}, got {publication_workers}"
        )
    physical_signature = hashlib.sha256(
        json.dumps(physical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    timings = obj.get("timings", {})
    for name in (
        "lavd_build_multi", "lavd_build_rank", "lavd_build_materialize",
        "lavd_build_record_assemble", "lavd_build_record_publish",
    ):
        value = float(timings.get(name, 0.0))
        if not math.isfinite(value) or value < 0.0:
            raise SystemExit(f"A/B build timing is invalid: {name}")
    build = {
        "build_mode": publication_mode,
        "build_workers": publication_workers,
        "rank_workers": rank_workers,
        "rank_workers_recorded": rank_workers_recorded,
        "staging_bytes": int(publication["staging_bytes"]),
        "record_write_posts": int(publication["record_write_posts"]),
        "selection_hash": int(policy["selection_hash"]),
        "descriptor_version": descriptor_versions.pop(),
        "physical_signature": physical_signature,
        "budget_map_required": budget_map_required,
        "physical_bytes": physical_bytes,
        "build_total_ms": float(timings["lavd_build_multi"]),
        "build_rank_ms": float(timings.get("lavd_build_rank", 0.0)),
        "build_materialize_ms": float(timings["lavd_build_materialize"]),
        "build_assemble_ms": float(timings.get("lavd_build_record_assemble", 0.0)),
        "build_publish_ms": float(timings.get("lavd_build_record_publish", 0.0)),
    }
row = {
    "repeat": rep,
    "dataset": dataset,
    "position": position,
    "variant": variant,
    "label": label,
    "compute_host": compute_host,
    "binary_sha256": expected_sha,
    "mn_binary_sha256": mn_binary_sha,
    "input_signature": input_signature,
    "result_hash_version": result_hash_version,
    "execution_provenance": str(provenance_path),
    "execution_provenance_sha256": provenance_sha,
    "variant_env": variant_env,
    "query_contexts": int(query_contexts),
    "compute_recall": int(compute_recall),
    "query_tile": query_tile,
    "qps": qps,
    "recall": recall,
    "result_hash": result_hash,
    "p50_us": float(queries.get("local_latency_p50_us", 0.0)),
    "p95_us": float(queries.get("local_latency_p95_us", 0.0)),
    "p99_us": float(queries.get("local_latency_p99_us", 0.0)),
    "sq8_prefix_rejections_per_query": (
        float(queries.get("local_sq8_prefix_rejections", 0.0)) / processed
    ),
    "posts_per_query": float(queries["rdma_posts"]) / processed,
    "bytes_per_query": float(queries["rdma_reads_in_bytes"]) / processed,
    "processed": processed,
    **build,
    "json": str(result_path),
    "campaign": str(campaign_path),
    "status": "ok",
}
with open(runs_s, "a", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(row))
    writer.writerow(row)
PY
  python3 "$EVIDENCE_TOOL" verify --root "$run_out" >/dev/null
}

for ((rep = 0; rep < REPEATS; ++rep)); do
  if ((rep % 2 == 0)); then
    run_one "$rep" 0 A
    run_one "$rep" 1 B
  else
    run_one "$rep" 0 B
    run_one "$rep" 1 A
  fi
done

python3 - "$RUNS" "$OUT_ROOT/summary.csv" "$OUT_ROOT/comparison.json" \
  "$REPEATS" "$REQUIRE_QUERY_INVARIANTS" <<'PY'
import csv, json, math, statistics, sys

runs_path, summary_path, comparison_path, repeats_s, require_invariants_s = sys.argv[1:]
repeats = int(repeats_s)
require_invariants = bool(int(require_invariants_s))
rows = list(csv.DictReader(open(runs_path)))
if len(rows) != 2 * repeats or any(row["status"] != "ok" for row in rows):
    raise SystemExit("incomplete A/B run matrix")
datasets = {row["dataset"] for row in rows}
if len(datasets) != 1:
    raise SystemExit("A/B dataset identity drift")
dataset = datasets.pop()
compute_hosts = {row["compute_host"] for row in rows if row["compute_host"]}
if compute_hosts != {rows[0]["compute_host"]} or len(compute_hosts) != 1:
    raise SystemExit("A/B compute host changed within campaign")
compute_host = next(iter(compute_hosts))
for repeat in range(repeats):
    cells = [row for row in rows if int(row["repeat"]) == repeat]
    expected = {"A": 0, "B": 1} if repeat % 2 == 0 else {"B": 0, "A": 1}
    observed = {row["variant"]: int(row["position"]) for row in cells}
    if len(cells) != 2 or observed != expected:
        raise SystemExit(f"repeat {repeat} violates the declared A/B position schedule")
tcrit = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
         6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}
metrics = (
    "qps", "recall", "p50_us", "p95_us", "p99_us",
    "sq8_prefix_rejections_per_query", "posts_per_query", "bytes_per_query",
    "record_write_posts", "physical_bytes", "build_total_ms",
    "build_rank_ms", "build_materialize_ms", "build_assemble_ms",
    "build_publish_ms",
)
summary = []
for variant in ("A", "B"):
    cell = [row for row in rows if row["variant"] == variant]
    if len(cell) != repeats or sorted(int(row["repeat"]) for row in cell) != list(range(repeats)):
        raise SystemExit(f"incomplete variant {variant}")
    static_fields = (
        "compute_host", "binary_sha256", "mn_binary_sha256", "input_signature",
        "result_hash_version", "build_mode", "build_workers", "rank_workers",
        "rank_workers_recorded", "staging_bytes", "selection_hash",
        "descriptor_version", "physical_signature", "budget_map_required",
        "result_hash",
    )
    for field in static_fields:
        if len({row[field] for row in cell}) != 1:
            raise SystemExit(f"{field} drift within variant {variant}")
    record = {
        "dataset": dataset,
        "variant": variant,
        "label": cell[0]["label"],
        "n": len(cell),
        **{field: cell[0][field] for field in static_fields},
    }
    for metric in metrics:
        values = [float(row[metric]) for row in cell]
        if any(not math.isfinite(value) for value in values):
            raise SystemExit(f"non-finite A/B metric: {metric}")
        mean = statistics.mean(values)
        ci = "" if len(values) < 2 else tcrit.get(len(values)-1, 1.96) * statistics.stdev(values) / math.sqrt(len(values))
        record[f"{metric}_mean"] = mean
        record[f"{metric}_ci95"] = ci
    summary.append(record)
with open(summary_path, "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(summary[0]))
    writer.writeheader(); writer.writerows(summary)
means = {row["variant"]: row for row in summary}
paired = []
for repeat in range(repeats):
    cell = {row["variant"]: row for row in rows if int(row["repeat"]) == repeat}
    if set(cell) != {"A", "B"}:
        raise SystemExit(f"repeat {repeat} is not a complete A/B pair")
    build_capture = bool(
        cell["A"]["physical_signature"] or cell["B"]["physical_signature"]
    )
    if build_capture:
        if not cell["A"]["physical_signature"] or not cell["B"]["physical_signature"]:
            raise SystemExit("A/B build capture is incomplete across variants")
        if cell["A"]["selection_hash"] != cell["B"]["selection_hash"]:
            raise SystemExit("A/B build selection hash changed")
        if cell["A"]["physical_bytes"] != cell["B"]["physical_bytes"]:
            raise SystemExit("A/B build physical bytes changed")
        if cell["A"]["physical_signature"] != cell["B"]["physical_signature"]:
            raise SystemExit("A/B build physical layout signature changed")
        for field, label in (
            ("rank_workers", "rank-worker count"),
            ("rank_workers_recorded", "rank-worker provenance"),
            ("descriptor_version", "descriptor version"),
            ("budget_map_required", "budget-map state"),
        ):
            if cell["A"][field] != cell["B"][field]:
                raise SystemExit(f"A/B build {label} changed")
    if cell["A"]["result_hash_version"] != cell["B"]["result_hash_version"]:
        raise SystemExit("A/B query-result hash version changed")
    if cell["A"]["compute_host"] != cell["B"]["compute_host"]:
        raise SystemExit("A/B compute host changed")
    if cell["A"]["input_signature"] != cell["B"]["input_signature"]:
        raise SystemExit("A/B input signature changed")
    if require_invariants:
        if cell["A"]["result_hash"] != cell["B"]["result_hash"]:
            raise SystemExit("A/B final query-result hash changed")
        for metric, label in (
            ("recall", "recall"),
            ("posts_per_query", "posts/query"),
            ("bytes_per_query", "bytes/query"),
        ):
            if float(cell["A"][metric]) != float(cell["B"][metric]):
                raise SystemExit(f"A/B query {label} changed")
    paired.append({
        "order": "AB" if repeat % 2 == 0 else "BA",
        "qps_delta": float(cell["B"]["qps"]) - float(cell["A"]["qps"]),
        "qps_speedup": float(cell["B"]["qps"]) / float(cell["A"]["qps"]),
        "recall_delta": float(cell["B"]["recall"]) - float(cell["A"]["recall"]),
        "posts_delta": float(cell["B"]["posts_per_query"]) - float(cell["A"]["posts_per_query"]),
        "bytes_delta": float(cell["B"]["bytes_per_query"]) - float(cell["A"]["bytes_per_query"]),
        "p99_us_delta": float(cell["B"]["p99_us"]) - float(cell["A"]["p99_us"]),
        "sq8_prefix_rejections_per_query_delta": (
            float(cell["B"]["sq8_prefix_rejections_per_query"])
            - float(cell["A"]["sq8_prefix_rejections_per_query"])
        ),
        "build_speedup": (
            float(cell["A"]["build_total_ms"]) / float(cell["B"]["build_total_ms"])
            if float(cell["B"]["build_total_ms"]) > 0 else 0.0
        ),
        "rank_speedup": (
            float(cell["A"]["build_rank_ms"])
            / float(cell["B"]["build_rank_ms"])
            if float(cell["B"]["build_rank_ms"]) > 0 else 0.0
        ),
        "materialize_speedup": (
            float(cell["A"]["build_materialize_ms"])
            / float(cell["B"]["build_materialize_ms"])
            if float(cell["B"]["build_materialize_ms"]) > 0 else 0.0
        ),
    })

def paired_summary(metric, cells=paired):
    values = [row[metric] for row in cells]
    if not values:
        return None, None
    mean = statistics.mean(values)
    ci = None if len(values) < 2 else tcrit.get(len(values)-1, 1.96) * statistics.stdev(values) / math.sqrt(len(values))
    return mean, ci

qps_delta_mean, qps_delta_ci = paired_summary("qps_delta")
qps_speedup_mean, qps_speedup_ci = paired_summary("qps_speedup")
recall_delta_mean, recall_delta_ci = paired_summary("recall_delta")
posts_delta_mean, posts_delta_ci = paired_summary("posts_delta")
bytes_delta_mean, bytes_delta_ci = paired_summary("bytes_delta")
p99_delta_mean, p99_delta_ci = paired_summary("p99_us_delta")
rejections_delta_mean, rejections_delta_ci = paired_summary(
    "sq8_prefix_rejections_per_query_delta"
)
build_speedup_mean, build_speedup_ci = paired_summary("build_speedup")
rank_speedup_mean, rank_speedup_ci = paired_summary("rank_speedup")
materialize_speedup_mean, materialize_speedup_ci = paired_summary(
    "materialize_speedup"
)

metric_names = {
    "qps_delta": "qps_delta_B_minus_A",
    "qps_speedup": "qps_speedup_B_over_A",
    "recall_delta": "recall_delta_B_minus_A",
    "posts_delta": "posts_per_query_delta_B_minus_A",
    "bytes_delta": "bytes_per_query_delta_B_minus_A",
    "p99_us_delta": "p99_us_delta_B_minus_A",
    "sq8_prefix_rejections_per_query_delta": (
        "sq8_prefix_rejections_per_query_delta_B_minus_A"
    ),
    "build_speedup": "build_speedup_A_over_B",
    "rank_speedup": "rank_speedup_A_over_B",
    "materialize_speedup": "materialize_speedup_A_over_B",
}
order_stratified = {}
for order in ("AB", "BA"):
    cells = [row for row in paired if row["order"] == order]
    record = {"n": len(cells)}
    for metric, public_name in metric_names.items():
        mean, ci = paired_summary(metric, cells)
        record[f"{public_name}_mean"] = mean
        record[f"{public_name}_ci95"] = ci
    order_stratified[order] = record
comparison = {
    "dataset": dataset,
    "compute_host": compute_host,
    "qps_speedup_B_over_A": means["B"]["qps_mean"] / means["A"]["qps_mean"],
    "recall_delta_B_minus_A": means["B"]["recall_mean"] - means["A"]["recall_mean"],
    "posts_per_query_delta_B_minus_A": means["B"]["posts_per_query_mean"] - means["A"]["posts_per_query_mean"],
    "bytes_per_query_delta_B_minus_A": means["B"]["bytes_per_query_mean"] - means["A"]["bytes_per_query_mean"],
    "p99_us_delta_B_minus_A": means["B"]["p99_us_mean"] - means["A"]["p99_us_mean"],
    "sq8_prefix_rejections_per_query_delta_B_minus_A": (
        means["B"]["sq8_prefix_rejections_per_query_mean"]
        - means["A"]["sq8_prefix_rejections_per_query_mean"]
    ),
    "paired_repeats": len(paired),
    "order_stratified": order_stratified,
    "paired_qps_delta_B_minus_A_mean": qps_delta_mean,
    "paired_qps_delta_B_minus_A_ci95": qps_delta_ci,
    "paired_qps_speedup_B_over_A_mean": qps_speedup_mean,
    "paired_qps_speedup_B_over_A_ci95": qps_speedup_ci,
    "paired_recall_delta_B_minus_A_mean": recall_delta_mean,
    "paired_recall_delta_B_minus_A_ci95": recall_delta_ci,
    "paired_posts_per_query_delta_B_minus_A_mean": posts_delta_mean,
    "paired_posts_per_query_delta_B_minus_A_ci95": posts_delta_ci,
    "paired_bytes_per_query_delta_B_minus_A_mean": bytes_delta_mean,
    "paired_bytes_per_query_delta_B_minus_A_ci95": bytes_delta_ci,
    "paired_p99_us_delta_B_minus_A_mean": p99_delta_mean,
    "paired_p99_us_delta_B_minus_A_ci95": p99_delta_ci,
    "paired_sq8_prefix_rejections_per_query_delta_B_minus_A_mean": rejections_delta_mean,
    "paired_sq8_prefix_rejections_per_query_delta_B_minus_A_ci95": rejections_delta_ci,
    "paired_build_speedup_A_over_B_mean": build_speedup_mean,
    "paired_build_speedup_A_over_B_ci95": build_speedup_ci,
    "paired_rank_speedup_A_over_B_mean": rank_speedup_mean,
    "paired_rank_speedup_A_over_B_ci95": rank_speedup_ci,
    "paired_materialize_speedup_A_over_B_mean": materialize_speedup_mean,
    "paired_materialize_speedup_A_over_B_ci95": materialize_speedup_ci,
    "record_write_posts_B_over_A": (
        means["B"]["record_write_posts_mean"]
        / means["A"]["record_write_posts_mean"]
        if means["A"]["record_write_posts_mean"] > 0 else 0.0
    ),
}
json.dump(comparison, open(comparison_path, "w"), indent=2, sort_keys=True)
PY

verify_harness
python3 - "$OUT_ROOT" "$RUNS" <<'PY'
import csv
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
rows = list(csv.DictReader(open(sys.argv[2])))
for row in rows:
    provenance = pathlib.Path(row["execution_provenance"]).resolve()
    campaign = pathlib.Path(row["campaign"]).resolve()
    result = pathlib.Path(row["json"]).resolve()
    child = campaign.parent
    for path in (provenance, campaign, result):
        try:
            path.relative_to(root)
        except ValueError as error:
            raise SystemExit(f"A/B closure path escapes campaign: {path}") from error
        if not path.is_file():
            raise SystemExit(f"A/B closure artifact is missing: {path}")
    digest = hashlib.sha256(provenance.read_bytes()).hexdigest()
    if digest != row["execution_provenance_sha256"]:
        raise SystemExit(f"A/B execution provenance drifted before closure: {provenance}")
    if not (child / "SEALED.json").is_file() or not (child / "SHA256SUMS").is_file():
        raise SystemExit(f"A/B child evidence is not sealed: {child}")
PY
for run_out in "$OUT_ROOT"/r[0-9]*_[01]_[AB]; do
  [[ -d "$run_out" ]] || { echo "Missing A/B child directory: $run_out" >&2; exit 1; }
  python3 "$EVIDENCE_TOOL" verify --root "$run_out" >/dev/null
done
verify_harness
python3 "$EVIDENCE_TOOL" seal --root "$OUT_ROOT" \
  --campaign "$OUT_ROOT/campaign.json" >/dev/null
python3 "$EVIDENCE_TOOL" verify --root "$OUT_ROOT" >/dev/null
[[ -s "$OUT_ROOT/SHA256SUMS" ]] || { echo "Missing SHA256SUMS" >&2; exit 1; }
[[ -s "$OUT_ROOT/SEALED.json" ]] || { echo "Missing SEALED.json" >&2; exit 1; }

echo "Sealed binary A/B campaign written to $OUT_ROOT"
