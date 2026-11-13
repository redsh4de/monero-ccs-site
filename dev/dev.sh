#!/bin/sh
# Serves the site on http://localhost:4321, and builds and renders it again
# when a file that it is made from changes. Reload the page to see a change.
#
# Usage: dev/dev.sh
#        run from the repo's root.
set -eu

stamps=$(mktemp -d)
trap 'rm -rf -- "$stamps"; podman rm -f -t 1 ccs-dev >/dev/null 2>&1' EXIT
trap 'exit 130' INT TERM

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
podman run --rm -d --name ccs-dev -p 127.0.0.1:4321:4321 -v "$PWD:/w:ro,z" \
  -w /w ccs-render python3 -B dev/preview.py >/dev/null
echo "Serving dist/ on http://localhost:4321, Ctrl+C stops it"

# a file watcher would miss a file that an editor saves by a rename, so this
# looks each second for a file newer than the last build's start
touch "$stamps/built"
while true; do
  pnpm build && sh dev/render.sh || echo "The build failed; fix it and save."
  changed=""
  while [ -z "$changed" ]; do
    sleep 1
    touch "$stamps/next"
    changed=$(find src public example-data deploy/render \
      -newer "$stamps/built" -print -quit)
  done
  mv "$stamps/next" "$stamps/built"
done
