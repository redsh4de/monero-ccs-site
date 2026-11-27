#!/bin/sh
# Renders example-data/ into dist/ with production's tool versions.
#
# Usage: dev/render.sh
#        run from the repo's root, after `pnpm build`.
set -eu

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
mkdir -p .render-cache
# the render writes dist.spans beside dist, so dist sits in a writable folder
podman run --rm --network=none -v "$PWD:/w:ro,z" -v "$PWD/dist:/out/dist:z" \
  -v "$PWD/.render-cache:/w/.render-cache:z" -w /w ccs-render \
  python3 -B deploy/render/render.py example-data /out/dist/templates /out/dist \
  .render-cache
