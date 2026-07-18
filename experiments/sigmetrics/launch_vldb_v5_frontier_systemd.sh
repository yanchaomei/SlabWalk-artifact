#!/usr/bin/env bash
# Launch a durable v5 frontier recovery as a systemd user service.
set -euo pipefail

SOURCE_ROOT=${SOURCE_ROOT:?set SOURCE_ROOT to the frozen v5 source tree}
OUT_ROOT=${OUT_ROOT:?set OUT_ROOT to the existing parent campaign}
GB_BIN=${GB_BIN:?set GB_BIN to the frozen candidate binary}
EXPECTED_BINARY_SHA=${EXPECTED_BINARY_SHA:?set EXPECTED_BINARY_SHA}
ACTUAL_HOST=${ACTUAL_HOST:-$(hostname)}
EXPECTED_HOST=${EXPECTED_HOST:-skv-node1}
UNIT=${UNIT:-v5-frontier-1m-recovery}
SERVICE_LOG=${SERVICE_LOG:-${OUT_ROOT}.systemd.log}
FRONTIER_LIFECYCLE_ROOT=${FRONTIER_LIFECYCLE_ROOT:-${OUT_ROOT}.${UNIT}.lifecycle}
CAMPAIGN_ID=${CAMPAIGN_ID:-vldb-v5-frontier-1m-20260717}
SW_PORT=${SW_PORT:-17980}
DATASETS_SW=${DATASETS_SW:-"SIFT1M GIST1M DEEP1M BIGANN1M SPACEV1M TURING1M TEXT1M"}
EXPECTED_DATASETS=${EXPECTED_DATASETS:-SIFT1M,GIST1M,DEEP1M,BIGANN1M,SPACEV1M,TURING1M,TEXT1M}
RUNNER="$SOURCE_ROOT/experiments/sigmetrics/run_frontier_repeated.sh"

die() {
  echo "$*" >&2
  exit 2
}

[[ "$ACTUAL_HOST" == "$EXPECTED_HOST" ]] || \
  die "frontier service must launch on $EXPECTED_HOST"
[[ "$UNIT" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "invalid systemd unit name"
[[ "$EXPECTED_BINARY_SHA" =~ ^[0-9a-f]{64}$ ]] || \
  die "invalid candidate SHA-256"
[[ -x "$GB_BIN" ]] || die "missing candidate binary: $GB_BIN"
[[ "$(sha256sum "$GB_BIN" | awk '{print $1}')" == \
    "$EXPECTED_BINARY_SHA" ]] || die "candidate binary SHA mismatch"
[[ -s "$RUNNER" ]] || die "missing repeated-frontier runner: $RUNNER"
[[ -s "$OUT_ROOT/campaign.json" ]] || \
  die "missing parent campaign manifest: $OUT_ROOT/campaign.json"
[[ ! -e "$OUT_ROOT/SW_FRONTIER_COMPLETE.json" ]] || \
  die "frontier already has SW_FRONTIER_COMPLETE.json"
[[ ! -e "$FRONTIER_LIFECYCLE_ROOT" ]] || \
  die "refusing existing frontier lifecycle root: $FRONTIER_LIFECYCLE_ROOT"

for repeat in 3 4 5; do
  child="$OUT_ROOT/sw_r$repeat"
  if [[ -e "$child" &&
        !( -s "$child/SEALED.json" && -s "$child/SHA256SUMS" ) ]]; then
    die "refusing incomplete frontier child: $child"
  fi
done

state=$(systemctl --user is-system-running 2>/dev/null || true)
[[ "$state" == "running" || "$state" == "degraded" ]] || \
  die "systemd user manager is unavailable: $state"
if systemctl --user show "$UNIT.service" >/dev/null 2>&1; then
  die "systemd unit already exists: $UNIT.service"
fi

systemd-run --user \
  --unit="$UNIT" \
  --description="SlabWalk v5 fixed-pool frontier recovery" \
  --service-type=exec \
  --property="WorkingDirectory=$SOURCE_ROOT" \
  --property="StandardOutput=append:$SERVICE_LOG" \
  --property="StandardError=append:$SERVICE_LOG" \
  --property=CPUAccounting=yes \
  --property=MemoryAccounting=yes \
  --property=KillMode=control-group \
  --property=TimeoutStopSec=60 \
  --setenv=PHASES=sw \
  --setenv=REPEATS=5 \
  --setenv=THREADS=10 \
  --setenv=QUERY_CONTEXTS=10 \
  --setenv=COROS=2 \
  --setenv=RESUME=1 \
  --setenv="GB_BIN=$GB_BIN" \
  --setenv="EXPECTED_BINARY_SHA=$EXPECTED_BINARY_SHA" \
  --setenv="OUT_ROOT=$OUT_ROOT" \
  --setenv="FRONTIER_LIFECYCLE_ROOT=$FRONTIER_LIFECYCLE_ROOT" \
  --setenv="CAMPAIGN_ID=$CAMPAIGN_ID" \
  --setenv="SW_PORT=$SW_PORT" \
  --setenv="DATASETS_SW=$DATASETS_SW" \
  --setenv="EXPECTED_DATASETS=$EXPECTED_DATASETS" \
  --setenv="SIFT1_EFS=${SIFT1_EFS:-48 64 80 100 150}" \
  --setenv="GIST1_EFS=${GIST1_EFS:-100 200 300 400 600}" \
  --setenv="DEEP1_EFS=${DEEP1_EFS:-30 50 80 100 150}" \
  --setenv="BIGANN1_EFS=${BIGANN1_EFS:-48 64 80 100 150}" \
  --setenv="SPACEV1_EFS=${SPACEV1_EFS:-100 200 300 400 600}" \
  --setenv="TURING1_EFS=${TURING1_EFS:-200 400 600 900 1200}" \
  --setenv="TEXT1_EFS=${TEXT1_EFS:-100 150 200 300 500}" \
  /bin/bash "$RUNNER"

systemctl --user show "$UNIT.service" \
  -p Id -p ActiveState -p SubState -p MainPID -p ExecMainStatus -p Result
