# Deploy tests

These tests run the scripts in `deploy/` as the servers run them, and check what each one
guarantees. Each test file runs in a fresh Debian 13 container, with the repository
read-only and no network.

## Run them

```sh
pnpm test:deploy
```

This builds the site with pnpm and Node, then runs every test file in podman. To run one
file, or one test:

```sh
sh tests/deploy/run.sh tests/deploy/test_update.py
sh tests/deploy/run.sh 'tests/deploy/test_update.py::test_a_bad_values_line_keeps_the_old_values'
```

`run.sh` needs only podman, and tests the `dist/` that is already built. After a change in
`src/`, build the site again first, or use `pnpm test:deploy`.

## Read a failure

1. The test's name states the guarantee that broke, such as
   `test_a_bad_values_line_keeps_the_old_values`.
2. A script can exit with a status that the test does not expect. The failure then
   shows the command, the exit status and the last 40 lines of its output.
3. A test can also fail on a later check, after its scripts ran as expected. Then each
   run shows under "Captured stdout": the command, its exit status and its output.
4. Open the script that the command names. Its output usually names the check that
   refused, such as `values: a line doesn't match`.

## Add a test

- Put the test in the file for the script that it tests, such as `test_update.py` for
  `deploy/web/update.sh`.
- Take the setup from the fixtures in `conftest.py`. For example, `rendered` gives a web
  server that has rendered the site once. A file can also have its own fixtures, such as
  `monitored` in `test_monitor.py`. Do not copy setup into a test file.
- Test one guarantee, and name the test after it.
- Change example data or a template only through `replaced()`. When the data changes
  later, the test then fails with its own message, and does not blame the script.
- Prove the test before you commit it. Break the code that it guards, in a copy, and
  check that the test fails with a clear message:

```sh
cp deploy/web/update.sh /tmp/update.sh   # then change the copy
podman run --rm --network=none -v "$PWD:/w:ro,z" \
  -v /tmp/update.sh:/w/deploy/web/update.sh:ro,z -w /w localhost/ccs-deploy-tests \
  python3 -B -m pytest -c tests/deploy/pytest.ini tests/deploy/test_update.py
```

The shared setup is in eight files:

| File | Holds |
|---|---|
| `scripts.py` | The repository's paths; `run()`, which shows a command's output on a failure; `refuses()`; `replaced()`; and `lib_sh()`, which calls a `lib.sh` function |
| `signing.py` | The test keys, made for each run, signed address lists and signed releases |
| `web.py` | A web server's folders and actions, as `update.sh` sees them |
| `fetching.py` | `fetch.sh`'s folders, and a proposals repository for it to fetch |
| `fake_server.py` | A fake GitLab and alert channel, in the test's own process |
| `monitoring.py` | A site for the monitor to check, built once for each test file and copied for each test |
| `installing.py` | Both servers in one container for `install.sh`, and the saved states that the tests start from |
| `conftest.py` | The fixtures that the tests ask for |

A test that needs a package the image lacks adds it to `Containerfile`.
