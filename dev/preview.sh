#!/bin/sh
# Builds and renders the site, then serves dist/ on http://localhost:4321.
#
# Usage: dev/preview.sh
#        run from the repo's root.
set -eu

pnpm build
sh dev/render.sh
podman run --rm -p 127.0.0.1:4321:4321 -v "$PWD:/w:ro,z" -w /w ccs-render \
  python3 -B dev/preview.py
