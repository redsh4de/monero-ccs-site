"""release.sh: two identical builds, attestation, the review, and its refusals."""

import hashlib
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
from scripts import CHECKS, REPO, refuses, replaced, run, tail
from signing import RELEASE_SIGNERS, Keys, publish

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
OTHER_BYTES = f"{'0' * 64}  ccs-site-v2.tar\n"
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


def attest(root: Path, tag: str, builder: str, sums: str, keys: Keys, signer: str):
    """Write builder's attestation of sums under root, as release.sh writes one."""
    folder = root / "attestations" / tag / builder
    folder.mkdir(parents=True)
    (folder / "SHA256SUMS").write_text(sums)
    keys.sign(folder / "SHA256SUMS", signer)


def digests(archive: Path, folder: str) -> dict[str, str]:
    """Return the sha256 of each file under folder in the archive, by name."""
    with tarfile.open(archive) as files:
        return {
            member.name: hashlib.sha256(files.extractfile(member).read()).hexdigest()
            for member in files.getmembers()
            if member.isfile() and member.name.startswith(folder)
        }


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


@pytest.fixture(scope="module")
def bob(tmp_path_factory, repo, env, homes, keys, first) -> Build:
    """Build release 1 again as bob, after carol and alice attested it."""
    root = tmp_path_factory.mktemp("attested-by-carol-and-alice")
    shutil.copytree(first.out / "attestations", root / "attestations")
    attest(root, "site-v1", "alice", first.sums, keys, "signer-a")
    bob = {
        **env,
        "ATTESTATIONS_REF": tree(repo, root),
        "GNUPGHOME": str(homes["signer-b"]),
    }
    folder = tmp_path_factory.mktemp("bob")
    return build(repo, folder, "site-v1", "bob", bob, answer="y")


def test_a_yes_writes_an_attestation_that_verifies(bob, keyring):
    folder = bob.out / "attestations/site-v1/bob"
    assert (folder / "SHA256SUMS").read_text() == bob.sums
    run("gpgv", "--keyring", keyring, folder / "SHA256SUMS.asc", folder / "SHA256SUMS")


def test_the_second_signer_sees_the_first_signers_attestation(bob):
    assert "The same bytes as alice, a release signer." in bob.said, bob.said


def test_a_builder_sees_an_outsiders_attestation_too(bob):
    assert "The same bytes as carol." in bob.said, bob.said


def test_two_release_signers_make_a_release_that_verifies(bob, keyring, tmp_path):
    assert "Create the GitHub release site-v1" in bob.said, bob.said
    run(CHECKS, "release", bob.out, keyring, "1", tmp_path / "files")


def test_two_builds_of_a_tag_give_the_same_sha256sums(bob, first):
    assert bob.sums == first.sums


@pytest.fixture(scope="module")
def published(tmp_path_factory, host, keys, first) -> None:
    """Publish releases 1 to 3 on the release host, each with first's files.

    Release 2's deploy/README.md also ends in a terminal escape.
    """
    files = tmp_path_factory.mktemp("first-files")
    run("tar", "-xf", first.out / "ccs-site-v1.tar", "-C", files)
    publish(host.state, 1, keys, tree=files)
    publish(host.state, 3, keys, tree=files)
    readme = files / "deploy/README.md"
    readme.write_text(readme.read_text() + ESCAPE)
    publish(host.state, 2, keys, tree=files)


@pytest.fixture(scope="module")
def second(tmp_path_factory, repo, env, published) -> Build:
    """Build release 2, and decline to sign it."""
    return build(repo, tmp_path_factory.mktemp("second"), "site-v2", "tester", env)


def test_no_attestation_without_a_yes(second):
    assert "Not signed." in second.said, second.said
    assert not (second.out / "attestations").exists()


def test_the_review_shows_only_what_changed_since_the_release_before(second):
    assert second.review.startswith("Changes since site-v1:\n")
    assert "new/deploy/install.sh" not in second.review


def test_the_review_lists_the_packages_first(second):
    packages = "Packages since site-v1:\n  none changed\n"
    assert second.review.startswith(f"Changes since site-v1:\n{packages}")


def test_a_changed_page_shows_in_the_review(second):
    assert "(v2)" in second.review


def test_a_planted_css_rule_shows_word_by_word(second):
    assert "{+.planted" in second.review


def test_a_renamed_stylesheet_is_paired_with_the_old_one(second):
    assert re.search(r"style\.[^ ]*\.css => [^ ]*style\.", second.review)


@pytest.fixture(scope="module")
def others(tmp_path_factory, repo, env, keys, homes, published) -> Build:
    """Build release 2 with odd git settings, after two odd attestations.

    eve, who is not a release signer, built other bytes. linky's attestation is
    two links to a file on this machine.
    """
    root = tmp_path_factory.mktemp("attested-by-eve-and-linky")
    attest(root, "site-v2", "eve", OTHER_BYTES, keys, "outsider")
    linky = root / "attestations/site-v2/linky"
    linky.mkdir()
    for name in ("SHA256SUMS", "SHA256SUMS.asc"):
        (linky / name).symlink_to(homes["signer-a"] / "pubring.kbx")
    settings = tmp_path_factory.mktemp("git-settings")
    (settings / "attributes").write_text("deploy/README.md export-ignore\n")
    gitconfig = settings / "config"
    gitconfig.write_text(f"[core]\n\tattributesFile = {settings}/attributes\n")
    odd = {
        **env,
        "ATTESTATIONS_REF": tree(repo, root),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_EXTERNAL_DIFF": "/bin/true",
    }
    return build(repo, tmp_path_factory.mktemp("others"), "site-v2", "tester", odd)


def test_the_builders_git_settings_do_not_change_the_build(others, second):
    assert others.sums == second.sums


def test_an_external_diff_tool_does_not_replace_the_review(others):
    assert "(v2)" in others.review


def test_an_outsiders_different_build_is_only_reported(others):
    reported = "eve, who isn't a release signer, built different bytes"
    assert reported in others.said, others.said
    assert others.review.startswith("Changes since site-v1:\n")


def test_an_attestation_made_of_links_is_never_read(others):
    assert "linky" not in others.said, others.said


@pytest.fixture(scope="module")
def third(tmp_path_factory, repo, env, published) -> Build:
    """Build release 3 from a tag on release 1's branch, not on release 2's."""
    return build(repo, tmp_path_factory.mktemp("third"), "site-v3", "tester", env)


def test_a_tag_off_an_old_branch_compares_with_the_release_before_it(third):
    assert third.review.startswith("Changes since site-v2:\n")


def test_deploy_is_compared_line_by_line_so_a_joined_line_shows(third):
    assert "+Type=oneshot User=ccs-update" in third.review.splitlines()


def test_no_raw_escape_reaches_the_terminal(third):
    assert "\x1b" not in third.review
    assert "^[[8mhidden^[[0m" in third.review


def test_deploy_in_the_archive_is_the_tags_own_even_when_the_build_writes_to_it(
    third, repo, tmp_path
):
    tagged = tmp_path / "site-v3.tar"
    run("git", "-C", repo, "archive", "-o", tagged, "site-v3", "deploy")
    built = third.out / "ccs-site-v3.tar"
    assert digests(built, "deploy/") == digests(tagged, "deploy/")


def test_the_review_lists_a_package_the_tag_added(third):
    assert "  + planted-package@1.0.0" in third.review.splitlines()
