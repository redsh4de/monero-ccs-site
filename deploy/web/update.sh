#!/bin/sh
# shellcheck shell=dash
# Renders the fetched content and pushed values into the site nginx serves.
#
# Exit: 0 when the site is current, 1 on a failure; the last good site stays.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

# the physical path, so a run that overlaps an install stays in one release
DEPLOY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

# DEPLOY is releases/<N>/deploy, so the release's folder name is its number
readonly RELEASE_DIR="${DEPLOY%/deploy}"
readonly VERSION="${RELEASE_DIR##*/}"
readonly IN_DIR="$CCS_DIR/in"
readonly STATE_DIR="$CCS_DIR/state"
readonly WORK_DIR="$CCS_DIR/work"

# check_new_values: writes state/values.new from the push when checks.py
# accepts it, or else from the values applied last.
check_new_values() {
  touch -- "$STATE_DIR/values"
  if ! "$DEPLOY/checks.py" values "$IN_DIR/values" "$STATE_DIR/values" \
    "$WORK_DIR/addresses" >"$STATE_DIR/values.new"; then
    cp -- "$STATE_DIR/values" "$STATE_DIR/values.new"
  fi
}

# link_inputs <repo> <mr>: links the fetched content into the render's input.
link_inputs() {
  local repo="$1"
  local mr="$2"

  ln -s -- "$repo/proposals" "$WORK_DIR/in/proposals"
  ln -s -- "$mr/open.json" "$repo/mr/merged.tsv" "$WORK_DIR/in/mr/"
}

# render_site <commit>: renders the site and its status.json into WORK_DIR/out.
render_site() {
  local commit="$1"

  rsync -a --exclude=/templates/ "$RELEASE_DIR/dist/" "$WORK_DIR/out/"
  timeout 300 python3 -I -B "$DEPLOY/render/render.py" \
    "$WORK_DIR/in" "$RELEASE_DIR/dist/templates" "$WORK_DIR/out" \
    "$CCS_DIR/cache" || stop "the render failed, or took more than 300 s"
  python3 -I -B "$DEPLOY/render/status.py" write \
    "$WORK_DIR/in" "$VERSION" "$commit" >"$WORK_DIR/out/status.json"
  "$DEPLOY/checks.py" site "$WORK_DIR/out" "$WORK_DIR/addresses" \
    "$DEPLOY/site-hosts" \
    || stop "the render shows an address or a link it may not, so the site stays"
}

# switch_site: points nginx's link at a new copy of WORK_DIR/out.
# Unchanged files stay hard links to the current site, so they keep their time.
switch_site() {
  local current=""
  local next=""

  current=$(readlink -f "$CCS_DIR/current") || current=""
  next=$(mktemp -d "$CCS_DIR/site/XXXXXX")
  chmod 755 "$next"
  rsync -a --checksum --link-dest="$current" "$WORK_DIR/out/" "$next/"
  # on disk before it goes live, so a crash cannot serve an empty file
  sync -f "$next"
  swap "$next" "$CCS_DIR/current"
  keep "$CCS_DIR/site/" "$next" "$current"
}

main() {
  local repo=""
  local mr=""
  local commit=""
  local values_sum=""
  local inputs_hash=""
  local rendered=""

  mkdir -p -- "$STATE_DIR" "$CCS_DIR/site" "$CCS_DIR/cache"
  rm -rf -- "$WORK_DIR"
  mkdir -p -- "$WORK_DIR/in/mr"

  verify_list "$WORK_DIR/addresses"
  check_new_values

  if [ ! -L "$IN_DIR/repo" ] || [ ! -L "$IN_DIR/mr" ]; then
    log "no content fetched yet"
    exit 0
  fi
  repo=$(readlink -f "$IN_DIR/repo")
  mr=$(readlink -f "$IN_DIR/mr")
  # in/repo points at fetch/trees/<commit>
  commit="${repo##*/}"
  link_inputs "$repo" "$mr"
  values_sum=$(render_values "$STATE_DIR/values.new" "$WORK_DIR/in" \
    "$WORK_DIR/addresses") || stop "can't build the render's values"

  inputs_hash=$(printf '%s\n' "$VERSION" "$repo" "$mr" "$values_sum" \
    | sha256sum | cut -c1-64)
  rendered=$(cat "$STATE_DIR/rendered" 2>/dev/null) || rendered=""
  if [ "$inputs_hash" != "$rendered" ]; then
    render_site "$commit"
    switch_site
    printf '%s\n' "$inputs_hash" >"$STATE_DIR/rendered"
  fi

  # the values count as applied only now, so a failing render trips
  # fetch.sh's stale-values alert
  mv -- "$STATE_DIR/values.new" "$STATE_DIR/values"
  rm -rf -- "$WORK_DIR"
}

main "$@"
