"""A failing command shows the reader its output, so no log is needed."""

import os
import subprocess

from scripts import REPO

pytest_plugins = ["pytester"]


def failed_run(pytester, source):
    """Run a test file that must fail, and return what pytest printed."""
    pytester.makepyfile(source)
    result = pytester.runpytest("-p", "no:cacheprovider", "--tb=short")
    result.assert_outcomes(failed=1)
    return result.stdout


def test_a_failing_command_shows_its_output_in_the_failure(pytester):
    output = failed_run(
        pytester,
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-stdout; echo the-stderr >&2; exit 3')\n",
    )
    output.fnmatch_lines(["*exited 3*", "*the-stdout*", "*the-stderr*"])


def test_a_failed_assert_after_a_run_shows_the_run_s_output(pytester):
    output = failed_run(
        pytester,
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-reason >&2')\n"
        "    assert False\n",
    )
    output.fnmatch_lines(["*Captured stdout call*", "*exit 0*", "*the-reason*"])


def test_a_command_that_hangs_shows_its_output(pytester):
    output = failed_run(
        pytester,
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-reason >&2; sleep 5', timeout=1)\n",
    )
    output.fnmatch_lines(["*timed out after 1 s*", "*the-reason*"])


def test_a_failing_gpg_shows_its_error(pytester):
    output = failed_run(
        pytester,
        "from signing import Keys\n"
        "def test_x(tmp_path):\n"
        "    Keys(tmp_path).gpg('--no-such-option')\n",
    )
    output.fnmatch_lines(["*invalid option*--no-such-option*"])


def test_a_test_edit_that_no_longer_applies_says_so(pytester):
    output = failed_run(
        pytester,
        "from scripts import replaced\n"
        "def test_x():\n"
        "    replaced('the page', 'gone', 'new')\n",
    )
    output.fnmatch_lines(["*the test's own edit did not apply*'gone'*"])


def test_the_tests_refuse_to_run_outside_a_container():
    outside = {name: value for name, value in os.environ.items() if name != "container"}
    one_test = "tests/deploy/test_harness.py::test_a_failing_gpg_shows_its_error"
    result = subprocess.run(
        ["python3", "-B", "-m", "pytest", "-c", "tests/deploy/pytest.ini", one_test],
        cwd=REPO,
        env=outside,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "run them with tests/deploy/run.sh" in result.stdout + result.stderr
