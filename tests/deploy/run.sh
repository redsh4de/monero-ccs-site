#!/bin/sh
# Runs the deploy tests, each test file in a fresh container of its own, with
# the repo read-only at /w and no network.
#
# Usage: tests/deploy/run.sh [<test file>...]
#        run from the repo's root; with no file, it runs every one.
#
# Exit: 0 when every file passes, 1 when any fails.
set -eu

podman build -q -t localhost/ccs-deploy-tests tests/deploy >/dev/null

if [ "$#" -eq 0 ]; then
  set -- tests/deploy/test_*.py
fi
failed=""
for file in "$@"; do
  echo "== $file"
  # CI, when set, makes pytest show long assertion diffs whole
  if ! podman run --rm --network=none -e CI -v "$PWD:/w:ro,z" -w /w \
    localhost/ccs-deploy-tests \
    python3 -B -m pytest -c tests/deploy/pytest.ini "$file"; then
    failed="$failed $file"
  fi
done

if [ -n "$failed" ]; then
  echo "failed:$failed"
  exit 1
fi
