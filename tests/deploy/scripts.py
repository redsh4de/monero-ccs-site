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
    ok: bool = True,
    env: dict[str, str] | None = None,
    input: str | None = None,
    cwd: Path | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a command, and fail the test with its output on an unexpected exit.

    ok=True needs exit 0, and ok=False needs any other exit. env adds to the
    test's environment. Each run is also printed, so pytest shows it when a
    later assert in the test fails.
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
    print(f"$ {shlex.join(command)} -> exit {result.returncode}")
    print(result.stdout + result.stderr, end="")
    if (result.returncode == 0) != ok:
        expected = "0" if ok else "a failure"
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


def refuses(*cmd: str | Path, reason: str, **options) -> None:
    """Run a command that must refuse, and check that it says why, not crash.

    options go to run(), such as env.
    """
    stderr = run(*cmd, ok=False, **options).stderr
    assert reason in stderr, f"expected {reason!r} on stderr, got:\n{stderr}"


def lib_sh(
    snippet: str,
    *args: str | Path,
    ok: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet with lib.sh loaded, its arguments as $1, $2 and on.

    The shell runs as every deploy script does: set -eu, and pipefail.
    """
    lib = f"set -eu; set -o pipefail; DEPLOY={DEPLOY}; . {DEPLOY}/lib.sh"
    return run("sh", "-c", f"{lib}; {snippet}", "sh", *args, ok=ok, env=env)


def stand_in(folder: Path, name: str, script: str) -> str:
    """Write a stand-in for a command into folder; return a PATH that finds it first."""
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(script)
    (folder / name).chmod(0o755)
    return f"{folder}:{os.environ['PATH']}"


def tail(text: str) -> str:
    return "\n".join(text.splitlines()[-TAIL:])


def replaced(text: str, old: str, new: str) -> str:
    """Return the text with old replaced by new, and fail the test if old is gone.

    A test edits fixture data or a template to cause a case. When that data
    changes, the edit fails here, and the test does not blame the script.
    """
    if old not in text:
        pytest.fail(
            f"the test's own edit did not apply: {old!r} is no longer in the text",
            pytrace=False,
        )
    return text.replace(old, new)
