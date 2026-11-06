"""The monitor warns 30 days before a signer's key can no longer sign."""

import pytest

import checks


def export(gpg, tmp_path, name):
    keyring = tmp_path / f"{name}.gpg"
    keyring.write_bytes(gpg("--export", name).stdout)
    return keyring


def fingerprint(gpg, name):
    listing = gpg("--with-colons", "--list-keys", name).stdout.decode().splitlines()
    return next(line.split(":")[9] for line in listing if line.startswith("fpr:"))


def test_key_expiring_within_30_days_is_named(gpg, tmp_path):
    gpg("--quick-gen-key", "soon-expiring", "ed25519", "sign", "10d")
    with pytest.raises(checks.Reject, match="soon-expiring: 20"):
        checks.expiring([export(gpg, tmp_path, "soon-expiring")])


def test_primary_key_expiring_ends_its_subkeys_signing(gpg, tmp_path):
    gpg("--quick-gen-key", "cert-only", "ed25519", "cert", "10d")
    gpg("--quick-add-key", fingerprint(gpg, "cert-only"), "ed25519", "sign", "never")
    with pytest.raises(checks.Reject, match="cert-only: 20"):
        checks.expiring([export(gpg, tmp_path, "cert-only")])


def test_signing_subkey_expiring_ends_its_signatures(gpg, tmp_path):
    gpg("--quick-gen-key", "subkey-signs", "ed25519", "sign,cert", "2y")
    gpg("--quick-add-key", fingerprint(gpg, "subkey-signs"), "ed25519", "sign", "10d")
    with pytest.raises(checks.Reject, match="subkey-signs: 20"):
        checks.expiring([export(gpg, tmp_path, "subkey-signs")])


def test_expired_subkey_after_a_rotation_passes(gpg, tmp_path):
    past = ["--faked-system-time", "20250101T000000"]
    gpg(*past, "--quick-gen-key", "rotated", "ed25519", "cert", "never")
    key = fingerprint(gpg, "rotated")
    gpg(*past, "--quick-add-key", key, "ed25519", "sign", "1d")
    gpg("--quick-add-key", key, "ed25519", "sign", "never")
    checks.expiring([export(gpg, tmp_path, "rotated")])


def test_signer_whose_signing_keys_all_expired_is_named(gpg, tmp_path):
    past = ["--faked-system-time", "20250101T000000"]
    gpg(*past, "--quick-gen-key", "all-expired", "ed25519", "cert", "never")
    key = fingerprint(gpg, "all-expired")
    gpg(*past, "--quick-add-key", key, "ed25519", "sign", "1d")
    with pytest.raises(checks.Reject, match="all-expired: 2025"):
        checks.expiring([export(gpg, tmp_path, "all-expired")])


def test_keys_that_never_expire_pass(keyring, tmp_path):
    checks.expiring([keyring, tmp_path / "not-placed-yet.gpg"])
