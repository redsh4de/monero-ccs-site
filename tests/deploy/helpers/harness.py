"""The repo's paths, and the one way the deploy tests run a command."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO = Path("/w")
DEPLOY = REPO / "deploy"
CHECKS = DEPLOY / "checks.py"
EXAMPLE = REPO / "example-data"
TAIL = 40


def run(
    *cmd: str | Path,
    refusal: str | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a command, and fail the test with its output on an unexpected exit.

    With a refusal, the command must fail and say it on stderr, so a crash never
    passes for the expected failure. An empty refusal expects a silent failure.
    """
    command = [str(part) for part in cmd]
    try:
        result = subprocess.run(
            command,
            env={**os.environ, **(env or {})},
            input=input,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as hung:
        # the partial output is bytes here, even with text=True
        output = [
            (stream or b"").decode(errors="replace")
            for stream in (hung.stdout, hung.stderr)
        ]
        fail(command, f"timed out after {timeout} s", *output)
    # pytest shows what a run printed when a later assert fails
    print(f"$ {shlex.join(command)} -> exit {result.returncode}")
    print(result.stdout + result.stderr, end="")
    if refusal is None:
        wrong = result.returncode != 0
    else:
        said = refusal in result.stderr if refusal else not result.stderr
        wrong = result.returncode == 0 or not said
    if wrong:
        expected = "0" if refusal is None else f"a failure saying {refusal!r}"
        headline = f"exited {result.returncode}, expected {expected}"
        fail(command, headline, result.stdout, result.stderr)
    return result


def fail(command: list[str], headline: str, stdout: str, stderr: str) -> None:
    pytest.fail(
        f"{shlex.join(command)} {headline}\n"
        f"--- stdout, last {TAIL} lines\n{tail(stdout)}\n"
        f"--- stderr, last {TAIL} lines\n{tail(stderr)}",
        pytrace=False,
    )


def lib_sh(
    snippet: str,
    *args: str | Path,
    refusal: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet with lib.sh loaded, its arguments as $1, $2 and on."""
    lib = f"set -eu; set -o pipefail; DEPLOY={DEPLOY}; . {DEPLOY}/lib.sh"
    return run("sh", "-c", f"{lib}; {snippet}", "sh", *args, refusal=refusal, env=env)


def stand_in(folder: Path, name: str, script: str) -> str:
    """Write a stand-in for a command into folder; return a PATH that finds it first."""
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(script)
    (folder / name).chmod(0o755)
    return f"{folder}:{os.environ['PATH']}"


def tail(text: str) -> str:
    return "\n".join(text.splitlines()[-TAIL:])


def replaced(text: str, old: str, new: str) -> str:
    """Return the text with old replaced by new, and fail the test if old is gone."""
    if old not in text:
        pytest.fail(
            f"the test's own edit did not apply: {old!r} is no longer in the text",
            pytrace=False,
        )
    return text.replace(old, new)


def bomb(repo: Path, base: str) -> str:
    """Return a commit on base that adds 10**12 folders, from a few objects."""
    git = ["git", "-C", repo]
    entry = "040000 tree " + run(*git, "mktree", input="").stdout.strip()
    for _ in range(12):
        lines = "".join(f"{entry}\t{n}\n" for n in range(10))
        entry = "040000 tree " + run(*git, "mktree", input=lines).stdout.strip()
    root = run(*git, "ls-tree", base).stdout + f"{entry}\tbomb\n"
    root = run(*git, "mktree", input=root).stdout.strip()
    user = ["-c", "user.name=t", "-c", "user.email=t@example.org"]
    commit = run(*git, *user, "commit-tree", root, "-p", base, "-m", "bomb")
    return commit.stdout.strip()
