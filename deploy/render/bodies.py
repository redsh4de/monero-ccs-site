"""Proposal Markdown to HTML, through an allowlist."""

import html
import os
import re
import resource

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin

from safety import REMOVED, unlisted

TAGS = "details|summary|sup"
ESCAPED_TAG = re.compile(rf"&lt;(/?({TAGS}))&gt;")
ALLOWED_SCHEMES = ("http", "https", "mailto")
SCHEME = re.compile(r"([a-z][a-z0-9+.-]*):", re.I)
COMMENT = re.compile(r"<!--.*?-->", re.S)

# characters, about three times the longest proposal's
BODY_CAP = 100_000
# characters of HTML, as a table or footnotes grow from little text
PAGE_CAP = 3 * BODY_CAP
# what a body's render may add in bytes and CPU seconds: markdown-it has no
# bound of its own, and pads each table row to the header's width
BODY_MEMORY = 300_000_000
BODY_SECONDS = 10
TOO_LONG = "<p>This proposal's text is too long to show here.</p>\n"


def safe_target(url: str) -> bool:
    """Return True if links may go to this URL."""
    # browsers ignore whitespace and control characters in a scheme
    probe = "".join(ch for ch in url if ch.isprintable() and not ch.isspace())
    scheme = SCHEME.match(probe)
    return scheme is None or scheme.group(1).lower() in ALLOWED_SCHEMES


MARKDOWN = MarkdownIt("gfm-like", {"html": True}).use(footnote_plugin)
# a link or image that fails this stays plain text
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
            # a shown image would leak each visitor's IP to its host
            children += image_as_link(child, inside_link=links > 0)
            continue
        if child.type == "link_open":
            links += 1
            # a title shows on hover, and no address check reads it
            child.attrs.pop("title", None)
        elif child.type == "link_close":
            links -= 1
        elif child.type == "html_inline":
            child.content = allow_tags(child.content)
        children.append(child)
    # Check the text as shown: markup and the allowed tags add no characters.
    shown = "".join(child.content for child in children if child.type != "html_inline")
    hrefs = " ".join(c.attrs["href"] for c in children if c.type == "link_open")
    if any(unlisted(shown, set(), 0)) or any(unlisted(hrefs, set(), 0)):
        children = [Token("text", "", 0, content=REMOVED)]
    token.children = children


def narrow(tokens: list[Token]) -> None:
    for token in tokens:
        if token.type == "inline":
            narrow_inline(token)
        elif token.type == "html_block":
            token.content = allow_tags(token.content)
            if any(unlisted(token.content, set())):
                token.content = REMOVED
        elif token.type in ("fence", "code_block") and any(
            unlisted(token.content, set(), 0)
        ):
            token.content = REMOVED
        elif token.type == "ordered_list_open":
            # its marker would show the number, which could end an address
            token.attrs.pop("start", None)
        elif token.type in ("th_open", "td_open") and "style" in token.attrs:
            # a class, as the CSP blocks inline styles
            align = str(token.attrs.pop("style")).removeprefix("text-align:")
            token.attrs["class"] = f"align-{align}"


def rendered(markdown: str) -> str:
    tokens = MARKDOWN.parse(markdown)
    narrow(tokens)
    page = MARKDOWN.renderer.render(tokens, MARKDOWN.options, {})
    # narrow() checks each block alone, and checks.py site also finds an address
    # split across blocks, or in an attribute such as a code block's language
    if any(unlisted(page, set())):
        return f"<p>{REMOVED}</p>\n"
    return page


def render(markdown: str) -> str:
    """Return the body as HTML, made in a child held to its own limits, or TOO_LONG."""
    if len(markdown) > BODY_CAP:
        return TOO_LONG
    read, write = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            with open("/proc/self/statm", encoding="ascii") as file:
                held = int(file.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
            resource.setrlimit(resource.RLIMIT_AS, (held + BODY_MEMORY,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (BODY_SECONDS,) * 2)
            page = rendered(markdown)
            if len(page) <= PAGE_CAP:
                with open(write, "wb") as pipe:
                    pipe.write(page.encode())
                os._exit(0)
        finally:
            os._exit(1)
    os.close(write)
    with open(read, "rb") as pipe:
        page = pipe.read()
    return page.decode() if os.waitpid(child, 0)[1] == 0 else TOO_LONG
