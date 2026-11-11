"""checks.py's commands, as the deploy scripts call them.

The fast suite in tests/ covers the logic behind several of them; these tests
cover what only the command shows: its files, its refusals and its messages.
"""

import json

import pytest

from scripts import CHECKS, run

COMMIT = "0" * 40


def status(tmp_path, data):
    path = tmp_path / "status.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_status_prints_the_release_and_commit_on_one_line(tmp_path):
    path = status(tmp_path, {"release": 7, "commit": COMMIT})
    assert run(CHECKS, "status", path).stdout == f"7 {COMMIT}\n"


@pytest.mark.parametrize(
    "data",
    [
        {"release": True, "commit": COMMIT},
        {"release": "7", "commit": COMMIT},
        {"release": 0, "commit": COMMIT},
        {"release": 1_000_000, "commit": COMMIT},
        {"release": 7, "commit": "abc"},
        {"release": 7},
        "nope",
        [1, 2],
    ],
    ids=[
        "release true",
        "release as a string",
        "release 0",
        "release over 999999",
        "a short commit",
        "a missing field",
        "not JSON",
        "a list, not an object",
    ],
)
def test_status_refuses_a_malformed_file(tmp_path, data):
    run(CHECKS, "status", status(tmp_path, data), ok=False)


def test_an_unknown_command_exits_2():
    assert run(CHECKS, "frobnicate", ok=False).returncode == 2
