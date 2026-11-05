"""Proposal Markdown can only become a small, fixed set of HTML."""

import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import bodies

ADDRESS = "8" + "A" * 94

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
ALLOWED_ATTRIBUTES = {"href", "class", "id", "start"}
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
}
# fmt: off
# Markdown and raw HTML to mix the attacks with
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
    """What a browser makes of some HTML: its tags, attributes, link schemes, text."""

    def __init__(self, html):
        super().__init__()
        self.tags = set()
        self.attributes = set()
        self.schemes = set()
        self.links = []
        self.text = ""
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for name, value in attrs:
            self.attributes.add(name)
            if name == "href":
                self.schemes.add(urllib.parse.urlsplit(value).scheme)
                self.links.append(value)

    def handle_data(self, data):
        self.text += data


def shows_address(text):
    """Whether a visitor could copy an address out of the text.

    Copying drops format characters, such as a zero-width space, and combining
    marks, so they can't split an address.
    """
    visible = "".join(c for c in text if unicodedata.category(c) not in ("Cf", "Mn"))
    return re.search(r"[1-9A-HJ-NP-Za-km-z]{90,}", visible) is not None


def assert_safe(markdown):
    page = Page(bodies.render(markdown))
    assert page.tags <= ALLOWED_TAGS
    assert page.attributes <= ALLOWED_ATTRIBUTES
    assert page.schemes <= SAFE_SCHEMES
    assert not shows_address(page.text)
    assert not any(shows_address(link) for link in page.links)


@pytest.mark.parametrize("markdown", KNOWN_ATTACKS.values(), ids=KNOWN_ATTACKS.keys())
def test_known_attack_renders_safely(markdown):
    assert_safe(markdown)


@settings(max_examples=500)
@given(proposals)
def test_any_mix_of_attacks_renders_safely(markdown):
    assert_safe(markdown)
