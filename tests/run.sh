#!/bin/sh
# Lints the Python and the shell, then runs tests/ in a container with no network.
#
# Usage: tests/run.sh
#        run from the repo's root.
set -eu

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
podman build -q -t ccs-tests tests >/dev/null
podman run --rm --network=none -v "$PWD:/w:ro,z" -w /w ccs-tests sh -eu -c '
  black -q --check deploy dev tests
  # each function stays simple enough to follow, and holds no unused name
  flake8 --select=C901,F --max-complexity=10 deploy dev tests
  MYPYPATH=deploy:deploy/render mypy --no-incremental --cache-dir=/dev/null \
    --explicit-package-bases --check-untyped-defs deploy/*.py deploy/*/*.py
  pydocstyle --convention=pep257 \
    --add-ignore=D100,D101,D102,D103,D105,D107 deploy dev tests
  shellcheck dev/*.sh tests/*.sh tests/deploy/*.sh
  python3 -B -m pytest -q tests
'
