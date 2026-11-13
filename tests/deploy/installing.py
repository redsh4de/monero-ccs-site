"""One container as both servers, for install.sh, and its saved states."""

import json
import os
import shutil
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

from scripts import DEPLOY, EXAMPLE, REPO, refuses, run, stand_in
from signing import RELEASE_SIGNERS, Keys, publish

LIB = Path("/usr/local/lib/ccs")
CCS_BACK = Path("/srv/ccs-back/storage/app")
PUSH_KEY = Path("/etc/ccs/push_key")
MERGE = Path("/etc/ccs/merge-once")
# every system path that the scripts or the tests write; a test that writes
# another one adds it here, or the change leaks into the tests after it
STATE = """etc/ccs usr/local/lib/ccs etc/systemd/system etc/tmpfiles.d etc/sysusers.d
    etc/nginx/sites-enabled etc/ssh/sshd_config.d etc/ssl/ccs var/lib/ccs srv/ccs-back
    var/lib/systemd/linger home etc/passwd etc/group etc/shadow etc/gshadow""".split()
# no systemd runs here. A start of ccs-update.service runs update.sh as its
# user. With the merge file, a start returns at once, as when systemd joins it
# to a run already going.
SYSTEMCTL = f"""#!/bin/sh
[ "$1 $2" = "start ccs-update.service" ] || exit 0
if [ -e "{MERGE}" ]; then rm "{MERGE}"; exit 0; fi
exec runuser -u ccs-update -- /usr/local/lib/ccs/current/deploy/web/update.sh
"""


def values() -> str:
    """Return the example values as the backend pushes them, written now."""
    text = (EXAMPLE / "values").read_text()
    return text + f"gitlab sha256={'0' * 64}\nupdated {int(time.time())}\n"


def push_key() -> str:
    """Return the backend's public push key, as install.sh backend prints it."""
    return Path(f"{PUSH_KEY}.pub").read_text().strip()


def make_certificate() -> Path:
    """Make a certificate in /etc/ssl/ccs; return its key, readable by root only."""
    folder = Path("/etc/ssl/ccs")
    folder.mkdir()
    key = folder / "privkey.pem"
    subject = ["-subj", "/CN=ccs.getmonero.org", "-days", "1", "-nodes"]
    files = ["-keyout", key, "-out", folder / "fullchain.pem"]
    run("openssl", "req", "-x509", "-newkey", "ed25519", *subject, *files)
    return key


class Machine:
    """One container as both servers: sshd for the push, and a release host.

    The keyrings are in /etc/ccs, readable by root only, and release 1 is
    published. save() and restore() keep and bring back every path in STATE.
    """

    def __init__(self, root: Path, keys: Keys):
        self.root, self.keys = root, keys
        self.host = root / "host"
        self.sshd_log = root / "sshd.log"
        self.env = {"PATH": stand_in(root / "bin", "systemctl", SYSTEMCTL)}
        run("ssh-keygen", "-A")
        Path("/run/sshd").mkdir(exist_ok=True)
        run("/usr/sbin/sshd", "-E", self.sshd_log)
        CCS_BACK.mkdir(parents=True)
        etc = Path("/etc/ccs")
        etc.mkdir()
        keys.keyring(etc / "release-signers.gpg", *RELEASE_SIGNERS).chmod(0o600)
        keys.keyring(etc / "address-signers.gpg", "addresser").chmod(0o600)
        self.publish(1)

    def publish(self, version: int, edit: Callable[[Path], None] | None = None) -> Path:
        """Publish this repo as that release, or a copy that edit(tree) changed."""
        tree = REPO
        if edit:
            tree = self.root / f"tree-{version}"
            shutil.copytree(REPO / "dist", tree / "dist")
            shutil.copytree(DEPLOY, tree / "deploy")
            edit(tree)
        return publish(self.host, version, self.keys, tree)

    def install(
        self, *args: str | Path, first: bool = False, **env: str
    ) -> CompletedProcess[str]:
        """Run install.sh with args: the installed copy, or with first, the repo's."""
        return run(*self.script(first), *args, env={**self.env, **env}, timeout=300)

    def refuses(self, *args: str | Path, reason: str, first: bool = False) -> None:
        refuses(*self.script(first), *args, reason=reason, env=self.env, timeout=300)

    def script(self, first: bool) -> list[str | Path]:
        if first:
            # an operator's strict umask, which install.sh must not pass on
            return ["sh", "-c", 'umask 077 && exec "$0" "$@"', DEPLOY / "install.sh"]
        return [LIB / "current/deploy/install.sh"]

    def push(
        self, text: str, *command: str, source: str = "127.0.0.1", ok: bool = True
    ) -> CompletedProcess[str]:
        """Push text over ssh with the backend's push key, from the source address."""
        ssh = ["ssh", "-i", PUSH_KEY, "-b", source, "-o", "BatchMode=yes"]
        ssh += ["-o", "UserKnownHostsFile=/etc/ccs/known_hosts"]
        return run(*ssh, "ccs-fetch@127.0.0.1", *command, input=text, ok=ok)

    def save(self, name: str) -> None:
        paths = [path for path in STATE if os.path.lexists(f"/{path}")]
        run("tar", "-cpf", self.root / name, "--numeric-owner", "-C", "/", *paths)

    def restore(self, name: str) -> "Machine":
        """Bring back every path in STATE as it was saved, and return the machine."""
        run("rm", "-rf", *(f"/{path}" for path in STATE))
        run("tar", "-xpf", self.root / name, "--numeric-owner", "-C", "/")
        return self

    def saved(self, name: str, path: Path) -> str:
        """Return a file's text as it was saved."""
        with tarfile.open(self.root / name) as saved:
            return saved.extractfile(str(path)[1:]).read().decode()

    def installed(self) -> str:
        return os.readlink(LIB / "current")

    def served(self) -> int:
        status = Path("/var/lib/ccs/current/status.json").read_text()
        return json.loads(status)["release"]

    def stop(self) -> None:
        run("kill", Path("/run/sshd.pid").read_text().strip())
