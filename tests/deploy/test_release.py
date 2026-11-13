"""release.sh: two identical builds, attestation, the review, and its refusals."""

import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_server import FakeServer
from scripts import REPO, refuses, replaced, run, tail
from signing import RELEASE_SIGNERS

CAP_SYS_ADMIN = 21
CAPABILITIES = re.search(
    r"^CapEff:\s*(\w+)$", Path("/proc/self/status").read_text(), re.M
)
PRIVILEGED = bool(int(CAPABILITIES[1], 16) >> CAP_SYS_ADMIN & 1)
REASON = (
    "podman cannot run here: run the file with tests/deploy/run.sh, under rootless "
    "podman"
)
if not PRIVILEGED and os.environ.get("CI"):
    pytest.fail(REASON, pytrace=False)
pytestmark = pytest.mark.skipif(not PRIVILEGED, reason=REASON)

# an Astro hook that writes into deploy/ while the site builds
PLANT = (
    'integrations: [mdx(), { name: "plant", hooks: { "astro:build:done": () => '
    'import("node:fs").then((fs) => fs.appendFileSync("deploy/checks.py", '
    '"# PLANTED\\n")) } }],'
)
ESCAPE = "\x1b[8mhidden\x1b[0m\n"
# a lockfile entry that no package uses, so pnpm never fetches it
PACKAGE = (
    "  planted-package@1.0.0:\n"
    f"    resolution: {{integrity: sha512-{'A' * 86}==}}\n\n"
)


@dataclass
class Build:
    """One release.sh run of a tag, and the folder it writes, release-<tag>."""

    tag: str
    result: subprocess.CompletedProcess[str]
    out: Path

    @property
    def review(self) -> str:
        """Return what release.sh shows in less, from "Changes since" to its question."""
        stdout = self.result.stdout
        return stdout[stdout.index("Changes since ") : stdout.rindex("Sign release-")]

    @property
    def said(self) -> str:
        """Return the end of what release.sh said outside the review, on both streams."""
        before, _, after = self.result.stdout.partition(self.review)
        return f"{tail(before + after)}\n{tail(self.result.stderr)}"

    @property
    def sums(self) -> str:
        return (self.out / "SHA256SUMS").read_text()

    def members(self) -> list[tarfile.TarInfo]:
        with tarfile.open(self.out / f"ccs-{self.tag}.tar") as archive:
            return archive.getmembers()


def tag(repo: Path, name: str, edits: dict[str, Callable[[str], str]]):
    """Commit edits to site-v1's files, and tag the commit as name."""
    run("git", "-C", repo, "checkout", "-q", "--detach", "site-v1")
    for path, edit in edits.items():
        file = repo / path
        file.write_text(edit(file.read_text()))
    run("git", "-C", repo, "commit", "-q", "-a", "-m", name)
    run("git", "-C", repo, "tag", name)


def checkout(repo: Path, tag: str, folder: Path) -> Path:
    """Check the tag out into a new worktree in folder, as a builder does."""
    worktree = folder / tag
    run("git", "-C", repo, "worktree", "add", "-q", "--detach", worktree, tag)
    return worktree


def release(
    worktree: Path,
    *args: str,
    env: dict[str, str],
    answer: str = "n",
    refusal: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the worktree's release.sh, and answer its question.

    With refusal given, the run must stop and say it on stderr.
    """
    script = worktree / "deploy/release.sh"
    options = {"cwd": worktree, "env": env, "input": f"{answer}\n", "timeout": 600}
    if refusal:
        return refuses(script, *args, reason=refusal, **options)
    return run(script, *args, **options)


def build(
    repo: Path, folder: Path, tag: str, builder: str, env: dict[str, str], answer="n"
) -> Build:
    """Build the tag in a fresh worktree in folder."""
    worktree = checkout(repo, tag, folder)
    result = release(worktree, tag, builder, env=env, answer=answer)
    return Build(tag, result, worktree / f"release-{tag}")


def tree(repo: Path, root: Path) -> str:
    """Return root's files as a git tree in repo, as ATTESTATIONS_REF takes one."""
    git = {
        "GIT_DIR": str(repo / ".git"),
        "GIT_WORK_TREE": str(root),
        "GIT_INDEX_FILE": f"{root}.index",
    }
    run("git", "add", "-A", cwd=root, env=git)
    return run("git", "write-tree", env=git).stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """Return a clone of the repo, tagged with the releases the tests build.

    site-v1 holds the working tree's deploy/, which is the code under test.
    """
    repo = tmp_path_factory.mktemp("repo")
    run("git", "clone", "-q", "--no-tags", "--no-hardlinks", REPO, repo)
    run("git", "-C", repo, "config", "user.name", "tester")
    run("git", "-C", repo, "config", "user.email", "tester@example.org")
    shutil.rmtree(repo / "deploy")
    shutil.copytree(REPO / "deploy", repo / "deploy")
    run("git", "-C", repo, "add", "-A", "deploy")
    run("git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "deploy/ under test")
    run("git", "-C", repo, "tag", "site-v1")
    layout = "src/layouts/Layout.astro"
    css = "src/styles/global.css"
    unit = "deploy/web/ccs-update.service"
    lock = "pnpm-lock.yaml"
    tag(
        repo,
        "site-v2",
        {
            layout: lambda text: replaced(text, "Monero CCS", "Monero CCS (v2)"),
            css: lambda text: text + ".planted {\n  color: red;\n}\n",
        },
    )
    tag(
        repo,
        "site-v3",
        {
            "astro.config.mjs": lambda text: replaced(
                text, "integrations: [mdx()],", PLANT
            ),
            unit: lambda text: replaced(
                text,
                "Type=oneshot\nUser=ccs-update\n",
                "Type=oneshot User=ccs-update\n",
            ),
            lock: lambda text: replaced(
                text,
                "\nignoredOptionalDependencies:",
                f"\n{PACKAGE}ignoredOptionalDependencies:",
            ),
        },
    )
    tag(repo, "site-v4", {"deploy/README.md": lambda text: text + ESCAPE})
    tag(
        repo,
        "site-v5",
        {
            "package.json": lambda text: replaced(
                text, '"packageManager": "pnpm@', '"packageManager": "pnpm@0'
            ),
        },
    )
    return repo


@pytest.fixture(scope="module")
def homes(tmp_path_factory, keys) -> dict[str, Path]:
    """Return a gpg home for each signer, holding only that signer's secret key."""
    homes = {}
    for name in ("signer-a", "signer-b", "outsider"):
        home = tmp_path_factory.mktemp(f"gnupg-{name}")
        secret = keys.gpg("--armor", "--export-secret-keys", name).decode()
        run("gpg", "--homedir", home, "--batch", "--import", input=secret)
        homes[name] = home
    return homes


@pytest.fixture(scope="module")
def keyring(tmp_path_factory, keys) -> Path:
    path = tmp_path_factory.mktemp("keyring") / "release-signers.gpg"
    return keys.keyring(path, *RELEASE_SIGNERS)


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    server = FakeServer(tmp_path_factory.mktemp("host"))
    yield server
    server.stop()


@pytest.fixture(scope="module")
def env(tmp_path_factory, repo, host, keyring, homes) -> dict[str, str]:
    """Return release.sh's environment, with no attestations, as signer-a."""
    return {
        "RELEASES": host.url,
        "KEYRING": str(keyring),
        "ATTESTATIONS_REF": tree(repo, tmp_path_factory.mktemp("no-attestations")),
        "GNUPGHOME": str(homes["signer-a"]),
    }


@pytest.fixture(scope="module")
def first(tmp_path_factory, repo, env, homes) -> Build:
    """Build release 1, attested by carol, who is not a release signer."""
    carol = {**env, "GNUPGHOME": str(homes["outsider"])}
    folder = tmp_path_factory.mktemp("first")
    return build(repo, folder, "site-v1", "carol", carol, answer="y")


def test_sha256sums_is_sha256sums_own_output(first):
    checked = run("sha256sum", "--check", "SHA256SUMS", cwd=first.out).stdout
    assert checked == "ccs-site-v1.tar: OK\n"


def test_the_first_release_is_reviewed_whole_against_nothing(first):
    assert first.review.startswith("Changes since nothing:\n")
    assert "new/deploy/install.sh" in first.review


def test_the_archive_holds_dist_and_deploy(first):
    assert {m.name.split("/")[0] for m in first.members()} == {"dist", "deploy"}


def test_every_file_is_owned_by_root_and_dated_at_the_tags_commit(first, repo):
    commit_time = int(
        run("git", "-C", repo, "log", "-1", "--format=%ct", "site-v1").stdout
    )
    found = {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in first.members()}
    assert found == {(0, 0, "", "", commit_time)}


def test_deploy_keeps_the_tags_file_modes(first, repo):
    listed = run("git", "-C", repo, "ls-tree", "-r", "site-v1", "deploy").stdout
    modes = {"100755": 0o755, "100644": 0o644}
    expected = {}
    for line in listed.splitlines():
        details, name = line.split("\t")
        expected[name] = modes[details.split()[0]]
    found = {
        member.name: member.mode
        for member in first.members()
        if member.isfile() and member.name.startswith("deploy/")
    }
    assert found == expected


def test_a_release_is_not_ready_until_two_release_signers_attest(first):
    assert "Not ready to publish" in first.said, first.said
    assert not (first.out / "SHA256SUMS.asc").exists()


def test_an_outsider_can_attest_but_does_not_count(first):
    attestation = first.out / "attestations/site-v1/carol/SHA256SUMS"
    assert attestation.read_text() == first.sums
    assert "your key isn't in" in first.said, first.said
