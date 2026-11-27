#!/bin/sh
# Serves the site on http://localhost:4321 and rebuilds it on each change.
#
# Usage: dev/dev.sh
#        run from the repo's root, and reload the page to see a change.
set -eu

stamps=$(mktemp -d)
trap 'rm -rf -- "$stamps"; podman rm -f -t 1 ccs-dev >/dev/null 2>&1' EXIT
trap 'exit 130' INT TERM

podman build -q -t ccs-render -f dev/Containerfile.render dev >/dev/null
podman run --rm -d --name ccs-dev -p 127.0.0.1:4321:4321 -v "$PWD:/w:ro,z" \
  -w /w ccs-render python3 -B dev/serve.py >/dev/null
echo "Serving dist/ on http://localhost:4321, Ctrl+C stops it"

# polls: a file watcher misses a file that an editor saves by a rename
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
