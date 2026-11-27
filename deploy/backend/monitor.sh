#!/bin/sh
# shellcheck shell=dash
# Checks the live site against its sources and a render of its release.
#
# Exit: 0 when the run ends, whatever it found; it reports through alerts.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

# the physical path, so a run that overlaps an install stays in one release
DEPLOY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

# DEPLOY is releases/<N>/deploy, so the release's folder name is its number
readonly INSTALLED_DIR="${DEPLOY%/deploy}"
readonly INSTALLED_VERSION="${INSTALLED_DIR##*/}"
readonly STATE_DIR="${STATE_DIRECTORY:?}"
readonly RUN_DIR="$STATE_DIR/run"
# in memory, so a full disk can still alert
readonly ALERTS_DIR="${RUNTIME_DIRECTORY:?}/alerts"
readonly EXPECTED_DIR="$STATE_DIR/expected"
readonly PROPOSALS_CHECKOUT="$CCS_BACK/proposals"
readonly BACKEND_VALUES=/var/lib/ccs-backend/values
readonly MESSAGE_PREFIX="ccs monitor:"
# what Tor Browser asks for a page; keep it to each Tor Browser major release
readonly BROWSER_AGENT="Mozilla/5.0 (Windows NT 10.0; rv:153.0) Gecko/20100101 Firefox/153.0"
readonly BROWSER_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
readonly PAGE_DIFFERS=" differs from the expected render"
NOW=$(date +%s)
readonly NOW

# ccs-back's checkout belongs to another user, so git is told to trust it
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$PROPOSALS_CHECKOUT"

# check <name> <failing> <text>: alerts after 10 minutes of failing, which is
# the second failing run, and then once a day.
check() {
  local name="$1"
  local failing="$2"
  local text="$3"

  alert "$name" "$failing" 600 86400 "$MESSAGE_PREFIX $text"
}

# require <name> <failing> <text>: a check that ends the run when it fails.
require() {
  local name="$1"
  local failing="$2"
  local text="$3"

  check "$name" "$failing" "$text"
  if [ "$failing" != 0 ]; then
    end_run
  fi
}

# begin_run: makes any crash from here on raise the "monitor" alert, then
# clears the last run's files.
begin_run() {
  alerts_begin
  trap 'check monitor 1 "the monitor crashed"; alerts_send' EXIT
  # dash skips the EXIT trap on a signal, so a stop from systemd exits instead
  trap 'exit 1' TERM
  rm -rf -- "$RUN_DIR"
  mkdir -p -- "$RUN_DIR" "$STATE_DIR/releases"
}

# end_run: passes "monitor", sends the alerts and the heartbeat, and exits.
end_run() {
  check monitor 0 "the monitor crashed"
  trap - EXIT
  alerts_send
  # a heartbeat service sees the monitor or Tor stop, which no alert can show
  if [ -s "${CREDENTIALS_DIRECTORY:-/nonexistent}/heartbeat_url" ]; then
    request_url heartbeat_url || log "heartbeat not sent"
  fi
  rm -rf -- "$RUN_DIR"
  exit 0
}

# new_circuit: prints a new SOCKS user name, for a Tor circuit of its own.
# The web server then cannot tie one set of requests to another by their exit.
new_circuit() {
  od -An -N8 -tx1 /dev/urandom | tr -d ' \n'
}

# fetch_url <url> <file> <circuit> [<curl option>...]: downloads one file on
# that Tor circuit, through the proxy in ALL_PROXY.
fetch_url() {
  local url="$1"
  local file="$2"
  local circuit="$3"
  shift 3

  curl --disable -fsS -L --max-time 60 --max-filesize 200000000 \
    --proxy-user "$circuit:x" "$@" -o "$file" "$url"
}

# fetch_page <url> <file> <circuit>: downloads one page as Tor Browser asks
# for it, so the web server cannot single out the monitor by its requests.
fetch_page() {
  fetch_url "$1" "$2" "$3" --compressed -A "$BROWSER_AGENT" \
    -H "Accept: $BROWSER_ACCEPT" -H "Accept-Language: en-US,en;q=0.5"
}

# status_py <release> <arguments...>: runs that release's status.py.
status_py() {
  local release="$1"
  shift

  python3 -I -B "$release/deploy/render/status.py" "$@" 2>/dev/null
}

# read_site_status <circuit>: prints the "<release> <commit>" that the site's
# status.json names.
# The web server is not trusted, so checks.py checks both fields.
read_site_status() {
  local circuit="$1"

  fetch_url "$SITE/status.json" "$RUN_DIR/status.json" "$circuit" \
    -w '%{certs}' >"$RUN_DIR/certs" || return 1
  "$DEPLOY/checks.py" status "$RUN_DIR/status.json" || return 1
}

# check_certificate: alerts 14 days before the site's TLS certificate expires.
# curl lists the certificate that served status.json first.
check_certificate() {
  local expires=""
  local expiry=0
  local failing=0

  expires=$(sed -n '/^Expire date:/{s///p;q}' "$RUN_DIR/certs")
  if [ -z "$expires" ]; then
    return 0
  fi
  # a date that cannot be read counts as expired, so it alerts
  expiry=$(date -d "$expires" +%s 2>/dev/null) || expiry=0
  if [ "$expiry" -lt $((NOW + 1209600)) ]; then
    failing=1
  fi
  check cert-expiry "$failing" \
    "the site's certificate expires on $(date -u -d "@$expiry" +%Y-%m-%d)"
}

# get_verified_release <version>: prints the release's folder, which it first
# downloads and verifies as install.sh does.
get_verified_release() {
  local version="$1"
  local release="$STATE_DIR/releases/$version"

  if [ ! -d "$release" ]; then
    mkdir -p -- "$RUN_DIR/download" || return 1
    get_release "site-v$version" "$RUN_DIR/download" || return 1
    "$DEPLOY/checks.py" release "$RUN_DIR/download" \
      /etc/ccs/release-signers.gpg "$version" "$RUN_DIR/tree" || return 1
    mv -- "$RUN_DIR/tree" "$release" || return 1
  fi
  printf '%s\n' "$release"
}

# is_recent_enough <version> <release>: true unless the site runs an older
# release that differs from this one in more than backend/, signer/ and the docs.
# So the site cannot fall back to a fixed bug, and no older render runs here.
is_recent_enough() {
  local version="$1"
  local release="$2"

  if [ "$version" -ge "$INSTALLED_VERSION" ]; then
    return 0
  fi
  diff -rq -x backend -x signer -x '*.md' "$release" "$INSTALLED_DIR" >/dev/null
}

# check_stall <commit>: alerts when the site's commit stays behind
# ccs-back's for over an hour.
check_stall() {
  local commit="$1"
  local head=""
  local behind=0

  head=$(git -C "$PROPOSALS_CHECKOUT" rev-parse HEAD)
  if [ "$head" != "$commit" ] \
    && git -C "$PROPOSALS_CHECKOUT" merge-base --is-ancestor "$commit" "$head"; then
    behind=1
  fi
  alert stall "$behind" 3600 86400 \
    "$MESSAGE_PREFIX the site's content is over an hour behind ccs-back"
}

# is_recently_verified <snapshot sum>: true when GitLab confirmed this same
# set of merge requests within the hour.
is_recently_verified() {
  local snapshot_sum="$1"
  local verified=""
  local verified_sum=""
  local verified_at=0
  local age=0

  verified=$(cat "$STATE_DIR/mrs-verified" 2>/dev/null) || return 1
  verified_sum="${verified% *}"
  verified_at="${verified#* }"
  age=$((NOW - verified_at))
  [ "$verified_sum" = "$snapshot_sum" ] && [ "$age" -lt 3600 ]
}

# verify_merge_requests <release> <snapshot> <snapshot sum>: fails when the
# site's merge requests differ from GitLab's.
# When GitLab does not answer, it fails and prints why.
verify_merge_requests() {
  local release="$1"
  local snapshot="$2"
  local snapshot_sum="$3"
  local gitlab=""

  if is_recently_verified "$snapshot_sum"; then
    return 0
  fi
  mkdir -p -- "$RUN_DIR/gitlab/mr" || return 1
  if ! "$DEPLOY/mrs.py" "$GITLAB_API" "$RUN_DIR/gitlab/mr" \
    "$STATE_DIR/changes" 2>/dev/null; then
    printf '%s %s\n' "GitLab's merge request API doesn't answer," \
      "so the site's merge requests are unverified"
    return 1
  fi
  gitlab=$(status_py "$release" mrs "$RUN_DIR/gitlab") || return 1
  if [ "$gitlab" != "$snapshot" ]; then
    return 1
  fi
  printf '%s %s\n' "$snapshot_sum" "$NOW" >"$STATE_DIR/mrs-verified"
}

# build_values: builds the render's values from the backend's latest file,
# as the web server builds them, and prints their hash.
# A site with other values fails the page compare, index.php/projects first.
build_values() {
  verify_list "$RUN_DIR/addresses"
  if ! "$DEPLOY/checks.py" values "$BACKEND_VALUES" /dev/null \
    "$RUN_DIR/addresses" >"$RUN_DIR/checked"; then
    : >"$RUN_DIR/checked"
  fi
  render_values "$RUN_DIR/checked" "$RUN_DIR/in" "$RUN_DIR/addresses"
}

# render_expected <release> <commit> <inputs-hash>: renders the expected
# pages into EXPECTED_DIR, when the render's inputs changed since last time.
render_expected() {
  local release="$1"
  local commit="$2"
  local inputs_hash="$3"
  local rendered=""

  rendered=$(cat "$STATE_DIR/rendered" 2>/dev/null) || rendered=""
  if [ "$inputs_hash" = "$rendered" ] && [ -d "$EXPECTED_DIR" ]; then
    return 0
  fi
  # with rendered gone, a render cut short is redone next run
  rm -rf -- "$EXPECTED_DIR" "$STATE_DIR/rendered"
  export_tree "$PROPOSALS_CHECKOUT" "$commit" "$RUN_DIR/in"
  cp -R -- "$release/dist" "$EXPECTED_DIR"
  rm -rf -- "$EXPECTED_DIR/templates"
  timeout 300 python3 -I -B "$release/deploy/render/render.py" "$RUN_DIR/in" \
    "$release/dist/templates" "$EXPECTED_DIR"
  printf '%s\n' "$inputs_hash" >"$STATE_DIR/rendered"
}

# check_addresses: alerts when the address list does not verify, or when a
# proposal that takes donations shows no address.
check_addresses() {
  local missing=""
  local failing=0

  if [ ! -s "$RUN_DIR/addresses" ]; then
    check addresses 1 \
      "the address list doesn't verify, so no donation address shows"
    return 0
  fi
  # grep fails on no match or an unreadable page, but still prints the matches
  missing=$(cd -- "$EXPECTED_DIR" \
    && { grep -lF 'class="no-address"' proposals/*.html || true; } \
    | paste -sd' ' -)
  if [ -n "$missing" ]; then
    failing=1
  fi
  check addresses "$failing" \
    "proposals take donations with no address on the list: $missing"
}

# list_always: prints the files compared on every run.
list_always() {
  local qr=""
  local name=""

  printf '%s\n' index.html ideas/index.html funding-required/index.html \
    work-in-progress/index.html completed-proposals/index.html \
    donate/index.html index.php/projects feed.xml
  (cd -- "$EXPECTED_DIR" && find _astro -name '*.css')
  for qr in "$EXPECTED_DIR"/qr/*.png; do
    if [ ! -e "$qr" ]; then
      continue
    fi
    name="${qr##*/}"
    printf 'qr/%s\n' "$name"
    (cd -- "$EXPECTED_DIR" && { grep -lF "${name%.png}" proposals/*.html || true; })
  done
}

# list_sample <file-list>: prints the next ten files of the list, from where
# the last run stopped.
list_sample() {
  local file_list="$1"
  local total=0
  local offset=0
  local next=0

  total=$(wc -l <"$file_list")
  if [ "$total" -eq 0 ]; then
    return 0
  fi
  offset=$(cat "$STATE_DIR/sample-offset" 2>/dev/null) || offset=0
  if [ "$offset" -ge "$total" ]; then
    offset=0
  fi
  LC_ALL=C awk -v start="$offset" -v count=10 '
    { line[NR - 1] = $0 }
    END { for (i = 0; i < count && i < NR; i++) print line[(start + i) % NR] }
  ' "$file_list"
  next=$(((offset + 10) % total))
  printf '%s\n' "$next" >"$STATE_DIR/sample-offset"
}

# list_failing <file-list> <always-list>: prints each failing page still on
# the site, so it is compared again until it passes.
list_failing() {
  local file_list="$1"
  local always_list="$2"
  local state=""
  local text=""
  local path=""

  for state in "$ALERTS_DIR"/page-*; do
    if [ ! -e "$state" ]; then
      continue
    fi
    # compare_page writes "<MESSAGE_PREFIX> page <path><PAGE_DIFFERS>"
    read -r _ _ text <"$state"
    path="${text#"$MESSAGE_PREFIX page "}"
    path="${path%"$PAGE_DIFFERS"}"
    if grep -qxF -- "$path" "$file_list" "$always_list"; then
      printf '%s\n' "$path"
    else
      check "${state##*/}" 0 "page $path left the site"
    fi
  done
}

# compare_page <path> <circuit>: fetches one file from the live site, and
# checks it against the expected render, byte for byte.
compare_page() {
  local path="$1"
  local circuit="$2"
  local url=""
  local name=""
  local failing=0
  local quote='import sys, urllib.parse as u; print(u.quote(sys.argv[1]))'

  url=$(python3 -I -c "$quote" "$path")
  name=$(printf '%s' "$path" | sha256sum | cut -c1-16)
  if ! fetch_page "$SITE/$url" "$RUN_DIR/page" "$circuit" \
    || ! cmp -s "$RUN_DIR/page" "$EXPECTED_DIR/$path"; then
    failing=1
  fi
  check "page-$name" "$failing" "page $path$PAGE_DIFFERS"
}

# compare_pages <circuit>: compares the files that matter most on every run,
# ten more in turn, and every page that failed before.
compare_pages() {
  local circuit="$1"
  local path=""

  list_always >"$RUN_DIR/always"
  find "$EXPECTED_DIR" -type f -printf '%P\n' | LC_ALL=C sort >"$RUN_DIR/all"
  list_sample "$RUN_DIR/all" >"$RUN_DIR/sample"
  list_failing "$RUN_DIR/all" "$RUN_DIR/always" >"$RUN_DIR/again"
  sort -u "$RUN_DIR/always" "$RUN_DIR/sample" "$RUN_DIR/again" \
    >"$RUN_DIR/compare"
  while IFS= read -r path; do
    compare_page "$path" "$circuit"
  done <"$RUN_DIR/compare"
}

# check_keys: alerts on a signing key that expires soon. It runs first, as it
# does not depend on the site.
check_keys() {
  local failing=0
  local text=""

  text=$("$DEPLOY/checks.py" expiring /etc/ccs/release-signers.gpg \
    /etc/ccs/address-signers.gpg 2>&1) || failing=1
  check key-expiry "$failing" "${text:-a signing key expires within 30 days}"
}

# read_status <circuit>: reads the site's status.json, and sets main's version
# and commit.
read_status() {
  local failing=0
  local status=""

  status=$(read_site_status "$1") || failing=1
  check_certificate
  require status "$failing" "status.json is missing or malformed"
  version="${status% *}"
  commit="${status#* }"
}

# take_release: verifies the site's release, and sets main's release.
take_release() {
  local failing=0

  release=$(get_verified_release "$version") || failing=1
  require release "$failing" "release $version doesn't download or verify"
  keep "$STATE_DIR/releases/" "$release" "$release"
  failing=0
  is_recent_enough "$version" "$release" || failing=1
  require old-release "$failing" \
    "the site runs release $version, older than release $INSTALLED_VERSION here"
}

# check_site_commit: requires the site's commit in ccs-back's checkout.
check_site_commit() {
  local failing=0

  git -C "$PROPOSALS_CHECKOUT" cat-file -e "$commit^{commit}" 2>/dev/null \
    || failing=1
  require commit "$failing" \
    "the site's commit $commit isn't in ccs-back's checkout"
  check_stall "$commit"
}

# check_snapshot: restores the site's merge requests, and compares them with
# GitLab's.
check_snapshot() {
  local failing=0
  local snapshot=""
  local snapshot_sum=""
  local text=""

  "$DEPLOY/checks.py" snapshot "$RUN_DIR/status.json" || failing=1
  status_py "$release" restore "$RUN_DIR/status.json" "$RUN_DIR/in" \
    || failing=1
  snapshot=$(status_py "$release" mrs "$RUN_DIR/in") || failing=1
  require snapshot "$failing" "status.json's merge requests are malformed"
  snapshot_sum=$(printf '%s' "$snapshot" | sha256sum | cut -c1-64)
  failing=0
  text=$(verify_merge_requests "$release" "$snapshot" "$snapshot_sum") \
    || failing=1
  [ -n "$text" ] || text="the site's merge requests differ from GitLab's"
  # the web server can lag GitLab by an hour, so the alert waits 75 minutes
  alert merge-requests "$failing" 4500 86400 "$MESSAGE_PREFIX $text"
}

# check_render: renders the site as it should be, and checks it.
check_render() {
  local failing=0
  local values_sum=""
  local inputs_hash=""

  values_sum=$(build_values)
  inputs_hash=$(printf '%s\n' "$(sha256sum <"$RUN_DIR/status.json")" "$values_sum" \
    | sha256sum | cut -c1-64)
  render_expected "$release" "$commit" "$inputs_hash"
  check_addresses
  "$DEPLOY/checks.py" site "$EXPECTED_DIR" "$RUN_DIR/addresses" \
    "$release/deploy/site-hosts" || failing=1
  check unlisted "$failing" "the render shows an address or a link it may not"
}

main() {
  local version=""
  local commit=""
  local release=""
  local status_circuit=""
  local page_circuit=""

  begin_run
  check_keys
  # two new circuits, so the web server cannot tie the pages to the request
  # for status.json, which marks the monitor
  status_circuit=$(new_circuit)
  page_circuit=$(new_circuit)
  read_status "$status_circuit"
  take_release
  check_site_commit
  check_snapshot
  check_render
  compare_pages "$page_circuit"
  end_run
}

main "$@"
