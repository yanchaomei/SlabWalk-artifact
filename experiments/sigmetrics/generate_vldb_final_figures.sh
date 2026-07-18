#!/usr/bin/env bash
# Validate, render, and atomically publish the final VLDB evidence-bound release.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
PUBLICATION_ROOT=${PUBLICATION_ROOT:-$REPO_ROOT}
PAPER_DIR=${PAPER_DIR:-$PUBLICATION_ROOT/paper_vldb}
EVIDENCE_ROOT=${EVIDENCE_ROOT:-$PUBLICATION_ROOT/results/vldb_final_evidence}
OUT_DIR=${OUT_DIR:-$PAPER_DIR/figs}
FINAL_SHA=${FINAL_SHA:-2e60c6ea3bb3893b66cbce65cfdc5d82faab4a74fcc469fdeb7b5ff8dda34fb6}
PROFILE_RUNNER_SHA=${PROFILE_RUNNER_SHA:-3d20f0968654a3ad27fa1b4624c5425e1844258a1a75cc905232ac78e68bcf6e}
EXPECTED_COLOCATION_CAMPAIGN_ID=${EXPECTED_COLOCATION_CAMPAIGN_ID:-vldb-colocation-deep1-final-v4-20260715}
EXPECTED_COLOCATION_PROTOCOL_FINGERPRINT=${EXPECTED_COLOCATION_PROTOCOL_FINGERPRINT:-d99de8f75c7153f0182b21a9804e050a45c8c4e60c04ceac37e7071afa4c8bb5}
EXPECTED_MECHANISM_CAMPAIGN_ID=${EXPECTED_MECHANISM_CAMPAIGN_ID:-vldb-mechanism-controls-final-v6-20260715}
EXPECTED_MECHANISM_PROTOCOL_FINGERPRINT=${EXPECTED_MECHANISM_PROTOCOL_FINGERPRINT:-d60f2d12f0f23c2bbccecb65db1a2fe074ba819ef11567887c6734768129e31a}
PHYSICAL_DESIGN_ADVISOR_ROOT=${PHYSICAL_DESIGN_ADVISOR_ROOT:-$EVIDENCE_ROOT/physical_design_advisor}
EXPECTED_ADVISOR_SOURCE_SHA=${EXPECTED_ADVISOR_SOURCE_SHA:-bf377c5ad52c743759777a38a0fe6d764b8aced6f81b528f80091621e61e8ac8}
EXPECTED_ADVISOR_COMPUTE_HOST=${EXPECTED_ADVISOR_COMPUTE_HOST:-skv-node3}
GATE=${GATE:-$EVIDENCE_ROOT/evidence_gate.json}
FRONTIER_1M_BUNDLE=${FRONTIER_1M_BUNDLE:-$EVIDENCE_ROOT/frontier_1m}
FRONTIER_1M_GATE=${FRONTIER_1M_GATE:-$EVIDENCE_ROOT/frontier_1m_gate.json}
HEADLINES=${HEADLINES:-$EVIDENCE_ROOT/headline_candidates.json}
CLAIMS=${CLAIMS:-$EVIDENCE_ROOT/manuscript_claims.json}
GENERATED_CLAIMS=${GENERATED_CLAIMS:-$PAPER_DIR/generated_claims.tex}
RELEASE_MANIFEST=${RELEASE_MANIFEST:-$EVIDENCE_ROOT/release_bundle.json}
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-946684800}
export SLABWALK_SVG_RENDERER=${SLABWALK_SVG_RENDERER:-auto}

PUBLICATION_ROOT=$(python3 - "$PUBLICATION_ROOT" <<'PY'
import sys
from pathlib import Path

print(Path(sys.argv[1]).resolve(strict=True))
PY
)

canonical_release_path() {
  python3 - "$PUBLICATION_ROOT" "$1" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve(strict=False)
try:
    relative = target.relative_to(root)
except ValueError as exc:
    raise SystemExit(f"release path is outside publication root: {target}") from exc
if not relative.parts:
    raise SystemExit(f"release path must not equal publication root: {target}")
print(target)
PY
}

PAPER_DIR=$(canonical_release_path "$PAPER_DIR")
EVIDENCE_ROOT=$(canonical_release_path "$EVIDENCE_ROOT")
OUT_DIR=$(canonical_release_path "$OUT_DIR")
GATE=$(canonical_release_path "$GATE")
FRONTIER_1M_BUNDLE=$(canonical_release_path "$FRONTIER_1M_BUNDLE")
FRONTIER_1M_GATE=$(canonical_release_path "$FRONTIER_1M_GATE")
HEADLINES=$(canonical_release_path "$HEADLINES")
CLAIMS=$(canonical_release_path "$CLAIMS")
GENERATED_CLAIMS=$(canonical_release_path "$GENERATED_CLAIMS")
RELEASE_MANIFEST=$(canonical_release_path "$RELEASE_MANIFEST")
PHYSICAL_DESIGN_ADVISOR_ROOT=$(canonical_release_path "$PHYSICAL_DESIGN_ADVISOR_ROOT")

STAGING=""
release_published=0
invalidate_marker() {
  local marker_parent
  marker_parent=$(dirname -- "$RELEASE_MANIFEST")
  rm -f -- "$RELEASE_MANIFEST"
  if [[ -d $marker_parent ]]; then
    python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$marker_parent"
  fi
}
cleanup() {
  local status=$?
  local final_status=$status
  trap - EXIT
  if [[ -n $STAGING ]]; then
    rm -rf "$STAGING"
  fi
  if (( status != 0 || release_published != 1 )); then
    if ! invalidate_marker; then
      final_status=1
    fi
  fi
  exit "$final_status"
}
cancel_release() {
  local status=$1
  trap - INT TERM
  exit "$status"
}
trap cleanup EXIT
trap 'cancel_release 130' INT
trap 'cancel_release 143' TERM

invalidate_marker
mkdir -p "$EVIDENCE_ROOT" "$OUT_DIR"

for variable in \
  EXPECTED_COLOCATION_PROTOCOL_FINGERPRINT \
  EXPECTED_MECHANISM_PROTOCOL_FINGERPRINT \
  EXPECTED_ADVISOR_SOURCE_SHA \
  EXPECTED_ADVISOR_COMPUTE_HOST; do
  [[ -n ${!variable} ]] || {
    echo "Required final campaign identity is unset: $variable" >&2
    exit 2
  }
done

STAGING=$(mktemp -d "$EVIDENCE_ROOT/.release-staging.XXXXXX")

STAGED_GATE_BASE="$STAGING/evidence_gate_base.json"
STAGED_GATE_FRONTIER_1M="$STAGING/evidence_gate_frontier_1m.json"
STAGED_GATE="$STAGING/evidence_gate.json"
STAGED_FRONTIER_1M_GATE="$STAGING/frontier_1m_gate.json"
STAGED_HEADLINES="$STAGING/headline_candidates.json"
STAGED_CLAIMS="$STAGING/manuscript_claims.json"
STAGED_GENERATED_CLAIMS="$STAGING/generated_claims.tex"

python3 "$SCRIPT_DIR/validate_vldb_final_evidence.py" \
  --frontier "$EVIDENCE_ROOT/frontier" \
  --robustness "$EVIDENCE_ROOT/robustness" \
  --worker-scaling "$EVIDENCE_ROOT/worker_scaling" \
  --topology-control "$EVIDENCE_ROOT/topology_control" \
  --build-cost "$EVIDENCE_ROOT/build_cost" \
  --build-scaling-10m "$EVIDENCE_ROOT/build_scaling_10m" \
  --index-construction "$EVIDENCE_ROOT/index_construction" \
  --lifecycle-controls "$EVIDENCE_ROOT/lifecycle_controls" \
  --cache-control "$EVIDENCE_ROOT/cache_control" \
  --colocation-control "$EVIDENCE_ROOT/colocation_control" \
  --mechanism-controls "$EVIDENCE_ROOT/mechanism_controls" \
  --query-profile "$EVIDENCE_ROOT/query_profile" \
  --resource-ledger "$EVIDENCE_ROOT/resource_ledger" \
  --model-controls "$EVIDENCE_ROOT/model_controls" \
  --query-pools "$EVIDENCE_ROOT/query_pools" \
  --expected-slabwalk-sha "$FINAL_SHA" \
  --expected-profile-runner-sha "$PROFILE_RUNNER_SHA" \
  --expected-colocation-campaign-id "$EXPECTED_COLOCATION_CAMPAIGN_ID" \
  --expected-colocation-protocol-fingerprint "$EXPECTED_COLOCATION_PROTOCOL_FINGERPRINT" \
  --expected-mechanism-campaign-id "$EXPECTED_MECHANISM_CAMPAIGN_ID" \
  --expected-mechanism-protocol-fingerprint "$EXPECTED_MECHANISM_PROTOCOL_FINGERPRINT" \
  --out "$STAGED_GATE_BASE"

python3 "$SCRIPT_DIR/validate_vldb_frontier_1m.py" \
  --bundle "$FRONTIER_1M_BUNDLE" \
  --expected-slabwalk-sha "$FINAL_SHA" \
  --out "$STAGED_FRONTIER_1M_GATE"

python3 "$SCRIPT_DIR/bind_vldb_frontier_1m_gate.py" \
  --main-gate "$STAGED_GATE_BASE" \
  --frontier-1m-gate "$STAGED_FRONTIER_1M_GATE" \
  --out "$STAGED_GATE_FRONTIER_1M"

python3 "$SCRIPT_DIR/bind_vldb_physical_design_advisor_gate.py" \
  --main-gate "$STAGED_GATE_FRONTIER_1M" \
  --source-bundle "$PHYSICAL_DESIGN_ADVISOR_ROOT/source" \
  --validation-bundle "$PHYSICAL_DESIGN_ADVISOR_ROOT/validation" \
  --expected-sha "$EXPECTED_ADVISOR_SOURCE_SHA" \
  --expected-compute-host "$EXPECTED_ADVISOR_COMPUTE_HOST" \
  --out "$STAGED_GATE"

python3 "$SCRIPT_DIR/summarize_vldb_headlines.py" \
  --summary "$EVIDENCE_ROOT/frontier/frontier_summary.csv" \
  --gate "$STAGED_GATE" \
  --out "$STAGED_HEADLINES"

python3 "$SCRIPT_DIR/assemble_vldb_manuscript_claims.py" \
  --gate "$STAGED_GATE" \
  --frontier-summary "$EVIDENCE_ROOT/frontier/frontier_summary.csv" \
  --headline "$STAGED_HEADLINES" \
  --cache-summary "$EVIDENCE_ROOT/cache_control/summary/summary.csv" \
  --colocation-summary "$EVIDENCE_ROOT/colocation_control/summary/summary.csv" \
  --budget-summary "$EVIDENCE_ROOT/mechanism_controls/summary/budget_summary.csv" \
  --resident-summary "$EVIDENCE_ROOT/mechanism_controls/summary/resident_summary.csv" \
  --profile-summary "$EVIDENCE_ROOT/query_profile/summary/summary.csv" \
  --resource-summary "$EVIDENCE_ROOT/resource_ledger/summary.csv" \
  --resource-runs "$EVIDENCE_ROOT/resource_ledger/runs.csv" \
  --worker-runs "$EVIDENCE_ROOT/worker_scaling/runs.csv" \
  --rdma-runs "$EVIDENCE_ROOT/model_controls/rdma_tau_runs.csv" \
  --robustness-runs "$EVIDENCE_ROOT/robustness/runs.csv" \
  --topology-summary "$EVIDENCE_ROOT/topology_control/summary.csv" \
  --lifecycle-refresh "$EVIDENCE_ROOT/lifecycle_controls/refresh.csv" \
  --lifecycle-tti "$EVIDENCE_ROOT/lifecycle_controls/tti.csv" \
  --build-summary "$EVIDENCE_ROOT/build_cost/summary.csv" \
  --build-scaling-10m-summary "$EVIDENCE_ROOT/build_scaling_10m/summary.csv" \
  --physical-design-advisor-report "$PHYSICAL_DESIGN_ADVISOR_ROOT/validation/report.json" \
  --out "$STAGED_CLAIMS"

python3 "$SCRIPT_DIR/render_vldb_claims_tex.py" \
  --claims "$STAGED_CLAIMS" \
  --out "$STAGED_GENERATED_CLAIMS"

python3 "$REPO_ROOT/paper_vldb/figs/gen_vldb_design_figures.py" \
  --only fig_physical_units \
  --cache-summary "$EVIDENCE_ROOT/cache_control/summary/summary.csv" \
  --profile-summary "$EVIDENCE_ROOT/query_profile/summary/summary.csv" \
  --svg-only \
  --output-dir "$STAGING"

if [[ -s "$OUT_DIR/fig_physical_units.svg" \
      && -s "$OUT_DIR/fig_physical_units.pdf" ]] \
    && cmp -s "$STAGING/fig_physical_units.svg" \
      "$OUT_DIR/fig_physical_units.svg"; then
  cp -- "$OUT_DIR/fig_physical_units.pdf" "$STAGING/fig_physical_units.pdf"
else
  python3 "$REPO_ROOT/paper_vldb/figs/gen_vldb_design_figures.py" \
    --only fig_physical_units \
    --cache-summary "$EVIDENCE_ROOT/cache_control/summary/summary.csv" \
    --profile-summary "$EVIDENCE_ROOT/query_profile/summary/summary.csv" \
    --output-dir "$STAGING"
fi

python3 "$SCRIPT_DIR/plot_vldb_frontier_all.py" \
  --summary-1m "$FRONTIER_1M_BUNDLE/frontier_summary.csv" \
  --gate-1m "$STAGED_FRONTIER_1M_GATE" \
  --summary-10m "$EVIDENCE_ROOT/frontier/frontier_summary.csv" \
  --gate-10m "$STAGED_GATE" \
  --out "$STAGING/eval_frontier_curves.pdf"

python3 "$SCRIPT_DIR/plot_vldb_robustness.py" \
  --runs "$EVIDENCE_ROOT/robustness/runs.csv" \
  --worker-runs "$EVIDENCE_ROOT/worker_scaling/runs.csv" \
  --rdma-runs "$EVIDENCE_ROOT/model_controls/rdma_tau_runs.csv" \
  --topology-runs "$EVIDENCE_ROOT/topology_control/runs.csv" \
  --colocation-runs "$EVIDENCE_ROOT/colocation_control/summary/runs.csv" \
  --gate "$STAGED_GATE" \
  --out "$STAGING/eval_access_scaling.pdf"

python3 "$SCRIPT_DIR/plot_vldb_resource_ledger.py" \
  --runs "$EVIDENCE_ROOT/resource_ledger/runs.csv" \
  --budget-summary "$EVIDENCE_ROOT/mechanism_controls/summary/budget_summary.csv" \
  --resident-summary "$EVIDENCE_ROOT/mechanism_controls/summary/resident_summary.csv" \
  --gate "$STAGED_GATE" \
  --out "$STAGING/eval_index_cost.pdf"

python3 "$SCRIPT_DIR/plot_vldb_lifecycle.py" \
  --build-cost "$EVIDENCE_ROOT/build_cost" \
  --lifecycle-controls "$EVIDENCE_ROOT/lifecycle_controls" \
  --gate "$STAGED_GATE" \
  --out "$STAGING/eval_lifecycle_boundaries.pdf"

GENERATED_FIGURES=(
  fig_physical_units.pdf
  eval_frontier_curves.pdf
  eval_access_scaling.pdf
  eval_index_cost.pdf
  eval_lifecycle_boundaries.pdf
)
EXISTING_FIGURES=(
  fig_construction_refresh.pdf
  fig_search_placement.pdf
  fig_slab_layout.pdf
  overview.pdf
)
for name in "${GENERATED_FIGURES[@]}"; do
  [[ -s "$STAGING/$name" ]] || {
    echo "Refusing incomplete final figure: $name" >&2
    exit 2
  }
done
for name in \
  fig_physical_units.svg \
  evidence_gate.json \
  frontier_1m_gate.json \
  headline_candidates.json \
  manuscript_claims.json \
  generated_claims.tex; do
  [[ -s "$STAGING/$name" ]] || {
    echo "Refusing incomplete final release source: $name" >&2
    exit 2
  }
done

PUBLICATION_PDFS=(
  "$STAGING/fig_physical_units.pdf"
  "$STAGING/eval_frontier_curves.pdf"
  "$STAGING/eval_access_scaling.pdf"
  "$STAGING/eval_index_cost.pdf"
  "$STAGING/eval_lifecycle_boundaries.pdf"
)
for name in "${EXISTING_FIGURES[@]}"; do
  PUBLICATION_PDFS+=("$OUT_DIR/$name")
done
python3 "$SCRIPT_DIR/verify_publication_pdf.py" "${PUBLICATION_PDFS[@]}"

repo_relative() {
  python3 - "$PUBLICATION_ROOT" "$1" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
target = Path(sys.argv[2]).resolve(strict=False)
try:
    print(target.relative_to(root).as_posix())
except ValueError as exc:
    raise SystemExit(f"release target is outside publication root: {target}") from exc
PY
}

PUBLISH=(
  python3 "$SCRIPT_DIR/publish_vldb_release.py"
  --repo-root "$PUBLICATION_ROOT"
  --gate "$STAGED_GATE"
  --claims "$STAGED_CLAIMS"
  --generated-claims "$STAGED_GENERATED_CLAIMS"
  --manifest "$RELEASE_MANIFEST"
)
add_release_entry() {
  local target=$1
  local source=$2
  PUBLISH+=(--entry "$(repo_relative "$target")=$source")
}

add_release_entry "$GATE" "$STAGED_GATE"
add_release_entry "$FRONTIER_1M_GATE" "$STAGED_FRONTIER_1M_GATE"
add_release_entry "$HEADLINES" "$STAGED_HEADLINES"
add_release_entry "$CLAIMS" "$STAGED_CLAIMS"
add_release_entry "$GENERATED_CLAIMS" "$STAGED_GENERATED_CLAIMS"
for name in "${GENERATED_FIGURES[@]}"; do
  add_release_entry "$OUT_DIR/$name" "$STAGING/$name"
done
add_release_entry "$OUT_DIR/fig_physical_units.svg" "$STAGING/fig_physical_units.svg"

for name in ACM-Reference-Format.bst acmart.cls main.tex pvldb.sty refs.bib; do
  add_release_entry "$PAPER_DIR/$name" "$PAPER_DIR/$name"
done
for name in "${EXISTING_FIGURES[@]}"; do
  add_release_entry "$OUT_DIR/$name" "$OUT_DIR/$name"
done
for name in "${GENERATED_FIGURES[@]}" "${EXISTING_FIGURES[@]}"; do
  PUBLISH+=(--pdf-target "$(repo_relative "$OUT_DIR/$name")")
done

"${PUBLISH[@]}"
release_published=1
echo "Published validated VLDB release in $PUBLICATION_ROOT"
