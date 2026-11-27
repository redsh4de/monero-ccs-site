#!/usr/bin/python3 -I
"""Install one machine from a signed release, or upgrade it to a newer one.

  install.py backend site-vN [<web server address> <ccs-back folder>]
  install.py web site-vN [<backend IP address> '<push key>']

Exit: 0 installed, 1 refused or failed, 2 usage.
"""

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

MODULES = Path(__file__).resolve().parent / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import lib  # noqa: E402

LIB = Path("/usr/local/lib/ccs")
UNITS = Path("/etc/systemd/system")
ETC = Path("/etc/ccs")
CONFIG = ETC / "config"
ALERT_URL = ETC / "alert_url"
AUTHORIZED_KEYS = Path("/home/ccs-fetch/.ssh/authorized_keys")
CERT = Path("/etc/ssl/ccs/fullchain.pem")
INSTALL_LOG = Path("/var/log/ccs-install.log")
TMPFILES = "/etc/tmpfiles.d/ccs-web.conf"
ADDRESS_FILES = ["address-signers.gpg", "addresses.txt", "addresses.txt.asc"]
USAGE = """usage: install.py backend site-vN [<web server address> <ccs-back folder>]
       install.py web site-vN [<backend IP address> '<push key>']"""
# curl and git need ca-certificates for TLS, but only recommend it
PACKAGES = """
    ca-certificates curl git gpgv
    python3-yaml python3-markdown-it python3-mdit-py-plugins python3-jinja2
""".split()
ROLE_PACKAGES = {
    "web": ["nginx", "openssh-server", "rsync"],
    "backend": ["openssh-client", "tor", "libnss-systemd", "gpg"],
}
# sh runs it on each push; the \" are quotes inside the command="..." option
PUSH_COMMAND = (
    "f=$(mktemp /var/lib/ccs/in/values.XXXXXX)"
    # one byte past checks.py's cap, so checks.py refuses a bigger push
    r" && head -c 1000001 > \"$f\""
    r" && chmod 644 \"$f\""
    r" && mv \"$f\" /var/lib/ccs/in/values"
)
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT)


def usage() -> NoReturn:
    print(USAGE, file=sys.stderr)
    sys.exit(2)


def read_config() -> dict[str, str]:
    """Return the config's KEY=value lines as a mapping.

    A quote or a space would mean one thing here and another to systemd.
    """
    lines = CONFIG.read_text().splitlines()
    pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
    settings = [line for line in lines if pattern.match(line)]
    for line in settings:
        if re.search(r"[\s'\"\\]", line):
            raise lib.Stop(f"write {CONFIG} as KEY=value lines, with no quotes: {line}")
    return dict(line.split("=", 1) for line in settings)


def load_config() -> dict[str, str]:
    """Make the config and alert URL files on the first install; return the config.

    Its RELEASES and SITE replace lib's defaults.
    """
    lib.run("install", "-d", ETC)
    if not CONFIG.exists():
        lib.run("install", "-m", "0600", lib.DEPLOY / "config.example", CONFIG)
    ALERT_URL.touch()
    CONFIG.chmod(0o600)
    ALERT_URL.chmod(0o600)
    config = read_config()
    lib.RELEASES = config.get("RELEASES", lib.RELEASES)
    lib.SITE = config.get("SITE", lib.SITE)
    return config


def takes_passwords(settings: str) -> bool:
    """Return whether the settings that sshd -T prints let a password alone log in."""
    found = {}
    for line in settings.splitlines():
        name, _, value = line.partition(" ")
        found[name] = value
    password = found.get("passwordauthentication") != "no"
    kbd = found.get("kbdinteractiveauthentication") != "no"
    methods = found.get("authenticationmethods")
    keyed = methods is not None and all(
        re.search(r"(^|,)publickey(,|$)", method) for method in methods.split()
    )
    return (password or kbd) and not keyed


def check_keys_only() -> None:
    """Stop while sshd's global settings let a password alone log in."""
    # sshd -T reads the files, not the running sshd, and skips most Match blocks
    settings = subprocess.run(["sshd", "-T"], stdout=subprocess.PIPE, text=True)
    if settings.returncode != 0 or takes_passwords(settings.stdout):
        raise lib.Stop(
            "sshd may take passwords: turn PasswordAuthentication and"
            " KbdInteractiveAuthentication off in /etc/ssh/sshd_config.d/,"
            " reload ssh, then run this again"
        )


def check_tls_key() -> None:
    """Stop when other users can read the TLS key, or the file its link points at."""
    key = Path("/etc/ssl/ccs/privkey.pem")
    if not key.exists():
        return
    key = key.resolve()
    if key.stat().st_mode & stat.S_IROTH:
        raise lib.Stop(f"make {key} readable by root only: chmod 600 {key}")


def unreadable(ccs_back: str) -> list[str]:
    """Return ccs-back's files and folders that other users cannot read."""
    found = []
    for name in ["proposals.json", "complete.json", "proposals"]:
        path = f"{ccs_back}/{name}"
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and not mode & stat.S_IROTH:
            found.append(path)
        elif stat.S_ISDIR(mode) and (mode & 0o005) != 0o005:
            found.append(path)
    return found


def check_backend_config(
    settings: dict[str, str], web_host: str, ccs_back: str
) -> None:
    """Check the two settings, given or from the config; given ones win."""
    if web_host or ccs_back:
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", web_host):
            usage()
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", ccs_back):
            usage()
        settings.update(WEB_HOST=web_host, CCS_BACK=ccs_back)
    if not settings.get("WEB_HOST") or not settings.get("CCS_BACK"):
        usage()

    # the public name can point at the old site, and ssh would trust its key
    public = lib.SITE.split("://", 1)[-1].split("/", 1)[0]
    if settings["WEB_HOST"] == public:
        raise lib.Stop(
            "set WEB_HOST to the web server's own address, not the public name"
        )
    # values.py and the monitor read these as temporary users
    found = "\n".join(unreadable(settings["CCS_BACK"]))
    if found:
        raise lib.Stop(f"make ccs-back's files readable by all users: {found}")


def check_push_key_args(backend_ip: str, push_key: str) -> None:
    """Check the two values of the push key's line."""
    # sshd matches from= to a host name only with UseDNS on
    if not re.fullmatch(r"[0-9A-Fa-f.:]+", backend_ip):
        usage()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+=*( [A-Za-z0-9@._-]+)?", push_key):
        raise lib.Stop("the push key must be the one line install.py backend printed")


def make_keyrings_readable() -> None:
    """Make the keyrings and the address list readable by the services' users."""
    for name in ["release-signers.gpg", *ADDRESS_FILES]:
        if (ETC / name).exists():
            (ETC / name).chmod(0o644)


def install_packages(role: str) -> None:
    """Install the role's packages, but not the ones they only recommend."""
    options = ["-y", "--no-upgrade", "--no-install-recommends"]
    lib.run("apt-get", "install", *options, *PACKAGES, *ROLE_PACKAGES[role])


def verify_release(tag: str, version: int, download: Path) -> None:
    """Download the release into the folder, and have checks.py unpack it as tree."""
    try:
        lib.get_release(tag, download)
    except lib.FAILED as error:
        raise lib.Stop(f"can't download {tag}") from error
    tree = download / "tree"
    if not lib.accepts("release", download, lib.RELEASE_SIGNERS, version, tree):
        raise lib.Stop(f"{tag} doesn't verify")


def download_release(tag: str, version: int, release: Path, old: Path | None) -> None:
    """Download and verify the release, then rename it into place.

    A release older than the one installed is refused.
    """
    if old and version < int(old.name):
        raise lib.Stop(f"{tag} is older than the installed release")
    (LIB / "releases").mkdir(parents=True, exist_ok=True)
    for killed in LIB.glob("tmp.*"):
        lib.remove(killed)
    # beside releases/, so the move is one rename and leaves no partial release
    download = Path(tempfile.mkdtemp(prefix="tmp.", dir=LIB))
    try:
        verify_release(tag, version, download)
        lib.rename_synced(download / "tree", release)
    finally:
        lib.remove(download)


def unit_files(folder: Path) -> list[Path]:
    """Return the folder's services, paths and timers."""
    kinds = ["service", "path", "timer"]
    return [unit for kind in kinds for unit in sorted(folder.glob(f"*.{kind}"))]


def install_web_files(source: Path) -> None:
    """Install the web server's users, folders, nginx site and nginx sandbox."""
    sysusers = "/etc/sysusers.d/ccs-web.conf"
    lib.run("install", "-D", "-m", "0644", source / "tmpfiles.conf", TMPFILES)
    lib.run("install", "-D", "-m", "0644", source / "sysusers.conf", sysusers)
    site = "/etc/nginx/sites-enabled/ccs.conf"
    lib.run("install", "-m", "0644", source / "nginx.conf", site)
    Path("/etc/nginx/sites-enabled/default").unlink(missing_ok=True)
    sandbox = UNITS / "nginx.service.d/ccs-sandbox.conf"
    lib.run("install", "-D", "-m", "0644", source / "nginx-sandbox.conf", sandbox)


def install_files(release: Path, role: str, other: Path | None) -> None:
    """Install the role's units and files; remove each unit only the other has."""
    source = release / "deploy" / role
    others = unit_files(other / "deploy" / role) if other else []
    for unit in others:
        if (UNITS / unit.name).exists() and not (source / unit.name).exists():
            lib.run("systemctl", "disable", "--now", "--", unit.name)
            lib.remove(UNITS / unit.name)
            lib.remove(UNITS / f"{unit.name}.d")
    lib.run("install", "-m", "0644", *unit_files(source), UNITS)
    for service in sorted(source.glob("*.service")):
        sandbox = UNITS / f"{service.name}.d/sandbox.conf"
        lib.run("install", "-D", "-m", "0644", release / "deploy/sandbox.conf", sandbox)
    if role == "web":
        install_web_files(source)


def write_monitor_view(ccs_back: str) -> None:
    """Hide the backend's files from the monitor, but for ccs-back and the values.

    It renders what the web server sends, so it must not see the wallet's files.
    """
    lib.write(
        UNITS / "ccs-monitor.service.d/view.conf",
        "[Service]\n"
        "TemporaryFileSystem=/var:ro /srv:ro /opt:ro\n"
        f"BindReadOnlyPaths={ccs_back} /var/lib/ccs-backend",
    )


def write_backend_units(ccs_back: str) -> None:
    """Point the values path at ccs-back's JSON, and the monitor at its folder."""
    lib.run("install", "-d", UNITS / "ccs-values.path.d")
    # the empty PathChanged= clears the unit's own path
    lib.write(
        UNITS / "ccs-values.path.d/path.conf",
        f"[Path]\nPathChanged=\nPathChanged={ccs_back}/proposals.json",
    )
    write_monitor_view(ccs_back)


def start_backend(settings: dict[str, str]) -> None:
    """Make the backend's keys and files on the first install; start its units."""
    push_key, heartbeat_url = ETC / "push_key", ETC / "heartbeat_url"
    if not push_key.exists():
        keygen = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ccs-push"]
        lib.run(*keygen, "-f", push_key)
    # the monitor's unit loads it, and does not start without it
    heartbeat_url.touch()
    push_key.chmod(0o600)
    heartbeat_url.chmod(0o600)
    known_hosts, web_host = ETC / "known_hosts", settings["WEB_HOST"]
    if not lib.has_data(known_hosts):
        with open(known_hosts, "w") as file:
            lib.run("ssh-keyscan", "-q", "-t", "ed25519", web_host, stdout=file)
        if not lib.has_data(known_hosts):
            raise lib.Stop(f"can't read {web_host}'s host key")

    write_backend_units(settings["CCS_BACK"])
    lib.run("systemctl", "daemon-reload")
    lib.run(
        "systemctl", "enable", "--now", "tor", "ccs-values.path", "ccs-monitor.timer"
    )

    print(
        "The web server's host key; check it matches"
        " ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub there:"
    )
    lib.run("ssh-keygen", "-lf", known_hosts)


def write_push_key(backend_ip: str, push_key: str) -> None:
    """Let the key, from that address only, replace in/values and nothing else."""
    line = f'restrict,from="{backend_ip}",command="{PUSH_COMMAND}" {push_key}'
    lib.write(AUTHORIZED_KEYS, line)


def write_backend_config(settings: dict[str, str]) -> None:
    """Write WEB_HOST and CCS_BACK into the config lines that set them."""
    text = CONFIG.read_text()
    for name in ["WEB_HOST", "CCS_BACK"]:
        # neither value allows a \, so both are safe as re.sub's replacement
        text = re.sub(f"^{name}=.*", f"{name}={settings[name]}", text, flags=re.M)
    CONFIG.write_text(text)


def renders() -> bool:
    """Render with the current release; with a certificate, have nginx check it."""
    # twice, as systemd joins a start to a run already going
    for _ in range(2):
        if not lib.succeeds("systemctl", "start", "ccs-update.service"):
            return False
    return not CERT.exists() or lib.succeeds("nginx", "-t")


def start_web(tag: str) -> None:
    """Start the units, and stop unless the new release renders."""
    lib.run("systemctl", "daemon-reload")
    lib.run("systemctl", "enable", "--now", "ccs-fetch.path", "ccs-fetch.timer")
    if not renders():
        raise lib.Stop(f"{tag} failed to render, or nginx refused its site")
    if CERT.exists():
        lib.run("systemctl", "restart", "nginx")
        # an nginx from before its sandbox deletes the pid file link as it stops
        lib.run("systemd-tmpfiles", "--create", TMPFILES)
    else:
        print(
            "Put the certificate in /etc/ssl/ccs/ (fullchain.pem,"
            " privkey.pem), then run this command again."
        )


def place(role: str, release: Path, other: Path | None) -> None:
    """Point current at the release, and install its files in place of the other's."""
    lib.swap(release, LIB / "current")
    install_files(release, role, other)
    if role == "web":
        lib.run("systemd-sysusers")
        lib.run("systemd-tmpfiles", "--create", TMPFILES)


def start_role(role: str, release: Path, settings: dict[str, str]) -> None:
    """Start the role on the release in current."""
    if role == "backend":
        start_backend(settings)
    else:
        start_web(f"site-v{release.name}")


@dataclass
class Install:
    """The role, the release it moves to, the one it leaves, and the settings."""

    role: str
    new: Path
    old: Path | None
    downloaded: bool
    settings: dict[str, str]
    given: list[str]

    @property
    def tag(self) -> str:
        return f"site-v{self.new.name}"


def switch(install: Install) -> None:
    """Place the new release, write the settings given, and start the role."""
    place(install.role, install.new, install.old)
    if any(install.given):
        if install.role == "backend":
            write_backend_config(install.settings)
        else:
            write_push_key(*install.given)
    start_role(install.role, install.new, install.settings)


def undo(install: Install, old: Path, saved: Path) -> None:
    """Put back the saved files and the old release; delete a release this run made."""
    lib.log(f"Going back to release {old.name}.")
    for path in [CONFIG, AUTHORIZED_KEYS]:
        if (saved / path.name).exists():
            shutil.copy2(saved / path.name, path)
        else:
            path.unlink(missing_ok=True)
    install.settings.update(read_config())
    place(install.role, old, install.new)
    start_role(install.role, old, install.settings)
    if install.downloaded and install.new != old:
        lib.remove(install.new)
    lib.remove(saved)
    lib.log(f"Back on release {old.name}, its units, settings and site.")


def log_failure(error: BaseException) -> None:
    """Log why a step failed, as lib.main does; a stop signal's exit says nothing."""
    if isinstance(error, lib.FAILED):
        lib.log_failed(error)
    elif not isinstance(error, SystemExit):
        traceback.print_exception(error)


def go_back(install: Install, saved: Path) -> None:
    """Undo the switch; when the undo stops, log how to end it."""
    if install.old is None:
        lib.remove(saved)
        return
    try:
        undo(install, install.old, saved)
    except Exception as error:
        log_failure(error)
        lib.log(f"The undo stopped. To end it, install site-v{install.old.name} again.")


def announce(role: str, tag: str) -> None:
    """Post the install to the alert channel, so a wrong alert URL shows now."""
    if not lib.has_data(ALERT_URL):
        print("/etc/ccs/alert_url is empty, so no alert reaches anyone yet.")
        return
    env = {**os.environ, "CREDENTIALS_DIRECTORY": str(ETC)}
    if role == "backend":
        # the backend posts through Tor, so the alert service never learns its address
        env["ALL_PROXY"] = "socks5h://localhost/run/tor/socks"
    with tempfile.NamedTemporaryFile("w") as message:
        message.write(f"ccs install: {tag}, {role} role\n")
        message.flush()
        data = ["--data-binary", f"@{message.name}"]
        posted = lib.request_url("alert_url", *data, env=env)
    if posted:
        print("The alert channel has the install.")
    else:
        print(
            "The install's alert didn't arrive;"
            " deploy/docs/ALERTS.md lists the usual causes."
        )


def print_next_steps(role: str, tag: str) -> None:
    """Name the files still to place, and on the backend the web server's command."""
    for name in ADDRESS_FILES:
        if not (ETC / name).exists():
            print(f"Still to place by hand: /etc/ccs/{name}")
    if role == "backend":
        push_key = (ETC / "push_key.pub").read_text().rstrip("\n")
        print("On the web server, run:")
        print(f"  install.py web {tag} <backend IP address> '{push_key}'")


def finish(install: Install) -> None:
    """Keep the release before, to go back to by name, and say what was installed."""
    if install.old != install.new:
        lib.keep(f"{LIB}/releases/", install.new, install.old or "")
    announce(install.role, install.tag)
    print_next_steps(install.role, install.tag)


def check_machine(role: str, args: list[str]) -> dict[str, str]:
    """Check the keyring, the config and the machine, and install the role's packages.

    Return the settings: the config's, with the backend's given ones.
    """
    if not lib.has_data(lib.RELEASE_SIGNERS):
        raise lib.Stop(
            f"put the release signers' keyring in {lib.RELEASE_SIGNERS} first"
        )
    # first, as the config can name another release host
    settings = load_config()
    if role == "backend":
        check_backend_config(settings, *args)
    else:
        if any(args) or not AUTHORIZED_KEYS.exists():
            check_push_key_args(*args)
        check_tls_key()
    make_keyrings_readable()
    install_packages(role)
    if role == "web":
        check_keys_only()
    return settings


def save_settings() -> Path:
    """Copy the config and the push key into a new folder, for an undo."""
    saved = Path(tempfile.mkdtemp())
    for path in [CONFIG, AUTHORIZED_KEYS]:
        if path.exists():
            shutil.copy2(path, saved)
    return saved


def write_to_log() -> tuple[int, int]:
    """Send all output to the log; return the old stdout and stderr."""
    # the commands write to the same file, so each line must go out at once
    lib.line_buffered()
    terminal = os.dup(1), os.dup(2)
    log = os.open(INSTALL_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(log)
    return terminal


def show_log(stream: int) -> None:
    """Copy the log to the operator's stdout or stderr, unless the session dropped."""
    with contextlib.suppress(OSError), open(stream, "wb", closefd=False) as out:
        out.write(INSTALL_LOG.read_bytes())


def ignore_stops() -> None:
    """Ignore INT, TERM and QUIT, here and in each command run from now on."""
    for number in STOPS:
        signal.signal(number, signal.SIG_IGN)


def on_stop(number: int, frame: object) -> None:
    """End the install as a failed step does, and ignore any later stop."""
    ignore_stops()
    sys.exit(1)


def switch_over(install: Install) -> None:
    """Switch to the new release, and go back to the old one when anything stops it.

    The output goes to the log, which the operator sees once it ends.
    """
    saved = save_settings()
    writes = f"the rest of the install writes to {INSTALL_LOG}"
    print(f"Switching to {install.tag}; {writes}.", flush=True)
    stdout, stderr = write_to_log()
    try:
        # a hangup stops neither the switch nor the undo
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        for number in STOPS:
            signal.signal(number, on_stop)
        switch(install)
    except BaseException as error:
        ignore_stops()
        try:
            log_failure(error)
            go_back(install, saved)
        finally:
            show_log(stderr)
        sys.exit(1)
    try:
        lib.remove(saved)
        finish(install)
    except BaseException as error:
        log_failure(error)
        sys.exit(1)
    finally:
        show_log(stdout)


def main() -> None:
    # the services must read what this writes; the secrets are made root-only
    os.umask(0o022)
    # a stop before the switch ends the install at once: nothing needs an undo, and a
    # raise would kill a running apt-get
    for number in STOPS:
        signal.signal(number, signal.SIG_DFL)
    role, tag, *args = (sys.argv[1:] + ["", "", "", ""])[:4]
    version = lib.release_tag(tag)
    if role not in ["backend", "web"] or version is None:
        usage()
    settings = check_machine(role, args)

    # a release already on disk was checked when it arrived
    new = LIB / "releases" / str(version)
    current = LIB / "current"
    old = Path(os.readlink(current)) if current.is_symlink() else None
    downloaded = not new.is_dir()
    if downloaded:
        download_release(tag, version, new, old)
    switch_over(Install(role, new, old, downloaded, settings, args))


if __name__ == "__main__":
    lib.main(main)
