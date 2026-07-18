#!/bin/bash
# GraphBeyond: after every `make`, push the fresh binary to BOTH the
# node1 CN path AND node4 (MN runs a SEPARATE copy — nodes do NOT share
# a filesystem). Skipping the node4 hop silently runs a stale MN; when
# the CN<->MN wire protocol changed it fails as "Receive request failed".
#
# Run ON node1 (skv-node1) after build:
#   bash scripts/deploy_mn.sh
set -euo pipefail
BUILD=/home/kvgroup/chaomei/graphbeyond-c1/graphbeyond/build/shine
CN_BIN=/home/kvgroup/chaomei/graphbeyond-c1-shine     # node1 local
MN=skv-node4

cp "$BUILD" "$CN_BIN"
scp -o StrictHostKeyChecking=no "$CN_BIN" "$MN:$CN_BIN" >/dev/null
echo "node1 build : $(md5sum "$BUILD" | cut -d' ' -f1)"
echo "node1 CN    : $(md5sum "$CN_BIN" | cut -d' ' -f1)"
echo "node4 MN    : $(ssh -o StrictHostKeyChecking=no "$MN" "md5sum $CN_BIN | cut -d' ' -f1")"
echo "==> all three MUST match"
