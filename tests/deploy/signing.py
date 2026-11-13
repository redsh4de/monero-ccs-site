"""Test keys, signatures and signed address lists, made fresh for each run."""

import base64
import hashlib
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts import REPO, fail, run

NAMES = ("signer-a", "signer-b", "outsider", "addresser")
RELEASE_SIGNERS = ("signer-a", "signer-b")
# 37 by 37 modules, one bit each: the size of a Monero address's QR code
CODE = base64.b64encode(bytes(172)).decode()
GENERAL_FUND = (REPO / "example-data/addresses").read_text().split()[1]


@dataclass
class Keys:
    """A gpg home holding one signing key for each of NAMES."""

    home: Path

    @classmethod
    def create(cls, home: Path) -> "Keys":
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        keys = cls(home)
        for name in NAMES:
            keys.gpg("--quick-gen-key", name, "ed25519", "sign", "never")
        return keys

    def gpg(self, *args: str | Path) -> bytes:
        options = ["--batch", "--quiet", "--yes", "--pinentry-mode", "loopback"]
        command = ["gpg", "--homedir", self.home, *options, "--passphrase", ""]
        command = [str(part) for part in [*command, *args]]
        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0:
            fail(command, f"exited {result.returncode}", "", result.stderr.decode())
        return result.stdout

    def sign(self, path: Path, *signers: str) -> None:
        """Write path.asc, holding each signer's signature of path, in order."""
        Path(f"{path}.asc").write_bytes(
            b"".join(
                self.gpg(
                    "--local-user", signer, "--detach-sign", "--armor", "-o-", path
                )
                for signer in signers
            )
        )

    def keyring(self, path: Path, *names: str) -> Path:
        path.write_bytes(self.gpg("--export", *names))
        return path


def write_list(
    path: Path, addresses: Iterable[str], keys: Keys, signer: str = "addresser"
) -> None:
    """Write a signed address list, as address-list.py prints it."""
    lines = [f"{address} {CODE}" for address in addresses]
    lines.append(f"general-fund {GENERAL_FUND} {CODE}")
    path.write_text("".join(f"{line}\n" for line in lines))
    keys.sign(path, signer)


def publish(
    host: Path,
    version: int,
    keys: Keys,
    tree: Path = REPO,
    signers: Sequence[str] = RELEASE_SIGNERS,
) -> Path:
    """Publish tree as a signed release, as get_release fetches it; return its tar."""
    folder = host / "download" / f"site-v{version}"
    folder.mkdir(parents=True)
    tar = folder / f"ccs-site-v{version}.tar"
    run("tar", "-cf", tar, "--exclude=__pycache__", "-C", tree, "dist", "deploy")
    digest = hashlib.sha256(tar.read_bytes()).hexdigest()
    (folder / "SHA256SUMS").write_text(f"{digest}  {tar.name}\n")
    keys.sign(folder / "SHA256SUMS", *signers)
    return tar
