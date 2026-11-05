# Architecture

To run the deploy, see [README.md](README.md). [THREAT-MODEL.md](THREAT-MODEL.md) covers
the threats and the input checks.

---

## Parts and data flow

Data flows one way, from the backend to the web server, and the monitor checks the
result.

```
.. BACKEND .............................................................
:                                                                      :
: +------------+    +------------+                      +------------+ :
: |  ccs-back  |--->| values.py  |                      | monitor.sh | :
: +------------+    +------------+                      +------------+ :
:                          |                                   ^       :
...........................|...................................|........
                           |  SSH push: the values             |  Tor: the pages
                           |                                   |
.. WEB SERVER .............|...................................|........
:                          v                                   |       :
:                   +------------+    +------------+    +------------+ :  +------------+
:                   |  fetch.sh  |--->| update.sh  |--->|   nginx    |--->|  visitors  |
:                   +------------+    +------------+    +------------+ :  +------------+
:                          ^                                           :
...........................|............................................
                           |  HTTPS, on a change or hourly
                           |
                    +------------+
                    |   GitLab   |
                    +------------+
```

1. `values.py` reads ccs-back's JSON about once a minute, and writes the values: each
   proposal's amount raised, contribution count and donation address. It also reads
   each address's QR code back with zbar, and adds the hash of the code. The push sends
   them to the web server over SSH.
2. Each push starts `fetch.sh`. It calls GitLab only when the push's `seen` line
   changes, or once an hour. `seen` is a hash of ccs-back's proposals commit and of its
   Ideas stage, which lists the open merge requests.
3. `update.sh` runs after each fetch, and checks the values and the address list. When
   something changed, it renders the site with [the render](render/README.md), and
   switches to it.
4. nginx serves the site.
5. The monitor renders the site again, and compares it with the live site over Tor, byte
   for byte.

Astro builds the design as templates, and the render fills them on the web server. Node
runs on neither machine.

### Files

Each folder in `deploy/` names the machine that runs it. `web/` runs on the web server
and `backend/` on the backend. `render/` and the scripts at the top run on both, except
`release.sh`, which the signers run.

---

## The release, from tag to install

Two maintainers build the same release from one tag, and a machine installs it only with
both signatures.

A release is three files on the GitHub release `site-vN`:

- `ccs-site-vN.tar`: `deploy/` and the built site in `dist/`, with its templates.
- `SHA256SUMS`: the archive's hash, as `sha256sum` writes it. The version is in the
  signed file name, so an older release cannot pass as a newer one.
- `SHA256SUMS.asc`: the release signers' signatures over `SHA256SUMS`.

Every builder's signature also goes into `attestations/site-vN/<builder>/`, as Bitcoin
Core keeps them in `guix.sigs`. Anyone can attest. A machine counts only good signatures
from the keys in `release-signers.gpg`, and skips any other.

`release.sh` packs the same bytes on any machine, so every builder signs one build. It:

- builds in a StageX Node image pinned by digest, with a pinned pnpm;
- installs dependencies without their build scripts, and builds with no network;
- takes `deploy/` from the tag, so the build cannot change the scripts it ships;
- checks release N-1 before it shows the changes since then;
- refuses a build holding a file type the site never serves, a link to a host not in
  `deploy/build-hosts`, or address-like text;
- refuses a font or image that is not byte for byte a file of the tag;
- stops when the build differs from a release signer's attestation.

`install.sh` switches a machine to a release only after it checks it. It:

- refuses a release older than the installed one, unless that release is still on disk;
- unpacks the release into `/usr/local/lib/ccs/releases/N`, and points
  `/usr/local/lib/ccs/current` at it;
- on the web server, renders with the new release and lets nginx check the new site
  first, and changes nothing if either fails.

The units run from `current`, so scripts, render and design change together.

---

## Units and timing

Two path units and two timers start all the work.

| Unit                 | Starts when                                        | Runs                                             |
| -------------------- | -------------------------------------------------- | ------------------------------------------------ |
| `ccs-values.path`    | ccs-back rewrites `proposals.json`                 | `values.py`, then the push in `ccs-push.service` |
| `ccs-fetch.path`     | A push lands in `/var/lib/ccs/in/values`           | `fetch.sh`                                       |
| `ccs-fetch.timer`    | Every hour, after a random wait of up to 5 minutes | `fetch.sh`, for when the backend is silent       |
| `ccs-update.service` | After every fetch, whatever its result             | `update.sh`                                      |
| `ccs-monitor.timer`  | Every 15 to 25 minutes                             | `monitor.sh`                                     |

`install.sh` places one sandbox, `sandbox.conf`, beside each service on both machines.
It takes away every privilege, device and namespace, and limits the system calls. Each
unit opens only the sockets it needs, and the monitor only Tor's.
`systemd-analyze security` rates each service 0.4 to 1.2 out of 10: SAFE or OK.

---

## What the monitor checks

Each run, the monitor rebuilds the site from the backend's own sources: the values from
ccs-back's latest file, and the pages rendered again without the cache when an input
changed. It compares them with the live site, byte for byte.
[ALERTS.md](ALERTS.md#monitor-alerts) lists its checks in order, each with its alert.

Each run compares the main pages, each page with a donation box, and each QR code. It
also compares the next 10 files of the site in turn, and every page that failed before.
