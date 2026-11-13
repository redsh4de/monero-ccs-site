"""checks.py's commands, as the deploy scripts call them.

The fast suite in tests/ covers the logic behind several of them; these tests
cover what only the command shows: its files, its refusals and its messages.
"""

import importlib.util
import io
import json
import os
import py_compile
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

from scripts import CHECKS, DEPLOY, REPO, refuses, replaced, run
from signing import CODE, GENERAL_FUND, Keys, write_list

COMMIT = "0" * 40
A1 = "8" + "A" * 94
A2 = "8" + "D" * 94
UNLISTED = "8" + "E" * 94
GITLAB = "gitlab sha256=" + "b" * 64
NOW = int(time.time())
LAST = NOW - 1000
PUSHED = NOW - 10


def status(tmp_path, data):
    path = tmp_path / "status.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_status_prints_the_release_and_commit_on_one_line(tmp_path):
    path = status(tmp_path, {"release": 7, "commit": COMMIT})
    assert run(CHECKS, "status", path).stdout == f"7 {COMMIT}\n"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"release": True, "commit": COMMIT}, id="release true"),
        pytest.param({"release": "7", "commit": COMMIT}, id="release as a string"),
        pytest.param({"release": 0, "commit": COMMIT}, id="release 0"),
        pytest.param(
            {"release": 1_000_000, "commit": COMMIT}, id="release over 999999"
        ),
        pytest.param({"release": 7, "commit": "abc"}, id="a short commit"),
        pytest.param({"release": 7}, id="a missing field"),
        pytest.param("nope", id="not JSON"),
        pytest.param([1, 2], id="a list, not an object"),
    ],
)
def test_status_refuses_a_malformed_file(tmp_path, data):
    refuses(CHECKS, "status", status(tmp_path, data), reason="status: ")


def test_an_unknown_command_exits_2():
    assert run(CHECKS, "frobnicate", ok=False).returncode == 2


@pytest.fixture
def deploy(tmp_path) -> Path:
    """Return a copy of deploy/, for a test to plant files beside its scripts."""
    ignored = shutil.ignore_patterns("__pycache__")
    return shutil.copytree(DEPLOY, tmp_path / "deploy", ignore=ignored)


def test_a_safety_folder_beside_safety_py_never_runs(deploy, tmp_path):
    (deploy / "render/safety").mkdir()
    (deploy / "render/safety/__init__.py").write_text(
        f'open("{tmp_path}/planted", "w")\n'
    )
    result = run(deploy / "checks.py", "frobnicate", ok=False)
    assert not (tmp_path / "planted").exists()
    assert result.returncode == 2


def test_a_cached_safety_pyc_never_runs(deploy, tmp_path):
    source = tmp_path / "planted.py"
    source.write_text(f'open("{tmp_path}/planted", "w")\n')
    cache = importlib.util.cache_from_source(deploy / "render/safety.py")
    unchecked = py_compile.PycInvalidationMode.UNCHECKED_HASH
    py_compile.compile(source, cfile=cache, invalidation_mode=unchecked)
    result = run(deploy / "checks.py", "frobnicate", ok=False)
    assert not (tmp_path / "planted").exists()
    assert result.returncode == 2


@pytest.fixture
def release_keyring(keys, tmp_path):
    return keys.keyring(tmp_path / "release-signers.gpg", "signer-a", "signer-b")


def verify(folder, keyring, dest, refusal=None, cwd=None):
    if refusal:
        return refuses(CHECKS, "release", folder, keyring, 2, dest, reason=refusal)
    return run(CHECKS, "release", folder, keyring, 2, dest, cwd=cwd)


def test_a_release_unpacks_with_nothing_left_over(
    release_folder, release_keyring, tmp_path
):
    dest = tmp_path / "out"
    assert verify(release_folder("ab"), release_keyring, dest).stdout == ""
    assert (dest / "dist/index.html").is_file()
    assert (dest / "deploy/render/render.py").is_file()
    assert not (tmp_path / "out.tmp").exists()


def test_a_keyring_path_is_read_from_the_working_folder(
    release_folder, release_keyring, tmp_path
):
    folder = release_folder("ab")
    verify(folder, release_keyring.name, tmp_path / "out", cwd=release_keyring.parent)


def test_the_same_signer_twice_counts_once(release_folder, release_keyring, tmp_path):
    folder = release_folder("aa", signers=("signer-a", "signer-a"))
    verify(
        folder,
        release_keyring,
        tmp_path / "out",
        refusal="needs good signatures from two release signers",
    )


def test_a_bad_signature_beside_two_good_ones_is_refused(
    keys, release_folder, release_keyring, tmp_path
):
    folder = release_folder("bad")
    other = tmp_path / "other"
    other.write_text("other\n")
    keys.sign(other, "signer-a")
    signatures = folder / "SHA256SUMS.asc"
    signatures.write_bytes(signatures.read_bytes() + Path(f"{other}.asc").read_bytes())
    verify(folder, release_keyring, tmp_path / "out", refusal="signature is bad")


def test_unknown_signers_are_skipped(release_folder, release_keyring, tmp_path):
    folder = release_folder("abo", signers=("signer-a", "signer-b", "outsider"))
    verify(folder, release_keyring, tmp_path / "out")


def unsafe_tar(kind, tmp_path):
    """Return a release archive holding the site's index and one unsafe member."""
    path = tmp_path / f"{kind}.tar"
    if kind == "sparse":
        big = tmp_path / "big/dist/big"
        big.parent.mkdir(parents=True)
        with open(big, "wb") as file:
            file.truncate(200_000_001)
        run("tar", "--sparse", "-cf", path, "-C", tmp_path / "big", "dist")
        return path
    member = tarfile.TarInfo(
        {"dotdot": "../x", "absolute": "/etc/x"}.get(kind, f"dist/{kind}")
    )
    if kind == "symlink":
        member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
    elif kind == "hardlink":
        member.type, member.linkname = tarfile.LNKTYPE, "dist/index.html"
    elif kind == "device":
        member.type, member.devmajor, member.devminor = tarfile.CHRTYPE, 1, 3
    else:
        member.size = 1
    index = tarfile.TarInfo("dist/index.html")
    index.size = 12
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(index, io.BytesIO(b"<p>site</p>\n"))
        tar.addfile(member, io.BytesIO(b"x") if member.isfile() else None)
    return path


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("symlink", id="a symlink"),
        pytest.param("hardlink", id="a hard link"),
        pytest.param("device", id="a device"),
        pytest.param("dotdot", id="a path that climbs out with .."),
        pytest.param("absolute", id="an absolute path"),
        pytest.param("sparse", id="a sparse member over the size cap"),
    ],
)
def test_a_release_archive_holding_an_unsafe_member_is_refused(
    kind, release_folder, release_keyring, tmp_path
):
    refusal = "too big" if kind == "sparse" else "a link, a device or an unsafe path"
    folder = release_folder(kind, tar=unsafe_tar(kind, tmp_path))
    verify(folder, release_keyring, tmp_path / "out", refusal=refusal)


@pytest.mark.parametrize(
    "rewrite, refusal",
    [
        pytest.param(
            lambda sums: "sha256 " + sums[:64] + "\n",
            "isn't sha256sum's output",
            id="not sha256sum's output",
        ),
        pytest.param(
            lambda sums: sums.replace("ccs-site-v2.tar", "ccs-site-v02.tar"),
            "this isn't release 2",
            id="the archive named for release 02, not 2",
        ),
    ],
)
def test_a_malformed_sums_file_is_refused(
    rewrite, refusal, keys, release_folder, release_keyring, tmp_path
):
    folder = release_folder("sums")
    sums = folder / "SHA256SUMS"
    sums.write_text(rewrite(sums.read_text()))
    keys.sign(sums, "signer-a", "signer-b")
    verify(folder, release_keyring, tmp_path / "out", refusal=refusal)


@pytest.fixture(scope="session")
def tag_files(tmp_path_factory):
    """Return the tag's own files: the repository without its build."""
    tag = tmp_path_factory.mktemp("tag")
    for folder in ("src", "public"):
        shutil.copytree(REPO / folder, tag / folder)
    return tag


@pytest.fixture
def site(tmp_path):
    """Return a copy of the real build, to plant something in."""
    return shutil.copytree(REPO / "dist", tmp_path / "site")


def build(dist, tag_files, refusal=None):
    command = [CHECKS, "build", dist, DEPLOY / "build-hosts", tag_files]
    if refusal:
        refuses(*command, reason=refusal)
    else:
        run(*command)


def plant(dist, where, text):
    """Add text to the home page's template, the first stylesheet, or a new file."""
    if where == "page":
        home = dist / "templates/home.html"
        home.write_text(replaced(home.read_text(), "</body>", f"{text}</body>"))
    elif where == "css":
        stylesheet = sorted((dist / "_astro").glob("*.css"))[0]
        stylesheet.write_text(stylesheet.read_text() + text)
    else:
        (dist / where).write_text(text)


def test_the_real_build_passes(tag_files):
    build(REPO / "dist", tag_files)


@pytest.mark.parametrize(
    "where, text",
    [
        pytest.param("_astro/x.js", "alert(1)\n", id="a script"),
        pytest.param(
            "x.svg", '<svg xmlns="x"/>\n', id="an SVG, which can carry script"
        ),
    ],
)
def test_a_build_holding_a_forbidden_file_is_refused(where, text, site, tag_files):
    plant(site, where, text)
    build(site, tag_files, refusal="isn't a file type the site serves")


@pytest.mark.parametrize(
    "where, text",
    [
        pytest.param(
            "page", '<a href="https://evil.example/donate">donate</a>', id="plain"
        ),
        pytest.param("page", '<a href="//evil.example/">x</a>', id="protocol-relative"),
        pytest.param(
            "page",
            '<a href="https:&#x2F;&#x2F;evil.example/">x</a>',
            id="behind HTML entities",
        ),
        pytest.param(
            "css",
            ".x{background:url(https://evil.example/a.png)}",
            id="in the stylesheet",
        ),
    ],
)
def test_a_build_linking_to_an_unlisted_host_is_refused(where, text, site, tag_files):
    plant(site, where, text)
    build(site, tag_files, refusal="evil.example, not a listed host")


def test_a_listed_host_passes_whatever_its_case_user_or_port(site, tag_files):
    plant(site, "page", '<a href="https://user@GetMonero.org:443/x">x</a>')
    build(site, tag_files)


@pytest.mark.parametrize(
    "where, text",
    [
        pytest.param("page", f"<p>{A1}</p>", id="in a page"),
        pytest.param(
            "page", f"<p>{A1[:48]}<span>{A1[48:]}</span></p>", id="split by a tag"
        ),
        pytest.param(
            "css", f'.x::after{{content:"{A1}"}}', id="in the stylesheet's text"
        ),
    ],
)
def test_a_build_showing_an_address_is_refused(where, text, site, tag_files):
    plant(site, where, text)
    build(site, tag_files, refusal="holds address-like text")


@pytest.fixture
def stale_keys(tmp_path):
    """Return a gpg home with a key to revoke, and one that expired in 2020."""
    stale = Keys(tmp_path / "stale")
    stale.home.mkdir(mode=0o700)
    stale.gpg("--quick-gen-key", "revoked", "ed25519", "sign", "never")
    stale.gpg(
        "--faked-system-time",
        "20200101T000000",
        "--quick-gen-key",
        "expired",
        "ed25519",
        "sign",
        "1d",
    )
    return stale


def revoke(keys, name):
    """Revoke the key with its own revocation certificate, as gpg made it."""
    listing = keys.gpg("--with-colons", "--list-keys", name).decode()
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr")
    )
    certificate = keys.home / "openpgp-revocs.d" / f"{fingerprint}.rev"
    certificate.write_text(certificate.read_text().replace(":-----", "-----"))
    keys.gpg("--import", certificate)


def test_a_revoked_or_expired_key_does_not_count(
    keys, stale_keys, release_folder, tmp_path
):
    folder = release_folder("mixed", signers=("signer-a",))
    sums, signatures = folder / "SHA256SUMS", folder / "SHA256SUMS.asc"
    sign = ["--detach-sign", "--armor", "-o-", sums]
    signatures.write_bytes(
        signatures.read_bytes()
        + stale_keys.gpg("--local-user", "revoked", *sign)
        + stale_keys.gpg(
            "--faked-system-time", "20200101T120000", "-u", "expired", *sign
        )
    )
    revoke(stale_keys, "revoked")
    keyring = tmp_path / "mixed.gpg"
    keyring.write_bytes(
        keys.gpg("--export", "signer-a")
        + stale_keys.gpg("--export", "revoked", "expired")
    )
    verify(folder, keyring, tmp_path / "out", refusal="from two release signers")


def test_a_list_signed_by_a_revoked_key_is_refused(keys, stale_keys, tmp_path):
    path = tmp_path / "addresses.txt"
    write_list(path, [A1], stale_keys, signer="revoked")
    revoke(stale_keys, "revoked")
    keyring = tmp_path / "address-signers.gpg"
    keyring.write_bytes(
        keys.gpg("--export", "addresser") + stale_keys.gpg("--export", "revoked")
    )
    refuses(CHECKS, "addresses", keyring, path, reason="signature is bad or missing")


@pytest.fixture
def address_keyring(keys, tmp_path):
    return keys.keyring(tmp_path / "address-signers.gpg", "addresser")


def test_a_list_signed_by_the_address_signer_prints(keys, address_keyring, tmp_path):
    path = tmp_path / "addresses.txt"
    write_list(path, [A1, A2], keys)
    assert run(CHECKS, "addresses", address_keyring, path).stdout == path.read_text()


FUND = f"general-fund {GENERAL_FUND} {CODE}"
NOT_SQUARE = "A" * 136  # 100 bytes, which no QR code's square of modules fills


@pytest.mark.parametrize(
    "lines, signer, refusal",
    [
        pytest.param(
            [f"{A1} {CODE}", FUND],
            "signer-a",
            "a signature is bad or missing",
            id="signed by a release signer",
        ),
        pytest.param(
            [f"{A1} {CODE}", FUND, FUND],
            "addresser",
            "address list: malformed",
            id="two general-fund lines",
        ),
        pytest.param(
            [f"{A1} {CODE}", f"not-an-address {CODE}", FUND],
            "addresser",
            "address list: malformed",
            id="a malformed address line",
        ),
        pytest.param(
            [A1, FUND],
            "addresser",
            "address list: malformed",
            id="a line without its QR code",
        ),
        pytest.param(
            [f"{A1} {NOT_SQUARE}", FUND],
            "addresser",
            "a QR code is malformed",
            id="a QR code that isn't square",
        ),
    ],
)
def test_a_malformed_or_wrongly_signed_list_is_refused(
    lines, signer, refusal, keys, address_keyring, tmp_path
):
    path = tmp_path / "addresses.txt"
    path.write_text("".join(f"{line}\n" for line in lines))
    keys.sign(path, signer)
    refuses(CHECKS, "addresses", address_keyring, path, reason=refusal)


def test_no_list_is_refused_silently(address_keyring, tmp_path):
    """update.sh then treats the list as empty, which closes every donation box."""
    missing = run(CHECKS, "addresses", address_keyring, tmp_path / "none", ok=False)
    assert missing.stderr == ""


@pytest.fixture
def applied(tmp_path):
    """Return the values applied last: none, from 1,000 seconds ago."""
    path = tmp_path / "applied"
    path.write_text(f"updated {LAST}\n")
    return path


@pytest.fixture
def verified_list(tmp_path):
    """Return a verified list of A1 and A2, as checks.py addresses prints it."""
    path = tmp_path / "list"
    path.write_text(f"{A1} {CODE}\n{A2} {CODE}\n{FUND}\n")
    return path


def values_file(tmp_path, *lines, name="values"):
    path = tmp_path / name
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def proposal(name, address, raised="0", contributions=0):
    return f"proposal {name} raised={raised} contributions={contributions} address={address}"


def values(path, applied, verified_list, ok=True):
    return run(CHECKS, "values", path, applied, verified_list, ok=ok)


def values_then_apply(path, applied, verified_list, tmp_path):
    checked = tmp_path / "checked"
    checked.write_text(values(path, applied, verified_list).stdout)
    return run(CHECKS, "apply", checked, verified_list).stdout


GOOD = [
    proposal("example-funding", A1, "10", 3),
    proposal("example-no-address", "-"),
    proposal("other", A2, "0.5", 1),
    GITLAB,
    f"updated {PUSHED}",
]


def test_a_valid_values_file_passes_unchanged(applied, verified_list, tmp_path):
    """values adds the pins, and apply leaves them out again."""
    path = values_file(tmp_path, *GOOD)
    assert values_then_apply(path, applied, verified_list, tmp_path) == path.read_text()


NO_MATCH = "values: a line doesn't match"
NO_TIME = "values: the updated line is missing or in the future"
NOT_ASCII = "values: a byte isn't ASCII"


def between(separator):
    return f"{proposal('x', '-')}{separator}{proposal('y', '-')}\nupdated {PUSHED}\n"


@pytest.mark.parametrize(
    "text, reason",
    [
        pytest.param(
            f"proposal x raised=1\tcontributions=2 address={A1}\nupdated {PUSHED}\n",
            NO_MATCH,
            id="a tab in a line",
        ),
        pytest.param(
            f"{proposal('x', '-')}\nupdated {NOW + 100_000}\n",
            NO_TIME,
            id="updated more than 300 s ahead",
        ),
        pytest.param(
            f"updated {PUSHED}\n{proposal('x', '-')}\n",
            NO_TIME,
            id="no updated last line",
        ),
        pytest.param(
            f"{GITLAB}\n{GITLAB}\nupdated {PUSHED}\n", NO_MATCH, id="two gitlab lines"
        ),
        pytest.param(
            f"updated {PUSHED - 1}\nupdated {PUSHED}\n",
            NO_MATCH,
            id="two updated lines",
        ),
        pytest.param(
            f"{proposal('x', '-', '1e-05', 3)}\nupdated {PUSHED}\n",
            NO_MATCH,
            id="an exponent amount",
        ),
        pytest.param(
            f"{proposal('x', '<script>')}\nupdated {PUSHED}\n",
            NO_MATCH,
            id="markup in a line",
        ),
        pytest.param(
            f"{proposal('x', '-')}\r\nupdated {PUSHED}\r\n",
            NO_TIME,
            id="CRLF line endings",
        ),
        pytest.param(
            "a" * 1_000_001 + f"\nupdated {PUSHED}\n",
            "values: over 1,000,000 bytes",
            id="over 1,000,000 bytes",
        ),
        pytest.param("", NO_TIME, id="an empty file"),
        pytest.param(
            f"{proposal('x', '-')}\n\nupdated {PUSHED}\n",
            NO_MATCH,
            id="an empty line in the middle",
        ),
        pytest.param(
            f"{proposal('x', '-')}\n\u00e9\nupdated {PUSHED}\n",
            NOT_ASCII,
            id="a non-ASCII byte",
        ),
        pytest.param(between("\x0b"), NO_MATCH, id="a vertical tab between lines"),
        pytest.param(between("\x0c"), NO_MATCH, id="a form feed between lines"),
        pytest.param(between("\x1c"), NO_MATCH, id="a file separator between lines"),
        pytest.param(between("\u2028"), NOT_ASCII, id="a line separator between lines"),
    ],
)
def test_values_refuses_a_malformed_file(
    text, reason, applied, verified_list, tmp_path
):
    path = tmp_path / "values"
    path.write_bytes(text.encode())
    refuses(CHECKS, "values", path, applied, verified_list, reason=reason)


def test_updated_200_seconds_ahead_passes(applied, verified_list, tmp_path):
    path = values_file(tmp_path, proposal("x", "-"), f"updated {NOW + 200}")
    values(path, applied, verified_list)


def test_no_final_newline_passes(applied, verified_list, tmp_path):
    path = tmp_path / "values"
    path.write_text(f"{proposal('x', '-')}\nupdated {PUSHED}")
    values(path, applied, verified_list)


def test_not_newer_is_silent(applied, verified_list, tmp_path):
    path = values_file(tmp_path, proposal("x", "-"), f"updated {LAST}")
    assert values(path, applied, verified_list, ok=False).stderr == ""


def test_a_repeated_proposal_drops_both_lines(applied, verified_list, tmp_path):
    path = values_file(
        tmp_path,
        proposal("example-funding", A1, "10", 3),
        proposal("example-funding", A1, "11", 4),
        proposal("example-no-address", "-"),
        f"updated {PUSHED}",
    )
    assert values_then_apply(path, applied, verified_list, tmp_path) == (
        f"{proposal('example-no-address', '-')}\nupdated {PUSHED}\n"
    )


def test_the_general_fund_is_not_a_proposal_address(applied, verified_list, tmp_path):
    path = values_file(tmp_path, proposal("x", GENERAL_FUND), f"updated {PUSHED}")
    assert values_then_apply(path, applied, verified_list, tmp_path) == (
        f"{proposal('x', '-')}\nupdated {PUSHED}\n"
    )


def test_without_a_list_no_address_shows(applied, tmp_path):
    path = values_file(tmp_path, proposal("x", A1), f"updated {PUSHED}")
    shown = values_then_apply(path, applied, Path("/dev/null"), tmp_path)
    assert shown == f"{proposal('x', '-')}\nupdated {PUSHED}\n"


def test_an_unreadable_list_fails_closed(applied, tmp_path):
    """With no list to check against, nothing is printed, so nothing applies."""
    path = values_file(tmp_path, proposal("x", A1), f"updated {PUSHED}")
    assert values(path, applied, tmp_path / "no-such-list", ok=False).stdout == ""


def test_values_leaves_an_unlisted_address_for_apply(applied, verified_list, tmp_path):
    path = values_file(tmp_path, proposal("x", UNLISTED), f"updated {PUSHED}")
    assert values(path, applied, verified_list).stdout == path.read_text()


def test_a_tab_cannot_hide_an_address_from_apply(verified_list, tmp_path):
    path = tmp_path / "values"
    path.write_text(
        f"proposal x raised=1\tcontributions=2 address={UNLISTED}\nupdated {PUSHED}\n"
    )
    assert run(CHECKS, "apply", path, verified_list).stdout == (
        f"proposal x raised=1\tcontributions=2 address=-\nupdated {PUSHED}\n"
    )


def test_a_values_file_renamed_in_during_the_check_never_leaks(verified_list, tmp_path):
    """values reads the file once: what it prints is what it checked."""
    good = values_file(tmp_path, proposal("x", A1), f"updated {PUSHED}", name="good")
    evil = tmp_path / "evil"
    evil.write_text(f"proposal x raised=1\tcontributions=2 address={A1}\n")
    target, swapped = tmp_path / "values", tmp_path / "values.tmp"
    shutil.copy(good, target)
    stop = threading.Event()

    def swap():
        while not stop.is_set():
            for source in (good, evil):
                shutil.copy(source, swapped)
                os.replace(swapped, target)

    expected = good.read_text() + f"pin x {A1}\n"
    accepted = 0
    swapper = threading.Thread(target=swap)
    swapper.start()
    try:
        for _ in range(300):
            checked = subprocess.run(
                [CHECKS, "values", target, "/dev/null", verified_list],
                capture_output=True,
                text=True,
            )
            if checked.returncode == 0:
                accepted += 1
                assert checked.stdout == expected
    finally:
        stop.set()
        swapper.join()
    assert accepted, "values never accepted the good file, so nothing was checked"


def pinned(tmp_path, *pins):
    """Return applied values from 1,000 seconds ago, holding these pins."""
    path = tmp_path / "applied-pins"
    lines = [f"updated {LAST}", *(f"pin {pin}" for pin in pins)]
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def test_listed_addresses_shown_are_pinned_after_the_values(
    applied, verified_list, tmp_path
):
    path = values_file(tmp_path, *GOOD)
    assert values(path, applied, verified_list).stdout == (
        path.read_text() + f"pin example-funding {A1}\npin other {A2}\n"
    )


def test_kept_pins_carry_over(verified_list, tmp_path):
    path = values_file(tmp_path, proposal("example-funding", A1), f"updated {PUSHED}")
    checked = values(path, pinned(tmp_path, f"other {A2}"), verified_list).stdout
    assert f"pin other {A2}" in checked.splitlines()


@pytest.mark.parametrize(
    "lines, pins, message",
    [
        pytest.param(
            [proposal("example-no-address", A1)],
            [f"example-funding {A1}"],
            "values: example-no-address shows the address of example-funding",
            id="an address given to another proposal",
        ),
        pytest.param(
            [proposal("example-funding", A2)],
            [f"example-funding {A1}"],
            "values: example-funding moves off its address",
            id="a proposal moved to another listed address",
        ),
        pytest.param(
            [proposal("a", A1), proposal("b", A1)],
            [],
            "values: b shows the address of a",
            id="two proposals with one address",
        ),
    ],
)
def test_a_push_breaking_a_pin_is_refused_naming_the_proposals(
    lines, pins, message, verified_list, tmp_path
):
    path = values_file(tmp_path, *lines, f"updated {PUSHED}")
    refused = values(path, pinned(tmp_path, *pins), verified_list, ok=False)
    assert refused.stderr.strip() == message


def test_a_pin_whose_address_left_the_list_holds_nothing_but_stays(
    verified_list, tmp_path
):
    path = values_file(tmp_path, proposal("example-funding", A2), f"updated {PUSHED}")
    pins = pinned(tmp_path, f"example-funding {UNLISTED}")
    checked = values(path, pins, verified_list).stdout.splitlines()
    assert f"pin example-funding {UNLISTED}" in checked
    assert f"pin example-funding {A2}" in checked


def test_an_unlisted_address_is_never_pinned(applied, verified_list, tmp_path):
    path = values_file(tmp_path, proposal("x", UNLISTED), f"updated {PUSHED}")
    assert "pin " not in values(path, applied, verified_list).stdout


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("applied", id="a malformed pin in the applied values"),
        pytest.param("pushed", id="a pin line in the push"),
    ],
)
def test_a_malformed_pin_or_a_pushed_pin_line_is_refused(
    where, applied, verified_list, tmp_path
):
    if where == "applied":
        path = values_file(tmp_path, *GOOD)
        applied = pinned(tmp_path, "example-funding <b>")
        refusal = "applied values: a pin is malformed"
    else:
        path = values_file(tmp_path, f"pin example-funding {A1}", f"updated {PUSHED}")
        refusal = "values: a line doesn't match"
    refuses(CHECKS, "values", path, applied, verified_list, reason=refusal)
