"""A site the monitor checks: its pages, ccs-back's checkout, and the backend."""

import hashlib
import json
import shutil
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fake_server import FakeServer
from fetching import Fetcher
from scripts import EXAMPLE, REPO, replaced, run
from signing import RELEASE_SIGNERS, Keys, write_list
from web import A, B

ETC = Path("/etc/ccs")
VALUES = Path("/var/lib/ccs-backend/values")
# monitor.sh's list_always, without the QR codes
ALWAYS = """index.html ideas/index.html funding-required/index.html
    work-in-progress/index.html completed-proposals/index.html donate/index.html
    index.php/projects feed.xml""".split()


class SiteServer:
    """Serve a folder as the live site, and note the path of each request."""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
        self.httpd.site = self
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def serve(self, root: Path) -> None:
        self.root, self.requested = root, []

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class SiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, request, address, server):
        super().__init__(request, address, server, directory=server.site.root)

    def log_request(self, *args):
        self.server.site.requested.append(unquote(urlsplit(self.path).path)[1:])


class Monitored:
    """A served site, ccs-back's checkout, and the monitor's state, under root.

    The monitor also reads /etc/ccs and the backend's values, which only one
    site at a time can own: copy() puts back the baseline's.
    """

    def __init__(self, root: Path, server: FakeServer, site_server: SiteServer):
        self.root, self.server, self.site_server = root, server, site_server
        self.site, self.state = root / "site", root / "state"
        self.checkout = root / "storage/proposals"
        self.env = {
            "SITE": site_server.url,
            "RELEASES": f"file://{root}/releases",
            "GITLAB_API": server.api,
            "CCS_BACK": str(root / "storage"),
            "STATE_DIRECTORY": str(self.state),
            "CREDENTIALS_DIRECTORY": str(server.credentials),
        }

    @classmethod
    def build(cls, root: Path, keys: Keys, site_server: SiteServer) -> "Monitored":
        """Build release 1, a site rendered from it, and one monitor run."""
        server = FakeServer(root / "server")
        built = cls(root, server, site_server)
        fetcher = Fetcher(root / "web", server)
        shutil.copytree(fetcher.work, built.checkout)
        built.publish_merge_requests()
        built.publish_release(keys)

        shutil.rmtree(ETC, ignore_errors=True)
        ETC.mkdir()
        keys.keyring(ETC / "release-signers.gpg", *RELEASE_SIGNERS)
        keys.keyring(ETC / "address-signers.gpg", "addresser")
        write_list(ETC / "addresses.txt", [A, B], keys)
        shutil.copytree(ETC, root / "etc")
        # as live, every proposal that takes donations has a listed address
        values = replaced(
            (EXAMPLE / "values").read_text(),
            "proposal example-no-address raised=0 contributions=0 address=-\n",
            f"proposal example-no-address raised=0 contributions=0 address={B}\n",
        )
        values += f"gitlab sha256={'0' * 64}\nupdated {int(time.time())}\n"
        VALUES.parent.mkdir(parents=True, exist_ok=True)
        for path in (VALUES, root / "values", fetcher.ccs / "in/values"):
            path.write_text(values)

        fetcher.fetch()
        env = {"CCS_DIR": str(fetcher.ccs), "SITE": site_server.url}
        run(built.release(1) / "deploy/web/update.sh", env=env)
        shutil.copytree((fetcher.ccs / "current").resolve(), built.site)
        site_server.serve(built.site)
        built.monitor()
        server.stop()
        return built

    def publish_merge_requests(self) -> None:
        """Give the fake GitLab the example's open merge requests."""
        shutil.copy(EXAMPLE / "mr/open.json", self.server.state / "mrs.json")
        changes: dict[str, list] = {}
        for line in (EXAMPLE / "mr/open.tsv").read_text().splitlines():
            iid, name = line.split("\t")
            changes.setdefault(iid, []).append({"new_path": name, "new_file": True})
        for iid, diffs in changes.items():
            path = self.server.state / "changes" / f"{iid}.json"
            path.write_text(json.dumps({"changes": diffs}))

    def publish_release(self, keys: Keys) -> None:
        """Publish release 1 of this repo, signed, and install it."""
        folder = self.root / "releases/download/site-v1"
        folder.mkdir(parents=True)
        tar = folder / "ccs-site-v1.tar"
        run("tar", "-cf", tar, "--exclude=__pycache__", "-C", REPO, "dist", "deploy")
        digest = hashlib.sha256(tar.read_bytes()).hexdigest()
        (folder / "SHA256SUMS").write_text(f"{digest}  {tar.name}\n")
        keys.sign(folder / "SHA256SUMS", *RELEASE_SIGNERS)
        shutil.unpack_archive(tar, self.release(1), filter="tar")

    def copy(self, root: Path, server: FakeServer) -> "Monitored":
        """Copy this site to root, serve the copy, and use server as its GitLab."""
        shutil.rmtree(ETC)
        shutil.copytree(self.root / "etc", ETC)
        shutil.copy(self.root / "values", VALUES)
        copy = Monitored(root, server, self.site_server)
        for name in ("site", "state", "storage", "lib", "releases"):
            shutil.copytree(self.root / name, root / name)
        # ccs-back's checkout belongs to another user, as live
        run("chown", "-R", "1234:1234", copy.checkout)
        copy.publish_merge_requests()
        self.site_server.serve(copy.site)
        return copy

    def release(self, version: int) -> Path:
        return self.root / "lib/releases" / str(version)

    def monitor(self, release: int = 1, ok: bool = True, **env: str):
        script = self.release(release) / "deploy/backend/monitor.sh"
        return run(script, ok=ok, env={**self.env, **env})

    def failures(self) -> list[str]:
        """Return the checks failing now, as the alert state names them."""
        return sorted(path.name for path in (self.state / "alerts").glob("[!.]*"))

    def age(self, name: str, seconds: int, field: int = 0) -> None:
        """Date a check's first failure seconds ago, or with field=1 its last alert."""
        parts = self.alert_state(name)
        parts[field] = str(int(time.time()) - seconds)
        (self.state / "alerts" / name).write_text(" ".join(parts))

    def failing_for(self, name: str, seconds: int) -> None:
        """Record a check as failing since seconds ago, so a failing run alerts."""
        (self.state / "alerts" / name).write_text(f"{int(time.time()) - seconds} 0 -\n")

    def reason(self, name: str) -> str:
        """Return the text a failing check records."""
        return self.alert_state(name)[2].rstrip("\n")

    def alert_state(self, name: str) -> list[str]:
        """Return a failing check's first failure, last alert and text."""
        path = self.state / "alerts" / name
        assert path.exists(), f"{name} is not failing; failing now: {self.failures()}"
        return path.read_text().split(" ", 2)

    def forget_verification(self) -> None:
        """Make GitLab's last check of the merge requests over an hour old."""
        path = self.state / "mrs-verified"
        digest = path.read_text().split()[0]
        path.write_text(f"{digest} {int(time.time()) - 3700}\n")

    def expected(self) -> list[str]:
        """Return each file of the expected render, sorted as the monitor sorts them."""
        root = self.state / "expected"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def flip(self, path: str) -> None:
        """Flip one bit of a served file. A second flip puts it back."""
        data = bytearray((self.site / path).read_bytes())
        data[-1] ^= 1
        (self.site / path).write_bytes(data)

    def edit_merge_requests(self, edit) -> None:
        """Change the merge requests in the site's status.json with edit(mrs)."""
        path = self.site / "status.json"
        status = json.loads(path.read_text())
        edit(status["merge_requests"])
        path.write_text(json.dumps(status))


def page(path: str) -> str:
    """Return the name of the check that compares the file at path."""
    return "page-" + hashlib.sha256(path.encode()).hexdigest()[:16]
