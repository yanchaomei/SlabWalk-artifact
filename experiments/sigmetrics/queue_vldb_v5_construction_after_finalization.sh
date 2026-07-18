#!/usr/bin/env bash
# Evaluate the pre-registered construction-only fallback after finalization.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

FINALIZER_SESSION=${FINALIZER_SESSION:-v5_finalize_after_ab}
FINALIZER_UNIT=${FINALIZER_UNIT:-}
FINALIZATION_ROOT=${FINALIZATION_ROOT:?set FINALIZATION_ROOT}
FINALIZATION_COMPLETE=${FINALIZATION_COMPLETE:-$FINALIZATION_ROOT/FINALIZATION_COMPLETE.json}
PROMOTION_GATE=${PROMOTION_GATE:-$FINALIZATION_ROOT/promotion_gate.json}
BASELINE_FRONTIER=${BASELINE_FRONTIER:?set BASELINE_FRONTIER to the certified raw frontier}
CONTROL_ROOT=${CONTROL_ROOT:?set CONTROL_ROOT to a fresh decision directory}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to a fresh construction-evidence directory}
TOOLING_DIR=${TOOLING_DIR:?set TOOLING_DIR to frozen fallback tools}
GB_BIN=${GB_BIN:-/home/kvgroup/chaomei/bin/slabwalk-v5-snapshot-reuse-3a0dc5d8091a}
GB_BIN_R=${GB_BIN_R:-$GB_BIN}
SHA_B=${SHA_B:-3a0dc5d8091aef223feb5f57eb498a8b60510fef5630baf9594ee8511465b94d}
SOURCE_ROOT=${SOURCE_ROOT:-/home/kvgroup/chaomei/source-snapshots/v5-f6587d5818d03bcd}
SOURCE_TREE_B=${SOURCE_TREE_B:-f6587d5818d03bcddff4cb94be56bcc54108012494a49b11173399c5477eec4c}
WAIT_SECONDS=${WAIT_SECONDS:-30}
PORT=${PORT:-18301}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-v5-build-cost-construction-$(date -u +%Y%m%dT%H%M%SZ)}

FRONTIER_CELLS="$FINALIZATION_ROOT/frontier_comparison/cells.csv"
CANDIDATE_FRONTIER="$FINALIZATION_ROOT/frontier_1m_candidate/frontier_repeated_raw.csv"
CONSTRUCTION_GATE="$CONTROL_ROOT/construction_candidate_gate.json"
DECISION="$CONTROL_ROOT/CONSTRUCTION_FALLBACK_COMPLETE.json"
BUILD_COMPLETE="$OUT_ROOT/BUILD_COST_COMPLETE.json"

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "construction fallback queue must run on skv-node1" >&2
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
[[ "$SHA_B" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid candidate SHA" >&2; exit 2; }
[[ "$SOURCE_TREE_B" =~ ^[0-9a-f]{64}$ ]] || {
  echo "invalid candidate source-tree SHA" >&2
  exit 2
}
[[ -d "$SOURCE_ROOT" ]] || { echo "missing candidate source snapshot" >&2; exit 2; }
[[ -s "$BASELINE_FRONTIER" ]] || { echo "missing certified raw frontier" >&2; exit 2; }
[[ -d "$TOOLING_DIR" && ! -w "$TOOLING_DIR" ]] || {
  echo "fallback tooling must be an existing read-only directory" >&2
  exit 2
}
for path in \
  "$TOOLING_DIR/wait_for_stage_marker.sh" \
  "$TOOLING_DIR/validate_vldb_construction_candidate.py" \
  "$TOOLING_DIR/run_vldb_v5_build_after_construction_admission.sh" \
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
  [[ -s "$path" ]] || { echo "missing frozen fallback tool: $path" >&2; exit 2; }
done
[[ -x "$GB_BIN" ]] || { echo "missing candidate binary" >&2; exit 2; }
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == "$SHA_B" ]] || {
  echo "candidate binary SHA drift" >&2
  exit 2
}
[[ ! -e "$CONTROL_ROOT" ]] || { echo "refusing existing CONTROL_ROOT" >&2; exit 2; }
[[ ! -e "$OUT_ROOT" ]] || { echo "refusing existing OUT_ROOT" >&2; exit 2; }

source "$TOOLING_DIR/wait_for_stage_marker.sh"
wait_for_stage_marker "$FINALIZATION_COMPLETE" "$FINALIZER_UNIT" \
  "$FINALIZER_SESSION" "$WAIT_SECONDS" finalizer

[[ -s "$PROMOTION_GATE" ]] || {
  echo "finalizer marker exists without a promotion report" >&2
  exit 2
}
finalization_state=$(python3 - "$FINALIZATION_COMPLETE" "$PROMOTION_GATE" "$SHA_B" <<'PY'
import hashlib
import json
import sys

marker_path, promotion_path, expected_sha = sys.argv[1:]
marker = json.load(open(marker_path))
promotion = json.load(open(promotion_path))
if marker.get("kind") != "vldb_finalization_complete_v1":
    raise SystemExit("unsupported finalization marker")
if promotion.get("kind") != "vldb_candidate_promotion_gate_v1":
    raise SystemExit("unsupported promotion report")
if marker.get("candidate_binary_sha256") != expected_sha:
    raise SystemExit("finalization candidate SHA drift")
promotion_sha = hashlib.sha256(open(promotion_path, "rb").read()).hexdigest()
if marker.get("promotion_report_sha256") != promotion_sha:
    raise SystemExit("finalization promotion report drift")
marker_ready = marker.get("promotion_ready")
report_ready = promotion.get("promotion_ready")
status = marker.get("promotion_status")
if status == 0 and marker_ready is True and report_ready is True:
    print("general_ready")
elif status == 2 and marker_ready is False and report_ready is False:
    print("general_rejected")
else:
    raise SystemExit("inconsistent finalization decision")
PY
)

[[ ! -e "$CONTROL_ROOT" && ! -e "$OUT_ROOT" ]] || {
  echo "fallback output appeared while waiting" >&2
  exit 2
}
mkdir -p "$CONTROL_ROOT"

write_decision() {
  local status="$1" exit_status="$2"
  python3 - "$DECISION" "$status" "$exit_status" "$FINALIZATION_COMPLETE" \
    "$PROMOTION_GATE" "$CONSTRUCTION_GATE" "$BUILD_COMPLETE" "$SHA_B" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

(destination_s, status, exit_status, finalization_s, promotion_s, gate_s,
 build_s, binary_sha) = sys.argv[1:]
destination = Path(destination_s)
finalization = Path(finalization_s)
promotion = Path(promotion_s)
gate = Path(gate_s)
build = Path(build_s)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

payload = {
    "schema_version": 1,
    "kind": "vldb_construction_fallback_complete_v1",
    "status": status,
    "exit_status": int(exit_status),
    "scope": "construction_measurements_only",
    "general_promotion_ready": status == "not_needed_general_promotion",
    "candidate_binary_sha256": binary_sha,
    "finalization_marker_sha256": digest(finalization),
    "promotion_report_sha256": digest(promotion),
}
if gate.is_file():
    gate_payload = json.loads(gate.read_text())
    payload["construction_ready"] = gate_payload.get("construction_ready")
    payload["construction_gate_sha256"] = digest(gate)
if build.is_file():
    build_payload = json.loads(build.read_text())
    if (
        build_payload.get("kind") != "vldb_v5_build_cost_complete_v1"
        or build_payload.get("scope") != "construction_measurements_only"
        or build_payload.get("candidate_binary_sha256") != binary_sha
    ):
        raise SystemExit("construction completion marker contract mismatch")
    payload["build_completion_sha256"] = digest(build)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=destination.parent, delete=False
) as handle:
    temporary = Path(handle.name)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, destination)
PY
}

if [[ "$finalization_state" == "general_ready" ]]; then
  status="not_needed_general_promotion"
  write_decision "$status" 0
  echo "construction fallback not needed: general promotion passed"
  exit 0
fi

for path in "$FRONTIER_CELLS" "$CANDIDATE_FRONTIER"; do
  [[ -s "$path" ]] || { echo "missing finalizer output: $path" >&2; exit 2; }
done
set +e
python3 "$TOOLING_DIR/validate_vldb_construction_candidate.py" \
  --promotion-report "$PROMOTION_GATE" \
  --frontier-cells "$FRONTIER_CELLS" \
  --candidate-frontier "$CANDIDATE_FRONTIER" \
  --baseline-frontier "$BASELINE_FRONTIER" \
  --allowed-dataset GIST1M --allowed-method SlabWalk --allowed-ef 100 \
  --out "$CONSTRUCTION_GATE"
admission_rc=$?
set -e
if (( admission_rc != 0 && admission_rc != 2 )); then
  echo "construction admission crashed with status $admission_rc" >&2
  exit "$admission_rc"
fi
[[ -s "$CONSTRUCTION_GATE" ]] || {
  echo "construction admission returned without a gate" >&2
  exit 2
}
construction_ready=$(python3 - "$CONSTRUCTION_GATE" <<'PY'
import json, sys
gate = json.load(open(sys.argv[1]))
if (
    gate.get("kind") != "vldb_construction_candidate_gate_v1"
    or gate.get("general_promotion_ready") is not False
    or gate.get("scope") != "construction_measurements_only"
):
    raise SystemExit("construction gate contract mismatch")
print("true" if gate.get("construction_ready") is True else "false")
PY
)
if (( admission_rc == 2 )) || [[ "$construction_ready" != "true" ]]; then
  if (( admission_rc != 2 )) || [[ "$construction_ready" != "false" ]]; then
    echo "construction admission status and gate disagree" >&2
    exit 2
  fi
  status="construction_not_admitted"
  write_decision "$status" 2
  echo "construction fallback was measured and rejected" >&2
  exit 2
fi

gate_sha=$(sha256sum "$CONSTRUCTION_GATE" | awk '{print $1}')
CONSTRUCTION_GATE="$CONSTRUCTION_GATE" \
PROMOTION_GATE="$PROMOTION_GATE" \
EXPECTED_CONSTRUCTION_GATE_SHA="$gate_sha" \
OUT_ROOT="$OUT_ROOT" \
TOOLING_DIR="$TOOLING_DIR" \
GB_BIN="$GB_BIN" GB_BIN_R="$GB_BIN_R" \
SHA_B="$SHA_B" SOURCE_ROOT="$SOURCE_ROOT" SOURCE_TREE_B="$SOURCE_TREE_B" \
PORT="$PORT" CAMPAIGN_ID="$CAMPAIGN_ID" \
ADMISSION_SCOPE="construction_measurements_only" \
bash "$TOOLING_DIR/run_vldb_v5_build_after_construction_admission.sh"

[[ -s "$BUILD_COMPLETE" ]] || {
  echo "construction runner ended without its completion marker" >&2
  exit 2
}
status="construction_measurements_complete"
write_decision "$status" 0
echo "construction-only fallback completed: $DECISION"
