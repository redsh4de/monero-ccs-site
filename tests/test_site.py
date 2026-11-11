"""A rendered site shows no address that the signed list lacks, however written."""

import pytest

import checks

LISTED = "8" + "A" * 94
UNLISTED = "4" + "B" * 94
CHECKSUMMED = "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A"
HIDDEN = {
    "as text": f"<p>{UNLISTED}</p>",
    "in a link": f'<a href="https://example.com/{UNLISTED}">x</a>',
    "as entities": "<p>" + "".join(f"&#{ord(c)};" for c in UNLISTED) + "</p>",
    "split by a zero-width space": (
        f"<p>{UNLISTED[:47]}\N{ZERO WIDTH SPACE}{UNLISTED[47:]}</p>"
    ),
    "split by a tag": f"<p>{UNLISTED[:47]}<b>{UNLISTED[47:]}</b></p>",
    "right after a listed one": f"<p>{LISTED}<b>{UNLISTED}</b></p>",
    "in groups": "<p>" + " ".join(CHECKSUMMED[i : i + 4] for i in range(0, 95, 4)),
}


def site(tmp_path, page):
    """Return a rendered site whose one page is `page`, and its verified list."""
    folder = tmp_path / "site"
    (folder / "proposals").mkdir(parents=True)
    (folder / "proposals/example.html").write_text(page)
    signed = tmp_path / "addresses"
    signed.write_text(f"{LISTED}\ngeneral-fund {LISTED}\n")
    return folder, signed


def test_site_showing_only_listed_addresses_passes(tmp_path):
    page = f'<p>{LISTED}</p><img src="/qr/{LISTED}.png"><button>Copy</button>'
    checks.check_site(*site(tmp_path, page))


@pytest.mark.parametrize("page", HIDDEN.values(), ids=HIDDEN.keys())
def test_unlisted_address_is_refused(tmp_path, page):
    with pytest.raises(checks.Reject, match="isn't on the signed list"):
        checks.check_site(*site(tmp_path, page))
