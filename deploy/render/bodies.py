"""Proposal Markdown to HTML, through an allowlist.

markdown-it-py renders GitHub-flavoured Markdown (tables, strikethrough, bare
links) plus footnotes. On the way, the token stream is narrowed:
- links and images may only go to http, https, mailto or a relative path;
- table alignment becomes a class, since the CSP blocks inline styles;
- images become links, since inline images from other hosts would leak every
  visitor's IP;
- raw HTML is escaped, comments dropped, and only details, summary and sup
  turned back;
- any block whose text or links hold an address-like run is replaced, and any
  link title, which would show on hover, is dropped.
"""

import html
import re

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin

from safety import REMOVED, has_address

TAGS = "details|summary|sup"
ALLOWED_TAG = re.compile(rf"</?({TAGS})>")
ESCAPED_TAG = re.compile(rf"&lt;(/?({TAGS}))&gt;")
ALLOWED_SCHEMES = ("http", "https", "mailto")
SCHEME = re.compile(r"([a-z][a-z0-9+.-]*):", re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)
ANY_TAG = re.compile(r"<[^>]*>")


def safe_target(url: str) -> bool:
    """Return True if links may go to this URL.

    Whitespace and control characters are removed first, since browsers ignore
    them in a scheme. A URL without a scheme is relative, which is fine.
    """
    probe = "".join(ch for ch in url if ch.isprintable() and not ch.isspace())
    scheme = SCHEME.match(probe)
    return scheme is None or scheme.group(1).lower() in ALLOWED_SCHEMES


MARKDOWN = MarkdownIt("gfm-like", {"html": True}).use(footnote_plugin)
# Links and images that fail this stay plain text.
MARKDOWN.validateLink = safe_target


def allow_tags(raw: str) -> str:
    """Drop comments, escape every tag, then turn back exactly the allowed ones."""
    return ESCAPED_TAG.sub(r"<\1>", html.escape(COMMENT.sub("", raw), quote=True))


def image_as_link(image: Token, inside_link: bool) -> list[Token]:
    """Turn an image into a link to it, or just its text inside another link."""
    src = image.attrs["src"]
    text = Token("text", "", 0, content=f"Image: {image.content or src}")
    if inside_link:
        return [text]
    return [
        Token("link_open", "a", 1, attrs={"href": src}),
        text,
        Token("link_close", "a", -1),
    ]


def narrow_inline(token: Token) -> None:
    children, links = [], 0
    for child in token.children:
        if child.type == "image":
            children += image_as_link(child, inside_link=links > 0)
            continue
        if child.type == "link_open":
            links += 1
            child.attrs.pop("title", None)
        elif child.type == "link_close":
            links -= 1
        elif child.type == "html_inline":
            child.content = allow_tags(child.content)
        children.append(child)
    # Check the text as shown: markup and the allowed tags add no characters.
    shown = "".join(child.content for child in children if child.type != "html_inline")
    hrefs = " ".join(c.attrs["href"] for c in children if c.type == "link_open")
    if has_address(shown) or has_address(hrefs):
        children = [Token("text", "", 0, content=REMOVED)]
    token.children = children


def narrow(tokens: list[Token]) -> None:
    for token in tokens:
        if token.type == "inline":
            narrow_inline(token)
        elif token.type == "html_block":
            token.content = allow_tags(token.content)
            if has_address(ALLOWED_TAG.sub("", token.content)):
                token.content = REMOVED
        elif token.type in ("fence", "code_block") and has_address(token.content):
            token.content = REMOVED
        elif token.type in ("th_open", "td_open") and "style" in token.attrs:
            align = str(token.attrs.pop("style")).removeprefix("text-align:")
            token.attrs["class"] = f"align-{align}"


def render(markdown: str) -> str:
    tokens = MARKDOWN.parse(markdown)
    narrow(tokens)
    page = MARKDOWN.renderer.render(tokens, MARKDOWN.options, {})
    # each block is checked alone, so this finds an address split across blocks
    if has_address(html.unescape(ANY_TAG.sub(" ", page))):
        return f"<p>{REMOVED}</p>\n"
    return page
