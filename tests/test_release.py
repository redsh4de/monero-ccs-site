"""A release reaches a server only as both release signers signed it."""

import shutil

import pytest

import checks


def test_release_signed_by_both_signers_installs(make_release, keyring, tmp_path):
    release = make_release(1, signers=["signer-a", "signer-b"])
    checks.install_release(release, keyring, 1, tmp_path / "installed")
    assert (tmp_path / "installed/dist/index.html").read_text() == "<p>release 1</p>"


def test_one_signature_is_not_enough(make_release, keyring, tmp_path):
    # the outsider's signature is good, but its key isn't in the keyring
    release = make_release(1, signers=["signer-a", "outsider"])
    with pytest.raises(checks.Reject, match="two release signers"):
        checks.install_release(release, keyring, 1, tmp_path / "installed")


def test_old_release_cannot_pass_as_newer(make_release, keyring, tmp_path):
    release = make_release(1, signers=["signer-a", "signer-b"])
    shutil.copy(release / "ccs-site-v1.tar", release / "ccs-site-v2.tar")
    with pytest.raises(checks.Reject, match="this isn't release 2"):
        checks.install_release(release, keyring, 2, tmp_path / "installed")


def test_tampered_archive_is_refused(make_release, keyring, tmp_path):
    release = make_release(1, signers=["signer-a", "signer-b"])
    with open(release / "ccs-site-v1.tar", "ab") as archive:
        archive.write(b"\0" * 512)
    with pytest.raises(checks.Reject, match="doesn't match its hash"):
        checks.install_release(release, keyring, 1, tmp_path / "installed")
