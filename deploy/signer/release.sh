#!/bin/sh
# shellcheck shell=dash
# Builds a release from a tag, compares it with other builds, and attests it.
#
#   KEYRING=~/release-signers.gpg deploy/signer/release.sh site-vN <your name>
#
# Exit: 0 signed or declined, 1 on any failure.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail

# the signer's environment must change neither the build nor the review
# --clean is an argument, as the environment could set a variable
if [ "${1:-}" != --clean ]; then
  exec env -i HOME="$HOME" PATH="$PATH" ${TERM+"TERM=$TERM"} \
    ${GPG_TTY+"GPG_TTY=$GPG_TTY"} ${GNUPGHOME+"GNUPGHOME=$GNUPGHOME"} \
    ${XDG_RUNTIME_DIR+"XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"} \
    ${KEYRING+"KEYRING=$KEYRING"} \
    ${ATTESTATIONS_REF+"ATTESTATIONS_REF=$ATTESTATIONS_REF"} \
    ${RELEASES+"RELEASES=$RELEASES"} /bin/sh "$0" --clean "$@"
fi
shift

DEPLOY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"
# env -i keeps the signer's git config files in reach; these ignore them
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_ATTR_NOSYSTEM=1
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.attributesFile
export GIT_CONFIG_VALUE_0=/dev/null

# pnpm as packageManager names it; Node and GCC from one StageX release
readonly PNPM_VERSION=11.4.0
readonly PNPM_URL="https://registry.npmjs.org/pnpm/-/pnpm-$PNPM_VERSION.tgz"
readonly PNPM_SHA512=f0febc7e37552ab485494a914241b338e0b3580b93d54ce31f00933015880863129038a1b4ae4e414a0ee63ac35bf21197e990172c4a68256450b5636310968f
readonly NODE=docker.io/stagex/pallet-nodejs@sha256:2a555068fb2e4c9a6fc3bec8fdce265a1ea1206a67ad3dc71083ef7b6f091501
readonly GCC=docker.io/stagex/core-gcc@sha256:396679821776a7da0e5806e9ce78fd92113452a0977d90956f62a7caa32520db
# made absolute in main, since gpgv looks a bare name up in ~/.gnupg
KEYRING="${KEYRING:-$HOME/release-signers.gpg}"
# the remote's branch by its full name, which no tag can take
readonly ATTESTATIONS_REF="${ATTESTATIONS_REF:-refs/remotes/origin/main}"
readonly USAGE="usage: release.sh site-vN <your name, in a-z 0-9 . _ ->"

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
  case $(tar --version 2>/dev/null) in
    *"GNU tar"*) ;;
    *) stop "needs GNU tar, for a byte-identical archive" ;;
  esac
}

# check_plain_files <tag>: stops when the tag holds anything but plain files
# with plain names.
# A submodule, a .gitattributes or an odd name can hide a change from review.
check_plain_files() {
  local tag="$1"
  local odd=""

  odd=$("$DEPLOY/gittree.py" plain "$GIT_DIR" "$tag") || stop "can't list $tag"
  if [ -n "$odd" ]; then
    stop "more than plain files, so $tag isn't built:" \
      "$(printf '%s\n' "$odd" | cat -v)"
  fi
}

# check_pnpm_pin <tag>: stops unless the tag's package.json names the pnpm
# pinned here.
check_pnpm_pin() {
  local tag="$1"

  case $(git show "$tag:package.json") in
    *"\"packageManager\": \"pnpm@$PNPM_VERSION\""*) ;;
    *) stop "the packageManager of $tag isn't pnpm@$PNPM_VERSION" ;;
  esac
}

# fetch_pnpm: downloads pnpm, checks it against its pinned hash, and
# unpacks it into WORK/pnpm.
fetch_pnpm() {
  local sum=""

  curl --disable -fsS -o "$WORK/pnpm.tgz" "$PNPM_URL"
  sum=$(sha512sum <"$WORK/pnpm.tgz" | cut -d' ' -f1)
  if [ "$sum" != "$PNPM_SHA512" ]; then
    stop "pnpm doesn't match its hash"
  fi
  tar -xzf "$WORK/pnpm.tgz" -C "$WORK/pnpm"
}

# get_previous_release <tag> <version>: downloads release N-1 into WORK/old,
# and verifies it by its number.
# A tag's ancestry can skip releases or lack their tags.
get_previous_release() {
  local tag="$1"
  local version="$2"

  git rev-parse -q --verify "$tag^{commit}" >/dev/null \
    || stop "there is no tag $tag here to compare the lockfile with"
  get_release "$tag" "$WORK/old" || stop "can't download $tag"
  if ! "$DEPLOY/checks.py" release "$WORK/old" "$KEYRING" "$version" \
    "$WORK/old/files"; then
    stop "$tag doesn't verify against $KEYRING"
  fi
}

# build_image: builds the image to build the site in, and prints its ID, so
# no one can swap the image under a name meanwhile.
build_image() {
  # lightningcss loads libgcc_s, which StageX's Node lacks
  podman build -q -f - "$WORK/image" <<EOF
FROM $GCC AS gcc
FROM $NODE
COPY --from=gcc /usr/lib/libgcc_s.so.1 /usr/lib/
EOF
}

# run_node <image> <dir> <network> <arguments...>: runs Node in the image on
# WORK/<dir>, with <network> "online" or "offline".
run_node() {
  local image="$1"
  local dir="$2"
  local network="$3"
  local isolation="--network=none"
  shift 3

  if [ "$network" = online ]; then
    isolation=""
  fi
  # --timeout, as Node, the container's first process, ignores a plain stop;
  # cat -v, as any byte from the tag could be a terminal escape
  # shellcheck disable=SC2086 # $isolation is one option, or none
  podman run --rm --timeout=1800 $isolation -e HOME=/tmp \
    -e SOURCE_DATE_EPOCH="$EPOCH" -e ASTRO_TELEMETRY_DISABLED=1 \
    -v "$WORK/$dir:/src:Z" -v "$WORK/store:/store:Z" \
    -v "$WORK/pnpm:/pnpm:ro,Z" -w /src \
    --entrypoint node "$image" "$@" 2>&1 | cat -v
}

# build_site <tag> <image>: builds the site from the tag's source into
# WORK/src, and checks the build.
build_site() {
  local tag="$1"
  local image="$2"
  local bad=""

  git_archive "$GIT_DIR" "$tag" | tar -x -C "$WORK/src"
  # online, pnpm sees only the lockfile, so no code of the tag has the network
  cp -- "$WORK/src/pnpm-lock.yaml" "$WORK/lock/"
  "$DEPLOY/checks.py" lockfile "$WORK/lock/pnpm-lock.yaml" \
    || stop "the lockfile takes a package from elsewhere, so $tag isn't signed"
  # the policies are set here, so no tag can loosen them
  run_node "$image" lock online /pnpm/package/bin/pnpm.cjs fetch --prod \
    --store-dir=/store --config.minimumReleaseAge=20160 \
    --config.trustPolicy=no-downgrade --config.blockExoticSubdeps=true
  # the offline install trusts the fetch's policy check
  run_node "$image" src offline /pnpm/package/bin/pnpm.cjs install --offline \
    --frozen-lockfile --trust-lockfile --prod --ignore-scripts \
    --ignore-pnpmfile --config.strictStorePkgContentCheck=true --store-dir=/store
  run_node "$image" src offline node_modules/astro/bin/astro.mjs build
  # deploy/ runs as root, so it comes from the tag, out of the build's reach
  rm -rf -- "$WORK/src/deploy"
  git_archive "$GIT_DIR" "$tag" deploy | tar -x -C "$WORK/src"

  # a control byte in deploy/ could hide a change from review
  # grep's 1 means none
  bad=$(LC_ALL=C grep -rlaP '[\x00-\x08\x0B-\x1F\x7F]' \
    "$WORK/src/deploy") || [ "$?" -eq 1 ] \
    || stop "can't scan deploy/ for control bytes"
  if [ -n "$bad" ]; then
    stop "a control byte in a text file, so this build isn't signed: $bad"
  fi
  "$DEPLOY/checks.py" build "$WORK/src/dist" "$WORK/src/deploy/site-hosts" \
    "$WORK/tag" || stop "the build holds what a release may not, so it isn't signed"
}

# pack_release <out-dir> <tag>: writes the archive and SHA256SUMS into a new
# folder <out-dir>; one already there, even from the tag, stops the run.
pack_release() {
  local out_dir="$1"
  local tag="$2"
  local archive="ccs-$tag.tar"

  mkdir -- "$out_dir" || stop "$out_dir already exists; move it away first"
  tar --sort=name --mtime="@$EPOCH" --owner=0 --group=0 --numeric-owner \
    --mode=u=rwX,go=rX --format=gnu -cf "$out_dir/$archive" \
    -C "$WORK/src" dist deploy
  (cd -- "$out_dir" && sha256sum -- "$archive") | tee "$out_dir/SHA256SUMS"
}

# get_attestations <tag> <dir>: copies each builder's attestation of the tag
# from ATTESTATIONS_REF into <dir>/<builder>/.
get_attestations() {
  local tag="$1"
  local dir="$2"

  if ! git cat-file -e "$ATTESTATIONS_REF:attestations/$tag" 2>/dev/null; then
    return 0
  fi
  tree_size "$GIT_DIR" "$ATTESTATIONS_REF:attestations/$tag" 1000 1000000
  git_archive "$GIT_DIR" "$ATTESTATIONS_REF" "attestations/$tag" \
    | tar -x -C "$dir" --strip-components=2
}

# is_plain_attestation <folder>: true when the folder holds SHA256SUMS and
# SHA256SUMS.asc as plain files.
# Anyone can add an attestation, so a link, which can point anywhere, fails.
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
# each attestation, and stops when a release signer's differs.
# It collects the matching release signers' signatures in WORK/trusted.asc.
compare_attestations() {
  local out_dir="$1"
  local attestations="$2"
  local folder=""
  local builder=""

  : >"$WORK/trusted.asc"
  for folder in "$attestations"/*/; do
    builder=$(basename -- "$folder")
    if ! is_builder_name "$builder" || ! is_plain_attestation "$folder"; then
      continue
    fi
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

# show_diff <git diff option>... <old> <new>: shows git's diff of two paths in
# WORK, with each hunk line indented, so none can pass for a file's header.
show_diff() {
  { git -C "$WORK" diff --no-index --no-color --no-ext-diff --no-textconv "$@" \
    || true; } | LC_ALL=C awk '
    /^diff --git /{ hunk = 0 }
    { print (hunk ? "  " : "") $0 }
    /^@@ /{ hunk = 1 }'
}

# show_changes <out-dir> <tag> <previous-tag>: shows in less what changed
# since the release before.
show_changes() {
  local out_dir="$1"
  local tag="$2"
  local previous_tag="$3"

  tar -xf "$out_dir/ccs-$tag.tar" -C "$WORK/new"
  printf 'Changes since %s:\n' "${previous_tag:-nothing}"
  # cat -vET shows control bytes, tabs and line ends, so nothing hides
  # a signer may quit less early; the run still goes on to the question
  {
    # the lockfile first, so a reviewer sees where each package comes from
    if [ -n "$previous_tag" ]; then
      git show "$previous_tag:pnpm-lock.yaml"
    fi >"$WORK/old/pnpm-lock.yaml"
    show_diff old/pnpm-lock.yaml tag/pnpm-lock.yaml
    show_diff --patch-with-stat old/files/deploy new/deploy
    # a word to a line, so a planted declaration shows in minified CSS;
    # -M10% pairs a renamed stylesheet with the old one
    show_diff --patch-with-stat -M10% --word-diff=porcelain \
      --word-diff-regex='[^;{}[:space:]]+|[;{}]' old/files/dist new/dist
  } | cat -vET | LESS='' LESSSECURE=1 less || true
}

# sign_release <tag> <out-dir> <version> <builder>: asks the builder, signs
# SHA256SUMS, writes the attestation and joins the signatures.
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
  mkdir -p -- "$folder"
  cp -- "$out_dir/SHA256SUMS" "$folder/SHA256SUMS"
  cp -- "$WORK/mine.asc" "$folder/SHA256SUMS.asc"
  if ! is_release_signer "$folder"; then
    log "your key isn't in $KEYRING, so this attests the build," \
      "but doesn't count towards the two release signers"
  fi

  printf 'Your attestation is in %s/. Add it to the repository under %s.\n' \
    "$folder" "attestations/$tag/$builder/"
  join_signatures "$tag" "$out_dir" "$version"
}

# join_signatures <tag> <out-dir> <version>: joins the release signers'
# signatures and this one into SHA256SUMS.asc.
# It keeps the file only when it passes the check that install.sh makes.
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

# open_work <tag>: checks KEYRING, then makes the scratch folder and a git
# repository of its own in it, so no setting or attribute of the checkout
# counts.
open_work() {
  if [ ! -e "$KEYRING" ]; then
    stop "set KEYRING to the release signers' keyring"
  fi
  KEYRING=$(realpath -- "$KEYRING")
  readonly KEYRING
  WORK=$(mktemp -d)
  readonly WORK
  trap 'rm -rf -- "$WORK"' EXIT
  git init -q --bare "$WORK/git"
  git rev-parse --path-format=absolute --git-path objects \
    >"$WORK/git/objects/info/alternates"
  # the refs this reads, and no replace ref of the checkout
  git for-each-ref --format='create %(refname) %(objectname)' \
    refs/tags "$ATTESTATIONS_REF" | git --git-dir="$WORK/git" update-ref --stdin
  export GIT_DIR="$WORK/git"
  EPOCH=$(git log -1 --format=%ct "$1")
  readonly EPOCH
  mkdir -p -- "$WORK/src" "$WORK/lock" "$WORK/store" "$WORK/tag" "$WORK/pnpm" \
    "$WORK/image" "$WORK/new" "$WORK/old" "$WORK/attestations"
}

# take_tag <tag>: refuses a tag of more than plain files, and copies its files
# out of the build's reach, for checks.py build.
take_tag() {
  tree_size "$GIT_DIR" "$1" 10000 100000000
  check_plain_files "$1"
  git_archive "$GIT_DIR" "$1" | tar -x -C "$WORK/tag"
}

# hand_over <scripts> <version> <tag> <builder> <previous-tag>: runs the
# scripts that may build this release, when they are not these.
hand_over() {
  # only release N-1's scripts, which two signers read, build release N
  if ! diff -rq -- "$DEPLOY" "$1" >/dev/null; then
    # nothing has signed release 1's scripts, so only the ones read here run
    [ "$2" != 1 ] || stop "run release 1 from a checkout of $3 itself"
    echo "Running the scripts of $5."
    /bin/sh "$1/signer/release.sh" "$3" "$4"
    exit 0
  fi
}

main() {
  local tag="${1:-}"
  local builder="${2:-}"
  local version=""
  local previous_tag=""
  local scripts=""
  local image=""
  local out_dir="release-${1:-}"

  version=$(release_tag "$tag") || stop "$USAGE"
  is_builder_name "$builder" || stop "$USAGE"
  check_gnu_tar
  open_work "$tag"
  take_tag "$tag"
  if [ "$version" = 1 ]; then
    scripts="$WORK/tag/deploy"
    mkdir -p -- "$WORK/old/files/deploy" "$WORK/old/files/dist"
  else
    previous_tag="site-v$((version - 1))"
    get_previous_release "$previous_tag" "$((version - 1))"
    scripts="$WORK/old/files/deploy"
  fi
  hand_over "$scripts" "$version" "$tag" "$builder" "$previous_tag"

  check_pnpm_pin "$tag"
  fetch_pnpm
  image=$(build_image) || stop "can't build the image to build in"
  build_site "$tag" "$image"
  pack_release "$out_dir" "$tag"

  get_attestations "$tag" "$WORK/attestations"
  compare_attestations "$out_dir" "$WORK/attestations"
  show_changes "$out_dir" "$tag" "$previous_tag"
  sign_release "$tag" "$out_dir" "$version" "$builder"
}

main "$@"
