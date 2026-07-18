#!/usr/bin/env bash
# Run the v5 fixed-layout build matrix only after the candidate promotion gate.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

FINALIZER_SESSION=${FINALIZER_SESSION:-v5_finalize_after_ab}
FINALIZER_UNIT=${FINALIZER_UNIT:-}
PROMOTION_GATE=${PROMOTION_GATE:?set PROMOTION_GATE to the finalizer report}
FINALIZATION_COMPLETE=${FINALIZATION_COMPLETE:?set FINALIZATION_COMPLETE to the finalizer marker}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to a fresh build-evidence parent}
TOOLING_DIR=${TOOLING_DIR:?set TOOLING_DIR to frozen build tools}
GB_BIN=${GB_BIN:-/home/kvgroup/chaomei/bin/slabwalk-v5-snapshot-reuse-3a0dc5d8091a}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
GB_LIB=${GB_LIB:-/home/kvgroup/chaomei/lib}
BUILD_MN=${BUILD_MN:-skv-node4}
SHA_B=${SHA_B:-3a0dc5d8091aef223feb5f57eb498a8b60510fef5630baf9594ee8511465b94d}
SOURCE_ROOT=${SOURCE_ROOT:-/home/kvgroup/chaomei/source-snapshots/v5-f6587d5818d03bcd}
SOURCE_TREE_B=${SOURCE_TREE_B:-f6587d5818d03bcddff4cb94be56bcc54108012494a49b11173399c5477eec4c}
WAIT_SECONDS=${WAIT_SECONDS:-30}
PORT=${PORT:-18300}

RAW="$OUT_ROOT/build_cost_raw"
BUNDLE="$OUT_ROOT/build_cost_candidate"
VALIDATION="$OUT_ROOT/build_cost_candidate.validation.json"

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "v5 build queue must run on skv-node1" >&2
  exit 2
}
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "WAIT_SECONDS must be positive" >&2
  exit 2
}
[[ "$PORT" =~ ^[1-9][0-9]*$ ]] && (( PORT <= 65535 )) || {
  echo "PORT must be in 1..65535" >&2
  exit 2
}
[[ "$SHA_B" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid candidate SHA" >&2
  exit 2
}
[[ "$SOURCE_TREE_B" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid candidate source-tree SHA" >&2
  exit 2
}
[[ -d "$SOURCE_ROOT" ]] || { echo "missing candidate source snapshot" >&2; exit 2; }
[[ -d "$TOOLING_DIR" ]] || {
  echo "build tooling directory is missing" >&2
  exit 2
}
[[ ! -w "$TOOLING_DIR" ]] || {
  echo "build tooling must be a read-only directory" >&2
  exit 2
}
for path in \
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
  "$TOOLING_DIR/publication_metadata.py" \
  "$TOOLING_DIR/wait_for_stage_marker.sh"; do
  [[ -s "$path" ]] || { echo "missing frozen build tool: $path" >&2; exit 2; }
done
[[ -x "$GB_BIN" ]] || { echo "missing candidate binary: $GB_BIN" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$SHA_B" ]] || {
  echo "candidate binary SHA drift" >&2
  exit 2
}
[[ ! -e "$OUT_ROOT" ]] || { echo "refusing existing OUT_ROOT" >&2; exit 2; }

source "$TOOLING_DIR/wait_for_stage_marker.sh"
wait_for_stage_marker "$FINALIZATION_COMPLETE" "$FINALIZER_UNIT" \
  "$FINALIZER_SESSION" "$WAIT_SECONDS" finalizer
[[ -s "$PROMOTION_GATE" ]] || {
  echo "finalizer ended without a promotion report" >&2
  exit 2
}
python3 - "$FINALIZATION_COMPLETE" "$PROMOTION_GATE" "$SHA_B" <<'PY'
import hashlib
import json
import sys

marker = json.load(open(sys.argv[1]))
promotion_path = sys.argv[2]
report = json.load(open(promotion_path))
expected_sha = sys.argv[3]
if marker.get("kind") != "vldb_finalization_complete_v1":
    raise SystemExit("unsupported finalization completion marker")
if marker.get("promotion_status") != 0:
    raise SystemExit("finalization did not produce a promotable candidate")
if marker.get("promotion_ready") is not True:
    raise SystemExit("finalization marker rejects the candidate")
if marker.get("candidate_binary_sha256") != expected_sha:
    raise SystemExit("finalization marker candidate SHA drift")
promotion_sha = hashlib.sha256(open(promotion_path, "rb").read()).hexdigest()
if marker.get("promotion_report_sha256") != promotion_sha:
    raise SystemExit("finalization promotion report drift")
if report.get("kind") != "vldb_candidate_promotion_gate_v1":
    raise SystemExit("unsupported promotion report")
if report.get("promotion_ready") is not True:
    raise SystemExit("candidate did not pass the promotion gate")
binary_ab = report.get("binary_ab")
if not isinstance(binary_ab, dict) or set(binary_ab) != {"slabwalk", "shine"}:
    raise SystemExit("promotion report lacks both binary A/B controls")
for method in ("slabwalk", "shine"):
    record = binary_ab[method]
    verification = record.get("verification", {})
    if record.get("ready") is not True:
        raise SystemExit(f"{method} A/B did not pass")
    if verification.get("binary_sha_b") != expected_sha:
        raise SystemExit(f"{method} A/B candidate SHA drift")
PY

[[ ! -e "$OUT_ROOT" ]] || { echo "OUT_ROOT appeared while waiting" >&2; exit 2; }
mkdir -p "$OUT_ROOT"

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
CAMPAIGN_ID="vldb-v5-build-cost-1m-20260717" \
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
  --out-dir "$BUNDLE"

python3 - "$TOOLING_DIR" "$BUNDLE" "$SHA_B" "$SOURCE_TREE_B" \
  "$VALIDATION" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

tooling = pathlib.Path(sys.argv[1])
bundle = pathlib.Path(sys.argv[2])
expected_sha = sys.argv[3]
expected_source_tree_sha = sys.argv[4]
output = pathlib.Path(sys.argv[5])
sys.path.insert(0, str(tooling))
import validate_vldb_final_evidence as validator

report = validator.validate_build_cost(
    bundle,
    expected_sha,
    expected_source_tree_sha=expected_source_tree_sha,
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

echo "v5 build-cost candidate validated: $BUNDLE"
