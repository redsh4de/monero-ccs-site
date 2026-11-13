"""values.py: the values the backend pushes, from ccs-back's own files."""

import json
import stat
import time
from urllib.parse import quote

import pytest

from scripts import CHECKS, DEPLOY, refuses, run

A1 = "888tNkZrPN6JsEgekjMnABU4TBzc2Dt29EPAvkRxbANsAnjyPbb3iQ1YBRk1UXcdRsiKc9dhwMVgN5S9cQUiyoogDavup3H"
VALUES_PY = DEPLOY / "backend/values.py"


class CcsBack:
    """ccs-back's JSON files, and its checkout owned by another user."""

    def __init__(self, folder):
        self.folder = folder
        checkout = folder / "proposals"
        run("git", "init", "-q", "-b", "master", checkout)
        self.git = ["git", "-C", checkout, "-c", "safe.directory=*"]
        self.git += ["-c", "user.email=t@t", "-c", "user.name=t"]
        run(*self.git, "commit", "-q", "--allow-empty", "-m", "one")
        run("chown", "-R", "1234:1234", checkout)
        self.write()

    def write(self, raised=10, idea="An idea"):
        stages = [
            {"stage": "Ideas", "proposals": [{"name": idea, "author": "a"}]},
            {"stage": "Funding Required", "proposals": self.funding(raised)},
            {
                "stage": "Work in Progress",
                "proposals": [proposal("example-progress", A1, 12, 60)],
            },
        ]
        (self.folder / "proposals.json").write_text(json.dumps(stages))
        completed = [proposal("example-completed", None, 30, 100)]
        done = [{"stage": "Completed Proposals", "proposals": completed}]
        (self.folder / "complete.json").write_text(json.dumps(done))

    @staticmethod
    def funding(raised):
        return [
            proposal("Core website translation to serbian", A1, 3, raised),
            proposal("fcmp++-development", "not-an-address", 1, 0.00001),
            proposal("text-amount", None, 0, "10.500000000000"),
            proposal("null-amount", None, 0, None),
            proposal("negative", None, 0, -1),
            proposal("huge", None, 0, 1234567890123),
            proposal("minus-zero", None, 0, -0.0),
            proposal("bool", None, True, 1),
        ]

    def commit(self):
        run(*self.git, "commit", "-q", "--allow-empty", "-m", "two")

    def values(self, out):
        return run(VALUES_PY, out, env={"CCS_BACK": str(self.folder)})


def proposal(slug, address, contributions, raised):
    return {
        "name": slug,
        "local-url": f"/proposals/{slug}.html",
        "donate-address": address,
        "contributions": contributions,
        "amount-funded": raised,
    }


@pytest.fixture
def ccs_back(tmp_path):
    folder = tmp_path / "storage"
    folder.mkdir()
    return CcsBack(folder)


@pytest.fixture
def out(tmp_path):
    return tmp_path / "values"


def line(values, slug):
    """Return the proposal's line without its name, or None."""
    prefix = f"proposal {quote(slug, safe='')} "
    found = [row for row in values.splitlines() if row.startswith(prefix)]
    return found[0].removeprefix(prefix) if found else None


def test_values_py_writes_lines_checks_py_accepts(ccs_back, out):
    ccs_back.values(out)
    checked = run(CHECKS, "values", out, "/dev/null", "/dev/null")
    assert checked.stdout == out.read_text()


def test_the_values_hold_no_qr_lines(ccs_back, out):
    ccs_back.values(out)
    assert not any(row.startswith("qr ") for row in out.read_text().splitlines())


def test_a_run_replaces_the_file_whole(ccs_back, out):
    ccs_back.values(out)
    inode = out.stat().st_ino
    ccs_back.values(out)
    assert out.stat().st_ino != inode
    assert stat.S_IMODE(out.stat().st_mode) == 0o644


def test_a_run_that_cannot_read_ccs_back_fails_and_keeps_the_last_file(
    ccs_back, out, tmp_path
):
    ccs_back.values(out)
    last = out.read_text()
    missing = {"CCS_BACK": str(tmp_path / "missing")}
    refuses(VALUES_PY, out, reason="can't read ccs-back's files", env=missing)
    assert out.read_text() == last


@pytest.mark.parametrize(
    "slug, expected",
    [
        pytest.param(
            "Core website translation to serbian",
            f"raised=10 contributions=3 address={A1}",
            id="a slug with spaces",
        ),
        pytest.param(
            "fcmp++-development",
            "raised=0.00001 contributions=1 address=-",
            id="a slug with plus signs, and a malformed address",
        ),
        pytest.param(
            "text-amount",
            "raised=10.5 contributions=0 address=-",
            id="a string amount, and a null address",
        ),
        pytest.param(
            "null-amount", "raised=0 contributions=0 address=-", id="a null amount"
        ),
        pytest.param(
            "example-progress",
            f"raised=60 contributions=12 address={A1}",
            id="a proposal in progress keeps its address",
        ),
        pytest.param(
            "example-completed",
            "raised=100 contributions=30 address=-",
            id="a completed proposal",
        ),
        pytest.param(
            "minus-zero", "raised=0 contributions=0 address=-", id="-0.0 prints as 0"
        ),
    ],
)
def test_each_proposal_becomes_one_line(slug, expected, ccs_back, out):
    ccs_back.values(out)
    assert line(out.read_text(), slug) == expected


@pytest.mark.parametrize(
    "slug",
    [
        pytest.param("negative", id="a negative amount"),
        pytest.param("huge", id="a 13-digit amount"),
        pytest.param("bool", id="a boolean count"),
    ],
)
def test_a_malformed_value_leaves_its_proposal_out(slug, ccs_back, out):
    written = ccs_back.values(out)
    assert line(out.read_text(), slug) is None
    assert f"left out {slug}" in written.stderr


def test_lines_are_sorted_and_end_with_the_time(ccs_back, out):
    ccs_back.values(out)
    rows = out.read_text().splitlines()
    proposals = [row for row in rows if row.startswith("proposal ")]
    assert proposals == sorted(proposals)
    assert rows[-1].startswith("updated ")
    assert abs(int(rows[-1].removeprefix("updated ")) - time.time()) <= 60


def test_the_gitlab_line_changes_only_with_the_ideas_or_the_commit(ccs_back, out):
    def gitlab():
        ccs_back.values(out)
        return next(r for r in out.read_text().splitlines() if r.startswith("gitlab "))

    first = gitlab()
    ccs_back.write(raised=11)
    assert gitlab() == first
    ccs_back.write(raised=11, idea="Another idea")
    second = gitlab()
    assert second != first
    ccs_back.commit()
    assert gitlab() != second


def test_no_proposals_gives_only_the_gitlab_and_updated_lines(ccs_back, out):
    (ccs_back.folder / "proposals.json").write_text(
        '[{"stage": "Ideas", "proposals": []}]'
    )
    (ccs_back.folder / "complete.json").write_text("[]")
    ccs_back.values(out)
    rows = out.read_text().splitlines()
    assert [row.split(" ")[0] for row in rows] == ["gitlab", "updated"]
    assert (
        run(CHECKS, "values", out, "/dev/null", "/dev/null").stdout == out.read_text()
    )


def test_values_py_without_ccs_back_fails_and_writes_nothing(out):
    run(VALUES_PY, out, ok=False)
    assert not out.exists()


def test_values_py_needs_a_file_to_write():
    refuses(VALUES_PY, reason="usage: values.py <file>")
