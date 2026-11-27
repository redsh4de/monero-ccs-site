"""The rendered site, which may show no address the signed list lacks."""

import html
import json
import re
from collections.abc import Iterator
from pathlib import Path

from guards.common import (
    BINARY_SUFFIXES,
    LIST_CAP,
    Reject,
    lines,
    loose_ampersand,
    read_capped,
    read_hosts,
)
from safety import BIDI_CONTROLS, unlisted


ELEMENTS = set(
    "a article aside blockquote body br button code dd details div dl dt em"
    " figcaption figure footer h1 h2 h3 h4 h5 h6 head header hr html img input"
    " label li link main meta nav ol p pre progress s section span strong summary"
    " sup table tbody td th thead title tr ul".split()
)
ATTRIBUTES = set(
    "alt as autofocus charset checked class content crossorigin decoding disabled"
    " fetchpriority for height href id lang loading max name open popover"
    " popovertarget popovertargetaction property rel role sizes src start target"
    " title type value width".split()
)
SPACE = "[\t\n\f\r ]"
# double-quoted values only, so a browser reads each tag as these rules do
PLAIN_TAG = re.compile(
    rf"(?i:<!doctype html>)|</?([a-z][a-z0-9]*)((?:{SPACE}+[a-z][a-z0-9-]*"
    rf'(?:="[^"<>]*")?)*){SPACE}*/?>'
)
ATTRIBUTE = re.compile(r'([a-z][a-z0-9-]*)(?:="([^"<>]*)")?')
OPEN_NAMES = ("aria-", "data-")
# on any other element, these set what a list's markers show
PLACES = {"start": {"ol"}, "type": {"button", "input", "link"}, "value": {"progress"}}
META = {("name", n) for n in ("description", "generator", "viewport", "twitter:card")}
META |= {("property", "og:" + n) for n in "description image site_name title".split()}
URL_PART = r"[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*"
LINK = re.compile(
    rf"|#{URL_PART}|/(?!/){URL_PART}"
    rf"|(?:https|irc)://([a-z0-9.-]+)(?:[/?#]{URL_PART})?|monero:(\w+)"
)
# what nginx.conf serves as JSON, which a reader reads value by value
JSON_SERVED = re.compile(r"[^/]+\.json|index\.php/projects")


def json_texts(value: object) -> Iterator[str]:
    """Yield each key, string and number in a JSON value, as a reader takes them."""
    if isinstance(value, dict):
        value = [*value, *value.values()]
    if isinstance(value, list):
        for item in value:
            yield from json_texts(item)
    elif value is not None:
        yield str(value)


def check_links(
    name: str, page: str, allowed: set[str], listed: set[str], body: range
) -> None:
    """Refuse a page that is not plain, or that links where it may not.

    A tag that starts in `body`, the proposal's own text, may link anywhere. A
    template can hide a link from check_build, but not from this check.
    """
    if re.search("[<>]", PLAIN_TAG.sub("", page)):
        raise Reject(f"site: {name} holds a < or > outside a plain tag")
    for tag in PLAIN_TAG.finditer(page):
        element, attributes = tag.groups()
        if element is None:
            continue
        pairs = ATTRIBUTE.findall(attributes)
        check_tag(name, element, pairs)
        if tag.start() not in body:
            check_tag_links(name, element, pairs, allowed, listed)


def check_tag(name: str, element: str, pairs: list[tuple[str, str]]) -> None:
    """Refuse an element, an attribute or a meta tag the site never writes."""
    odd = [a for a, _ in pairs if a not in ATTRIBUTES and a[:5] not in OPEN_NAMES]
    odd += [a for a, _ in pairs if element not in PLACES.get(a, {element})]
    if len(dict(pairs)) < len(pairs):
        raise Reject(f"site: {name} holds <{element}> with an attribute twice")
    if element == "meta":
        kinds = [(a, v) for a, v in pairs if a in ("name", "property")]
        odd += [f"{a}={v}" for a, v in kinds if (a, v) not in META]
    if element not in ELEMENTS or odd:
        raise Reject(f"site: {name} holds <{' '.join([element, *odd])}>")


def check_tag_links(
    name: str,
    element: str,
    pairs: list[tuple[str, str]],
    allowed: set[str],
    listed: set[str],
) -> None:
    """Refuse a link off the site, unless it goes to a listed host or wallet."""
    named = dict(pairs)
    links = [v for a, v in pairs if a in ("href", "src")]
    if named.get("property") == "og:image":
        links.append(named.get("content", ""))
    # a browser fetches what other elements name without a click
    hosts = {None, *allowed} if element in ("a", "meta") else {None}
    for url in map(html.unescape, links):
        link = LINK.fullmatch(url)
        if not (link and link[1] in hosts and link[2] in {None, *listed}):
            raise Reject(f"site: {name} links where it may not: {url[:80]}")


def check_site(site: str, list_path: str, hosts_path: str) -> None:
    """Refuse a rendered site that holds an address the signed list lacks."""
    allowed = read_hosts(hosts_path)
    rows = lines(read_capped(list_path, LIST_CAP, "address list"), "address list")
    listed = {row.removeprefix("general-fund ").split(" ")[0] for row in rows}
    spans_file = Path(f"{Path(site)}.spans")
    spans = json.loads(spans_file.read_text()) if spans_file.exists() else {}
    bodies = {page: range(*span) for page, span in spans.items()}
    for path in sorted(Path(site).rglob("*")):
        name = path.relative_to(site).as_posix()
        if path.is_dir() and not path.is_symlink():
            continue
        named = next(unlisted(name, listed, 0), None)
        if named:
            raise Reject(
                f"site: a name shows {named[0][:12]}..., not on the signed list"
            )
        if path.suffix in BINARY_SUFFIXES:
            continue
        check_page(path, name, listed, allowed, bodies.get(name, range(0)))


def check_page(
    path: Path, name: str, listed: set[str], allowed: set[str], body: range
) -> None:
    """Refuse a rendered page, feed or JSON file that shows what it may not."""
    page = path.read_bytes().decode(errors="replace")
    texts, text = [page], html.unescape(page)
    if path.suffix == ".html" and loose_ampersand(page):
        raise Reject(f"site: {name} holds an & that starts no character reference")
    # a feed reader decodes the XML, then shows the description as HTML
    decodes = 2 if path.suffix == ".xml" else 1
    if JSON_SERVED.fullmatch(name):
        try:
            texts = list(json_texts(json.loads(page)))
        except ValueError:
            raise Reject(f"site: {name} isn't JSON") from None
        text = "".join(texts)
    if text.translate(BIDI_CONTROLS) != text:
        raise Reject(f"site: {name} holds a bidi control, which reorders text")
    shown = next((f for t in texts for f in unlisted(t, listed, decodes)), None)
    if shown:
        message = f"{name} shows {shown[0][:12]}..."
        raise Reject(f"site: {message}, which isn't on the signed list")
    if path.suffix == ".html":
        check_links(name, page, allowed, listed, body)
