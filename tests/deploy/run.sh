#!/bin/sh
# Runs the deploy tests, each test file in a fresh container of its own, with
# the repo read-only at /w and no network. test_systemd.py runs in a container
# that boots systemd.
#
# Usage: tests/deploy/run.sh [<test file>...]
#        run from the repo's root; with no file, it runs every one.
#
# Exit: 0 when every file passes, 1 when any fails.
set -eu

IMAGE=localhost/ccs-deploy-tests

# plain <file>: runs the file's tests as the container's only process.
# CI, when set, makes pytest show long assertion diffs whole.
plain() {
  podman run --rm --network=none -e CI -v "$PWD:/w:ro,z" -w /w "$IMAGE" \
    python3 -B -m pytest -c tests/deploy/pytest.ini "$1"
}

# booted <file>: runs the file's tests in a container where systemd is PID 1.
# systemd needs --privileged to build each unit's sandbox. Only rootless
# podman keeps those privileges inside the container, off the host.
booted() {
  if [ "$(podman info -f '{{.Host.Security.Rootless}}')" != true ]; then
    echo "run.sh boots systemd under rootless podman only"
    plain "$1"
    return
  fi
  name="ccs-deploy-tests-$$"
  podman run -d --name "$name" --systemd=always --privileged --network=none \
    -e CI -v "$PWD:/w:ro,z" "$IMAGE" /usr/lib/systemd/systemd >/dev/null ||
    return 1
  trap 'podman rm -f -t 3 "$name" >/dev/null 2>&1' EXIT
  trap 'exit 130' INT TERM
  # the container comes up "degraded": its kernel mounts fail, which is fine
  podman exec "$name" systemctl is-system-running --wait >/dev/null 2>&1 || true
  status=0
  if [ "$(podman inspect -f '{{.State.Running}}' "$name")" = true ]; then
    podman exec -w /w "$name" \
      python3 -B -m pytest -c tests/deploy/pytest.ini "$1" || status=$?
  else
    podman logs "$name" 2>&1 | tail -n 5
    plain "$1" || status=$?
  fi
  podman rm -f -t 3 "$name" >/dev/null
  trap - EXIT
  return "$status"
}

podman build -q -t "$IMAGE" tests/deploy >/dev/null

if [ "$#" -eq 0 ]; then
  set -- tests/deploy/test_*.py
fi
failed=""
for file in "$@"; do
  echo "== $file"
  case "$file" in
    */test_systemd.py*) booted "$file" ;;
    *) plain "$file" ;;
  esac || failed="$failed $file"
done

if [ -n "$failed" ]; then
  echo "failed:$failed"
  exit 1
fi
