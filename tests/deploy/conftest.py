"""Shared setup for the deploy tests."""

import hashlib
import io
import os
import shutil
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from signing import Keys

RELEASE_SIGNERS = ("signer-a", "signer-b")


def pytest_configure(config):
    # the tests delete /etc/ccs and run the deploy scripts as root
    if os.environ.get("container") != "podman":
        pytest.exit("run them with tests/deploy/run.sh, in a container", returncode=2)


@pytest.fixture(scope="session")
def keys(tmp_path_factory) -> Keys:
    return Keys.create(tmp_path_factory.mktemp("gnupg"))


@pytest.fixture
def etc_ccs(keys) -> Path:
    """Return /etc/ccs, made afresh with the keyrings and an empty config."""
    etc = Path("/etc/ccs")
    shutil.rmtree(etc, ignore_errors=True)
    etc.mkdir()
    (etc / "config").write_text("")
    keys.keyring(etc / "release-signers.gpg", *RELEASE_SIGNERS)
    keys.keyring(etc / "address-signers.gpg", "addresser")
    return etc


@pytest.fixture
def release_folder(keys, tmp_path):
    """Return make(name, signers, tar): a folder holding release 2, as published."""

    def make(
        name: str, signers: Sequence[str] = RELEASE_SIGNERS, tar: Path | None = None
    ) -> Path:
        folder = tmp_path / f"release-{name}"
        folder.mkdir()
        archive = folder / "ccs-site-v2.tar"
        if tar:
            shutil.copy(tar, archive)
        else:
            with tarfile.open(archive, "w") as release:
                for member, data in [
                    ("dist/index.html", b"<p>site</p>\n"),
                    ("deploy/render/render.py", b'print("render")\n'),
                ]:
                    info = tarfile.TarInfo(member)
                    info.size = len(data)
                    release.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (folder / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
        keys.sign(folder / "SHA256SUMS", *signers)
        return folder

    return make
