#!/bin/sh
# shellcheck shell=dash
# Installs one machine from a signed release, or upgrades it to a newer one.
#
#   install.sh backend site-vN [<web server address> <ccs-back folder>]
#   install.sh web site-vN [<backend IP address> '<push key>']
#
# Runs as: root, by hand. Set up the backend first: it prints the web
#          server's command. The first time, run this from a release you
#          checked by hand (see deploy/README.md); after that, run
#          /usr/local/lib/ccs/current/deploy/install.sh.
# Reads:   /etc/ccs/: the config, the keyrings and the signed address list.
# Writes:  /etc/ccs/config and /etc/ccs/alert_url, the release in
#          /usr/local/lib/ccs/releases/N, the link /usr/local/lib/ccs/current,
#          the units, users, folders and nginx site; on the backend the push
#          key and the web server's host key, and on the web server the push
#          key's line in /home/ccs-fetch/.ssh/authorized_keys.
# Trust:   checks.py verifies the release: two signers, the version named, a
#          tar that matches its hash.
# Exit:    0 installed, 1 refused or failed, 2 usage.
set -eu
# shellcheck disable=SC3040 # Debian 13's dash has pipefail
set -o pipefail
# what it writes is readable by the services, whatever the operator's umask;
# the config, the alert URL and the push key are made root-only by name
umask 022

SCRIPT_DIR=$(dirname -- "$0")
DEPLOY=$(cd -- "$SCRIPT_DIR" && pwd -P)
readonly DEPLOY
# shellcheck source=lib.sh
. "$DEPLOY/lib.sh"

readonly LIB=/usr/local/lib/ccs
readonly UNITS=/etc/systemd/system
readonly CONFIG=/etc/ccs/config
readonly AUTHORIZED_KEYS=/home/ccs-fetch/.ssh/authorized_keys
readonly CERT=/etc/ssl/ccs/fullchain.pem
readonly NL='
'
readonly USAGE="usage: install.sh backend site-vN [<web server address> <ccs-back folder>]
       install.sh web site-vN [<backend IP address> '<push key>']"

# usage: prints how to run this, and exits 2.
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

# load_config: makes /etc/ccs/config from the example on the first install,
# keeps it root-only, and reads it. It comes first, since it can name
# another release host.
load_config() {
  install -d /etc/ccs /etc/sysusers.d /etc/tmpfiles.d
  if [ ! -e "$CONFIG" ]; then
    install -m 0600 "$DEPLOY/config.example" "$CONFIG"
  fi
  chmod 600 "$CONFIG"
  # shellcheck source=/dev/null
  . "$CONFIG"
  # the alert URL, root-only
  if [ ! -e /etc/ccs/alert_url ]; then
    install -m 0600 /dev/null /etc/ccs/alert_url
  fi
  chmod 600 /etc/ccs/alert_url
}

# check_tls_key: stops when the TLS private key, or the file a link points
# it at, can be read by other users. nginx reads it as root; nothing else
# should.
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

# set_backend_config <web host> <ccs-back folder>: checks the backend's two
# settings and writes them to the config. Given once, they are kept there;
# with neither given, the config's own are checked.
set_backend_config() {
  local web_host="$1"
  local ccs_back="$2"
  local public=""
  local unreadable=""

  if [ -n "$web_host$ccs_back" ]; then
    matches '[A-Za-z0-9.:-]+' "$web_host" || usage
    matches '/[A-Za-z0-9._/-]+' "$ccs_back" || usage
    # neither pattern allows a |, so both are safe inside sed's s|||
    sed -i "s|^WEB_HOST=.*|WEB_HOST=$web_host|" "$CONFIG"
    sed -i "s|^CCS_BACK=.*|CCS_BACK=$ccs_back|" "$CONFIG"
    WEB_HOST="$web_host"
    CCS_BACK="$ccs_back"
  fi
  if [ -z "${WEB_HOST:-}" ] || [ -z "${CCS_BACK:-}" ]; then
    usage
  fi

  # the public name may still point at the old site, whose host key ssh
  # would trust
  public="${SITE#*://}"
  public="${public%%/*}"
  if [ "$WEB_HOST" = "$public" ]; then
    stop "set WEB_HOST to the web server's own address, not the public name"
  fi

  # values.py and the monitor read these as temporary users. find fails
  # when one is missing, yet still prints the unreadable ones, so its status
  # is ignored.
  unreadable=$(find "$CCS_BACK/proposals.json" "$CCS_BACK/complete.json" \
    "$CCS_BACK/proposals" -maxdepth 0 \
    \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=rx \) \
    2>/dev/null || true)
  if [ -n "$unreadable" ]; then
    stop "make ccs-back's files readable by all users: $unreadable"
  fi
}

# check_push_key_args <backend IP> <push key>: checks the two values the push
# key line is built from. An IP address, since sshd matches from= against
# host names only with UseDNS, which is off by default.
check_push_key_args() {
  local backend_ip="$1"
  local push_key="$2"

  matches '[0-9A-Fa-f.:]+' "$backend_ip" || usage
  if ! matches 'ssh-ed25519 [A-Za-z0-9+/]+=*( [A-Za-z0-9@._-]+)?' \
    "$push_key"; then
    stop "the push key must be the one line install.sh backend printed"
  fi
}

# make_keyrings_readable: the services read the keyrings and the address list
# as other users, so each file there is made readable by all.
make_keyrings_readable() {
  local file=""

  for file in release-signers.gpg address-signers.gpg addresses.txt \
    addresses.txt.asc; do
    if [ -e "/etc/ccs/$file" ]; then
      chmod 644 "/etc/ccs/$file"
    fi
  done
}

# install_packages <role>: installs what the role runs, and nothing they
# merely recommend. ca-certificates is named, since curl and git need it to
# check TLS but only recommend it.
install_packages() {
  local role="$1"

  set -- ca-certificates curl git gpgv qrencode python3-yaml \
    python3-markdown-it python3-mdit-py-plugins python3-jinja2
  if [ "$role" = web ]; then
    set -- "$@" nginx openssh-server rsync
  else
    # zbar reads back each QR code, so only the backend needs it
    set -- "$@" openssh-client tor libnss-systemd python3-zbar
  fi
  apt-get install -y --no-upgrade --no-install-recommends "$@"
}

# download_release <tag> <version> <release-dir>: downloads the release,
# has checks.py verify it, and moves it into place as <release-dir>.
download_release() {
  local tag="$1"
  local version="$2"
  local release_dir="$3"
  local download=""

  # beside releases/, on one filesystem, so moving the checked tree in is one
  # rename: a cut-off copy never leaves a partial release a rerun trusts
  mkdir -p -- "$LIB/releases"
  rm -rf -- "$LIB"/tmp.*
  download=$(mktemp -d -p "$LIB")
  # shellcheck disable=SC2064 # the folder's name is fixed now
  trap "rm -rf -- '$download'" EXIT
  get_release "$tag" "$download" || stop "can't download $tag"
  # it must be the release named: one served under another name is never
  # unpacked
  "$DEPLOY/checks.py" release "$download" /etc/ccs/release-signers.gpg \
    "$version" "$download/tree" || stop "$tag doesn't verify"
  sync -f "$download/tree"
  mv -- "$download/tree" "$release_dir"
}

# install_files <release-dir> <role>: installs that release's units, the
# sandbox beside each service by name, and its users, folders and nginx site,
# named by role so one machine can hold both.
install_files() {
  local release="$1"
  local role="$2"
  local from="$release/deploy/$role"
  local service=""

  install -m 0644 "$from"/*.service "$from"/*.path "$from"/*.timer "$UNITS/"
  for service in "$from"/*.service; do
    install -D -m 0644 "$release/deploy/sandbox.conf" \
      "$UNITS/${service##*/}.d/sandbox.conf"
  done
  install -m 0644 "$from/tmpfiles.conf" "/etc/tmpfiles.d/ccs-$role.conf"
  if [ "$role" = web ]; then
    install -m 0644 "$from/sysusers.conf" /etc/sysusers.d/ccs-web.conf
    install -m 0644 "$from/nginx.conf" /etc/nginx/sites-enabled/ccs.conf
  fi
}

# start_backend: makes the push key and reads the web server's host key on
# the first install, points ccs-values.path at ccs-back's JSON, and starts
# the units.
start_backend() {
  if [ ! -e /etc/ccs/push_key ]; then
    ssh-keygen -q -t ed25519 -N '' -C ccs-push -f /etc/ccs/push_key
  fi
  chmod 600 /etc/ccs/push_key
  if [ ! -s /etc/ccs/known_hosts ]; then
    ssh-keyscan -q -t ed25519 "$WEB_HOST" >/etc/ccs/known_hosts
  fi
  if [ ! -s /etc/ccs/known_hosts ]; then
    stop "can't read $WEB_HOST's host key"
  fi

  # the empty PathChanged= clears the unit's own path, so only this one is
  # watched
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

# write_monitor_view: gives the monitor a private view of the backend's files.
# It renders what the web server sends, so it sees no more than it reads:
# /var, /srv and /opt are empty but for ccs-back's storage folder and the
# values. The wallet's files stay out of its sight, whatever their mode.
write_monitor_view() {
  install -d "$UNITS/ccs-monitor.service.d"
  printf '%s\n' "[Service]" \
    "TemporaryFileSystem=/var:ro /srv:ro /opt:ro" \
    "BindReadOnlyPaths=$CCS_BACK /var/lib/ccs-backend" \
    >"$UNITS/ccs-monitor.service.d/view.conf"
}

# shellcheck disable=SC2016 # the forced command expands on each push
# write_push_key <backend IP> <push key>: lets the backend's key, from its IP
# address only, do one thing: replace in/values. The forced command writes a
# temporary file of at most one byte past checks.py's cap, then renames it
# into place.
write_push_key() {
  local backend_ip="$1"
  local push_key="$2"
  local command=""

  # sshd's shell expands these on each push; the \" are quotes inside the
  # command="..." option
  command='f=$(mktemp /var/lib/ccs/in/values.XXXXXX)'
  command="$command"' && head -c 1000001 > \"$f\"'
  command="$command"' && chmod 644 \"$f\"'
  command="$command"' && mv \"$f\" /var/lib/ccs/in/values'
  printf 'restrict,from="%s",command="%s" %s\n' \
    "$backend_ip" "$command" "$push_key" >"$AUTHORIZED_KEYS"
}

# start_web <old-release> <new-release> <tag>: starts the units, and keeps
# the new release only once it renders; on a failure the old one comes back.
start_web() {
  local old_release="$1"
  local new_release="$2"
  local tag="$3"

  systemctl daemon-reload
  systemctl enable --now ccs-fetch.path ccs-fetch.timer
  if ! trial_render; then
    if [ -n "$old_release" ] && [ "$old_release" != "$new_release" ]; then
      swap "$old_release" "$LIB/current"
      install_files "$old_release" web
      systemctl daemon-reload
      rm -rf -- "$new_release"
    fi
    stop "$tag failed to render, or nginx refused its site;" \
      "the site and the release stay as they were"
  fi
  if [ -e "$CERT" ]; then
    systemctl reload nginx
  else
    echo "Put the certificate in /etc/ssl/ccs/ (fullchain.pem," \
      "privkey.pem), then reload nginx."
  fi
}

# trial_render: renders with the current release twice, as systemd joins a
# start to a run already going, perhaps the old release's; then nginx checks
# the site, once it has a certificate.
trial_render() {
  systemctl start ccs-update.service || return 1
  systemctl start ccs-update.service || return 1
  if [ -e "$CERT" ]; then
    nginx -t || return 1
  fi
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

main() {
  local role="${1:-}"
  local tag="${2:-}"
  local version=""
  local new_release=""
  local old_release=""
  local installed_version=""
  local new_key=no

  case $role in
    backend | web) ;;
    *) usage ;;
  esac
  version=$(release_tag "$tag") || usage
  if [ ! -s /etc/ccs/release-signers.gpg ]; then
    stop "put the release signers' keyring in" \
      "/etc/ccs/release-signers.gpg first"
  fi

  # 1. The settings and packages, before the release changes.
  load_config
  if [ "$role" = backend ]; then
    set_backend_config "${3:-}" "${4:-}"
  else
    # the push key is written on the first install, or when it is given
    # again
    if [ -n "${3:-}${4:-}" ] || [ ! -e "$AUTHORIZED_KEYS" ]; then
      new_key=yes
      check_push_key_args "${3:-}" "${4:-}"
    fi
    check_tls_key
  fi
  make_keyrings_readable
  install_packages "$role"

  # 2. The release: newer than the installed one, or already on disk, which
  #    was checked when it arrived. The installed version may come again;
  #    with releases/N deleted, it downloads afresh.
  new_release="$LIB/releases/$version"
  old_release=$(readlink "$LIB/current") || old_release=""
  if [ ! -d "$new_release" ]; then
    installed_version="${old_release##*/}"
    if [ "$version" -lt "${installed_version:-0}" ]; then
      stop "$tag is older than the installed release"
    fi
    download_release "$tag" "$version" "$new_release"
  fi

  # 3. Scripts, render, design and units switch together.
  swap "$new_release" "$LIB/current"
  install_files "$new_release" "$role"
  systemd-sysusers
  systemd-tmpfiles --create "/etc/tmpfiles.d/ccs-$role.conf"
  if [ "$role" = backend ]; then
    start_backend
  else
    if [ "$new_key" = yes ]; then
      write_push_key "${3:-}" "${4:-}"
    fi
    start_web "$old_release" "$new_release" "$tag"
  fi

  # 4. The release before stays, to switch back to by name.
  if [ "$old_release" != "$new_release" ]; then
    keep "$LIB/releases/" "$new_release" "$old_release"
  fi
  print_next_steps "$role" "$tag"
}

main "$@"
