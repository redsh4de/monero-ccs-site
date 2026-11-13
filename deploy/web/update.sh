#!/bin/sh
# shellcheck shell=dash
# Turns the fetched content and the pushed values into the served site, with
# the release this script came in.
#
# Runs as: ccs-update.service, as the ccs-update user, with no network, after
#          every fetch.
# Reads:   the push in /var/lib/ccs/in/values, the signed address list in
#          /etc/ccs/, and the content behind the links in/repo and in/mr.
# Writes:  /var/lib/ccs/state, the rendered trees in /var/lib/ccs/site/, and
#          the link /var/lib/ccs/current that nginx serves.
# Trust:   checks.py decides on the pushed values and the signed list; the
#          render escapes the fetched content.
# Exit:    0 when the site is current, 1 on any failure, which keeps the last
#          good site.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

# the physical path, so a run that overlaps an install stays in one release
DEPLOY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

# a release unpacks to /usr/local/lib/ccs/releases/<N>/, holding deploy/ and
# the built site in dist/
readonly RELEASE_DIR="${DEPLOY%/deploy}"
readonly VERSION="${RELEASE_DIR##*/}"
readonly IN_DIR="$CCS_DIR/in"
readonly STATE_DIR="$CCS_DIR/state"
readonly WORK_DIR="$CCS_DIR/work"

# check_new_values: writes state/values.new: the pushed values and their pins
# when checks.py accepts them, or else the values applied last.
check_new_values() {
  touch -- "$STATE_DIR/values"
  if ! "$DEPLOY/checks.py" values "$IN_DIR/values" "$STATE_DIR/values" \
    "$WORK_DIR/addresses" >"$STATE_DIR/values.new"; then
    cp -- "$STATE_DIR/values" "$STATE_DIR/values.new"
  fi
}

# link_inputs <repo> <mr>: fills the render's input folder in WORK_DIR with
# links to the fetched content, which belongs to the ccs-fetch user.
link_inputs() {
  local repo="$1"
  local mr="$2"

  ln -s -- "$repo/proposals" "$WORK_DIR/in/proposals"
  ln -s -- "$mr/open.json" "$repo/mr/merged.tsv" "$WORK_DIR/in/mr/"
}

# render_site <commit>: renders the site into WORK_DIR/out, with its
# status.json.
render_site() {
  local commit="$1"

  rsync -a --exclude=/templates/ "$RELEASE_DIR/dist/" "$WORK_DIR/out/"
  timeout 300 python3 -I -B "$DEPLOY/render/render.py" \
    "$WORK_DIR/in" "$RELEASE_DIR/dist/templates" "$WORK_DIR/out" \
    "$CCS_DIR/cache"
  python3 -I -B "$DEPLOY/render/status.py" write \
    "$WORK_DIR/in" "$VERSION" "$commit" >"$WORK_DIR/out/status.json"
  # whatever the render did, no file may show an address the list lacks
  "$DEPLOY/checks.py" site "$WORK_DIR/out" "$WORK_DIR/addresses" \
    || stop "the render shows an address the signed list lacks, so the site stays"
}

# switch_site: copies WORK_DIR/out to a new folder and points nginx's link
# at it. Unchanged files stay hard links to the current site, so they keep
# their time.
switch_site() {
  local current=""
  local next=""

  current=$(readlink -f "$CCS_DIR/current") || current=""
  next=$(mktemp -d "$CCS_DIR/site/XXXXXX")
  chmod 755 "$next"
  rsync -a --checksum --link-dest="$current" "$WORK_DIR/out/" "$next/"
  # on disk before it goes live, so a crash can't serve an empty file
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

  # 1. The values, when a newer file arrived that keeps every pin.
  verify_list "$WORK_DIR/addresses"
  check_new_values

  # 2. The render's input: the fetched content, and the values with every
  #    address checked against the signed list.
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

  # 3. Render only when something besides the values' time changed.
  inputs_hash=$(printf '%s\n' "$VERSION" "$repo" "$mr" "$values_sum" \
    | sha256sum | cut -c1-64)
  rendered=$(cat "$STATE_DIR/rendered" 2>/dev/null) || rendered=""
  if [ "$inputs_hash" != "$rendered" ]; then
    render_site "$commit"
    switch_site
    printf '%s\n' "$inputs_hash" >"$STATE_DIR/rendered"
  fi

  # 4. Only now do the values count as applied, so a render that keeps
  #    failing trips fetch.sh's stale-values alert.
  mv -- "$STATE_DIR/values.new" "$STATE_DIR/values"
  rm -rf -- "$WORK_DIR"
}

main "$@"
