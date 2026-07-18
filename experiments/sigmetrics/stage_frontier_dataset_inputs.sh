#!/usr/bin/env bash
# Stage the dataset files required by the formal 1M frontier on the compute
# node. Every file is copied through a same-directory temporary and promoted
# only after its SHA-256 matches the source host.
set -euo pipefail

GB_DATA=${GB_DATA:-/home/kvgroup/chaomei/hnsw-data}
SOURCE_HOST=${SOURCE_HOST:-skv-node1}
DATASETS=${DATASETS:-"BIGANN1M SPACEV1M TURING1M TEXT1M"}
CURRENT_TMP=""

cleanup() {
  if [[ -n "$CURRENT_TMP" && -e "$CURRENT_TMP" ]]; then
    rm -f -- "$CURRENT_TMP"
  fi
}
trap cleanup EXIT INT TERM

copy_one() {
  local relative=$1
  local src="${GB_DATA%/}/$relative"
  local dst="$src"
  local source_sha staged_sha existing_sha

  source_sha=$(ssh -n -o BatchMode=yes "$SOURCE_HOST" \
    "sha256sum '$src'" | awk '{print $1}')
  [[ "$source_sha" =~ ^[0-9a-f]{64}$ ]] || {
    printf 'Could not fingerprint source input: %s:%s\n' \
      "$SOURCE_HOST" "$src" >&2
    return 2
  }

  if [[ -s "$dst" ]]; then
    existing_sha=$(sha256sum "$dst" | awk '{print $1}')
    if [[ "$existing_sha" == "$source_sha" ]]; then
      printf 'REUSE\t%s\t%s\t%s\n' "$dst" "$(stat -c '%s' "$dst")" \
        "$existing_sha"
      return 0
    fi
  fi

  mkdir -p "$(dirname "$dst")"
  CURRENT_TMP=$(mktemp "${dst}.incoming.XXXXXX")
  rsync -a --partial "$SOURCE_HOST:$src" "$CURRENT_TMP"
  staged_sha=$(sha256sum "$CURRENT_TMP" | awk '{print $1}')
  test "$source_sha" = "$staged_sha"
  mv "$CURRENT_TMP" "$dst"
  CURRENT_TMP=""
  printf 'STAGED\t%s\t%s\t%s\n' "$dst" "$(stat -c '%s' "$dst")" \
    "$staged_sha"
}

stage_dataset() {
  local dataset=$1 descriptor
  case "$dataset" in
    BIGANN1M) descriptor='BIGANN1M|bigann1m|base.u8bin|query-uniform.u8bin' ;;
    SPACEV1M) descriptor='SPACEV1M|spacev1m|base.i8bin|query-uniform.i8bin' ;;
    TURING1M) descriptor='TURING1M|turing1m|base.fbin|query-uniform.fbin' ;;
    TEXT1M) descriptor='TEXT1M|tti1m|base.fbin|query-uniform.fbin' ;;
    *)
      printf 'Unsupported frontier staging dataset: %s\n' "$dataset" >&2
      return 2
      ;;
  esac

  local name directory base query
  IFS='|' read -r name directory base query <<< "$descriptor"
  [[ "$name" == "$dataset" ]]
  copy_one "$directory/$base"
  copy_one "$directory/queries/$query"
  copy_one "$directory/queries/groundtruth-uniform.bin"
}

for dataset in $DATASETS; do
  stage_dataset "$dataset"
done
