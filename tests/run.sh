#!/bin/sh
# Lints the Python and the shell, then runs the tests in tests/, in a
# container with the repo read-only at /w and no network.
#
# Usage: tests/run.sh
#        run from the repo's root.
set -eu

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
podman build -q -t ccs-tests tests >/dev/null
podman run --rm --network=none -v "$PWD:/w:ro,z" -w /w ccs-tests sh -eu -c '
  black -q --check deploy dev tests
  pydocstyle --convention=pep257 \
    --add-ignore=D100,D101,D102,D103,D105,D107 deploy dev tests
  shellcheck -x -P deploy deploy/*.sh deploy/*/*.sh dev/*.sh tests/*.sh \
    tests/deploy/*.sh
  python3 -B -m pytest -q tests
'
