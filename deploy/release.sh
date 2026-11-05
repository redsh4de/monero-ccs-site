#!/bin/sh
# shellcheck shell=dash
# Builds a release from a tag the same way on every builder's machine,
# compares it with each attestation already made, shows what changed since
# the last release, and attests it: a signature over SHA256SUMS. Once two
# release signers have attested, it also joins their signatures into the
# release's SHA256SUMS.asc. See deploy/RELEASING.md.
#
#   KEYRING=~/release-signers.gpg deploy/release.sh site-vN <your name>
#
# Runs as: a builder, anyone, by hand, from a checkout of the tag, in an
#          account with rootless Podman (or Docker, with CONTAINER=docker).
# Reads:   the tag; its attestations on ATTESTATIONS_REF, origin/main unless
#          set; release N-1 from GitHub; and the release signers' keyring in
#          KEYRING.
# Writes:  release-site-vN/: ccs-site-vN.tar, SHA256SUMS, the attestation to
#          add to the repository, and SHA256SUMS.asc once two release
#          signers have attested.
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
# made absolute in main, since gpgv looks a bare name up in ~/.gnupg
KEYRING="${KEYRING:-$HOME/release-signers.gpg}"
readonly ATTESTATIONS_REF="${ATTESTATIONS_REF:-origin/main}"
readonly USAGE="usage: release.sh site-vN <your name, in a-z 0-9 . _ ->"

# main sets these two once: the scratch folder, and the tag's commit time,
# which every file in the build gets
WORK=""
EPOCH=""

# is_builder_name <name>: true for a name that is safe as a folder name.
is_builder_name() {
  local name="$1"

  case $name in
    "" | .* | *[!a-z0-9._-]*) return 1 ;;
  esac
}

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
  # a dependency can't slip a script file, a link elsewhere or an address
  # into the site: checks.py refuses the build before anyone reads it
  "$DEPLOY/checks.py" build "$WORK/src/dist" "$WORK/src/deploy/build-hosts" \
    || stop "the build holds what a release may not, so it isn't signed"
}

# pack_release <out-dir> <tag>: writes the release's archive and SHA256SUMS
# into a new folder <out-dir>. One already there, even one the tag holds,
# stops the run. SHA256SUMS is sha256sum's own output, so anyone can check
# the archive with sha256sum --check.
pack_release() {
  local out_dir="$1"
  local tag="$2"
  local archive="ccs-$tag.tar"

  mkdir -- "$out_dir" || stop "$out_dir already exists; move it away first"
  tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
    --mode=u=rwX,go=rX --format=gnu -cf "$out_dir/$archive" \
    -C "$WORK/src" dist deploy
  (cd -- "$out_dir" && sha256sum -- "$archive") >"$out_dir/SHA256SUMS"
  cat -- "$out_dir/SHA256SUMS"
}

# get_attestations <tag> <dir>: copies each builder's attestation of the tag
# into <dir>/<builder>/. They live in this repository's attestations/ folder,
# on ATTESTATIONS_REF. Moving them to a repository of their own, with the
# same <tag>/<builder>/ layout, changes only this function: it would clone
# that repository and copy its <tag>/ folder instead.
get_attestations() {
  local tag="$1"
  local dir="$2"

  mkdir -p -- "$dir"
  if ! git cat-file -e "$ATTESTATIONS_REF:attestations/$tag" 2>/dev/null; then
    return 0
  fi
  git archive "$ATTESTATIONS_REF" "attestations/$tag" \
    | tar -x -C "$dir" --strip-components=2
}

# is_plain_attestation <folder>: true when the folder holds SHA256SUMS and
# SHA256SUMS.asc as plain files. Anyone adds attestations, so a link, which
# could point anywhere on this machine, is never followed.
is_plain_attestation() {
  local folder="${1%/}"
  local name=""

  if [ -L "$folder" ]; then
    return 1
  fi
  for name in SHA256SUMS SHA256SUMS.asc; do
    if [ -L "$folder/$name" ] || [ ! -f "$folder/$name" ]; then
      return 1
    fi
  done
}

# is_release_signer <folder>: true when the attestation in the folder carries
# a good signature from a key in KEYRING.
is_release_signer() {
  local folder="$1"

  gpgv --keyring "$KEYRING" "$folder/SHA256SUMS.asc" "$folder/SHA256SUMS" \
    >/dev/null 2>&1
}

# compare_attestations <out-dir> <attestations>: compares this build with
# each builder's attestation. A release signer whose build differs stops the
# run; anyone else's is only reported. The signatures of the release signers
# who built the same bytes are collected in WORK/trusted.asc.
compare_attestations() {
  local out_dir="$1"
  local attestations="$2"
  local folder=""
  local builder=""

  : >"$WORK/trusted.asc"
  for folder in "$attestations"/*/; do
    if ! is_plain_attestation "$folder"; then
      continue
    fi
    builder=$(basename -- "$folder")
    if is_release_signer "$folder"; then
      if ! cmp -s "$folder/SHA256SUMS" "$out_dir/SHA256SUMS"; then
        stop "this build differs from $builder's; don't sign it"
      fi
      cat -- "$folder/SHA256SUMS.asc" >>"$WORK/trusted.asc"
      printf 'The same bytes as %s, a release signer.\n' "$builder"
    elif cmp -s "$folder/SHA256SUMS" "$out_dir/SHA256SUMS"; then
      printf 'The same bytes as %s.\n' "$builder"
    else
      log "$builder, who isn't a release signer, built different bytes"
    fi
  done
}

# list_packages <tag>: prints name@version for each package in the tag's
# lockfile, sorted. Each is a key, two spaces in, of its packages: section.
list_packages() {
  local tag="$1"

  git show "$tag:pnpm-lock.yaml" \
    | sed -n '/^packages:$/,/^[^ ]/p' \
    | sed -n "s/^  '\{0,1\}\([^' ]*\)'\{0,1\}:\$/\1/p" \
    | LC_ALL=C sort
}

# show_packages <tag> <previous-tag>: lists the packages the lockfile
# dropped (-) and added (+) since the release before, so a reviewer sees
# what changed before reading what it did. A new version shows as both.
show_packages() {
  local tag="$1"
  local previous_tag="$2"
  local dropped=""
  local added=""

  if [ -z "$previous_tag" ]; then
    return 0
  fi
  if ! git rev-parse -q --verify "$previous_tag^{commit}" >/dev/null; then
    printf 'Packages: there is no tag %s here to compare with.\n' \
      "$previous_tag"
    return 0
  fi
  list_packages "$previous_tag" >"$WORK/packages.old"
  list_packages "$tag" >"$WORK/packages.new"
  dropped=$(LC_ALL=C comm -23 "$WORK/packages.old" "$WORK/packages.new")
  added=$(LC_ALL=C comm -13 "$WORK/packages.old" "$WORK/packages.new")
  printf 'Packages since %s:\n' "$previous_tag"
  if [ -z "$dropped$added" ]; then
    printf '  none changed\n'
    return 0
  fi
  printf '%s\n' "$dropped" | sed '/^$/d; s/^/  - /'
  printf '%s\n' "$added" | sed '/^$/d; s/^/  + /'
}

# show_changes <out-dir> <tag> <previous-tag>: shows what changed since the
# release before, in less. deploy/ runs as root, so it is compared line by
# line. dist/ is compared word by word, so a planted CSS declaration stands out
# in a minified stylesheet, and the low rename threshold pairs a stylesheet
# with its renamed successor. cat -v shows control and non-ASCII bytes as text,
# so nothing in the build can hide itself on the terminal.
show_changes() {
  local out_dir="$1"
  local tag="$2"
  local previous_tag="$3"

  tar -xf "$out_dir/ccs-$tag.tar" -C "$WORK/new"
  printf 'Changes since %s:\n' "${previous_tag:-nothing}"
  # a signer may quit less before the end, which stops the commands
  # writing to it; the run carries on to the question
  {
    show_packages "$tag" "$previous_tag"
    git -C "$WORK" diff --no-index --no-color --no-ext-diff --no-textconv \
      --patch-with-stat old/files/deploy new/deploy || true
    git -C "$WORK" diff --no-index --no-color --no-ext-diff --no-textconv \
      --patch-with-stat -M10% --word-diff=plain \
      --word-diff-regex='[^;{}[:space:]]+' old/files/dist new/dist || true
  } | cat -v | LESSSECURE=1 less || true
}

# sign_release <tag> <out-dir> <version> <builder>: asks the builder, signs
# SHA256SUMS, and checks the signature before anything leaves this machine.
# It then writes the attestation, and joins the release signers' signatures.
sign_release() {
  local tag="$1"
  local out_dir="$2"
  local version="$3"
  local builder="$4"
  local answer=""
  local folder="$out_dir/attestations/$tag/$builder"

  printf 'Sign %s? [y/N] ' "$out_dir/SHA256SUMS"
  read -r answer || answer=""
  case $answer in
    y | Y) ;;
    *)
      echo "Not signed."
      exit 0
      ;;
  esac
  gpg --detach-sign --armor -o "$WORK/mine.asc" "$out_dir/SHA256SUMS"
  if ! gpg --verify "$WORK/mine.asc" "$out_dir/SHA256SUMS" \
    >/dev/null 2>&1; then
    stop "your signature doesn't verify with your own key"
  fi
  if ! gpgv --keyring "$KEYRING" "$WORK/mine.asc" "$out_dir/SHA256SUMS" \
    >/dev/null 2>&1; then
    log "your key isn't in $KEYRING, so this attests the build," \
      "but doesn't count towards the two release signers"
  fi

  mkdir -p -- "$folder"
  cp -- "$out_dir/SHA256SUMS" "$folder/SHA256SUMS"
  cp -- "$WORK/mine.asc" "$folder/SHA256SUMS.asc"
  printf 'Your attestation is in %s/. Add it to the repository under %s.\n' \
    "$folder" "attestations/$tag/$builder/"
  join_signatures "$tag" "$out_dir" "$version"
}

# join_signatures <tag> <out-dir> <version>: joins the release signers'
# signatures over this build, and this one, into SHA256SUMS.asc. It keeps
# the file only when it passes exactly what install.sh checks: two release
# signers, this version.
join_signatures() {
  local tag="$1"
  local out_dir="$2"
  local version="$3"

  cat -- "$WORK/trusted.asc" "$WORK/mine.asc" >"$out_dir/SHA256SUMS.asc"
  if "$DEPLOY/checks.py" release "$out_dir" "$KEYRING" "$version" \
    "$WORK/verify"; then
    printf 'Create the GitHub release %s with ccs-%s.tar, SHA256SUMS and %s\n' \
      "$tag" "$tag" "SHA256SUMS.asc, from $out_dir/."
  else
    rm -- "$out_dir/SHA256SUMS.asc"
    echo "Not ready to publish: the release needs two release signers'" \
      "attestations."
  fi
}

main() {
  local tag="${1:-}"
  local builder="${2:-}"
  local version=""
  local previous_tag=""
  local previous_version=0
  local image=""
  local out_dir=""

  version=$(release_tag "$tag") || stop "$USAGE"
  is_builder_name "$builder" || stop "$USAGE"
  check_gnu_tar
  if [ ! -e "$KEYRING" ]; then
    stop "set KEYRING to the release signers' keyring"
  fi
  KEYRING=$(realpath -- "$KEYRING")
  readonly KEYRING
  EPOCH=$(git log -1 --format=%ct "$tag")
  readonly EPOCH
  WORK=$(mktemp -d)
  readonly WORK
  trap 'rm -rf -- "$WORK"' EXIT
  mkdir -p -- "$WORK/src" "$WORK/pnpm" "$WORK/image" "$WORK/new" \
    "$WORK/old" "$WORK/attestations"

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

  # 3. The build: the archive and SHA256SUMS.
  image=$(build_image) || stop "can't build the image to build in"
  build_site "$tag" "$image"
  out_dir="release-$tag"
  pack_release "$out_dir" "$tag"

  # 4. The same bytes as every release signer who attested already, the
  #    review, and the attestation.
  get_attestations "$tag" "$WORK/attestations"
  compare_attestations "$out_dir" "$WORK/attestations"
  show_changes "$out_dir" "$tag" "$previous_tag"
  sign_release "$tag" "$out_dir" "$version" "$builder"
}

main "$@"
