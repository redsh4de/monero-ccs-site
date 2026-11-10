"""Shared setup: gpg keys made for this run, and releases signed with them."""

import hashlib
import subprocess
import tarfile

import pytest
from hypothesis import settings

import checks

# the same examples every run, so a failure in CI fails the same way locally
settings.register_profile("deterministic", derandomize=True, database=None)
settings.load_profile("deterministic")


@pytest.fixture(scope="session")
def gpg(tmp_path_factory):
    """Run gpg with three new keys: the two release signers, and an outsider."""
    home = tmp_path_factory.mktemp("gnupg")

    def run(*args):
        command = ["gpg", "--homedir", home, "--batch", "--quiet"]
        command += ["--pinentry-mode", "loopback", "--passphrase", ""]
        return subprocess.run([*command, *args], check=True, capture_output=True)

    for name in ("signer-a", "signer-b", "outsider"):
        run("--quick-gen-key", name, "ed25519", "sign", "never")
    yield run
    subprocess.run(["gpgconf", "--homedir", home, "--kill", "gpg-agent"])


@pytest.fixture(scope="session")
def keyring(gpg, tmp_path_factory):
    """Return the release signers' public keys, as a server keeps them."""
    path = tmp_path_factory.mktemp("keys") / "release-signers.gpg"
    path.write_bytes(gpg("--export", "signer-a", "signer-b").stdout)
    return path


@pytest.fixture
def make_release(gpg, tmp_path):
    """Build a release folder as the release host serves it, signed by `signers`."""

    def make(version, signers):
        site = tmp_path / f"site-{version}"
        (site / "dist").mkdir(parents=True)
        (site / "dist" / "index.html").write_text(f"<p>release {version}</p>")

        release = tmp_path / f"release-{version}"
        release.mkdir()
        archive = release / checks.release_name(version)
        with tarfile.open(archive, "w") as tar:
            tar.add(site / "dist", arcname="dist")

        sums = release / "SHA256SUMS"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        sums.write_text(f"{digest}  {archive.name}\n")
        detach_sign = ["--armor", "--detach-sign", "--output", "-", sums]
        signatures = b"".join(
            gpg("--local-user", signer, *detach_sign).stdout for signer in signers
        )
        (release / "SHA256SUMS.asc").write_bytes(signatures)
        return release

    return make
