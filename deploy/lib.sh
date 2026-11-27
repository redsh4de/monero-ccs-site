# shellcheck shell=dash
# Steps the deploy scripts share. Source it once DEPLOY names the release's
# deploy/ folder, and set ALERTS_DIR and NOW before an alert.

# set -e is off in an if or before a ||, so each step here ends in || return 1

# defaults for ccs.getmonero.org, which /etc/ccs/config overrides
CCS_DIR="${CCS_DIR:-/var/lib/ccs}"
GITLAB_API="${GITLAB_API:-https://repo.getmonero.org/api/v4/projects/monero-project%2Fccs-proposals}"
PROPOSALS_GIT="${PROPOSALS_GIT:-https://repo.getmonero.org/monero-project/ccs-proposals.git}"
RELEASES="${RELEASES:-https://github.com/redsh4de/monero-ccs-site/releases}"
SITE="${SITE:-https://ccs.getmonero.org}"

# log <message>: writes the message to stderr, for the journal.
log() {
  printf '%s\n' "$*" >&2
}

# stop <message>: logs the message with the script's name, and exits 1.
stop() {
  log "${0##*/}: $*"
  exit 1
}

# alert <name> <failing> <hold> <repeat> <text>: records a check that fails
# (1) or passes (0), and queues its alert or its "resolved" message.
# A failing check alerts after <hold> seconds, then every <repeat> seconds.
alert() {
  local name="$1"
  local failing="$2"
  local hold="$3"
  local repeat="$4"
  local text="$5"
  local state="$ALERTS_DIR/$name"
  local since="$NOW"
  local sent=0
  local due=0

  if [ -s "$state" ]; then
    read -r since sent _ <"$state" || return 1
  fi

  if [ "$failing" = 0 ]; then
    if [ "$sent" = 0 ]; then
      rm -f -- "$state" || return 1
    else
      queue "resolved: $text ($name)" "resolved $name" || return 1
    fi
    return 0
  fi

  printf '%s %s %s\n' "$since" "$sent" "$text" >"$state" || return 1
  if [ "$sent" = 0 ]; then
    due=$((since + hold))
  else
    due=$((sent + repeat))
  fi
  if [ "$NOW" -ge "$due" ]; then
    queue "$text ($name)" "sent $name" || return 1
  fi
}

# queue <message> <mark>: queues a message, and the mark that alerts_send
# applies once the message is posted.
queue() {
  local message="$1"
  local mark="$2"

  printf '%s\n' "$message" >>"$ALERTS_DIR/.outbox" || return 1
  printf '%s\n' "$mark" >>"$ALERTS_DIR/.pending" || return 1
}

# alerts_begin: drops the messages that a killed run left queued.
# No alert is lost: a check's state changes only when its message is posted.
alerts_begin() {
  mkdir -p -- "$ALERTS_DIR" || return 1
  rm -f -- "$ALERTS_DIR/.outbox" "$ALERTS_DIR/.pending" || return 1
}

# alerts_send: posts the queued messages as one, and applies their marks
# only after a good post.
alerts_send() {
  local outbox="$ALERTS_DIR/.outbox"
  local pending="$ALERTS_DIR/.pending"
  local messages=""
  local action=""
  local name=""

  if [ ! -s "$outbox" ]; then
    return 0
  fi
  messages=$(cat -- "$outbox") || return 1
  log "alert: $messages"

  # from a file, as curl reads a message that starts with @ as a file name
  if request_url alert_url --data-binary "@$outbox"; then
    while read -r action name; do
      mark_posted "$action" "$name" || return 1
    done <"$pending"
  else
    log "alert not sent"
  fi
  rm -f -- "$outbox" "$pending" || return 1
}

# request_url <name> [<curl option>...]: requests the URL held in the
# credential <name>, and fails without one.
request_url() {
  local credential="${CREDENTIALS_DIRECTORY:-/nonexistent}/$1"
  local url=""
  shift

  url=$(cat -- "$credential" 2>/dev/null) || return 1
  if [ -z "$url" ]; then
    return 1
  fi
  # on stdin, so ps never shows the URL
  printf 'url = %s\n' "$url" | curl --disable -fsS -m 20 -K - "$@" >/dev/null
}

# mark_posted <action> <name>: records when a check alerted, or forgets a
# check that resolved.
mark_posted() {
  local action="$1"
  local name="$2"
  local state="$ALERTS_DIR/$name"
  local since=""
  local text=""

  if [ ! -e "$state" ]; then
    return 0
  fi
  if [ "$action" = resolved ]; then
    rm -f -- "$state" || return 1
    return 0
  fi
  read -r since _ text <"$state" || return 1
  printf '%s %s %s\n' "$since" "$NOW" "$text" >"$state" || return 1
}

# release_tag <tag>: prints N for a tag site-vN with no leading zero, and
# fails for any other tag.
release_tag() {
  local tag="$1"
  local number="${tag#site-v}"

  case $tag in
    site-v[1-9]*) ;;
    *) return 1 ;;
  esac
  case $number in
    *[!0-9]*) return 1 ;;
  esac
  printf '%s\n' "$number"
}

# get_release <tag> <dir>: downloads the release's three files into <dir>.
get_release() {
  local tag="$1"
  local dir="$2"
  local name=""

  for name in SHA256SUMS SHA256SUMS.asc "ccs-$tag.tar"; do
    curl --disable -fsS -L --max-time 600 --max-filesize 200000000 \
      -o "$dir/$name" "$RELEASES/download/$tag/$name" || return 1
  done
}

# verify_list <list>: writes the signed address list to <list>, or an empty
# list when it does not verify.
# An empty list closes every donation box, so no unsigned address shows.
verify_list() {
  if ! "$DEPLOY/checks.py" addresses /etc/ccs/address-signers.gpg \
    /etc/ccs/addresses.txt >"$1"; then
    : >"$1" || return 1
  fi
}

# render_values <values> <input-dir> <list>: writes the render's values,
# addresses and site into <input-dir>, and prints their hash.
render_values() {
  local values="$1"
  local in_dir="$2"
  local list="$3"

  cp "$list" "$in_dir/addresses" || return 1
  printf '%s\n' "$SITE" >"$in_dir/site" || return 1
  "$DEPLOY/checks.py" apply "$values" "$list" >"$in_dir/values" || return 1

  # the values' time changes with every push, so it stays out of the hash
  sed '/^updated /d' "$in_dir/values" "$in_dir/addresses" "$in_dir/site" \
    | sha256sum | cut -c1-64
}

# swap <target> <link>: points the link at the target in one rename, so the
# link never goes missing.
swap() {
  local target="$1"
  local link="$2"

  ln -sfn -- "$target" "$link.tmp" || return 1
  mv -T -- "$link.tmp" "$link" || return 1
}

# keep <prefix> <first> <second>: deletes every path that starts with
# <prefix>, except <first> and <second>.
keep() {
  local prefix="$1"
  local first="$2"
  local second="$3"
  local path=""

  for path in "$prefix"*; do
    if [ "$path" != "$first" ] && [ "$path" != "$second" ]; then
      rm -rf -- "$path" || return 1
    fi
  done
}

# tree_size <git-dir> <tree> <entries> <bytes>: fails when the tree holds
# more entries, folders included, or more bytes.
tree_size() {
  "$DEPLOY/gittree.py" size "$@" && return 0
  log "$2 can't be listed, or holds more than $3 entries or $4 bytes"
  return 1
}

# git_archive <git-dir> <tree-ish> [<path>...]: writes the files as a tar.
# Attributes come from an empty tree, so no .gitattributes can change a file.
git_archive() {
  local git_dir="$1"
  local none
  shift

  none=$(git -C "$git_dir" hash-object -t tree /dev/null) || return 1
  git -C "$git_dir" --attr-source="$none" archive --worktree-attributes "$@"
}

# export_tree <git-dir> <commit> <dir>: writes the commit's files, and the
# merge request behind each file, into <dir>.
# Its caps, 2000 files and 4 MB, are about 6 and 3 times the proposals of 2026.
export_tree() {
  local git_dir="$1"
  local commit="$2"
  local dir="$3"

  tree_size "$git_dir" "$commit" 2000 4000000 || return 1
  mkdir -p -- "$dir/proposals" "$dir/mr" || return 1
  git_archive "$git_dir" "$commit" \
    | tar -x -C "$dir/proposals" --no-same-owner || return 1
  "$DEPLOY/gittree.py" merges "$git_dir" "$commit" >"$dir/mr/merged.tsv" \
    || return 1
}
