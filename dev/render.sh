#!/bin/sh
# Renders example-data/ into dist/, in a container with production's versions
# of the render's tools.
#
# Usage: dev/render.sh
#        run from the repo's root, after `pnpm build`.
set -eu

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
mkdir -p .render-cache
podman run --rm --network=none -v "$PWD:/w:ro,z" -v "$PWD/dist:/w/dist:z" \
  -v "$PWD/.render-cache:/w/.render-cache:z" -w /w ccs-render \
  python3 -B deploy/render/render.py example-data dist/templates dist \
  .render-cache
