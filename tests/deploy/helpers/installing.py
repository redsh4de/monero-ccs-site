"""One container as both servers, for install.py, and its saved states."""

import json
import os
import shutil
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

from harness import DEPLOY, EXAMPLE, REPO, run, stand_in
from signing import Keys, place_keyrings, publish
from web import as_pushed

LIB = Path("/usr/local/lib/ccs")
CCS_BACK = Path("/srv/ccs-back/storage/app")
PUSH_KEY = Path("/etc/ccs/push_key")
MERGE = Path("/etc/ccs/merge-once")
SYSTEMCTL_LOG = Path("/run/systemctl.log")
# each system path the scripts or tests write; one left out leaks into later tests
STATE = """etc/ccs usr/local/lib/ccs etc/systemd/system etc/tmpfiles.d etc/sysusers.d
    etc/nginx/sites-enabled etc/ssh/sshd_config.d etc/ssl/ccs var/lib/ccs srv/ccs-back
    var/lib/systemd/linger home etc/passwd etc/group etc/shadow etc/gshadow""".split()
# with MERGE, a start returns at once, as systemd does when a run is already going
SYSTEMCTL = f"""#!/bin/sh
echo "$*" >>{SYSTEMCTL_LOG}
[ "$1 $2" = "start ccs-update.service" ] || exit 0
if [ -e "{MERGE}" ]; then rm "{MERGE}"; exit 0; fi
exec runuser -u ccs-update -- /usr/local/lib/ccs/current/deploy/web/update.py
"""


def values() -> str:
    """Return the example values as the backend pushes them, written now."""
    return as_pushed((EXAMPLE / "values").read_text(), int(time.time()))


def push_key() -> str:
    """Return the backend's public push key, as install.py backend prints it."""
    return Path(f"{PUSH_KEY}.pub").read_text().strip()


def make_certificate() -> Path:
    """Make a certificate in /etc/ssl/ccs; return its key, readable by root only."""
    folder = Path("/etc/ssl/ccs")
    folder.mkdir()
    key = folder / "privkey.pem"
    subject = ["-subj", "/CN=ccs.getmonero.org", "-days", "30", "-nodes"]
    files = ["-keyout", key, "-out", folder / "fullchain.pem"]
    run("openssl", "req", "-x509", "-newkey", "ed25519", *subject, *files)
    return key


class Machine:
    """One container as both servers: sshd for the push, and a release host."""

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
        for keyring in place_keyrings(etc, keys):
            keyring.chmod(0o600)
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
        """Run install.py with args: the installed copy, or with first, the repo's."""
        return run(*self.script(first), *args, env={**self.env, **env}, timeout=300)

    def refuses(
        self, *args: str | Path, reason: str, first: bool = False, **env: str
    ) -> None:
        env = {**self.env, **env}
        run(*self.script(first), *args, refusal=reason, env=env, timeout=300)

    def script(self, first: bool) -> list[str | Path]:
        if first:
            # an operator's strict umask, which install.py must not pass on
            return ["sh", "-c", 'umask 077 && exec "$0" "$@"', DEPLOY / "install.py"]
        return [LIB / "current/deploy/install.py"]

    def push(
        self,
        text: str,
        *command: str,
        source: str = "127.0.0.1",
        refusal: str | None = None,
    ) -> CompletedProcess[str]:
        """Push text over ssh with the backend's push key, from the source address."""
        ssh = ["ssh", "-i", PUSH_KEY, "-b", source, "-o", "BatchMode=yes"]
        ssh += ["-o", "UserKnownHostsFile=/etc/ccs/known_hosts"]
        return run(*ssh, "ccs-fetch@127.0.0.1", *command, input=text, refusal=refusal)

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
