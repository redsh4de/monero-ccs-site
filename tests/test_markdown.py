"""Proposal Markdown can only become a small, fixed set of HTML."""

import re
import resource
import unicodedata
import urllib.parse
from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import bodies

ADDRESS = "888tNkZrPN6JsEgekjMnABU4TBzc2Dt29EPAvkRxbANsAnjyPbb3iQ1YBRk1UXcdRsiKc9dhwMVgN5S9cQUiyoogDavup3H"

# fmt: off
# what markdown-it renders, and the raw tags a proposal may use
ALLOWED_TAGS = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "em", "strong", "s", "code", "pre", "blockquote", "a",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "section", "sup",  # footnotes
    "details", "summary",
}
# fmt: on
ALLOWED_ATTRIBUTES = {"href", "class", "id"}
SAFE_SCHEMES = {"", "http", "https", "mailto"}

KNOWN_ATTACKS = {
    "script tag": "<script>alert(1)</script>",
    "event handler": "<img src=x onerror=alert(1)>",
    "inline style": '<p style="color: red">x</p>',
    "javascript link": "[x](javascript:alert(1))",
    "javascript link with a tab": "[x](java\tscript:alert(1))",
    "javascript link as an entity": "[x](&#106;avascript:alert(1))",
    "data link": "[x](data:text/html,x)",
    "remote image": "![x](https://example.com/i.png)",
    "link title": '[x](https://example.com "title")',
    "table alignment": "| a |\n|:-:|\n| 1 |",
    "address": ADDRESS,
    "address split by markup": f"{ADDRESS[:47]}**{ADDRESS[47:]}**",
    "address in a link": f"[explorer](https://example.com/{ADDRESS})",
    "address split by a zero-width space": (
        f"{ADDRESS[:47]}\N{ZERO WIDTH SPACE}{ADDRESS[47:]}"
    ),
    "address in groups": " ".join(ADDRESS[i : i + 4] for i in range(0, 95, 4)),
    "address split across paragraphs": f"{ADDRESS[:47]}\n\n{ADDRESS[47:]}",
    # its marker would show the number, which could end an address
    "a list number": "456918413. x\n",
}
# fmt: off
SYNTAX = [
    "**", "~~", "`", "```\n", "    ", "> ", "- ", "\n", "\n\n", "[^1]", "[^1]: ",
    "<details>", "</details>", "<summary>", "<sup>", "<!--", "-->",
    ADDRESS[:47], ADDRESS[47:],
]
# fmt: on
proposals = st.lists(
    st.sampled_from([*KNOWN_ATTACKS.values(), *SYNTAX]) | st.text(max_size=3)
).map("".join)


class Page(HTMLParser):
    """What a browser makes of some HTML: its tags, attributes, links, text."""

    def __init__(self, html):
        super().__init__()
        self.tags = set()
        self.attributes = set()
        self.links = []
        self.text = ""
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for name, value in attrs:
            self.attributes.add(name)
            if name == "href":
                self.links.append(value)

    def handle_data(self, data):
        self.text += data


def shows_address(text):
    """Whether a visitor could copy an address out of the text.

    Copying drops format characters and combining marks, and a wallet may drop spaces.
    """
    visible = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Mn"))
    return bool(re.search(r"[1-9A-HJ-NP-Za-km-z]{90,}", visible)) or ADDRESS in "".join(
        visible.split()
    )


def assert_safe(markdown):
    page = Page(bodies.render(markdown))
    assert page.tags <= ALLOWED_TAGS
    assert page.attributes <= ALLOWED_ATTRIBUTES
    assert {urllib.parse.urlsplit(link).scheme for link in page.links} <= SAFE_SCHEMES
    assert not shows_address(page.text)
    assert not any(shows_address(link) for link in page.links)


@pytest.mark.parametrize("markdown", KNOWN_ATTACKS.values(), ids=KNOWN_ATTACKS.keys())
def test_known_attack_renders_safely(markdown):
    assert_safe(markdown)


@settings(max_examples=500)
@given(proposals)
def test_any_mix_of_attacks_renders_safely(markdown):
    assert_safe(markdown)


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(f"{ADDRESS}\n", id="a paragraph"),
        pytest.param(f"[x](https://repo.getmonero.org/{ADDRESS})\n", id="a link"),
        pytest.param(f"<details>{ADDRESS}</details>\n", id="raw HTML"),
        pytest.param(f"```\n{ADDRESS}\n```\n", id="code"),
    ],
)
def test_only_the_block_that_shows_an_address_is_removed(block):
    page = bodies.render(f"Kept text.\n\n{block}")
    assert "Kept text." in page and "[address removed]" in page and ADDRESS not in page


def test_a_body_whose_cells_grow_past_its_memory_shows_a_notice():
    """markdown-it fills each row of a table with the header's cells."""
    table = "|" + "a|" * 1000 + "\n|" + "-|" * 1000 + "\n" + "x\n" * 1000
    assert bodies.render(table) == bodies.TOO_LONG
    used = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    assert used < 2 * bodies.BODY_MEMORY


def test_a_body_that_takes_too_long_shows_a_notice(monkeypatch):
    """Each place an address could start is checked, and here the limit is 1 s."""
    monkeypatch.setattr(bodies, "BODY_SECONDS", 1)
    assert bodies.render("4 8 " * 24_000) == bodies.TOO_LONG


def test_a_body_whose_footnotes_grow_past_the_cap_shows_a_notice():
    footnotes = "[^a]" * 24_000 + "\n\n[^a]: x\n"
    assert bodies.render(footnotes) == bodies.TOO_LONG
