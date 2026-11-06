# Releases and signing

Two maintainers build each release on x86_64 Linux, and both sign it. Each signs from a
separate signing account, set up as in [Maintainer setup](#maintainer-setup).
[ARCHITECTURE.md](ARCHITECTURE.md#the-release-from-tag-to-install) explains the build
and the install.

---

## Maintainer setup

**Keep development and signing apart.** Every pnpm command and editor extension runs
dependency code with your account's rights.

### Development account

- Use pnpm 11.4 or later, and only pnpm. Never run `npm install`, `npx` or `yarn` here.
- Keep your account out of the `docker` and `libvirt` groups. Either gives root to any
  code it runs.
- Read a change to `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`,
  `astro.config.mjs` or `.vscode/` as code that runs on your machine.
- Run an outside contributor's branch only in a container or a throwaway virtual
  machine.
- Update dependencies in their own pull request. Never allow a package's build scripts
  just to silence a warning.
- Put no token in a remote URL. Every package you run can read it.

### Signing account

- Sign on a second machine, or as a second user who logs in on their own console. Never
  switch to that user from your desktop's terminal.
- Give that account rootless Podman, GNU `tar`, `git`, `curl`, `gpg`, `less` and Python
  3.12 or later. Give it no Node, pnpm, editor or AI agent.
- Clone over HTTPS without credentials. The signing account holds no GitHub token and no
  browser session.
- Put `release-signers.gpg` in its home, built from fingerprints you checked yourself.

### Your key

- Keep the primary key offline, with an expiry, and the signing subkey on a hardware
  token.
- Turn on the token's touch policy, for example `ykman openpgp keys set-touch sig on`. A
  YubiKey signs without a touch by default.
- Unplug the token when you are not signing.

### GitHub

- Sign in with a passkey or a security key.
- Protect `main` and the `site-v*` tags with rulesets. A change to `main` needs the
  other maintainer's approval, admins included.
- Make any token fine-grained, for this repository only, with a short expiry.

---

## Build and sign

Each release signer builds the same tag, reads every change, and attests it. A release
goes out once two release signers have attested the same bytes.

```
  +---------------+      +---------------+      +---------------+      +---------------+
  |  First signer |      | Second signer |      |     GitHub    |      |   Operators   |
  |   builds and  |----->| builds again, |----->|  the release, |----->|   install it  |
  |    attests    |      |    attests    |      |  2 signatures |      |               |
  +---------------+      +---------------+      +---------------+      +---------------+
```

### Each release signer

1. The first signer tags the commit in their development account, and pushes the tag:

   ```sh
   git tag site-vN
   git push origin site-vN
   ```

2. In the signing account, fetch the tag and the attestations so far, and check out the
   tag:

   ```sh
   git fetch origin main tag site-vN
   git checkout site-vN
   ```

3. Read what changed in the scripts since the last release you signed, `site-vM`. If you
   never signed one, read the four scripts whole:

   ```sh
   git diff site-vM site-vN -- deploy/release.sh deploy/lib.sh deploy/checks.py \
     deploy/render/safety.py
   ```

   These scripts run before their own review, so your reading is their only check.

4. Build, with your name for your attestation's folder:
   `deploy/release.sh site-vN <your name>`. If it says your build differs from another
   release signer's, do not sign, and tell them.
5. Read every change it shows, as in [Read the changes](#read-the-changes). Answer `y`
   only if you expect each change.
6. Copy `release-site-vN/` to your development account, and add your attestation to the
   repository in a pull request:

   ```sh
   cp -r release-site-vN/attestations .
   git add attestations/site-vN/<your name>
   ```

7. When `release.sh` says to create the GitHub release, two release signers have
   attested. Publish it from your development account, and tell the operators which
   machines need it, as in [Upgrade](README.md#upgrade). Every install checks the
   signatures, so that account needs no trust:

   ```sh
   cd release-site-vN
   gh release create site-vN ccs-site-vN.tar SHA256SUMS SHA256SUMS.asc --notes ""
   ```

**The second signer builds once the first signer's attestation is on `main`.** Until
then, `release.sh` cannot see it, and says the release needs two attestations.

### Read the changes

**Refuse any change you do not expect.** `release.sh` first lists the packages that the
lockfile dropped (`-`) and added (`+`) since release N-1. It then shows every change:
`deploy/` line by line, and `dist/` word by word. Release 1 compares with nothing.

`release.sh` also refuses some builds on its own. It stops when `dist/` holds:

- a file type the site never serves;
- a link to a host not in `deploy/build-hosts`;
- address-like text;
- a font or image that is not byte for byte a file of the tag.

When the site really links to a new host, add it to `deploy/build-hosts`, where the
change shows in the review.

A font or image shows only as `Bin`. Each one is a file of the tag, so it changes only
when that file did.

---

## Attestations

Anyone can rebuild a release, and attest that they got the same bytes. The build is
reproducible, so every builder's `SHA256SUMS` matches byte for byte. An install counts
only the release signers' keys, but every attestation is public evidence of the build.

To attest, follow steps 2 to 6 of [Each release signer](#each-release-signer), with your
own key, in an account set up as in [Signing account](#signing-account). Your key needs
no place in `release-signers.gpg`.

Each attestation is a folder, `attestations/site-vN/<builder>/`, holding the builder's
`SHA256SUMS` and their signature over it, `SHA256SUMS.asc`. To see who built release N,
with keys you trust, run:

```sh
for dir in attestations/site-vN/*/; do
  gpgv --keyring <keys you trust> "$dir/SHA256SUMS.asc" "$dir/SHA256SUMS" &&
    cat "$dir/SHA256SUMS"
done
```

Every good signature should show the same line.

The folder follows the layout of Bitcoin Core's `guix.sigs`. To move the attestations to
a repository of their own, keep that layout, and change only `get_attestations` in
`release.sh`.

---

## The address list

A Core Team member, the address signer, makes the list once for each wallet.

1. Make a view-only wallet from the published CCS address and view key.
2. Write its first N subaddresses of account 0 to `addresses.txt`, one on each line.
   Pick N well above the count ccs-back has given out.
3. Add the line `general-fund <address>`, with the General Fund address from
   getmonero.org.
4. Sign the list: `gpg --detach-sign --armor addresses.txt`.
5. Give `addresses.txt` and `addresses.txt.asc` to the operators of both machines.
6. Publish the sha256 of `addresses.txt` in a Monero meta issue.

Anyone with the view key can make the same subaddresses and compare. When ccs-back gives
out an address beyond the list, the `addresses` alert fires: make a longer list the same
way.

---

## Keys

### Extend a signer key

**An expired signer key fails every check of its signatures, old ones included.** A
month before it expires:

1. On the machine with the primary key, the owner extends the key and its subkeys:

   ```sh
   gpg --quick-set-expire <fingerprint> 2y
   gpg --quick-set-expire <fingerprint> 2y '*'
   ```

2. The owner sends the extended key, `gpg --export <fingerprint>`, to the other signers.
3. One signer rebuilds the keyring that holds it from its signers' current keys:
   `gpg --export <fingerprint>... >release-signers.gpg`, or `address-signers.gpg`.
4. Each signing account and both operators install the new keyring. Operators follow
   [New keyrings](README.md#new-keyrings).

The second command skips subkeys that already expired, so extend the key in time.
Extending keeps old signatures good, so nothing needs signing again.

### Remove or replace a release signer

When a release signer leaves or replaces their key, the current signers sign each
installed release again. **Until then, rebuilds, the monitor and `release.sh` refuse
those releases.**

1. Update `release-signers.gpg` on both machines, and in each signing account.
2. Each of the two current signers signs the `SHA256SUMS` of each release a machine
   runs: `gpg --detach-sign --armor -o <name>.asc SHA256SUMS`.
3. Join the two signatures, and replace the published ones:

   ```sh
   cat first.asc second.asc >SHA256SUMS.asc
   gh release upload site-vN SHA256SUMS.asc --clobber
   ```
