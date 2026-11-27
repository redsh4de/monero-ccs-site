"""A rendered site shows no address that the signed list lacks, however written."""

import html
import json
import random
import tracemalloc

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from guards.common import Reject
from guards.site import check_site
import safety
import render

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
    "split by hidden 0s": "<p>"
    + "<i hidden>0</i>".join(CHECKSUMMED[i : i + 20] for i in range(0, 95, 20)),
    "split by tags holding an encoded <": "<p>"
    + '<b title="&lt;"></b>'.join(CHECKSUMMED[i : i + 19] for i in range(0, 95, 19)),
}


def site(tmp_path, page, body=""):
    """Return a rendered site whose one page is `page`, its list, and its hosts.

    BODY in `page` stands for `body`, a proposal's own text.
    """
    folder = tmp_path / "site"
    name = "proposals/example.html"
    (folder / name).parent.mkdir(parents=True)
    before, _, after = page.partition("BODY")
    (folder / name).write_text(before + body + after)
    span = [len(before), len(before) + len(body)]
    (tmp_path / "site.spans").write_text(json.dumps({name: span}))
    signed, hosts = tmp_path / "addresses", tmp_path / "hosts"
    signed.write_text(f"{LISTED}\ngeneral-fund {LISTED}\n")
    hosts.write_text("getmonero.org\n")
    return folder, signed, hosts


def test_site_showing_only_listed_addresses_passes(tmp_path):
    page = f'<p>{LISTED}</p><img src="/qr/{LISTED}.png"><button>Copy</button>'
    check_site(*site(tmp_path, page))


@pytest.mark.parametrize("page", HIDDEN.values(), ids=HIDDEN.keys())
def test_unlisted_address_is_refused(tmp_path, page):
    with pytest.raises(Reject, match="isn't on the signed list"):
        check_site(*site(tmp_path, page))


ELSEWHERE = [
    "https://evil.example/",
    "//evil.example/",
    "/\\evil.example",
    "http:evil.example",
    "/&#9;/evil.example",
    "https://getmonero.org(@evil.example",
    f"monero:{UNLISTED[:40]}",
    f"monero:{LISTED}?a=1",
]
LEAVING = [f'<a href="{url}">x</a>' for url in ELSEWHERE] + [
    '<img src="//evil.example/a.png">',
    '<link rel="preconnect" href="https://getmonero.org">',
    '<meta property="og:image" content="https://evil.example/a.png">',
]


@pytest.mark.parametrize("page", LEAVING)
def test_a_link_that_leaves_the_site_is_refused(tmp_path, page):
    with pytest.raises(Reject, match="links where it may not"):
        check_site(*site(tmp_path, page))


@pytest.mark.parametrize(
    "page",
    [
        "<!--><a href=x>",
        "<script>",
        '<img srcset="/a.png 1x, https://evil.example/b.png 2x">',
        '<ol type="A"><li>x</li></ol>',
        '<meta name="og:image" content="https://evil.example/a.png">',
        '<meta property="og:image" property="og:title" content="//evil.example/a.png">',
    ],
)
def test_markup_that_could_hide_a_link_is_refused(tmp_path, page):
    with pytest.raises(Reject, match="holds"):
        check_site(*site(tmp_path, page))


def test_json_is_read_value_by_value(tmp_path):
    """As its readers take it: an address split across two values shows nowhere."""
    folder, signed, hosts = site(tmp_path, "")
    split = {"title": f"{CHECKSUMMED[:47]} {CHECKSUMMED[47:94]}", "A": "&#x202E;"}
    (folder / "status.json").write_text(json.dumps(split))
    check_site(folder, signed, hosts)
    (folder / "status.json").write_text(json.dumps({"title": CHECKSUMMED}))
    with pytest.raises(Reject, match="status.json shows"):
        check_site(folder, signed, hosts)


@pytest.mark.parametrize(
    "page",
    ['<p title="a&gt6b">x</p>', '<p title="a&gt6b;">x</p>'],
)
def test_an_ampersand_a_browser_could_read_otherwise_is_refused(tmp_path, page):
    """It starts no reference that ends in ; and names a character."""
    with pytest.raises(Reject, match="an & that starts no character"):
        check_site(*site(tmp_path, page))


def test_a_name_holding_an_unlisted_address_is_refused(tmp_path):
    folder, signed, hosts = site(tmp_path, "")
    (folder / f"proposals/{UNLISTED}.html").write_text("")
    with pytest.raises(Reject, match="a name shows"):
        check_site(folder, signed, hosts)


def test_a_feed_is_read_as_its_reader_decodes_it(tmp_path):
    """The XML, then the description's HTML: the & is all that splits this one."""
    folder, signed, hosts = site(tmp_path, "")
    split = f"{CHECKSUMMED[:47]}&amp;amp;{CHECKSUMMED[47:]}"
    (folder / "feed.xml").write_text(f"<rss><description>{split}</description></rss>")
    with pytest.raises(Reject, match="feed.xml shows"):
        check_site(folder, signed, hosts)


def test_a_bidi_control_is_refused(tmp_path):
    with pytest.raises(Reject, match="holds a bidi control"):
        check_site(*site(tmp_path, "<p>&#x202E;</p>"))


def test_what_a_proposal_says_and_the_sites_own_links_pass(tmp_path):
    page = (
        '<!doctype html><meta property="og:title" content="See https://evil.example">'
        f'<a href="monero:{LISTED}">x</a><a href="https://getmonero.org/">x</a>'
        '<div class="prose">BODY</div>'
        '<p>https://evil.example</p><a href="/ideas/">x</a><a href="#top">x</a>'
    )
    body = '<div><a href="https://evil.example">x</a></div>'
    check_site(*site(tmp_path, page, body=body))


@pytest.mark.parametrize(
    "page",
    [
        '<div class="prose">BODY</div><a href="https://evil.example">x</a>',
        '<div class="card prose"><a href="https://evil.example">x</a></div>BODY',
    ],
    ids=["after the text", "before the text"],
)
def test_a_link_outside_a_proposals_text_is_checked(tmp_path, page):
    with pytest.raises(Reject, match="links where it may not"):
        check_site(*site(tmp_path, page, body="<p>x</p>"))


@given(st.text("&#;x0123456789abcdefABlgtampnoi <>\N{ZERO WIDTH SPACE}", max_size=40))
@example("&lt &notit; &#x41; &#128; &#x110000; a&amp;b")
def test_the_check_decodes_a_page_as_html_unescape_does(page):
    """So each character keeps its place, which the render's own check needs."""
    text, where = safety.decoded(page)
    assert text == html.unescape(page) and len(where) == len(text)


def test_a_listed_address_stays_when_the_data_beside_it_is_left_out(tmp_path):
    """Its wallet link and the data after it end with an address the list lacks."""
    listed, data = "4" + "A" * 34 + CHECKSUMMED[:60], CHECKSUMMED[60:]
    (tmp_path / "page.html").write_text("<p>[[ link ]][[ data ]]</p>")
    run = render.Render({listed})
    template = render.templates(tmp_path, ["page"], run.printable)["page"]
    values = {"link": f"monero:{listed}", "data": data}
    text, _ = run.fill(template, values)
    assert text == f"<p>monero:{listed}</p>"


def test_a_value_that_shows_an_unlisted_address_beside_a_listed_one_is_left_out(
    tmp_path,
):
    listed = "4" + "A" * 34 + CHECKSUMMED[:60]
    (tmp_path / "page.html").write_text("<p>[[ value ]]</p>")
    run = render.Render({listed})
    template = render.templates(tmp_path, ["page"], run.printable)["page"]
    value = f"monero:{listed} {CHECKSUMMED}"
    text, _ = run.fill(template, {"value": value})
    assert text == "<p></p>"


def test_the_leave_out_reads_a_page_twice_at_most(monkeypatch):
    """Each read finds the next value, as a chain of crafted titles would."""
    reads = []

    def unlisted(text, listed, decodes=1):
        reads.append(text)
        start = text.find("v")
        return [("v", start, start + 1)] if start >= 0 else []

    monkeypatch.setattr(render.safety, "unlisted", unlisted)
    skeleton = "".join(f"<p>{render.HOLE}{i}.</p>" for i in range(10))
    text, _ = render.kept(skeleton, ["v"] * 10, set())
    assert len(reads) == 2 and "v" not in text


def test_a_run_of_a_million_digits_is_read_in_little_memory():
    """Cutting listed addresses out of it copies no window of it."""
    digits = "".join(random.Random(1).choices("1235679", k=1_000_000))
    tracemalloc.start()
    list(safety.unlisted(digits, {CHECKSUMMED}))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 100_000_000
