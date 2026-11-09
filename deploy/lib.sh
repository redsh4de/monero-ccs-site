# shellcheck shell=dash
# Steps the deploy scripts share. Each script sources this file after setting
# DEPLOY, the release's deploy/ folder, and before raising alerts it sets
# ALERTS_DIR and NOW.
#
# Each script's main calls its steps on lines of their own, where set -e stops
# the script at the first failure. The functions here also run inside an if or
# a $(...) || stop, where the shell turns set -e off, so they check every step
# themselves with || return 1.
#
# checks.py decides on every pushed value, signed list and release. GitLab's
# content is only extracted here, and the render escapes it.

# The settings come from /etc/ccs/config, which systemd passes to each service
# in its environment. The defaults serve ccs.getmonero.org.
CCS_DIR="${CCS_DIR:-/var/lib/ccs}"
GITLAB_API="${GITLAB_API:-https://repo.getmonero.org/api/v4/projects/monero-project%2Fccs-proposals}"
PROPOSALS_GIT="${PROPOSALS_GIT:-https://repo.getmonero.org/monero-project/ccs-proposals.git}"
RELEASES="${RELEASES:-https://github.com/redsh4de/monero-ccs-site/releases}"
SITE="${SITE:-https://ccs.getmonero.org}"

# log <message>: writes the message to stderr, which the journal keeps.
log() {
  printf '%s\n' "$*" >&2
}

# stop <message>: logs the message with the script's name, and exits 1.
stop() {
  log "${0##*/}: $*"
  exit 1
}

# alert <name> <failing> <hold> <repeat> <text>: the one alert rule.
#
# <failing> is 1 when the check fails this run, and 0 when it passes. A
# failing check alerts once it has failed for <hold> seconds with no pass in
# between, then again every <repeat> seconds. A check that passes after it
# alerted sends one "resolved" message, and one that never alerted is
# forgotten quietly. A check not called in a run changes nothing.
#
# Each failing check has one file, ALERTS_DIR/<name>, holding
# "<since> <sent> <text>": when it started failing, when it last alerted (0
# for never), and its newest text. This only queues messages; alerts_send
# posts them. Each message ends with the check's name, so an operator can
# look it up in ALERTS.md.
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
    # the rest of the line is the old text, which the new one replaces
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

# queue <message> <mark>: queues a message, with the mark ("sent <name>" or
# "resolved <name>") that alerts_send applies once it is posted.
queue() {
  local message="$1"
  local mark="$2"

  printf '%s\n' "$message" >>"$ALERTS_DIR/.outbox" || return 1
  printf '%s\n' "$mark" >>"$ALERTS_DIR/.pending" || return 1
}

# alerts_begin: drops the messages a killed run left queued. This run's
# checks queue them again if they still apply, because a check's state moves
# on only once its message is posted.
alerts_begin() {
  mkdir -p -- "$ALERTS_DIR" || return 1
  rm -f -- "$ALERTS_DIR/.outbox" "$ALERTS_DIR/.pending" || return 1
}

# alerts_send: logs the queued messages, posts them as one, and only after a
# good post applies their marks. A failed post, or no alert URL, changes
# nothing, so the next run sends them again: emptying /etc/ccs/alert_url
# pauses alerts without losing them.
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

  if post_alerts "$outbox"; then
    while read -r action name; do
      mark_posted "$action" "$name" || return 1
    done <"$pending"
  else
    log "alert not sent"
  fi
  rm -f -- "$outbox" "$pending" || return 1
}

# post_alerts <file>: posts the file's text to the alert URL. systemd hands
# the URL, from /etc/ccs/alert_url, only to the units that send alerts
# (LoadCredential=), so no other service, the render among them, ever sees
# it. Fails when the post fails, or when there is no URL. The URL reaches
# curl on stdin, so ps never shows it, and the text is read from the file,
# so a leading @ in a message stays text.
post_alerts() {
  local file="$1"
  local credential="${CREDENTIALS_DIRECTORY:-/nonexistent}/alert_url"
  local url=""

  url=$(cat -- "$credential" 2>/dev/null) || return 1
  if [ -z "$url" ]; then
    return 1
  fi
  printf 'url = %s\n' "$url" \
    | curl -fsS -m 20 -K - --data-binary "@$file" >/dev/null
}

# mark_posted <action> <name>: after a good post, records when a check
# alerted, or forgets a check that resolved.
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
  # the second field is the old time sent, which NOW replaces
  read -r since _ text <"$state" || return 1
  printf '%s %s %s\n' "$since" "$NOW" "$text" >"$state" || return 1
}

# release_tag <tag>: prints N for a release named site-vN, where N has no
# leading zero. Fails for any other name.
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

# get_release <tag> <dir>: downloads a release's three files into <dir>: the
# archive ccs-<tag>.tar, SHA256SUMS, and the signatures in SHA256SUMS.asc.
get_release() {
  local tag="$1"
  local dir="$2"
  local name=""

  for name in SHA256SUMS SHA256SUMS.asc "ccs-$tag.tar"; do
    curl -fsS -L --max-time 600 --max-filesize 200000000 \
      -o "$dir/$name" "$RELEASES/download/$tag/$name" || return 1
  done
}

# render_values <values> <input-dir> <list>: writes the render's values,
# general-fund and site into the render's input folder, with every address not
# on the signed list as -, and prints a hash of all three. The verified list
# goes to <list>, outside the input folder.
#
# A list that doesn't verify counts as empty, so every donation box closes
# rather than show an address nobody signed. update.sh and the monitor both
# build the render's input this way, so they agree.
render_values() {
  local values="$1"
  local in_dir="$2"
  local list="$3"

  if ! "$DEPLOY/checks.py" addresses /etc/ccs/address-signers.gpg \
    /etc/ccs/addresses.txt >"$list"; then
    : >"$list" || return 1
  fi
  sed -n 's/^general-fund //p' "$list" >"$in_dir/general-fund" || return 1
  printf '%s\n' "$SITE" >"$in_dir/site" || return 1
  "$DEPLOY/checks.py" apply "$values" "$list" >"$in_dir/values" || return 1

  # the values' time changes with every push, so it stays out of the hash
  sed '/^updated /d' "$in_dir/values" "$in_dir/general-fund" "$in_dir/site" \
    | sha256sum | cut -c1-64
}

# swap <target> <link>: points the link at the target with one rename, so a
# reader always finds the old target or the new one, never neither.
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

# export_tree <git-dir> <commit> <dir>: writes the commit's files to
# <dir>/proposals, and the merge request behind each file to
# <dir>/mr/merged.tsv. Its body is a subshell, so that pipefail fails the
# export when git does, whatever the caller's settings.
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
export_tree() (
  set -o pipefail
  local git_dir="$1"
  local commit="$2"
  local dir="$3"

  mkdir -p -- "$dir/proposals" "$dir/mr" || exit 1
  git -C "$git_dir" archive "$commit" \
    | tar -x -C "$dir/proposals" --no-same-owner || exit 1
  list_merges "$git_dir" "$commit" >"$dir/mr/merged.tsv" || exit 1
)

# list_merges <git-dir> <commit>: prints "<merge request>\t<file>" for each
# file that a merge on the commit's first-parent history added, oldest first,
# so a file added again later maps to its newest merge. git quotes a file
# name holding a tab, a newline, a quote or a backslash; such a file is left
# out, and its page links to its history instead.
#
# git log frames each commit message between a line holding \002 and one
# holding \003, so awk can tell the message's lines from the file names.
list_merges() {
  local git_dir="$1"
  local commit="$2"

  git -C "$git_dir" -c core.quotePath=false log --reverse --first-parent \
    --diff-filter=A --name-only --format='%x02%n%B%n%x03' "$commit" \
    | LC_ALL=C awk '
      $0 == "\002" { in_message = 1; merge = ""; next }
      $0 == "\003" { in_message = 0; next }
      in_message {
        if ($0 ~ /^See merge request monero-project\/ccs-proposals![0-9]+$/)
          merge = substr($0, index($0, "!") + 1)
        next
      }
      merge != "" && $0 != "" && substr($0, 1, 1) != "\"" {
        print merge "\t" $0
      }
    '
}
