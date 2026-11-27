"""Stylesheets, which may show no text of their own."""

import re

from guards.common import Reject


# CSS shows text of its own only through strings, functions such as attr(),
# counters, list markers and url(), so each has an allowlist

# a browser reads whichever token starts first: a /* in a url() is part of the url.
# An unclosed string ends at its line's end, and the others at the file's end
TOKEN = re.compile(
    r"/\*.*?(?:\*/|\Z)|\"[^\"\n]*(?:\"|\n|\Z)|'[^'\n]*(?:'|\n|\Z)"
    r"|(?<![\w-])url\((?![ \n]*[\"'])[^)]*(?:\)|\Z)",
    re.S,
)
STYLE_FUNCTIONS = set(
    "blur calc circle clamp color-mix cubic-bezier has inset is light-dark"
    " max min minmax not nth-child nth-of-type repeat rotate scale scaleX scaleY"
    " selector translate translateY url var".split()
)
FUNCTION = re.compile(r"(?<![\w-])([\w-]+)\(")
AT_RULES = set(
    "@container @font-face @keyframes @media @starting-style @supports".split()
)
AT_RULE = re.compile(r"@[\w-]*")
STYLE_PROPERTIES = set(
    "align-items align-self animation appearance backdrop-filter background"
    " background-color block-size border border-block-end border-block-start"
    " border-bottom border-bottom-color border-collapse border-color border-inline"
    " border-inline-start border-inline-start-color border-radius border-top"
    " box-shadow box-sizing clip-path color color-scheme column-gap container-type"
    " content cursor display flex flex-basis flex-direction flex-flow flex-grow"
    " flex-shrink flex-wrap font font-display font-family font-size font-style"
    " font-variant-numeric font-weight gap grid-area grid-column grid-row"
    " grid-template-columns grid-template-rows height image-rendering inline-size"
    " inset inset-block inset-block-start inset-inline-end inset-inline-start"
    " justify-content justify-self letter-spacing line-height list-style margin"
    " margin-block margin-block-end margin-block-start margin-bottom margin-inline"
    " margin-inline-start margin-top mask max-block-size max-inline-size max-width"
    " min-block-size min-height min-inline-size opacity order outline outline-offset"
    " overflow overflow-wrap overflow-x overflow-y overscroll-behavior padding"
    " padding-block padding-block-end padding-block-start padding-inline"
    " padding-inline-start pointer-events position scroll-margin-block-start"
    " scroll-margin-top scrollbar-width src text-align text-decoration text-wrap"
    " touch-action transform transition unicode-range user-select vertical-align"
    " visibility white-space width z-index".split()
)
STYLE_VALUES = {"content": {'""'}, "list-style": {"none"}}


def check_style(what: str, css: str, files: set[str]) -> None:
    """Refuse a stylesheet that could show text of its own."""
    tokens = TOKEN.findall(css)
    odd = {t for t in tokens if t[0] in "\"'"} - {'""'}
    odd |= {t[4:].removesuffix(")") for t in tokens if t[0] == "u"} - files
    css = TOKEN.sub(lambda t: {"/": " ", "u": "url()"}.get(t[0][0], '""'), css)
    odd |= set(AT_RULE.findall(css)) - AT_RULES
    odd |= set(FUNCTION.findall(css)) - STYLE_FUNCTIONS
    parts = re.split("([;{}])", css + ";")
    for text, end in zip(parts[::2], parts[1::2]):
        name, colon, value = text.partition(":")
        name, value = name.strip(), value.strip()
        if end == "{" or not colon or name.startswith("--"):
            continue
        if name not in STYLE_PROPERTIES or value not in STYLE_VALUES.get(name, {value}):
            odd.add(f"{name}:{value}")
    if odd:
        raise Reject(f"{what} could show text of its own: {min(odd)[:40]}")
