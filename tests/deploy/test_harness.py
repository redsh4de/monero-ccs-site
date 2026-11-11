"""A failing command shows the reader its output, so no log is needed."""

pytest_plugins = ["pytester"]


def test_a_failing_command_shows_its_output_in_the_failure(pytester):
    pytester.makepyfile(
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-stdout; echo the-stderr >&2; exit 3')\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--tb=short")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*exited 3*", "*the-stdout*", "*the-stderr*"])


def test_a_failed_assert_after_a_run_shows_the_run_s_output(pytester):
    pytester.makepyfile(
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-reason >&2')\n"
        "    assert False\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--tb=short")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Captured stdout call*", "*exit 0*", "*the-reason*"])


def test_a_command_that_hangs_shows_its_output(pytester):
    pytester.makepyfile(
        "from scripts import run\n"
        "def test_x():\n"
        "    run('sh', '-c', 'echo the-reason >&2; sleep 5', timeout=1)\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--tb=short")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*timed out after 1 s*", "*the-reason*"])


def test_a_test_edit_that_no_longer_applies_says_so(pytester):
    pytester.makepyfile(
        "from scripts import replaced\n"
        "def test_x():\n"
        "    replaced('the page', 'gone', 'new')\n"
    )
    result = pytester.runpytest("-p", "no:cacheprovider", "--tb=short")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*the test's own edit did not apply*'gone'*"])
