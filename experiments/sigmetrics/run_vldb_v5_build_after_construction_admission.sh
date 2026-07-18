#!/usr/bin/env bash
# Run v5 construction measurements after the narrower construction-only gate.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

CONSTRUCTION_GATE=${CONSTRUCTION_GATE:?set CONSTRUCTION_GATE to the construction-only gate}
PROMOTION_GATE=${PROMOTION_GATE:?set PROMOTION_GATE to the original promotion report}
EXPECTED_CONSTRUCTION_GATE_SHA=${EXPECTED_CONSTRUCTION_GATE_SHA:?set the frozen construction-gate SHA}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to a fresh build-evidence parent}
TOOLING_DIR=${TOOLING_DIR:?set TOOLING_DIR to frozen build tools}
GB_BIN=${GB_BIN:-/home/kvgroup/chaomei/bin/slabwalk-v5-snapshot-reuse-3a0dc5d8091a}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
BUILD_MN=${BUILD_MN:-skv-node4}
SHA_B=${SHA_B:-3a0dc5d8091aef223feb5f57eb498a8b60510fef5630baf9594ee8511465b94d}
SOURCE_ROOT=${SOURCE_ROOT:-/home/kvgroup/chaomei/graphbeyond-c1-vldb-v5-snapshot-reuse-20260717T043305Z}
SOURCE_TREE_B=${SOURCE_TREE_B:-f6587d5818d03bcddff4cb94be56bcc54108012494a49b11173399c5477eec4c}
PORT=${PORT:-18300}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-v5-build-cost-1m-construction-$(date -u +%Y%m%dT%H%M%SZ)}

RAW="$OUT_ROOT/build_cost_raw"
BUNDLE="$OUT_ROOT/build_cost_candidate"
VALIDATION="$OUT_ROOT/build_cost_candidate.validation.json"
ADMISSION_VERIFICATION="$OUT_ROOT/admission_verification.json"
COMPLETE="$OUT_ROOT/BUILD_COST_COMPLETE.json"
TMP_DIR=""

cleanup() {
  [[ -z "$TMP_DIR" ]] || rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "v5 construction build must run on skv-node1" >&2
  exit 2
}
[[ "$EXPECTED_CONSTRUCTION_GATE_SHA" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid construction-gate SHA" >&2
  exit 2
}
[[ "$SHA_B" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid candidate SHA" >&2; exit 2; }
[[ "$SOURCE_TREE_B" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid candidate source-tree SHA" >&2
  exit 2
}
[[ "$PORT" =~ ^[1-9][0-9]*$ ]] && (( PORT <= 65535 )) || {
  echo "PORT must be in 1..65535" >&2
  exit 2
}
[[ -s "$CONSTRUCTION_GATE" ]] || { echo "missing construction gate" >&2; exit 2; }
[[ -s "$PROMOTION_GATE" ]] || { echo "missing promotion report" >&2; exit 2; }
[[ -d "$SOURCE_ROOT" ]] || { echo "missing candidate source snapshot" >&2; exit 2; }
[[ -d "$TOOLING_DIR" ]] || { echo "build tooling directory is missing" >&2; exit 2; }
[[ ! -w "$TOOLING_DIR" ]] || { echo "build tooling must be read-only" >&2; exit 2; }
for path in \
  "$TOOLING_DIR/verify_vldb_construction_admission.py" \
  "$TOOLING_DIR/run_slab_build_cost.sh" \
  "$TOOLING_DIR/summarize_slab_build_cost.py" \
  "$TOOLING_DIR/assemble_vldb_build_cost.py" \
  "$TOOLING_DIR/validate_vldb_final_evidence.py" \
  "$TOOLING_DIR/vldb_evidence_bundle.py" \
  "$TOOLING_DIR/aggregate_frontier_repeats.py" \
  "$TOOLING_DIR/assemble_vldb_10m_build_scaling.py" \
  "$TOOLING_DIR/assemble_vldb_query_profile.py" \
  "$TOOLING_DIR/assemble_vldb_lifecycle_controls.py" \
  "$TOOLING_DIR/summarize_vldb_cache_control.py" \
  "$TOOLING_DIR/summarize_vldb_colocation_control.py" \
  "$TOOLING_DIR/summarize_vldb_mechanism_controls.py" \
  "$TOOLING_DIR/summarize_vldb_resource_ledger.py" \
  "$TOOLING_DIR/publication_metadata.py"; do
  [[ -s "$path" ]] || { echo "missing frozen build tool: $path" >&2; exit 2; }
done
[[ -x "$GB_BIN" ]] || { echo "missing candidate binary: $GB_BIN" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$SHA_B" ]] || {
  echo "candidate binary SHA drift" >&2
  exit 2
}
[[ ! -e "$OUT_ROOT" ]] || { echo "refusing existing OUT_ROOT" >&2; exit 2; }

TMP_DIR=$(mktemp -d /tmp/vldb-v5-construction-admission.XXXXXX)
python3 "$TOOLING_DIR/verify_vldb_construction_admission.py" \
  --construction-gate "$CONSTRUCTION_GATE" \
  --promotion-report "$PROMOTION_GATE" \
  --expected-gate-sha "$EXPECTED_CONSTRUCTION_GATE_SHA" \
  --expected-sha-b "$SHA_B" \
  --expected-source-tree-b "$SOURCE_TREE_B" \
  --out "$TMP_DIR/admission_verification.json"

[[ ! -e "$OUT_ROOT" ]] || { echo "OUT_ROOT appeared during admission verification" >&2; exit 2; }
mkdir -p "$OUT_ROOT"
mv "$TMP_DIR/admission_verification.json" "$ADMISSION_VERIFICATION"

OUT="$RAW" \
DATASETS="SIFT1M DEEP1M GIST1M" \
REPEATS=5 \
GB_BIN="$GB_BIN" \
GB_BIN_R="$GB_BIN_R" \
GB_DATA="$GB_DATA" \
GB_LIB="$GB_LIB" \
BUILD_MN="$BUILD_MN" \
PORT="$PORT" \
EXPECTED_BINARY_SHA="$SHA_B" \
SOURCE_ROOT="$SOURCE_ROOT" \
EXPECTED_SOURCE_TREE_SHA="$SOURCE_TREE_B" \
ADMISSION_GATE="$CONSTRUCTION_GATE" \
EXPECTED_ADMISSION_GATE_SHA="$EXPECTED_CONSTRUCTION_GATE_SHA" \
ADMISSION_SCOPE="construction_measurements_only" \
CAMPAIGN_ID="$CAMPAIGN_ID" \
bash "$TOOLING_DIR/run_slab_build_cost.sh"

python3 "$TOOLING_DIR/vldb_evidence_bundle.py" seal \
  --root "$RAW" --campaign "$RAW/campaign.json" >/dev/null
python3 "$TOOLING_DIR/vldb_evidence_bundle.py" verify --root "$RAW" >/dev/null

python3 "$TOOLING_DIR/assemble_vldb_build_cost.py" \
  --sift-campaign "$RAW" \
  --deep-campaign "$RAW" \
  --gist-campaign "$RAW" \
  --expected-binary-sha "$SHA_B" \
  --expected-source-tree-sha "$SOURCE_TREE_B" \
  --expected-admission-gate-sha "$EXPECTED_CONSTRUCTION_GATE_SHA" \
  --expected-admission-scope "construction_measurements_only" \
  --out-dir "$BUNDLE"

python3 - "$TOOLING_DIR" "$BUNDLE" "$SHA_B" "$SOURCE_TREE_B" \
  "$EXPECTED_CONSTRUCTION_GATE_SHA" "$VALIDATION" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

tooling = pathlib.Path(sys.argv[1])
bundle = pathlib.Path(sys.argv[2])
expected_sha = sys.argv[3]
expected_source_tree_sha = sys.argv[4]
expected_admission_gate_sha = sys.argv[5]
output = pathlib.Path(sys.argv[6])
sys.path.insert(0, str(tooling))
import validate_vldb_final_evidence as validator

report = validator.validate_build_cost(
    bundle,
    expected_sha,
    expected_source_tree_sha=expected_source_tree_sha,
    expected_admission_gate_sha=expected_admission_gate_sha,
    expected_admission_scope="construction_measurements_only",
)
report["kind"] = "vldb_v5_build_cost_validation_v1"
report["binary_sha256"] = expected_sha
output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=output.parent, delete=False
) as handle:
    temporary = pathlib.Path(handle.name)
    json.dump(report, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, output)
PY

python3 - "$COMPLETE" "$RAW/campaign.json" "$ADMISSION_VERIFICATION" \
  "$VALIDATION" "$EXPECTED_CONSTRUCTION_GATE_SHA" "$SHA_B" "$SOURCE_TREE_B" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import tempfile

output, campaign, admission, validation, gate_sha, binary_sha, source_sha = sys.argv[1:]

def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

payload = {
    "kind": "vldb_v5_build_cost_complete_v1",
    "scope": "construction_measurements_only",
    "candidate_binary_sha256": binary_sha,
    "candidate_source_tree_sha256": source_sha,
    "construction_gate_sha256": gate_sha,
    "campaign_sha256": digest(campaign),
    "admission_verification_sha256": digest(admission),
    "validation_sha256": digest(validation),
}
destination = pathlib.Path(output)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=destination.parent, delete=False
) as handle:
    temporary = pathlib.Path(handle.name)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, destination)
PY

echo "v5 construction-only build-cost evidence complete: $BUNDLE"
