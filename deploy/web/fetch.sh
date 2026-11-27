#!/bin/sh
# shellcheck shell=dash
# Fetches the proposals repository and the open merge requests from GitLab.
#
# Exit: 0 when the content is current, 1 when a fetch failed.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

# the physical path, so a run that overlaps an install stays in one release
DEPLOY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

readonly IN_DIR="$CCS_DIR/in"
readonly FETCH_DIR="$CCS_DIR/fetch"
# in memory, so a full disk can still alert
readonly ALERTS_DIR="${RUNTIME_DIRECTORY:?}/alerts"
# 55 minutes, so each hourly run calls GitLab despite the timer's random delay
readonly REFETCH_AFTER=3300
NOW=$(date +%s)
readonly NOW

# drop_cut_off_pushes: deletes the temporary files of pushes cut off midway.
# A file ten minutes old is no push in flight.
drop_cut_off_pushes() {
  find "$IN_DIR" -maxdepth 1 -name 'values.??????' -mmin +10 -delete
}

# check_values_age: alerts when the values applied last are over 10 minutes old.
# update.sh counts values as applied only once the site shows them.
check_values_age() {
  local updated=""
  local age=0
  local stale=0

  updated=$(sed -n 's/^updated //p' "$CCS_DIR/state/values" 2>/dev/null) \
    || updated=0
  age=$((NOW - ${updated:-0}))
  if [ "$age" -gt 600 ]; then
    stale=1
  fi
  alert stale-values "$stale" 0 3000 \
    "ccs: no values from the backend applied for over 10 minutes"
}

# is_fetch_due <gitlab>: true when the backend's "gitlab" line changed since
# the last good fetch, or the last attempt is old enough to try again.
is_fetch_due() {
  local gitlab="$1"
  local gitlab_last=""
  local tried_at=0
  local age=0

  gitlab_last=$(cat "$FETCH_DIR/gitlab" 2>/dev/null) || gitlab_last=""
  if [ "$gitlab" != "$gitlab_last" ]; then
    return 0
  fi
  tried_at=$(cat "$FETCH_DIR/last-fetch" 2>/dev/null) || tried_at=0
  age=$((NOW - tried_at))
  [ "$age" -ge "$REFETCH_AFTER" ]
}

# fetch_proposals: fetches the proposals repository, exports each new commit
# once, with the merge request behind each file, and points in/repo at it.
fetch_proposals() {
  local repo="$FETCH_DIR/proposals.git"
  local commit=""
  local tree=""
  local old=""

  if [ ! -d "$repo" ]; then
    git init -q --bare "$repo" || return 1
  fi
  # a stall fails in a minute; the timeouts stay inside the unit's limit, so
  # the "fetch" alert still runs
  timeout 300 git -C "$repo" -c http.lowSpeedLimit=1000 \
    -c http.lowSpeedTime=60 \
    fetch -q "$PROPOSALS_GIT" '+refs/heads/master:refs/heads/master' \
    || return 1
  commit=$(git -C "$repo" rev-parse master) || return 1

  tree="$FETCH_DIR/trees/$commit"
  if [ ! -d "$tree" ]; then
    rm -rf -- "$tree.tmp" || return 1
    export_tree "$repo" "$commit" "$tree.tmp" || return 1
    # on disk before it gets the name that later runs trust
    sync -f "$tree.tmp" || return 1
    mv -- "$tree.tmp" "$tree" || return 1
  fi

  old=$(readlink "$IN_DIR/repo") || old=""
  if [ "$old" != "$tree" ]; then
    swap "$tree" "$IN_DIR/repo" || return 1
    keep "$FETCH_DIR/trees/" "$tree" "$old" || return 1
  fi
}

# fetch_merge_requests: fetches the open merge requests, keeps them as a new
# set only when their contents changed, and points in/mr at the set.
fetch_merge_requests() {
  local new="$FETCH_DIR/mr.tmp"
  local hash=""
  local set=""
  local old=""

  rm -rf -- "$new" || return 1
  mkdir -- "$new" || return 1
  # the changes fetched so far stay cached, so the next run goes further
  timeout 420 "$DEPLOY/mrs.py" "$GITLAB_API" "$new" "$FETCH_DIR/changes" \
    || return 1
  hash=$(sha256sum <"$new/open.json" | cut -c1-16) || return 1

  set="$FETCH_DIR/mr-$hash"
  if [ ! -d "$set" ]; then
    sync -f "$new" || return 1
    mv -- "$new" "$set" || return 1
  fi

  old=$(readlink "$IN_DIR/mr") || old=""
  if [ "$old" != "$set" ]; then
    swap "$set" "$IN_DIR/mr" || return 1
    keep "$FETCH_DIR/mr-" "$set" "$old" || return 1
  fi
}

main() {
  local gitlab=""
  local failing=0

  alerts_begin
  trap alerts_send EXIT
  # dash skips the EXIT trap on a signal, so a stop from systemd exits instead
  trap 'exit 1' TERM
  drop_cut_off_pushes
  check_values_age

  # the push is unchecked here, and only decides when to call GitLab
  gitlab=$(sed -n '/^gitlab sha256=[0-9a-f]\{64\}$/p' "$IN_DIR/values" \
    2>/dev/null | tail -n 1) || gitlab=""
  if is_fetch_due "$gitlab"; then
    printf '%s\n' "$NOW" >"$FETCH_DIR/last-fetch"
    if fetch_proposals && fetch_merge_requests; then
      printf '%s\n' "$gitlab" >"$FETCH_DIR/gitlab"
    else
      failing=1
      log "fetch: content failed"
    fi
    # under an hour, so the hourly tries alone raise and repeat the alert
    alert fetch "$failing" 3000 3000 \
      "ccs: content fetches have failed for 50 minutes"
  fi

  exit "$failing"
}

main "$@"
