#!/usr/bin/env bash
# Reverify the v5 campaign and A/Bs, then produce a fail-closed promotion report.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

AB_SESSION=${AB_SESSION:-v5_ab_after_frontier}
AB_UNIT=${AB_UNIT:-}
FRONTIER_ROOT=${FRONTIER_ROOT:?set FRONTIER_ROOT}
AB_ROOT=${AB_ROOT:?set AB_ROOT}
CERTIFIED_FRONTIER=${CERTIFIED_FRONTIER:?set CERTIFIED_FRONTIER}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to a fresh output parent}
TOOLING_DIR=${TOOLING_DIR:?set TOOLING_DIR to the frozen finalizer scripts}
SHA_A=${SHA_A:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
SHA_B=${SHA_B:-3a0dc5d8091aef223feb5f57eb498a8b60510fef5630baf9594ee8511465b94d}
SOURCE_TREE_A=${SOURCE_TREE_A:-274fe7782a376a8a3c1b8400bd9a898ab6d35d7631d2a1d20eacda6196fdc86c}
SOURCE_TREE_B=${SOURCE_TREE_B:-f6587d5818d03bcddff4cb94be56bcc54108012494a49b11173399c5477eec4c}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-v5-frontier-1m-20260717}
EXPECTED_DATASETS=${EXPECTED_DATASETS:-SIFT1M,GIST1M,DEEP1M,BIGANN1M,SPACEV1M,TURING1M,TEXT1M}
CERTIFIED_MANIFEST_SHA=${CERTIFIED_MANIFEST_SHA:-96ba4ce9e3112453d07545ac759dff4fdbb5942fbe0e7494521f217e07048518}
CERTIFIED_CAMPAIGN_SHA=${CERTIFIED_CAMPAIGN_SHA:-b070d994791cff9f2caf52b75bed71f5c0eb8696ddd324336bd4c4a978fb8d0e}
CERTIFIED_RAW_SHA=${CERTIFIED_RAW_SHA:-900db2951721d469edaae1cb1ab466cb77210369e9a7fe29b0a4f1981ba33427}
WAIT_SECONDS=${WAIT_SECONDS:-30}

CANDIDATE="$OUT_ROOT/frontier_1m_candidate"
VALIDATION="$OUT_ROOT/frontier_1m_candidate.validation.json"
COMPARISON="$OUT_ROOT/frontier_comparison"
PROMOTION="$OUT_ROOT/promotion_gate.json"
AB_COMPLETE="$AB_ROOT/AB_COMPLETE.json"
FINALIZATION_COMPLETE="$OUT_ROOT/FINALIZATION_COMPLETE.json"

[[ "$(hostname)" == "skv-node1" ]] || {
  echo "v5 finalizer must run on skv-node1" >&2
  exit 2
}
[[ "$WAIT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
  echo "WAIT_SECONDS must be positive" >&2
  exit 2
}
for digest in \
  "$SHA_A" "$SHA_B" "$SOURCE_TREE_A" "$SOURCE_TREE_B" \
  "$CERTIFIED_MANIFEST_SHA" "$CERTIFIED_CAMPAIGN_SHA" "$CERTIFIED_RAW_SHA"; do
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid expected SHA" >&2; exit 2; }
done
for path in \
  "$TOOLING_DIR/vldb_evidence_bundle.py" \
  "$TOOLING_DIR/verify_vldb_frontier_sweep.py" \
  "$TOOLING_DIR/verify_vldb_binary_ab.py" \
  "$TOOLING_DIR/summarize_vldb_materialization_policy.py" \
  "$TOOLING_DIR/assemble_vldb_frontier_1m.py" \
  "$TOOLING_DIR/aggregate_frontier_repeats.py" \
  "$TOOLING_DIR/validate_vldb_frontier_1m.py" \
  "$TOOLING_DIR/compare_vldb_frontier_candidate.py" \
  "$TOOLING_DIR/validate_vldb_candidate_promotion.py" \
  "$TOOLING_DIR/publication_metadata.py" \
  "$TOOLING_DIR/wait_for_stage_marker.sh"; do
  [[ -s "$path" ]] || { echo "missing frozen finalizer tool: $path" >&2; exit 2; }
done
for path in "$FRONTIER_ROOT" "$CERTIFIED_FRONTIER"; do
  [[ -d "$path" ]] || { echo "missing finalizer input: $path" >&2; exit 2; }
done
[[ ! -e "$OUT_ROOT" ]] || { echo "refusing existing OUT_ROOT" >&2; exit 2; }
[[ "$(sha256sum "$CERTIFIED_FRONTIER/SHA256SUMS" | awk '{print $1}')" == "$CERTIFIED_MANIFEST_SHA" ]] || {
  echo "certified frontier SHA256SUMS drift" >&2; exit 2;
}
[[ "$(sha256sum "$CERTIFIED_FRONTIER/campaign.json" | awk '{print $1}')" == "$CERTIFIED_CAMPAIGN_SHA" ]] || {
  echo "certified frontier campaign drift" >&2; exit 2;
}
[[ "$(sha256sum "$CERTIFIED_FRONTIER/frontier_repeated_raw.csv" | awk '{print $1}')" == "$CERTIFIED_RAW_SHA" ]] || {
  echo "certified frontier raw evidence drift" >&2; exit 2;
}

source "$TOOLING_DIR/wait_for_stage_marker.sh"
wait_for_stage_marker "$AB_COMPLETE" "$AB_UNIT" \
  "$AB_SESSION" "$WAIT_SECONDS" "A/B"
python3 - "$AB_COMPLETE" "$AB_ROOT" "$CAMPAIGN_ID" "$SHA_A" "$SHA_B" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

marker_s, root_s, campaign_id, sha_a, sha_b = sys.argv[1:]
marker = json.load(open(marker_s))
if marker.get("kind") != "vldb_binary_ab_complete_v1":
    raise SystemExit("unsupported A/B completion marker")
if marker.get("campaign_id") != campaign_id:
    raise SystemExit("A/B completion campaign mismatch")
if marker.get("baseline_binary_sha256") != sha_a:
    raise SystemExit("A/B completion baseline mismatch")
if marker.get("candidate_binary_sha256") != sha_b:
    raise SystemExit("A/B completion candidate mismatch")
controls = marker.get("controls")
if not isinstance(controls, dict) or set(controls) != {"slabwalk", "shine"}:
    raise SystemExit("A/B completion control set is incomplete")
root = Path(root_s)
expected_names = {
    "slabwalk": ("slabwalk_gist1m", "slabwalk_gist1m.verification.json"),
    "shine": ("shine_deep1m", "shine_deep1m.verification.json"),
}
for method, (bundle_name, verification_name) in expected_names.items():
    bundle = root / bundle_name
    verification = root / verification_name
    expected = {
        "root": bundle_name,
        "manifest_sha256": hashlib.sha256(
            (bundle / "SHA256SUMS").read_bytes()
        ).hexdigest(),
        "verification": verification_name,
        "verification_sha256": hashlib.sha256(
            verification.read_bytes()
        ).hexdigest(),
    }
    if controls.get(method) != expected:
        raise SystemExit(f"A/B completion {method} control drift")
PY

[[ ! -e "$OUT_ROOT" ]] || { echo "OUT_ROOT appeared while waiting" >&2; exit 2; }
mkdir -p "$OUT_ROOT"

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

verify_ab() {
  local method=$1 dataset=$2 name=$3
  local ab_root="$AB_ROOT/$name"
  local report="$OUT_ROOT/${name}.verification.json"
  [[ -d "$ab_root" ]] || { echo "missing $method A/B root" >&2; return 1; }
  python3 "$TOOLING_DIR/vldb_evidence_bundle.py" verify --root "$ab_root" >/dev/null
  if ! python3 "$TOOLING_DIR/verify_vldb_binary_ab.py" \
    --root "$ab_root" \
    --expected-sha-a "$SHA_A" \
    --expected-sha-b "$SHA_B" \
    --expected-compute-host skv-node1 > "$report.tmp"; then
    rm -f "$report.tmp"
    return 1
  fi
  mv "$report.tmp" "$report"
  python3 - "$report" "$method" "$dataset" "$SOURCE_TREE_A" "$SOURCE_TREE_B" <<'PY'
import json, sys
path, method, dataset, source_a, source_b = sys.argv[1:]
report = json.load(open(path))
if (
    report.get("method") != method
    or report.get("dataset") != dataset
    or report.get("source_tree_sha_a") != source_a
    or report.get("source_tree_sha_b") != source_b
    or int(report.get("paired_repeats", 0)) != 6
    or int(report.get("run_count", 0)) != 12
):
    raise SystemExit("A/B verification contract mismatch")
PY
}

verify_ab slabwalk GIST1M slabwalk_gist1m
verify_ab shine DEEP1M shine_deep1m

python3 "$TOOLING_DIR/assemble_vldb_frontier_1m.py" \
  --sw-campaign "$FRONTIER_ROOT" \
  --dhnsw-campaign "$CERTIFIED_FRONTIER" \
  --query-pools "$CERTIFIED_FRONTIER/query_pools" \
  --aggregate-script "$TOOLING_DIR/aggregate_frontier_repeats.py" \
  --out-dir "$CANDIDATE"

python3 "$TOOLING_DIR/validate_vldb_frontier_1m.py" \
  --bundle "$CANDIDATE" \
  --expected-slabwalk-sha "$SHA_B" \
  --out "$VALIDATION"

set +e
python3 "$TOOLING_DIR/compare_vldb_frontier_candidate.py" \
  --baseline "$CERTIFIED_FRONTIER/frontier_repeated_raw.csv" \
  --candidate "$CANDIDATE/frontier_repeated_raw.csv" \
  --out-dir "$COMPARISON"
compare_rc=$?
set -e
if (( compare_rc != 0 && compare_rc != 2 )); then
  echo "frontier comparison crashed with status $compare_rc" >&2
  exit "$compare_rc"
fi
[[ -s "$COMPARISON/report.json" ]] || {
  echo "frontier comparison produced no report" >&2
  exit 2
}

set +e
python3 "$TOOLING_DIR/validate_vldb_candidate_promotion.py" \
  --frontier-comparison "$COMPARISON/report.json" \
  --slabwalk-ab "$AB_ROOT/slabwalk_gist1m" \
  --shine-ab "$AB_ROOT/shine_deep1m" \
  --expected-sha-a "$SHA_A" \
  --expected-sha-b "$SHA_B" \
  --expected-source-tree-a "$SOURCE_TREE_A" \
  --expected-source-tree-b "$SOURCE_TREE_B" \
  --expected-compute-host skv-node1 \
  --out "$PROMOTION"
promotion_rc=$?
set -e
if (( promotion_rc != 0 && promotion_rc != 2 )); then
  echo "candidate promotion gate crashed with status $promotion_rc" >&2
  exit "$promotion_rc"
fi
[[ -s "$PROMOTION" ]] || { echo "candidate promotion gate produced no report" >&2; exit 2; }
python3 - "$PROMOTION" "$promotion_rc" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
status = int(sys.argv[2])
if bool(report.get("promotion_ready")) != (status == 0):
    raise SystemExit("promotion report and exit status disagree")
PY
python3 - "$FINALIZATION_COMPLETE" "$PROMOTION" "$VALIDATION" \
  "$COMPARISON/report.json" "$AB_COMPLETE" "$SHA_A" "$SHA_B" \
  "$promotion_rc" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

(marker_s, promotion_s, validation_s, comparison_s, ab_complete_s, sha_a,
 sha_b, promotion_status_s) = sys.argv[1:]
marker = Path(marker_s)
promotion_path = Path(promotion_s)
promotion = json.loads(promotion_path.read_text())
promotion_status = int(promotion_status_s)
payload = {
    "schema_version": 1,
    "kind": "vldb_finalization_complete_v1",
    "baseline_binary_sha256": sha_a,
    "candidate_binary_sha256": sha_b,
    "promotion_status": promotion_status,
    "promotion_ready": promotion.get("promotion_ready"),
    "promotion_report_sha256": hashlib.sha256(
        promotion_path.read_bytes()
    ).hexdigest(),
    "frontier_validation_sha256": hashlib.sha256(
        Path(validation_s).read_bytes()
    ).hexdigest(),
    "frontier_comparison_sha256": hashlib.sha256(
        Path(comparison_s).read_bytes()
    ).hexdigest(),
    "ab_completion_sha256": hashlib.sha256(
        Path(ab_complete_s).read_bytes()
    ).hexdigest(),
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
echo "v5 finalization completed with promotion status $promotion_rc"
exit "$promotion_rc"
