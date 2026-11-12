"""fetch.sh's folders, the proposals repository it fetches, and its actions."""

import json
import os
import shutil
import time
from pathlib import Path
from subprocess import CompletedProcess

from fake_server import FakeServer
from scripts import DEPLOY, EXAMPLE, run


def git(repo: Path, *args: str) -> str:
    return run("git", "-C", repo, *args).stdout.strip()


def new_repository(repo: Path) -> None:
    run("git", "init", "-q", "-b", "master", repo)
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")


def merge(repo: Path, branch: str, name: str, text: str, request: str) -> None:
    """Add a file on a branch, and merge it as GitLab merges a merge request."""
    git(repo, "checkout", "-q", "-b", branch)
    (repo / name).write_text(text)
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    git(repo, "checkout", "-q", "master")
    message = f"Merge branch '{branch}' into 'master'\n\nAdd {name}\n\n"
    message += f"See merge request {request}"
    git(repo, "merge", "-q", "--no-ff", branch, "-m", message)


class Fetcher:
    """A web server's fetch folders, and an origin with each example proposal.

    Each proposal arrives in its own merge commit, as GitLab writes them. The
    settings that systemd's EnvironmentFile= gives fetch.sh are in env.
    """

    def __init__(self, root: Path, server: FakeServer):
        self.server = server
        self.ccs = root / "ccs"
        (self.ccs / "in").mkdir(parents=True)
        self.work, self.origin = root / "work", root / "origin.git"
        new_repository(self.work)
        proposals = sorted((EXAMPLE / "proposals").glob("*.md"))
        for number, proposal in enumerate(proposals, 1):
            request = f"monero-project/ccs-proposals!{number}"
            merge(self.work, f"b{number}", proposal.name, proposal.read_text(), request)
        git(root, "clone", "-q", "--bare", str(self.work), str(self.origin))
        self.env = {
            "CCS_DIR": str(self.ccs),
            "GITLAB_API": server.api,
            "PROPOSALS_GIT": f"file://{self.origin}",
            "CREDENTIALS_DIRECTORY": str(server.credentials),
        }
        self.mrs(3)
        self.changes(3, [("idea.md", True), ("old.md", False)])

    def mrs(self, count: int) -> None:
        """Open merge requests count down to 1, newest first."""
        mrs = [{"iid": iid, "sha": f"{iid:040d}"} for iid in range(count, 0, -1)]
        (self.server.state / "mrs.json").write_text(json.dumps(mrs))

    def changes(self, iid: int, files: list[tuple[str, bool]]) -> None:
        diffs = [{"new_path": path, "new_file": new} for path, new in files]
        path = self.server.state / "changes" / f"{iid}.json"
        path.write_text(json.dumps({"changes": diffs}))

    def values(self, gitlab: int) -> None:
        """Push values whose gitlab line is this number, written now."""
        text = f"gitlab sha256={gitlab:064d}\nupdated {int(time.time())}\n"
        (self.ccs / "in/values").write_text(text)

    def fetch(self, ok: bool = True, **env: str) -> CompletedProcess[str]:
        return run(DEPLOY / "web/fetch.sh", ok=ok, env={**self.env, **env}, timeout=200)

    def links(self) -> tuple[str, str]:
        return os.readlink(self.ccs / "in/repo"), os.readlink(self.ccs / "in/mr")

    def tip(self) -> str:
        return git(self.origin, "rev-parse", "master")

    def commit(self, name: str) -> None:
        shutil.copy(EXAMPLE / "proposals/example-funding.md", self.work / name)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"add {name}")
        git(self.work, "push", "-q", str(self.origin), "master")

    def rewrite(self) -> None:
        git(self.work, "commit", "-q", "--amend", "-m", "rewritten")
        git(self.work, "push", "-q", "-f", str(self.origin), "master")
