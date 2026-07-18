#!/usr/bin/env bash
# Wait for the v5 frontier, verify every measured child, then run same-host A/Bs.
set -euo pipefail

FRONTIER_SESSION=${FRONTIER_SESSION:-v5_frontier_1m}
FRONTIER_UNIT=${FRONTIER_UNIT:-}
FRONTIER_ROOT=${FRONTIER_ROOT:?set FRONTIER_ROOT}
POST_ROOT=${POST_ROOT:?set POST_ROOT to a fresh output parent}
TOOLING_DIR=${TOOLING_DIR:?set TOOLING_DIR to the frozen experiment scripts}
SOURCE_ROOT_A=${SOURCE_ROOT_A:-/home/kvgroup/chaomei/graphbeyond-vldb-closure-20260713}
SOURCE_ROOT_B=${SOURCE_ROOT_B:-/home/kvgroup/chaomei/graphbeyond-c1-vldb-v5-snapshot-reuse-20260717T043305Z}
BIN_A=${BIN_A:-/home/kvgroup/chaomei/bin/slabwalk-baseline}
BIN_B=${BIN_B:-/home/kvgroup/chaomei/bin/slabwalk-v5-snapshot-reuse-3a0dc5d8091a}
SHA_A=${SHA_A:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
SHA_B=${SHA_B:-3a0dc5d8091aef223feb5f57eb498a8b60510fef5630baf9594ee8511465b94d}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-v5-frontier-1m-20260717}
EXPECTED_DATASETS=${EXPECTED_DATASETS:-SIFT1M,GIST1M,DEEP1M,BIGANN1M,SPACEV1M,TURING1M,TEXT1M}
WAIT_SECONDS=${WAIT_SECONDS:-60}
FRONTIER_COMPLETE="$FRONTIER_ROOT/SW_FRONTIER_COMPLETE.json"
AB_COMPLETE="$POST_ROOT/AB_COMPLETE.json"

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "post-frontier A/B queue must run on skv-node1" >&2
  exit 2
}
[[ "$SHA_A" =~ ^[0-9a-f]{64}$ && "$SHA_B" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid binary SHA" >&2
  exit 2
}
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "WAIT_SECONDS must be positive" >&2
  exit 2
}
for path in \
  "$TOOLING_DIR/vldb_evidence_bundle.py" \
  "$TOOLING_DIR/verify_vldb_frontier_sweep.py" \
  "$TOOLING_DIR/run_vldb_binary_ab.sh" \
  "$TOOLING_DIR/run_vldb_query_profile.sh" \
  "$TOOLING_DIR/verify_vldb_binary_ab.py" \
  "$TOOLING_DIR/summarize_vldb_materialization_policy.py" \
  "$TOOLING_DIR/wait_for_stage_marker.sh"; do
  [[ -s "$path" ]] || { echo "missing frozen tool: $path" >&2; exit 2; }
done
for path in "$FRONTIER_ROOT" "$SOURCE_ROOT_A" "$SOURCE_ROOT_B"; do
  [[ -d "$path" ]] || { echo "missing evidence/source root: $path" >&2; exit 2; }
done
for path in "$BIN_A" "$BIN_B"; do
  [[ -x "$path" ]] || { echo "missing binary: $path" >&2; exit 2; }
done
[[ "$(sha256sum "$BIN_A" | awk '{print $1}')" == "$SHA_A" ]] || {
  echo "baseline binary SHA mismatch" >&2; exit 2;
}
[[ "$(sha256sum "$BIN_B" | awk '{print $1}')" == "$SHA_B" ]] || {
  echo "candidate binary SHA mismatch" >&2; exit 2;
}
[[ ! -e "$POST_ROOT" ]] || { echo "refusing existing POST_ROOT" >&2; exit 2; }

source "$TOOLING_DIR/wait_for_stage_marker.sh"
wait_for_stage_marker "$FRONTIER_COMPLETE" "$FRONTIER_UNIT" \
  "$FRONTIER_SESSION" "$WAIT_SECONDS" frontier
python3 - "$FRONTIER_COMPLETE" "$FRONTIER_ROOT" "$CAMPAIGN_ID" "$SHA_B" \
  "$EXPECTED_DATASETS" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

marker_path, root_s, campaign_id, binary_sha, datasets_s = sys.argv[1:]
marker = json.load(open(marker_path))
expected_protocol = {
    "datasets": datasets_s.split(","),
    "repeats": 5,
    "workers": 10,
    "query_contexts": 10,
    "coroutines": 2,
    "top_k": 10,
}
if marker.get("kind") != "vldb_sw_frontier_complete_v1":
    raise SystemExit("unsupported frontier completion marker")
if marker.get("campaign_id") != campaign_id:
    raise SystemExit("frontier completion campaign mismatch")
if marker.get("binary_sha256") != binary_sha:
    raise SystemExit("frontier completion binary mismatch")
if marker.get("protocol") != expected_protocol:
    raise SystemExit("frontier completion protocol mismatch")
children = marker.get("children")
if not isinstance(children, list) or len(children) != 5:
    raise SystemExit("frontier completion child set is incomplete")
root = Path(root_s)
for repeat, record in enumerate(children, 1):
    child = root / f"sw_r{repeat}"
    expected = {
        "run_id": f"r{repeat}",
        "manifest_sha256": hashlib.sha256(
            (child / "SHA256SUMS").read_bytes()
        ).hexdigest(),
        "seal_sha256": hashlib.sha256(
            (child / "SEALED.json").read_bytes()
        ).hexdigest(),
        "semantic_verification_sha256": hashlib.sha256(
            (child / "semantic_verification.json").read_bytes()
        ).hexdigest(),
    }
    if record != expected:
        raise SystemExit(f"frontier completion child r{repeat} drift")
PY

for repeat in 1 2 3 4 5; do
  child="$FRONTIER_ROOT/sw_r$repeat"
  python3 "$TOOLING_DIR/vldb_evidence_bundle.py" verify --root "$child" >/dev/null
  python3 "$TOOLING_DIR/verify_vldb_frontier_sweep.py" \
    --root "$child" \
    --expected-binary-sha "$SHA_B" \
    --expected-campaign-id "$CAMPAIGN_ID" \
    --expected-run-id "r$repeat" \
    --expected-run-kind measure \
    --expected-datasets "$EXPECTED_DATASETS" \
    --expected-threads 10 \
    --expected-query-contexts 10 \
    --expected-coroutines 2 \
    --expected-trace 0 \
    --min-points 5 >/dev/null
done

install_remote_binary() {
  local host=$1 path=$2 expected_sha=$3 temporary
  local observed
  observed=$(ssh -o BatchMode=yes "$host" \
    "test -f '$path' && sha256sum '$path' | awk '{print \$1}'" || true)
  if [[ "$observed" == "$expected_sha" ]]; then
    return 0
  fi
  [[ -z "$observed" ]] || {
    echo "$host has a conflicting binary at $path" >&2
    return 2
  }
  temporary="$path.install.$$"
  scp -q "$path" "$host:$temporary"
  ssh -o BatchMode=yes "$host" \
    "test \"\$(sha256sum '$temporary' | awk '{print \$1}')\" = '$expected_sha'; chmod 755 '$temporary'; mv '$temporary' '$path'"
}

install_remote_binary skv-node3 "$BIN_A" "$SHA_A"
install_remote_binary skv-node5 "$BIN_A" "$SHA_A"
for host in skv-node3 skv-node5; do
  observed=$(ssh -o BatchMode=yes "$host" \
    "sha256sum '$BIN_B' | awk '{print \$1}'")
  [[ "$observed" == "$SHA_B" ]] || {
    echo "$host candidate binary SHA mismatch" >&2
    exit 2
  }
done

mkdir -p "$POST_ROOT"

run_ab() {
  local method=$1 dataset=$2 ef=$3 port=$4 out=$5
  local verification="$POST_ROOT/$(basename "$out").verification.json"
  if ! env \
    REPO_ROOT="$SOURCE_ROOT_B" \
    BINARY_AB_SOURCE_SCRIPT_DIR="$TOOLING_DIR" \
    EVIDENCE_TOOL="$TOOLING_DIR/vldb_evidence_bundle.py" \
    RUNNER="$TOOLING_DIR/run_vldb_query_profile.sh" \
    BIN_A="$BIN_A" BIN_B="$BIN_B" \
    SOURCE_ROOT_A="$SOURCE_ROOT_A" SOURCE_ROOT_B="$SOURCE_ROOT_B" \
    LABEL_A=certified-2e60 LABEL_B=v5-snapshot-reuse \
    METHOD="$method" DATASET="$dataset" THREADS=10 \
    QUERY_CONTEXTS_A=10 QUERY_CONTEXTS_B=10 COROUTINES=2 EF="$ef" TOP_K=10 \
    REPEATS=6 CAMPAIGN_KIND=formal COMPUTE_RECALL=1 QUERY_TILE=1 \
    CAPTURE_PERF=0 CAPTURE_BUILD_METRICS=0 REQUIRE_QUERY_INVARIANTS=1 \
    PORT="$port" TIMEOUT_S=7200 OUT_ROOT="$out" \
    bash "$TOOLING_DIR/run_vldb_binary_ab.sh"; then
    echo "$method $dataset A/B failed" >&2
    return 1
  fi
  if ! python3 "$TOOLING_DIR/verify_vldb_binary_ab.py" \
    --root "$out" \
    --expected-sha-a "$SHA_A" \
    --expected-sha-b "$SHA_B" \
    --expected-compute-host skv-node1 > "$verification.tmp"; then
    rm -f "$verification.tmp"
    echo "$method $dataset independent verification failed" >&2
    return 1
  fi
  mv "$verification.tmp" "$verification"
  if ! python3 "$TOOLING_DIR/vldb_evidence_bundle.py" verify --root "$out" >/dev/null; then
    echo "$method $dataset sealed bundle changed after verification" >&2
    return 1
  fi
}

status=0
run_ab slabwalk GIST1M 100 18100 "$POST_ROOT/slabwalk_gist1m" || status=1
run_ab shine DEEP1M 100 18101 "$POST_ROOT/shine_deep1m" || status=1
if [[ "$status" != "0" ]]; then
  exit "$status"
fi

python3 - "$AB_COMPLETE" "$CAMPAIGN_ID" "$SHA_A" "$SHA_B" \
  "$POST_ROOT/slabwalk_gist1m" "$POST_ROOT/slabwalk_gist1m.verification.json" \
  "$POST_ROOT/shine_deep1m" "$POST_ROOT/shine_deep1m.verification.json" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

(marker_s, campaign_id, sha_a, sha_b, slab_root_s, slab_verification_s,
 shine_root_s, shine_verification_s) = sys.argv[1:]
marker = Path(marker_s)


def control(root_s, verification_s):
    root = Path(root_s)
    verification = Path(verification_s)
    if not root.is_dir() or not verification.is_file():
        raise SystemExit(f"missing completed A/B control: {root}")
    return {
        "root": root.name,
        "manifest_sha256": hashlib.sha256(
            (root / "SHA256SUMS").read_bytes()
        ).hexdigest(),
        "verification": verification.name,
        "verification_sha256": hashlib.sha256(
            verification.read_bytes()
        ).hexdigest(),
    }


payload = {
    "schema_version": 1,
    "kind": "vldb_binary_ab_complete_v1",
    "campaign_id": campaign_id,
    "baseline_binary_sha256": sha_a,
    "candidate_binary_sha256": sha_b,
    "controls": {
        "slabwalk": control(slab_root_s, slab_verification_s),
        "shine": control(shine_root_s, shine_verification_s),
    },
}
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=marker.parent, delete=False
) as handle:
    temporary = Path(handle.name)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, marker)
PY

echo "v5 binary A/B stage completed: $AB_COMPLETE"
