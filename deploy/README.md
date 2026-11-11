# Deploy and run

Setup, upgrades and repairs for the two machines behind ccs.getmonero.org. The backend
runs ccs-back and its wallet. The web server renders and serves the site.

When an alert fires, see [ALERTS.md](ALERTS.md). Signers follow
[RELEASING.md](RELEASING.md), and [ARCHITECTURE.md](ARCHITECTURE.md) explains the
design.

---

## Set up

Set up the backend first: its install prints the command for the web server.

```
.. BACKEND .....................                      .. WEB SERVER ..................
:                              :                      :                              :
: +--------------------------+ :     the command      : +--------------------------+ :
: |  1. install.sh backend   |------------------------->|    2. install.sh web     | :
: +--------------------------+ :                      : +--------------------------+ :
:                              :                      :                              :
: +--------------------------+ :   its fingerprint    : +--------------------------+ :
: |    4. compare the two    |<-------------------------|    3. ssh-keygen -lf     | :
: +--------------------------+ :                      : +--------------------------+ :
:                              :                      :                              :
................................                      ................................
```

Watch both machines' disks with your usual monitoring. A full or read-only disk stops
the web server's alerts, but not the site.

Allow SSH to both machines only from the operators' addresses, and to the web server
also from the backend's, for the push. Ask whoever runs getmonero.org's DNS for a CAA
record. Then only the certificate authority you use can issue certificates for
ccs.getmonero.org.

### On both machines

Each machine needs four files in `/etc/ccs/`, and a release you checked by hand. Its
`install.sh` runs as root before anything else checks it. The release signers give you
`release-signers.gpg`. The address signer gives you `address-signers.gpg`,
`addresses.txt` and `addresses.txt.asc`, made as in
[The address list](RELEASING.md#the-address-list).

1. Install the tools: `apt install gpgv gnupg curl systemd-timesyncd`. The web server
   refuses values that the backend stamped over 5 minutes ahead, so the clocks must
   agree.
2. Copy the four files into `/etc/ccs/`:

   ```sh
   install -D -m 0644 -t /etc/ccs/ release-signers.gpg address-signers.gpg \
     addresses.txt addresses.txt.asc
   ```

3. Show the keyrings' fingerprints, and compare each with the one its owner publishes,
   for example in `utils/gpg_keys/` of the monero repository:

   ```sh
   gpg --show-keys /etc/ccs/release-signers.gpg /etc/ccs/address-signers.gpg
   ```

4. Download the release, and check its signatures. Expect good signatures from two
   release signers, with no warning of an expired or revoked key. A signature from a
   builder whose key you do not hold shows as one that gpgv cannot check, which is fine:

   ```sh
   url=https://github.com/redsh4de/monero-ccs-site/releases/download/site-vN
   for f in ccs-site-vN.tar SHA256SUMS SHA256SUMS.asc; do
     curl -fsSLO "$url/$f"
   done
   gpgv --keyring /etc/ccs/release-signers.gpg SHA256SUMS.asc SHA256SUMS
   ```

5. Check the archive. This prints `ccs-site-vN.tar: OK`: `sha256sum --check SHA256SUMS`.
6. Unpack it, and go into it:
   `mkdir site-vN && tar -xf ccs-site-vN.tar -C site-vN && cd site-vN`.

### The backend

Run these steps as root on the backend, in the release's folder.

1. Install, with the web server's address and ccs-back's storage folder, usually
   `/var/www/ccs-back/storage/app`:

   ```sh
   deploy/install.sh backend site-vN <web server address> <ccs-back folder>
   ```

2. Compare the host key fingerprint that the install printed with
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the web server.

**Keep ccs-back's wallet files readable only by their owner.**

### The web server

Run these steps as root on the web server, in the release's folder.

1. Put the certificate in `/etc/ssl/ccs/`, as `fullchain.pem` and `privkey.pem`. The
   first time, copy the current site's, since ccs.getmonero.org still points at the old
   site.
2. Run the command that the backend's install printed, with the backend's IP address. It
   checks the nginx site and restarts nginx in its sandbox:

   ```sh
   deploy/install.sh web site-vN <backend IP address> '<push key>'
   ```

3. Put the alert channel's URL in `/etc/ccs/alert_url`. Use one channel for both
   machines: a fault on one often alerts on the other.

   Each alert goes to that URL as a plain-text POST. So use a service that takes one,
   such as an ntfy topic, or a chat bridge's webhook. A service that wants JSON, such as
   Slack's webhooks, does not work. Anyone with the URL can post to the channel, so keep
   it secret:

   ```sh
   printf '%s\n' 'https://ntfy.sh/<your topic>' >/etc/ccs/alert_url
   ```

### Switch over

Once both machines run the release:

1. Point ccs.getmonero.org at the web server.
2. When it serves the new site, test the alert channel from the backend, through Tor.
   Some webhook services refuse Tor:

   ```sh
   curl -x socks5h://localhost/run/tor/socks --data test '<alert URL>'
   ```

3. Put the same URL in the backend's `/etc/ccs/alert_url`. Leave it empty until now: the
   monitor finds no `status.json` on the old site, so it would alert.

From then on, each install posts a line to the channel. A channel that stops working
shows at the next install.

---

## Upgrade

Install a release only on the machines whose code it changes. The signers say which.

| The release changes    | Install it on  |
| ---------------------- | -------------- |
| Only `deploy/backend/` | The backend    |
| Only `deploy/web/`     | The web server |
| Anything else          | Both           |

Upgrade the web server first: a site older than the backend raises `old-release`. On
each machine, run:

```sh
/usr/local/lib/ccs/current/deploy/install.sh web site-vN   # or backend
```

To go back, use the opposite order: the backend first. The previous release stays on
disk, so going back one release needs no download. Run the same command with its name.

**Upgrade the render's Debian packages on both machines together.** Otherwise the
monitor finds pages that differ. These are `python3-yaml`, `python3-markdown-it`,
`python3-mdit-py-plugins` and `python3-jinja2`.

---

## Repair and rebuild

Most repairs delete state that the machine rebuilds, while the site keeps serving.

### Repair the web server

After a crash, a fetch error about objects, or a page alert that stays:

```sh
systemctl stop ccs-fetch.path ccs-fetch.timer
rm -rf /var/lib/ccs/cache /var/lib/ccs/state/rendered
find /var/lib/ccs/fetch -mindepth 1 -delete
systemctl start ccs-fetch.service
systemctl start ccs-fetch.path ccs-fetch.timer
```

### Repair the monitor

On the backend:

```sh
systemctl stop ccs-monitor.timer
rm -rf /var/lib/private/ccs-monitor
systemctl start ccs-monitor.timer
```

### Replace a damaged release

When a release on disk is damaged:

1. Delete it: `rm -rf /usr/local/lib/ccs/releases/N`.
2. Check release N by hand, as in steps 4 to 6 of [On both machines](#on-both-machines).
3. In its folder, run `deploy/install.sh backend site-vN` or
   `deploy/install.sh web site-vN`.

### Rebuild a machine

**To rebuild the web server, save `/etc/ccs/` and the certificate.** Nothing else on it
matters.

1. Set it up again, as in [The web server](#the-web-server). The push key is in the
   backend's `/etc/ccs/push_key.pub`.
2. On the backend, read the new host key, as in
   [SSH keys and addresses](#ssh-keys-and-addresses).

**To rebuild the backend, you need only its `/etc/ccs/`.**

1. Restore `/etc/ccs/`.
2. Set it up again, as in [The backend](#the-backend).
3. On the web server, run the command that `install.sh backend` prints.

### After a compromise

Rebuild a compromised machine, and replace the secrets it held.

A compromised web server:

1. Take the web server offline.
2. Rebuild the web server, as in [Rebuild a machine](#rebuild-a-machine).
3. Replace the certificate.
4. Replace the alert URL in `/etc/ccs/alert_url` on both machines.
5. On the backend, delete the monitor's state, which read the web server's
   `status.json`: `rm -rf /var/lib/private/ccs-monitor`.

A compromised backend:

1. On the web server, revoke the push key:
   `truncate -s 0 /home/ccs-fetch/.ssh/authorized_keys`.
2. Replace the alert URL in `/etc/ccs/alert_url` on both machines.
3. Rebuild the backend without its old `/etc/ccs/push_key`, so the install makes a new
   one.

The site keeps the last values meanwhile, and all their addresses are on the list.

---

## Keys and certificates

### New keyrings

**An expired address signer key closes every donation box on both machines.** An expired
release signer key stops installs, and the monitor raises `release`. Signers extend and
replace keys as in [Keys](RELEASING.md#keys).

When the signers send a new keyring, install it on both machines:

```sh
install -m 0644 release-signers.gpg /etc/ccs/   # or address-signers.gpg
```

### A new address list

**A new address list closes the donation boxes for up to a minute, while its two files
change.** The web server uses it at the next push.

1. On one machine, run `install -m 0644 addresses.txt addresses.txt.asc /etc/ccs/`.
2. Within 10 minutes, do the same on the other. Otherwise the monitor finds pages that
   differ.

### SSH keys and addresses

When the push key leaks, the backend's address changes, or the web server's host key
changes:

1. On the backend, delete what changed: `rm /etc/ccs/push_key*` for a new push key, or
   `rm /etc/ccs/known_hosts` for a new host key.
2. Run `/usr/local/lib/ccs/current/deploy/install.sh backend site-vN`, with the
   installed release.
3. Compare the host key fingerprint that it prints with
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the web server.
4. For a new push key or address, run the command that it prints on the web server, with
   the backend's IP address.

### TLS certificate renewals

Renew the certificate with an ACME client, through port 80. Port 80 serves
`/var/www/acme` at `/.well-known/acme-challenge/`.

1. Give the ACME client `/var/www/acme` as its webroot.
2. Replace `/etc/ssl/ccs/` with a link to the certificate folder it keeps.
3. Make the client run `systemctl reload nginx` after each renewal.

**Do not let the ACME client edit the nginx site.** `install.sh` replaces it on each
upgrade.
