#!/bin/sh
# shellcheck shell=dash
# Checks the live site against what it should be. It reads what the site
# says it was built from, checks each part against the backend's own
# sources, renders the same pages again with the signed release, and
# compares them byte for byte.
#
# Runs as: ccs-monitor.service, as a temporary user, every 15 to 25 minutes.
#          Its own network holds nothing, and every request goes through
#          Tor's socket (ALL_PROXY), so the wallet's RPC stays out of reach.
# Reads:   the live site, GitLab's merge request API, the signed releases,
#          ccs-back's checkout of the proposals, and the backend's values.
# Writes:  its state folder, which systemd makes (StateDirectory=).
# Alerts:  each step below names its alert. A crash raises "monitor".
# Exit:    0 when the run ends, whatever it found; it reports through alerts.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

SCRIPT_DIR=$(dirname -- "$0")
# the physical path, so a run that overlaps an install stays in one release
DEPLOY=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

# this backend's own release, /usr/local/lib/ccs/releases/<N>
readonly INSTALLED_DIR="${DEPLOY%/deploy}"
readonly INSTALLED_VERSION="${INSTALLED_DIR##*/}"
readonly STATE_DIR="${STATE_DIRECTORY:?}"
readonly RUN_DIR="$STATE_DIR/run"
readonly ALERTS_DIR="$STATE_DIR/alerts"
readonly EXPECTED_DIR="$STATE_DIR/expected"
readonly PROPOSALS_CHECKOUT="$CCS_BACK/proposals"
readonly BACKEND_VALUES=/var/lib/ccs-backend/values
readonly MESSAGE_PREFIX="ccs monitor:"
# what Tor Browser asks for a page; keep it to each Tor Browser major release
readonly BROWSER_AGENT="Mozilla/5.0 (Windows NT 10.0; rv:140.0) Gecko/20100101 Firefox/140.0"
readonly BROWSER_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
readonly PAGE_DIFFERS=" differs from the expected render"
NOW=$(date +%s)
readonly NOW

# ccs-back's checkout belongs to another user, so git is told to trust it
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0="$PROPOSALS_CHECKOUT"

# check <name> <failing> <text>: a monitor check. It alerts after failing for
# ten minutes, so on its second failing run, and reminds once a day.
check() {
  local name="$1"
  local failing="$2"
  local text="$3"

  alert "$name" "$failing" 600 86400 "$MESSAGE_PREFIX $text"
}

# require <name> <failing> <text>: a check the rest of the run depends on. A
# failure ends the run here.
require() {
  local name="$1"
  local failing="$2"
  local text="$3"

  check "$name" "$failing" "$text"
  if [ "$failing" != 0 ]; then
    end_run
  fi
}

# begin_run: clears the last run's files, and makes any crash from here on
# raise the "monitor" alert.
begin_run() {
  rm -rf -- "$RUN_DIR"
  mkdir -p -- "$RUN_DIR" "$STATE_DIR/releases"
  alerts_begin
  trap 'check monitor 1 "the monitor crashed"; alerts_send' EXIT
  # dash runs no EXIT trap when a signal kills it, so a stop from systemd
  # exits instead, which runs it
  trap 'exit 1' TERM
}

# end_run: sends this run's messages, and exits. A run that gets here didn't
# crash, so the "monitor" check passes.
end_run() {
  check monitor 0 "the monitor crashed"
  trap - EXIT
  alerts_send
  rm -rf -- "$RUN_DIR"
  exit 0
}

# new_circuit: prints a SOCKS user name never used before. Tor builds a
# separate circuit for each name, so the web server can't tie one set of
# requests to another by the exit they leave from.
new_circuit() {
  od -An -N8 -tx1 /dev/urandom | tr -d ' \n'
}

# fetch_url <url> <file> <circuit> [<curl option>...]: downloads one file
# from the live site, through Tor on that circuit when the unit sets
# ALL_PROXY.
fetch_url() {
  local url="$1"
  local file="$2"
  local circuit="$3"
  shift 3

  if [ -n "${ALL_PROXY:-}" ]; then
    set -- "$@" --proxy "$ALL_PROXY" --proxy-user "$circuit:x"
  fi
  curl -fsS -L --max-time 60 --max-filesize 200000000 "$@" \
    -o "$file" "$url"
}

# fetch_page <url> <file> <circuit>: downloads one page as Tor Browser asks
# for it, so a web server that tells visitors apart by their request can't
# single out the monitor that way.
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

# read_site_status <circuit>: prints "<release> <commit>", what the site's
# status.json says it was built from. The web server isn't trusted, so
# checks.py checks both fields.
read_site_status() {
  local circuit="$1"

  fetch_url "$SITE/status.json" "$RUN_DIR/status.json" "$circuit" \
    || return 1
  "$DEPLOY/checks.py" status "$RUN_DIR/status.json" || return 1
}

# get_verified_release <version>: prints the folder of that release,
# downloaded and verified as install.sh verifies it, the first time it is
# needed.
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
# release that differs from this backend's in more than the backend's
# scripts and the docs, which the web server never runs. So the site can't
# fall back to a fixed bug, and no older render runs here.
is_recent_enough() {
  local version="$1"
  local release="$2"

  if [ "$version" -ge "$INSTALLED_VERSION" ]; then
    return 0
  fi
  diff -rq -x backend -x '*.md' "$release" "$INSTALLED_DIR" >/dev/null
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
# set of merge requests within the hour. After an hour it is asked again, so
# a set that stops changing is still compared.
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

# verify_merge_requests <release> <snapshot> <snapshot sum>: compares the
# site's merge requests with GitLab's, and prints what a failure means.
# Fails when they differ, or when GitLab doesn't answer.
verify_merge_requests() {
  local release="$1"
  local snapshot="$2"
  local snapshot_sum="$3"
  local gitlab=""

  if is_recently_verified "$snapshot_sum"; then
    printf '%s\n' "the site's merge requests differ from GitLab's"
    return 0
  fi
  mkdir -p -- "$RUN_DIR/gitlab/mr" || return 1
  if ! "$DEPLOY/mrs.py" "$GITLAB_API" "$RUN_DIR/gitlab/mr" \
    "$STATE_DIR/changes" 2>/dev/null; then
    printf '%s %s\n' "GitLab's merge request API doesn't answer," \
      "so the site's merge requests are unverified"
    return 1
  fi
  printf '%s\n' "the site's merge requests differ from GitLab's"
  gitlab=$(status_py "$release" mrs "$RUN_DIR/gitlab") || return 1
  if [ "$gitlab" != "$snapshot" ]; then
    return 1
  fi
  printf '%s %s\n' "$snapshot_sum" "$NOW" >"$STATE_DIR/mrs-verified"
}

# build_values: builds the render's values from the backend's latest file,
# as the web server builds them, and prints their hash. A site showing other
# values fails the page compare, index.php/projects first.
build_values() {
  if ! "$DEPLOY/checks.py" values "$BACKEND_VALUES" 0 >"$RUN_DIR/checked"; then
    : >"$RUN_DIR/checked" || return 1
  fi
  render_values "$RUN_DIR/checked" "$RUN_DIR/in" "$RUN_DIR/addresses"
}

# render_expected <release> <commit> <inputs-hash>: renders the pages as
# they should be into EXPECTED_DIR, but only when <inputs-hash>, a hash of
# everything the render reads, changed since the last render.
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
  python3 -I -B "$release/deploy/render/render.py" "$RUN_DIR/in" \
    "$release/dist/templates" "$EXPECTED_DIR"
  printf '%s\n' "$inputs_hash" >"$STATE_DIR/rendered"
}

# check_addresses: alerts when the address list doesn't verify, or when a
# proposal that takes donations shows no address. Either fix is a newly
# signed list.
check_addresses() {
  local missing=""
  local failing=0

  if [ ! -s "$RUN_DIR/addresses" ]; then
    check addresses 1 \
      "the address list doesn't verify, so no donation address shows"
    return 0
  fi
  # grep fails when every proposal shows an address, and when a page can't
  # be read, yet still prints the matches; either way its status is ignored
  missing=$(cd -- "$EXPECTED_DIR" \
    && { grep -lF 'class="no-address"' proposals/*.html || true; } \
    | paste -sd' ' -)
  if [ -n "$missing" ]; then
    failing=1
  fi
  check addresses "$failing" \
    "proposals take donations with no address on the list: $missing"
}

# list_always: prints the files compared on every run: the stage lists, the
# donate page, index.php/projects, each QR code, and each page showing one.
list_always() {
  local qr=""
  local name=""

  printf '%s\n' index.html ideas/index.html funding-required/index.html \
    work-in-progress/index.html completed-proposals/index.html \
    donate/index.html index.php/projects
  for qr in "$EXPECTED_DIR"/qr/*.png; do
    if [ ! -e "$qr" ]; then
      continue
    fi
    name="${qr##*/}"
    printf 'qr/%s\n' "$name"
    # grep fails when no page shows the code, which is no failure
    (cd -- "$EXPECTED_DIR" && { grep -lF "${name%.png}" proposals/*.html || true; })
  done
}

# list_sample <file-list>: prints the next ten files of the list, from where the
# last run stopped, so that every file is compared within a few hours.
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
  # ten lines from the offset on, carrying on from the top at the end
  LC_ALL=C awk -v start="$offset" -v count=10 '
    { line[NR - 1] = $0 }
    END { for (i = 0; i < count && i < NR; i++) print line[(start + i) % NR] }
  ' "$file_list"
  next=$(((offset + 10) % total))
  printf '%s\n' "$next" >"$STATE_DIR/sample-offset"
}

# page_in_alert <state file>: prints the path a page check's alert names.
# compare_page writes its text as "<MESSAGE_PREFIX> page <path><PAGE_DIFFERS>".
page_in_alert() {
  local state="$1"
  local text=""
  local path=""

  # the line is "<since> <sent> <text>"
  read -r _ _ text <"$state" || return 1
  path="${text#"$MESSAGE_PREFIX page "}"
  path="${path%"$PAGE_DIFFERS"}"
  printf '%s\n' "$path"
}

# list_failing <file-list> <always-list>: prints each failing page, so it is
# compared again until it passes. One that left the site resolves, or is
# forgotten if it never alerted.
list_failing() {
  local file_list="$1"
  local always_list="$2"
  local state=""
  local path=""

  for state in "$ALERTS_DIR"/page-*; do
    if [ ! -e "$state" ]; then
      continue
    fi
    path=$(page_in_alert "$state") || return 1
    if grep -qxF -- "$path" "$file_list" "$always_list"; then
      printf '%s\n' "$path"
    else
      check "${state##*/}" 0 "page $path left the site" || return 1
    fi
  done
}

# compare_page <path> <circuit>: fetches one file from the live site, and
# checks it against the expected render, byte for byte. Its check is named
# by a hash of the path.
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

main() {
  local failing=0
  local status=""
  local version=""
  local commit=""
  local release=""
  local snapshot=""
  local snapshot_sum=""
  local text=""
  local values_sum=""
  local inputs_hash=""
  local status_circuit=""
  local page_circuit=""

  begin_run

  # 1. No signer's key stops signing within 30 days. It doesn't depend on the
  #    site, so it runs before anything can end the run.
  failing=0
  text=$("$DEPLOY/checks.py" expiring /etc/ccs/release-signers.gpg \
    /etc/ccs/address-signers.gpg 2>&1) || failing=1
  check key-expiry "$failing" "${text:-a signing key expires within 30 days}"

  # 2. What the site says it was built from. status.json and the pages are
  #    fetched over two new circuits, so the web server can't tie the pages
  #    to the request that marks the monitor.
  status_circuit=$(new_circuit)
  page_circuit=$(new_circuit)
  failing=0
  status=$(read_site_status "$status_circuit") || failing=1
  require status "$failing" "status.json is missing or malformed"
  version="${status% *}"
  commit="${status#* }"

  # 3. The release it names, and that it is no older than ours.
  failing=0
  release=$(get_verified_release "$version") || failing=1
  require release "$failing" "release $version doesn't download or verify"
  keep "$STATE_DIR/releases/" "$release" "$release"
  failing=0
  is_recent_enough "$version" "$release" || failing=1
  text="the site runs release $version, older than release $INSTALLED_VERSION"
  require old-release "$failing" "$text here"

  # 4. The proposals commit is ccs-back's, and not an hour behind it.
  failing=0
  git -C "$PROPOSALS_CHECKOUT" cat-file -e "$commit^{commit}" 2>/dev/null \
    || failing=1
  require commit "$failing" \
    "the site's commit $commit isn't in ccs-back's checkout"
  check_stall "$commit"

  # 5. The merge requests match GitLab's. The web server can lag GitLab by up
  #    to an hour, so only a mismatch lasting 75 minutes alerts.
  failing=0
  "$DEPLOY/checks.py" snapshot "$RUN_DIR/status.json" || failing=1
  status_py "$release" restore "$RUN_DIR/status.json" "$RUN_DIR/in" \
    || failing=1
  snapshot=$(status_py "$release" mrs "$RUN_DIR/in") || failing=1
  require snapshot "$failing" "status.json's merge requests are malformed"
  snapshot_sum=$(printf '%s' "$snapshot" | sha256sum | cut -c1-64)
  failing=0
  text=$(verify_merge_requests "$release" "$snapshot" "$snapshot_sum") \
    || failing=1
  alert merge-requests "$failing" 4500 86400 "$MESSAGE_PREFIX $text"

  # 6. The backend's latest values, built as the web server builds them.
  values_sum=$(build_values)

  # 7. The pages as they should be.
  inputs_hash=$(printf '%s\n' "$version" "$commit" "$snapshot_sum" "$values_sum" \
    | sha256sum | cut -c1-64)
  render_expected "$release" "$commit" "$inputs_hash"

  # 8. Every proposal that takes donations shows a signed address.
  check_addresses

  # 9. The pages as they should be show only listed addresses.
  failing=0
  "$DEPLOY/checks.py" site "$EXPECTED_DIR" "$RUN_DIR/addresses" || failing=1
  check unlisted "$failing" "the render shows an address the signed list lacks"

  # 10. The live site shows the same pages.
  compare_pages "$page_circuit"

  end_run
}

main "$@"
