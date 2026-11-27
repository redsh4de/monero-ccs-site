#!/bin/sh
# shellcheck shell=dash
# Installs one machine from a signed release, or upgrades it to a newer one.
#
#   install.sh backend site-vN [<web server address> <ccs-back folder>]
#   install.sh web site-vN [<backend IP address> '<push key>']
#
# Exit: 0 installed, 1 refused or failed, 2 usage.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail
# the services must read what this writes; the secrets are made root-only
umask 022

DEPLOY=$(cd -- "$(dirname -- "$0")" && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

readonly LIB=/usr/local/lib/ccs
readonly UNITS=/etc/systemd/system
readonly CONFIG=/etc/ccs/config
readonly AUTHORIZED_KEYS=/home/ccs-fetch/.ssh/authorized_keys
readonly CERT=/etc/ssl/ccs/fullchain.pem
readonly INSTALL_LOG=/var/log/ccs-install.log
readonly NL='
'
readonly USAGE="usage: install.sh backend site-vN [<web server address> <ccs-back folder>]
       install.sh web site-vN [<backend IP address> '<push key>']"

# usage: prints the usage, and exits 2.
usage() {
  printf '%s\n' "$USAGE" >&2
  exit 2
}

# matches <regex> <value>: true when the value is one line that matches the
# whole extended regex.
matches() {
  local regex="$1"
  local value="$2"

  case $value in
    *"$NL"*) return 1 ;;
  esac
  printf '%s\n' "$value" | grep -Eqx -- "$regex"
}

# load_config: makes the config and alert URL files on the first install,
# and reads the config.
load_config() {
  install -d /etc/ccs
  if [ ! -e "$CONFIG" ]; then
    install -m 0600 "$DEPLOY/config.example" "$CONFIG"
  fi
  touch /etc/ccs/alert_url
  chmod 600 "$CONFIG" /etc/ccs/alert_url
  # shellcheck source=/dev/null
  . "$CONFIG"
}

# check_keys_only: stops while sshd's global settings let a password alone
# log in.
check_keys_only() {
  # sshd -T reads the files, not the running sshd, and skips most Match blocks
  if ! sshd -T | awk '
    $1 == "passwordauthentication" { password = $2 }
    $1 == "kbdinteractiveauthentication" { kbd = $2 }
    $1 == "authenticationmethods" {
      keyed = 1
      for (i = 2; i <= NF; i++) if ($i !~ /(^|,)publickey(,|$)/) keyed = 0
    }
    END { exit (password != "no" || kbd != "no") && !keyed }'; then
    stop "sshd may take passwords: turn PasswordAuthentication and" \
      "KbdInteractiveAuthentication off in /etc/ssh/sshd_config.d/," \
      "reload ssh, then run this again"
  fi
}

# check_tls_key: stops when other users can read the TLS private key, or the
# file its link points at.
check_tls_key() {
  local key=""
  local readable=""

  if [ ! -e /etc/ssl/ccs/privkey.pem ]; then
    return 0
  fi
  key=$(readlink -f /etc/ssl/ccs/privkey.pem)
  readable=$(find "$key" -maxdepth 0 -perm -o=r)
  if [ -n "$readable" ]; then
    stop "make $key readable by root only: chmod 600 $key"
  fi
}

# check_backend_config <web host> <ccs-back folder>: checks the two settings,
# given or from the config, and uses them from here on.
check_backend_config() {
  local web_host="$1"
  local ccs_back="$2"
  local public=""
  local unreadable=""

  if [ -n "$web_host$ccs_back" ]; then
    matches '[A-Za-z0-9.:-]+' "$web_host" || usage
    matches '/[A-Za-z0-9._/-]+' "$ccs_back" || usage
    WEB_HOST="$web_host"
    CCS_BACK="$ccs_back"
  fi
  if [ -z "${WEB_HOST:-}" ] || [ -z "${CCS_BACK:-}" ]; then
    usage
  fi

  # the public name can point at the old site, and ssh would trust its key
  public="${SITE#*://}"
  public="${public%%/*}"
  if [ "$WEB_HOST" = "$public" ]; then
    stop "set WEB_HOST to the web server's own address, not the public name"
  fi

  # values.py and the monitor read these as temporary users. find fails on
  # a missing file, but still prints the unreadable ones.
  unreadable=$(find "$CCS_BACK/proposals.json" "$CCS_BACK/complete.json" \
    "$CCS_BACK/proposals" -maxdepth 0 \
    \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=rx \) \
    2>/dev/null || true)
  if [ -n "$unreadable" ]; then
    stop "make ccs-back's files readable by all users: $unreadable"
  fi
}

# check_push_key_args <backend IP> <push key>: checks the two values of the
# push key line.
check_push_key_args() {
  local backend_ip="$1"
  local push_key="$2"

  # sshd matches from= to a host name only with UseDNS on
  matches '[0-9A-Fa-f.:]+' "$backend_ip" || usage
  if ! matches 'ssh-ed25519 [A-Za-z0-9+/]+=*( [A-Za-z0-9@._-]+)?' \
    "$push_key"; then
    stop "the push key must be the one line install.sh backend printed"
  fi
}

# make_keyrings_readable: makes the keyrings and the address list readable
# by the services' users.
make_keyrings_readable() {
  local file=""

  for file in release-signers.gpg address-signers.gpg addresses.txt \
    addresses.txt.asc; do
    if [ -e "/etc/ccs/$file" ]; then
      chmod 644 "/etc/ccs/$file"
    fi
  done
}

# install_packages <role>: installs the role's packages, but not the ones
# they only recommend.
install_packages() {
  local role="$1"

  # curl and git need ca-certificates for TLS, but only recommend it
  set -- ca-certificates curl git gpgv python3-yaml \
    python3-markdown-it python3-mdit-py-plugins python3-jinja2
  if [ "$role" = web ]; then
    set -- "$@" nginx openssh-server rsync
  else
    set -- "$@" openssh-client tor libnss-systemd gpg
  fi
  apt-get install -y --no-upgrade --no-install-recommends "$@"
}

# download_release <tag> <version> <release-dir>: downloads the release, has
# checks.py verify it, and moves it into place as <release-dir>.
download_release() {
  local tag="$1"
  local version="$2"
  local release_dir="$3"
  local download=""

  mkdir -p -- "$LIB/releases"
  rm -rf -- "$LIB"/tmp.*
  # beside releases/, so the move is one rename and leaves no partial release
  download=$(mktemp -d -p "$LIB")
  # shellcheck disable=SC2064 # the folder's name is fixed now
  trap "rm -rf -- '$download'" EXIT
  get_release "$tag" "$download" || stop "can't download $tag"
  "$DEPLOY/checks.py" release "$download" /etc/ccs/release-signers.gpg \
    "$version" "$download/tree" || stop "$tag doesn't verify"
  sync -f "$download/tree"
  mv -- "$download/tree" "$release_dir"
  rm -rf -- "$download"
}

# install_files <release-dir> <role> <other-release-dir>: installs the role's
# units and files, and removes each unit that only the other release has.
install_files() {
  local release="$1"
  local role="$2"
  local other="$3"
  local from="$release/deploy/$role"
  local service=""
  local unit=""
  local name=""

  for unit in "$other/deploy/$role"/*.service "$other/deploy/$role"/*.path \
    "$other/deploy/$role"/*.timer; do
    name="${unit##*/}"
    if [ -n "$other" ] && [ -e "$UNITS/$name" ] && [ ! -e "$from/$name" ]; then
      systemctl disable --now -- "$name"
      rm -rf -- "$UNITS/${name:?}" "$UNITS/${name:?}.d"
    fi
  done
  install -m 0644 "$from"/*.service "$from"/*.path "$from"/*.timer "$UNITS/"
  for service in "$from"/*.service; do
    install -D -m 0644 "$release/deploy/sandbox.conf" \
      "$UNITS/${service##*/}.d/sandbox.conf"
  done
  if [ "$role" = web ]; then
    install -D -m 0644 "$from/tmpfiles.conf" /etc/tmpfiles.d/ccs-web.conf
    install -D -m 0644 "$from/sysusers.conf" /etc/sysusers.d/ccs-web.conf
    install -m 0644 "$from/nginx.conf" /etc/nginx/sites-enabled/ccs.conf
    rm -f /etc/nginx/sites-enabled/default
    install -D -m 0644 "$from/nginx-sandbox.conf" \
      "$UNITS/nginx.service.d/ccs-sandbox.conf"
  fi
}

# start_backend: makes the backend's keys and files on the first install,
# and starts its units.
start_backend() {
  if [ ! -e /etc/ccs/push_key ]; then
    ssh-keygen -q -t ed25519 -N '' -C ccs-push -f /etc/ccs/push_key
  fi
  # the monitor's unit loads it, and does not start without it
  touch /etc/ccs/heartbeat_url
  chmod 600 /etc/ccs/push_key /etc/ccs/heartbeat_url
  if [ ! -s /etc/ccs/known_hosts ]; then
    ssh-keyscan -q -t ed25519 "$WEB_HOST" >/etc/ccs/known_hosts
    [ -s /etc/ccs/known_hosts ] || stop "can't read $WEB_HOST's host key"
  fi

  # the empty PathChanged= clears the unit's own path
  install -d "$UNITS/ccs-values.path.d"
  printf '[Path]\nPathChanged=\nPathChanged=%s/proposals.json\n' \
    "$CCS_BACK" >"$UNITS/ccs-values.path.d/path.conf"
  write_monitor_view
  systemctl daemon-reload
  systemctl enable --now tor ccs-values.path ccs-monitor.timer

  echo "The web server's host key; check it matches" \
    "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there:"
  ssh-keygen -lf /etc/ccs/known_hosts
}

# write_monitor_view: hides the backend's files from the monitor, but for
# ccs-back's folder and the values.
# It renders what the web server sends, so it must not see the wallet's files.
write_monitor_view() {
  printf '%s\n' "[Service]" \
    "TemporaryFileSystem=/var:ro /srv:ro /opt:ro" \
    "BindReadOnlyPaths=$CCS_BACK /var/lib/ccs-backend" \
    >"$UNITS/ccs-monitor.service.d/view.conf"
}

# shellcheck disable=SC2016 # the forced command expands on each push
# write_push_key <backend IP> <push key>: lets the key, from that IP address
# only, replace in/values and do nothing else.
write_push_key() {
  local backend_ip="$1"
  local push_key="$2"
  local command=""

  # the \" are quotes inside the command="..." option
  command='f=$(mktemp /var/lib/ccs/in/values.XXXXXX)'
  # one byte past checks.py's cap, so checks.py refuses a bigger push
  command="$command"' && head -c 1000001 > \"$f\"'
  command="$command"' && chmod 644 \"$f\"'
  command="$command"' && mv \"$f\" /var/lib/ccs/in/values'
  printf 'restrict,from="%s",command="%s" %s\n' \
    "$backend_ip" "$command" "$push_key" >"$AUTHORIZED_KEYS"
}

# start_web <tag>: starts the units, and stops unless the new release renders.
start_web() {
  local tag="$1"

  systemctl daemon-reload
  systemctl enable --now ccs-fetch.path ccs-fetch.timer
  if ! trial_render; then
    stop "$tag failed to render, or nginx refused its site"
  fi
  if [ -e "$CERT" ]; then
    systemctl restart nginx
    # an nginx from before its sandbox deletes the pid file link as it stops
    systemd-tmpfiles --create /etc/tmpfiles.d/ccs-web.conf
  else
    echo "Put the certificate in /etc/ssl/ccs/ (fullchain.pem," \
      "privkey.pem), then run this command again."
  fi
}

# place <role> <release-dir> <other-release-dir>: points current at the
# release, and installs its files in place of the other release's.
place() {
  local role="$1"
  local release="$2"
  local other="$3"

  swap "$release" "$LIB/current"
  install_files "$release" "$role" "$other"
  if [ "$role" = web ]; then
    systemd-sysusers
    systemd-tmpfiles --create /etc/tmpfiles.d/ccs-web.conf
  fi
}

# start_role <role> <tag>: starts the role on the release in current.
start_role() {
  local role="$1"
  local tag="$2"

  if [ "$role" = backend ]; then
    start_backend
  else
    start_web "$tag"
  fi
}

# undo <role> <old-release> <new-release> <made> <saved>: puts back the
# files in <saved> and the old release, and deletes a release this run made.
undo() {
  local role="$1"
  local old_release="$2"
  local new_release="$3"
  local made="$4"
  local saved="$5"
  local file=""

  if [ -z "$old_release" ]; then
    rm -rf -- "$saved"
    return 0
  fi
  log "Going back to release ${old_release##*/}."
  for file in "$CONFIG" "$AUTHORIZED_KEYS"; do
    if [ -e "$saved/${file##*/}" ]; then
      cp -p -- "$saved/${file##*/}" "$file"
    else
      rm -f -- "$file"
    fi
  done
  # shellcheck source=/dev/null
  . "$CONFIG"
  place "$role" "$old_release" "$new_release"
  start_role "$role" "site-v${old_release##*/}"
  if [ "$made" = yes ] && [ "$new_release" != "$old_release" ]; then
    rm -rf -- "$new_release"
  fi
  rm -rf -- "$saved"
  log "Back on release ${old_release##*/}, its units, settings and site."
}

# trial_render: renders with the current release, then has nginx check the
# site once it has a certificate.
trial_render() {
  # twice, as systemd joins a start to a run already going
  systemctl start ccs-update.service || return 1
  systemctl start ccs-update.service || return 1
  if [ -e "$CERT" ]; then
    nginx -t || return 1
  fi
}

# announce <role> <tag>: posts the install to the alert channel, so a wrong
# alert URL shows now.
announce() {
  local role="$1"
  local tag="$2"
  local message=""

  if [ ! -s /etc/ccs/alert_url ]; then
    echo "/etc/ccs/alert_url is empty, so no alert reaches anyone yet."
    return 0
  fi
  message=$(mktemp)
  printf 'ccs install: %s, %s role\n' "$tag" "$role" >"$message"
  # the backend posts through Tor, so the alert service never learns its address
  if (
    if [ "$role" = backend ]; then
      export ALL_PROXY=socks5h://localhost/run/tor/socks
    fi
    CREDENTIALS_DIRECTORY=/etc/ccs request_url alert_url \
      --data-binary "@$message"
  ); then
    echo "The alert channel has the install."
  else
    echo "The install's alert didn't arrive; deploy/docs/ALERTS.md lists the usual causes."
  fi
  rm -f -- "$message"
}

# print_next_steps <role> <tag>: names the files still to place, and on the
# backend prints the command for the web server.
print_next_steps() {
  local role="$1"
  local tag="$2"
  local file=""
  local push_key=""

  for file in address-signers.gpg addresses.txt addresses.txt.asc; do
    if [ ! -e "/etc/ccs/$file" ]; then
      printf 'Still to place by hand: /etc/ccs/%s\n' "$file"
    fi
  done
  if [ "$role" = backend ]; then
    push_key=$(cat /etc/ccs/push_key.pub)
    echo "On the web server, run:"
    printf "  install.sh web %s <backend IP address> '%s'\n" "$tag" "$push_key"
  fi
}

# check_machine <role> <new-key> <host-or-key> <second>: checks the keyring,
# the config and the machine, and installs the packages the role needs.
check_machine() {
  local role="$1"
  local new_key="$2"

  if [ ! -s /etc/ccs/release-signers.gpg ]; then
    stop "put the release signers' keyring in" \
      "/etc/ccs/release-signers.gpg first"
  fi
  # first, as the config can name another release host
  load_config
  if [ "$role" = backend ]; then
    check_backend_config "$3" "$4"
  else
    if [ "$new_key" = yes ]; then
      check_push_key_args "$3" "$4"
    fi
    check_tls_key
  fi
  make_keyrings_readable
  install_packages "$role"
  if [ "$role" = web ]; then
    check_keys_only
  fi
}

# fetch_release <tag> <version> <new-release> <old-release>: downloads and
# checks a release that isn't on disk, unless it is older than the one
# installed.
fetch_release() {
  local installed_version="${4##*/}"

  if [ "$2" -lt "${installed_version:-0}" ]; then
    stop "$1 is older than the installed release"
  fi
  download_release "$1" "$2" "$3"
}

# save_settings: copies the config and the push key into a new folder, which
# it prints, so an undo can put them back.
save_settings() {
  local saved=""
  local file=""

  saved=$(mktemp -d) || return 1
  for file in "$CONFIG" "$AUTHORIZED_KEYS"; do
    if [ -e "$file" ]; then
      cp -p -- "$file" "$saved/" || return 1
    fi
  done
  printf '%s\n' "$saved"
}

# switch <role> <tag> <new-release> <old-release> <new-key> <host-or-key>
# <second>: places the new release, writes its settings and starts the role.
switch() {
  local role="$1"

  place "$role" "$3" "$4"
  if [ "$role" = backend ] && [ -n "$6$7" ]; then
    # neither value allows a |, so both are safe inside sed's s|||
    sed -i "s|^WEB_HOST=.*|WEB_HOST=$WEB_HOST|" "$CONFIG"
    sed -i "s|^CCS_BACK=.*|CCS_BACK=$CCS_BACK|" "$CONFIG"
  elif [ "$5" = yes ]; then
    write_push_key "$6" "$7"
  fi
  start_role "$role" "$2"
}

# finish <role> <tag> <new-release> <old-release>: keeps the release before,
# to go back to by name, and says what was installed.
finish() {
  if [ "$4" != "$3" ]; then
    keep "$LIB/releases/" "$3" "$4"
  fi
  announce "$1" "$2"
  print_next_steps "$1" "$2"
}

main() {
  local role="${1:-}"
  local tag="${2:-}"
  local version=""
  local new_release=""
  local old_release=""
  local new_key=no
  local made=no
  local saved=""

  case $role in
    backend | web) ;;
    *) usage ;;
  esac
  version=$(release_tag "$tag") || usage
  if [ "$role" = web ]; then
    if [ -n "${3:-}${4:-}" ] || [ ! -e "$AUTHORIZED_KEYS" ]; then
      new_key=yes
    fi
  fi
  check_machine "$role" "$new_key" "${3:-}" "${4:-}"

  # a release already on disk was checked when it arrived
  new_release="$LIB/releases/$version"
  old_release=$(readlink "$LIB/current") || old_release=""
  if [ ! -d "$new_release" ]; then
    fetch_release "$tag" "$version" "$new_release" "$old_release"
    made=yes
  fi

  saved=$(save_settings)
  echo "Switching to $tag; the rest of the install writes to $INSTALL_LOG."
  exec 3>&1 4>&2 >"$INSTALL_LOG" 2>&1
  # from here, a failure puts the old release back, and no signal stops that
  trap 'set +e
    trap "" INT TERM QUIT
    (set -e; undo "$role" "$old_release" "$new_release" "$made" "$saved")
    [ "$?" -eq 0 ] || log "The undo stopped. To end it, install" \
      "site-v${old_release##*/} again."
    cat -- "$INSTALL_LOG" >&4' EXIT
  # a hangup stops neither the switch nor the undo
  trap '' HUP
  trap 'exit 1' INT TERM QUIT
  switch "$role" "$tag" "$new_release" "$old_release" "$new_key" \
    "${3:-}" "${4:-}"
  trap 'cat -- "$INSTALL_LOG" >&3 || true' EXIT
  rm -rf -- "$saved"
  finish "$role" "$tag" "$new_release" "$old_release"
}

main "$@"
