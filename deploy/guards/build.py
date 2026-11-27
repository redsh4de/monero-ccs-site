"""The built site, before a signer signs it."""

import hashlib
import html
import re
from pathlib import Path

from guards.common import BINARY_SUFFIXES, Reject, loose_ampersand, read_hosts, url_host
from guards.style import check_style
from safety import unlisted

BUILD_SUFFIXES = {".html", ".css"} | BINARY_SUFFIXES
PLAIN_NAME = re.compile(r"[A-Za-z0-9._/-]+")
# anything but a newline, printable ASCII or the guides' quotation marks. A
# backslash is odd too: it could hide an address or a host in CSS
ODD_CHARACTER = re.compile(
    "[^\n -\\[\\]-~\N{LEFT SINGLE QUOTATION MARK}-\N{RIGHT DOUBLE QUOTATION MARK}]"
)
# longer than any word of the site, and shorter than a quarter of an address
LONG_RUN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{24,}")
# a browser finds a host after a scheme such as http:, or after two slashes of
# either kind, and skips any slashes that follow
HOST_IN_TEXT = re.compile(
    r"(?:(?<![a-z0-9+.-])(?:https?|wss?|ftp):|(?<![/\\])[/\\]{2})[/\\]*([^/\\?#\s\"'<>)]+)",
    re.I,
)


def sha256(path: Path) -> str:
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_build(dist: str, hosts_path: str, tag_files: str) -> None:
    """Refuse a built site that holds anything a release may not."""
    allowed = read_hosts(hosts_path)
    paths = Path(tag_files).rglob("*")
    tag = {sha256(p) for p in paths if p.is_file() and not p.is_symlink()}
    built = {f"/{p.relative_to(dist).as_posix()}" for p in Path(dist).rglob("*")}
    for path in sorted(Path(dist).rglob("*")):
        name = path.relative_to(dist)
        if not PLAIN_NAME.fullmatch(name.as_posix()):
            raise Reject("build: a name holds more than A-Z a-z 0-9 . _ - /")
        if path.is_dir() and not path.is_symlink():
            continue
        check_built_file(path, name, tag, built, allowed)


def check_built_file(
    path: Path, name: Path, tag: set[str], built: set[str], allowed: set[str]
) -> None:
    """Refuse a file of the build that the site does not serve, or a swapped one."""
    if not path.is_file() or path.is_symlink():
        raise Reject(f"build: {name} isn't a plain file")
    if path.suffix not in BUILD_SUFFIXES:
        raise Reject(f"build: {name} isn't a file type the site serves")
    # a dependency could swap a font or image for a picture of another address
    if path.suffix in BINARY_SUFFIXES:
        if sha256(path) not in tag:
            raise Reject(f"build: {name} isn't byte for byte a file of the tag")
        return
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".css":
        check_style(f"build: {name}", raw, built)
    if path.suffix == ".html" and loose_ampersand(raw):
        raise Reject(f"build: {name} holds an & that starts no character reference")
    check_built_text(name, raw, allowed)


def check_built_text(name: Path, raw: str, allowed: set[str]) -> None:
    """Refuse a page or stylesheet of the build that could show what it may not."""
    text = html.unescape(raw)
    odd = ODD_CHARACTER.search(text)
    if odd:
        raise Reject(f"build: {name} holds {odd[0]!a}, which could hide something")
    hosts = map(url_host, HOST_IN_TEXT.findall(text))
    host = next((host for host in hosts if host not in allowed), None)
    if host:
        raise Reject(f"build: {name} names {host}, not a listed host")
    if LONG_RUN.search(text) or any(unlisted(raw, set())):
        raise Reject(f"build: {name} holds address-like text")
