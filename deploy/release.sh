#!/bin/sh
# shellcheck shell=dash
# Builds a release from a tag the same way on every signer's machine, shows
# what changed since the last release, and signs it. See deploy/RELEASING.md.
#
#   KEYRING=~/release-signers.gpg deploy/release.sh site-vN
#
# Runs as: a signer, by hand, from a checkout of the tag, in an account
#          with rootless Podman (or Docker, with CONTAINER=docker).
# Reads:   the tag, release N-1 from GitHub, and the release signers'
#          keyring in KEYRING.
# Writes:  release-site-vN/: release.tar, release.sha256, and the signature.
# Trust:   only the tag's own scripts run, in a pinned container that builds
#          offline; deploy/ comes from the tag.
# Exit:    0 signed or declined, 1 on any failure.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

SCRIPT_DIR=$(dirname -- "$0")
DEPLOY=$(cd -- "$SCRIPT_DIR" && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"
# the signer's own git settings stay out of the archive and the review
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1

# pinned: pnpm as packageManager names it, and StageX's Node and C++ runtime
# of one StageX release
readonly PNPM_VERSION=11.4.0
readonly PNPM_URL="https://registry.npmjs.org/pnpm/-/pnpm-$PNPM_VERSION.tgz"
readonly PNPM_SHA512=f0febc7e37552ab485494a914241b338e0b3580b93d54ce31f00933015880863129038a1b4ae4e414a0ee63ac35bf21197e990172c4a68256450b5636310968f
readonly NODE=docker.io/stagex/pallet-nodejs@sha256:2a555068fb2e4c9a6fc3bec8fdce265a1ea1206a67ad3dc71083ef7b6f091501
readonly GCC=docker.io/stagex/core-gcc@sha256:396679821776a7da0e5806e9ce78fd92113452a0977d90956f62a7caa32520db
readonly CONTAINER="${CONTAINER:-podman}"
readonly KEYRING="${KEYRING:-$HOME/release-signers.gpg}"

# main sets these two once: the scratch folder, and the tag's commit time,
# which every file in the build gets
WORK=""
EPOCH=""

# check_gnu_tar: stops unless tar is GNU tar, whose options make the
# archive byte-identical on every machine.
check_gnu_tar() {
  local version=""

  version=$(tar --version 2>/dev/null) || version=""
  case $version in
    *"GNU tar"*) ;;
    *) stop "needs GNU tar, for a byte-identical archive" ;;
  esac
}

# check_same_scripts <tag>: stops unless release.sh, lib.sh and checks.py
# are the tag's own. Both signers then run the scripts they read first, and
# build with the same pins and the tag's pnpm. release.sh is compared as the
# file that runs, $0.
check_same_scripts() {
  local tag="$1"
  local name=""

  # cmp stops at the first difference, which also fails git show: either
  # way the scripts differ
  if ! git show "$tag:deploy/release.sh" | cmp -s - "$0"; then
    stop "run the deploy/ of $tag"
  fi
  for name in lib.sh checks.py; do
    if ! git show "$tag:deploy/$name" | cmp -s - "$DEPLOY/$name"; then
      stop "run the deploy/ of $tag"
    fi
  done
}

# check_pnpm_pin <tag>: stops unless the tag's package.json names the pnpm
# pinned here.
check_pnpm_pin() {
  local tag="$1"
  local package_json=""

  package_json=$(git show "$tag:package.json")
  case $package_json in
    *"\"packageManager\": \"pnpm@$PNPM_VERSION\""*) ;;
    *) stop "the packageManager of $tag isn't pnpm@$PNPM_VERSION" ;;
  esac
}

# fetch_pnpm: downloads pnpm, checks it against its pinned hash, and
# unpacks it into WORK/pnpm.
fetch_pnpm() {
  local sum=""

  curl -fsS -o "$WORK/pnpm.tgz" "$PNPM_URL"
  sum=$(sha512sum <"$WORK/pnpm.tgz" | cut -d' ' -f1)
  if [ "$sum" != "$PNPM_SHA512" ]; then
    stop "pnpm doesn't match its hash"
  fi
  tar -xzf "$WORK/pnpm.tgz" -C "$WORK/pnpm"
}

# get_previous_release <tag> <version>: downloads release N-1 into WORK/old,
# and verifies it, by its number: a tag's ancestry can skip releases or lack
# their tags.
get_previous_release() {
  local tag="$1"
  local version="$2"

  get_release "$tag" "$WORK/old" || stop "can't download $tag"
  if ! "$DEPLOY/checks.py" release "$WORK/old" "$KEYRING" "$version" \
    "$WORK/old/files"; then
    stop "$tag doesn't verify against $KEYRING"
  fi
}

# build_image: builds the image the site is built in, and prints its ID.
# StageX's Node lacks the two libraries that npm's prebuilt native modules
# load, so they are copied in from StageX's GCC.
build_image() {
  printf 'FROM %s AS gcc\nFROM %s\n' "$GCC" "$NODE" \
    >"$WORK/image/Containerfile" || return 1
  printf 'COPY --from=gcc %s %s /usr/lib/\n' /usr/lib/libgcc_s.so.1 \
    /usr/lib/libstdc++.so.6 >>"$WORK/image/Containerfile" || return 1
  "$CONTAINER" build -q -f "$WORK/image/Containerfile" "$WORK/image"
}

# run_node <image> <network> <arguments...>: runs Node in the image, by its
# ID so nothing can swap it under a name meanwhile, on the source in
# WORK/src. <network> is "online" or "offline".
run_node() {
  local image="$1"
  local network="$2"
  local isolation="--network=none"
  shift 2

  if [ "$network" = online ]; then
    isolation=""
  fi
  # shellcheck disable=SC2086 # $isolation is one option, or none
  "$CONTAINER" run --rm $isolation -e HOME=/tmp \
    -e SOURCE_DATE_EPOCH="$EPOCH" -e ASTRO_TELEMETRY_DISABLED=1 \
    -v "$WORK/src:/src:Z" -v "$WORK/pnpm:/pnpm:ro,Z" -w /src \
    --entrypoint node "$image" "$@"
}

# list_control_bytes <dir>: prints each text file under <dir>'s dist/ and
# deploy/ that holds a control byte; fonts and images are the only binary
# files. Fails when grep can't read the build, so a scan that didn't run
# never passes.
list_control_bytes() {
  local dir="$1"
  local status=0

  LC_ALL=C grep -rlaP '[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]' \
    "$dir/dist" "$dir/deploy" --exclude='*.avif' --exclude='*.webp' \
    --exclude='*.png' --exclude='*.woff2' || status=$?
  # 1 means no file holds one
  [ "$status" -le 1 ]
}

# build_site <tag> <image>: builds the site from the tag's source into
# WORK/src, and stops on a control byte in any text file, so nothing can
# hide itself in the review.
build_site() {
  local tag="$1"
  local image="$2"
  local bad=""

  git archive "$tag" | tar -x -C "$WORK/src"
  run_node "$image" online /pnpm/package/bin/pnpm.cjs install \
    --frozen-lockfile --prod --ignore-scripts
  # the build needs no network, so no dependency can reach out while it runs
  run_node "$image" offline node_modules/astro/bin/astro.mjs build
  # deploy/ runs as root on both machines: take it from the tag, not from
  # the tree the build could write to
  rm -rf -- "$WORK/src/deploy"
  git archive "$tag" deploy | tar -x -C "$WORK/src"

  bad=$(list_control_bytes "$WORK/src") \
    || stop "can't scan the build for control bytes"
  if [ -n "$bad" ]; then
    stop "a control byte in a text file, so this build isn't signed: $bad"
  fi
}

# pack_release <out-dir> <version> <commit>: writes release.tar and
# release.sha256 into a new folder <out-dir>. One already there, even one the
# tag holds, stops the run.
pack_release() {
  local out_dir="$1"
  local version="$2"
  local commit="$3"
  local sum=""

  mkdir -- "$out_dir" || stop "$out_dir already exists; move it away first"
  tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
    --mode=u=rwX,go=rX --format=gnu -cf "$out_dir/release.tar" \
    -C "$WORK/src" dist deploy
  sum=$(sha256sum <"$out_dir/release.tar" | cut -d' ' -f1)
  printf 'version %s\ncommit %s\nsha256 %s\n' "$version" "$commit" "$sum" \
    >"$out_dir/release.sha256"
  cat -- "$out_dir/release.sha256"
}

# check_published <tag> <out-dir>: when the first signer published this tag
# already, stops unless this build matches theirs, and downloads their
# signature. Only a missing file means nothing is published; any other
# answer stops.
check_published() {
  local tag="$1"
  local out_dir="$2"
  local url="$RELEASES/download/$tag"
  local code=""

  code=$(curl -sS -L -o "$WORK/published/release.sha256" -w '%{http_code}' \
    "$url/release.sha256") || stop "can't reach $RELEASES"
  case $code in
    404) ;;
    200)
      if ! cmp -s "$WORK/published/release.sha256" "$out_dir/release.sha256"; then
        stop "this build differs from the published $tag; don't sign it"
      fi
      curl -fsS -L -o "$WORK/published/release.sha256.asc" \
        "$url/release.sha256.asc" \
        || stop "can't download the published signature"
      ;;
    *) stop "$RELEASES answered $code for $tag" ;;
  esac
}

# show_changes <out-dir> <previous-tag>: shows what changed since the release
# before, in less. deploy/ runs as root, so it is compared line by line.
# dist/ is compared word by word, so a planted CSS declaration stands out in
# a minified stylesheet, and the low rename threshold pairs a stylesheet
# with its renamed successor. cat -v shows control and non-ASCII bytes as
# text, so nothing in the build can hide itself on the terminal.
show_changes() {
  local out_dir="$1"
  local previous_tag="$2"

  tar -xf "$out_dir/release.tar" -C "$WORK/new"
  printf 'Changes since %s:\n' "${previous_tag:-nothing}"
  # a signer may quit less before the end, which stops the commands
  # writing to it; the run carries on to the question
  {
    git -C "$WORK" diff --no-index --no-color --no-ext-diff --no-textconv \
      --patch-with-stat old/files/deploy new/deploy || true
    git -C "$WORK" diff --no-index --no-color --no-ext-diff --no-textconv \
      --patch-with-stat -M10% --word-diff=plain \
      --word-diff-regex='[^;{}[:space:]]+' old/files/dist new/dist || true
  } | cat -v | LESSSECURE=1 less || true
}

# sign_release <tag> <out-dir> <version>: asks the signer, signs release.sha256,
# and checks the signatures before anything is uploaded. The second signer's
# joined file must pass exactly what install.sh checks: two signers in the
# keyring, this version.
sign_release() {
  local tag="$1"
  local out_dir="$2"
  local version="$3"
  local answer=""
  local keyring=""

  printf 'Sign %s? [y/N] ' "$out_dir/release.sha256"
  read -r answer || answer=""
  case $answer in
    y | Y) ;;
    *)
      echo "Not signed."
      exit 0
      ;;
  esac
  gpg --detach-sign --armor -o "$WORK/mine.asc" "$out_dir/release.sha256"

  if [ -s "$WORK/published/release.sha256.asc" ]; then
    cat -- "$WORK/published/release.sha256.asc" "$WORK/mine.asc" \
      >"$out_dir/release.sha256.asc"
    if ! "$DEPLOY/checks.py" release "$out_dir" "$KEYRING" "$version" \
      "$WORK/verify"; then
      rm -- "$out_dir/release.sha256.asc"
      stop "the joined signatures don't verify against $KEYRING"
    fi
    printf 'Replace release.sha256.asc on the %s release with %s.\n' \
      "$tag" "$out_dir/release.sha256.asc"
  else
    keyring=$(realpath -- "$KEYRING")
    if ! gpgv --keyring "$keyring" "$WORK/mine.asc" "$out_dir/release.sha256" \
      >/dev/null 2>&1; then
      stop "your signature doesn't verify against $KEYRING:" \
        "sign with your key in it"
    fi
    cp -- "$WORK/mine.asc" "$out_dir/release.sha256.asc"
    printf 'Create the GitHub release %s, and upload the three files in %s/.\n' \
      "$tag" "$out_dir"
  fi
}

main() {
  local tag="${1:-}"
  local version=""
  local commit=""
  local previous_tag=""
  local previous_version=0
  local image=""
  local out_dir=""

  version=$(release_tag "$tag") || stop "usage: release.sh site-vN"
  check_gnu_tar
  if [ ! -e "$KEYRING" ]; then
    stop "set KEYRING to the release signers' keyring"
  fi
  commit=$(git rev-parse --verify "$tag^{commit}")
  EPOCH=$(git log -1 --format=%ct "$tag")
  readonly EPOCH
  WORK=$(mktemp -d)
  readonly WORK
  trap 'rm -rf -- "$WORK"' EXIT
  mkdir -p -- "$WORK/src" "$WORK/pnpm" "$WORK/image" "$WORK/new" \
    "$WORK/old" "$WORK/published"

  # 1. Only the tag's scripts, and the tag's pnpm.
  check_same_scripts "$tag"
  check_pnpm_pin "$tag"
  fetch_pnpm

  # 2. The release before, to compare with. The first is compared with
  #    nothing, so all of it shows.
  if [ "$version" = 1 ]; then
    mkdir -p -- "$WORK/old/files/deploy" "$WORK/old/files/dist"
  else
    previous_version=$((version - 1))
    previous_tag="site-v$previous_version"
    get_previous_release "$previous_tag" "$previous_version"
  fi

  # 3. The build, and the release's two files.
  image=$(build_image) || stop "can't build the image to build in"
  build_site "$tag" "$image"
  out_dir="release-$tag"
  pack_release "$out_dir" "$version" "$commit"

  # 4. The same bytes as the first signer's, the review, and the signature.
  check_published "$tag" "$out_dir"
  show_changes "$out_dir" "$previous_tag"
  sign_release "$tag" "$out_dir" "$version"
}

main "$@"
