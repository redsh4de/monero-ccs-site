"""Render the site's data pages from the input folder.

Usage: render.py <input-dir> <templates-dir> <out-dir> [<cache-dir>]

  <input-dir>      the render's input, as render/README.md describes it
  <templates-dir>  the Jinja templates, dist/templates in a release
  <out-dir>        where the pages go
  <cache-dir>      optional: rendered proposal bodies, kept between runs

Also writes <out-dir>.spans, where each proposal page holds its body, for
checks.py site.
"""

import io
import json
import os
import re
import secrets
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import replace
from email.utils import format_datetime
from hashlib import sha256
from html import escape
from pathlib import Path
from types import ModuleType

import jinja2.sandbox
from jinja2 import nodes
import linkify_it  # type: ignore[import-untyped]
import markdown_it
import mdit_py_plugins
import mdurl
from markupsafe import Markup

# python3 -I leaves this folder off the import path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodies  # noqa: E402
import content  # noqa: E402
import inputs  # noqa: E402
import qr  # noqa: E402
import safety  # noqa: E402

# all a template may use, so no page shows chance, such as a random pick, and
# the monitor renders the same site
NODES = tuple(
    getattr(nodes, name)
    for name in "Output TemplateData If For Name Const Getattr Getitem Compare"
    " Operand And Or Not".split()
)

# Each value a page prints waits in a hole until the page is whole, so the
# render knows where it lies. No template or value can guess a hole.
HOLE = secrets.token_hex(16)
HOLES = re.compile(f"{HOLE}([0-9]+)[.]")

# bytes, about seven times the whole site, so a template cannot fill the disk
OUTPUT_CAP = 100_000_000


def source_hash(*modules: ModuleType) -> bytes:
    """Hash the source of the code that shapes a body."""
    # files, not versions: a patched Debian library keeps its version number
    digest = sha256()
    for module in modules:
        path = Path(str(module.__file__))
        package = path.name == "__init__.py"
        files = sorted(path.parent.rglob("*.py")) if package else [path]
        for file in files:
            digest.update(file.read_bytes())
    return digest.digest()


BODY_CODE = source_hash(bodies, safety, markdown_it, mdit_py_plugins, linkify_it, mdurl)


def check_template(template: nodes.Template, name: str) -> None:
    """Stop the render on a template that uses more than NODES, range and length."""
    for node in template.find_all(nodes.Node):
        if isinstance(node, nodes.Call):
            known = node.node == nodes.Name("range", "load") and not node.kwargs
        elif isinstance(node, nodes.Filter):
            known = node.name == "length" and not node.args
        else:
            known = isinstance(node, NODES)
        if not known:
            raise ValueError(f"{name} uses {type(node).__name__}, which no page needs")


def shown(value: object) -> str:
    """Return a printed value as the page holds it: escaped, without bidi controls."""
    if isinstance(value, str):
        value = value.translate(safety.BIDI_CONTROLS)
    return Markup.escape(value)


class Render:
    """One run of the render: the listed addresses, and what its pages print."""

    def __init__(self, listed: set[str]) -> None:
        self.listed = listed
        self.printed: list[object] = []
        self.spent = 0

    def spend(self, size: int) -> None:
        """Count bytes of the pages, and stop the render once they pass OUTPUT_CAP."""
        self.spent += size
        if self.spent > OUTPUT_CAP:
            sys.exit(f"render: the pages hold more than {OUTPUT_CAP} bytes")

    # so Jinja calls this as it renders, for constants too
    @jinja2.pass_eval_context
    def printable(self, _: object, value: object) -> object:
        """Return a hole for what a template prints, or stop on anything else."""
        # an object's text holds its place in memory, so two renders would differ
        if not isinstance(value, (str, int, float, jinja2.Undefined)):
            raise TypeError(f"a template prints a {type(value).__name__}")
        self.spend(len(shown(value).encode()))
        self.printed.append(value)
        return Markup(f"{HOLE}{len(self.printed) - 1}.")

    def fill(
        self, template: jinja2.Template, values: Mapping[str, object]
    ) -> tuple[str, list[tuple[object, range]]]:
        """Return the page, and each value it prints with where it lies."""
        self.printed.clear()
        page = io.StringIO()
        for chunk in template.generate(values):
            self.spend(len(chunk.encode()))
            page.write(chunk)
        texts = [shown(value) for value in self.printed]
        text, spans = kept(page.getvalue(), texts, self.listed)
        return text, [(self.printed[i], at) for i, at in spans]


def templates(
    folder: Path, names: list[str], finalize: object
) -> dict[str, jinja2.Template]:
    """Compile every template up front, so a broken one stops the render early."""
    # sandboxed, as a dependency of the build could change a template
    loader = jinja2.FileSystemLoader(folder, encoding="utf-8")
    env = jinja2.sandbox.ImmutableSandboxedEnvironment(
        loader=loader,
        finalize=finalize,
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        # Astro writes the templates, and these do not clash with its braces
        variable_start_string="[[",
        variable_end_string="]]",
        block_start_string="[%",
        block_end_string="%]",
        comment_start_string="[#",
        comment_end_string="#]",
    )
    for name in names:
        check_template(env.parse(loader.get_source(env, f"{name}.html")[0]), name)
    return {name: env.get_template(f"{name}.html") for name in names}


def body_html(markdown: str, cache: Path | None, used: set[str]) -> str:
    if cache is None:
        return bodies.render(markdown)
    path = cache / f"{sha256(BODY_CODE + markdown.encode()).hexdigest()}.html"
    used.add(path.name)
    if path.exists():
        # as written: read as text, a carriage return would turn into a newline
        return path.read_bytes().decode()
    html = bodies.render(markdown)
    # synced, then renamed, as a later render trusts the name
    cache.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".tmp"), "w", encoding="utf-8") as file:
        file.write(html)
        os.fsync(file.fileno())
    path.with_suffix(".tmp").rename(path)
    return html


def filled(skeleton: str, texts: list[str]) -> tuple[str, list[tuple[int, range]]]:
    """Return the skeleton with each hole filled, and where each text lies."""
    parts, spans, size, last = [], [], 0, 0
    for hole in HOLES.finditer(skeleton):
        text = texts[int(hole[1])]
        parts += [skeleton[last : hole.start()], text]
        size += hole.start() - last
        spans.append((int(hole[1]), range(size, size + len(text))))
        size += len(text)
        last = hole.end()
    parts.append(skeleton[last:])
    return "".join(parts), spans


def kept(
    skeleton: str, texts: list[str], listed: set[str], decodes: int = 1
) -> tuple[str, list[tuple[int, range]]]:
    """Return the skeleton with its holes filled, and where each text lies.

    A text that shows an address the list lacks, alone or with the text near it,
    is left out, so no data makes checks.py site refuse a file.
    """
    stay = stays(texts, listed, decodes)
    for passes in range(2):
        text, spans = filled(skeleton, texts)
        out = touched(spans, safety.unlisted(text, listed, decodes), stay)
        if not any(texts[i] for i in out):
            return text, spans
        # on a second find, every text that may go goes, so no data can chain finds
        if passes == 1:
            out = {i for i, _ in spans if not stay[i]}
        for i in out:
            texts[i] = ""
    return filled(skeleton, texts)


def stays(texts: list[str], listed: set[str], decodes: int) -> list[bool]:
    """Return which texts stay, such as a wallet's link: a listed address, no other."""
    return [
        not listed.isdisjoint(safety.ADDRESS.findall(text))
        and not any(safety.unlisted(text, listed, decodes))
        for text in texts
    ]


def touched(
    spans: list[tuple[int, range]], found: Iterable[tuple], stay: list[bool]
) -> set[int]:
    """Return the texts that may go and overlap an address the list lacks."""
    found = list(found)
    return {
        i
        for i, at in spans
        for _, start, end in found
        if not stay[i] and start < at.stop and at.start < end
    }


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def feed(proposals: list[content.Proposal], site_url: str, listed: set[str]) -> str:
    """Return the Funding Required feed, each value left out as a page's can be."""
    texts: list[str] = []

    def hole(value: str) -> str:
        texts.append(shown(value))
        return f"{HOLE}{len(texts) - 1}."

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Monero CCS: Funding Required"
    ET.SubElement(channel, "link").text = f"{site_url}/funding-required/"
    ET.SubElement(channel, "description").text = "Proposals that need funding"
    for proposal in proposals:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = hole(proposal.title)
        ET.SubElement(item, "link").text = hole(site_url + proposal.url)
        ET.SubElement(item, "guid").text = hole(site_url + proposal.url)
        if proposal.day:
            ET.SubElement(item, "pubDate").text = hole(format_datetime(proposal.day))
        # a reader shows the description as HTML, so its text is escaped as such
        raised = f"Raised {proposal.raised} of {proposal.amount} XMR"
        ET.SubElement(item, "description").text = hole(
            escape(f"{raised}, by {proposal.author}")
        )
    skeleton = ET.tostring(rss, encoding="unicode")
    return kept(skeleton, texts, listed, decodes=2)[0]


def write_proposals(
    run: Render,
    template: jinja2.Template,
    proposals: list[content.Proposal],
    out: Path,
    cache: Path | None,
) -> set[str]:
    """Write each proposal's page and where its body lies; return the cache files used."""
    used: set[str] = set()
    spans = {}
    Path(f"{out}.spans").unlink(missing_ok=True)
    for proposal in proposals:
        body = Markup(body_html(proposal.markdown, cache, used))
        text, values = run.fill(template, {"it": replace(proposal, body=body)})
        # where the body went in, so nothing a template writes can widen it
        at = next((at for value, at in values if value is body), range(0))
        spans[proposal.file] = [at.start, at.stop]
        write(out / proposal.file, text)
    # JSON, as a proposal's file name may hold any character
    write(Path(f"{out}.spans"), json.dumps(spans))
    return used


def write_lists(
    run: Render, pages: dict[str, jinja2.Template], site: content.Site, out: Path
) -> None:
    """Write the home, stage and donate pages, and the projects JSON."""
    shared = {"counts": site.counts, "home": site.home}
    write(out / "index.html", run.fill(pages["home"], shared)[0])
    for page, cards in site.stage_pages.items():
        text, _ = run.fill(pages[page], {"cards": cards, **shared})
        write(out / page / "index.html", text)
    donate, _ = run.fill(pages["donate"], {"it": {"address": site.general_fund}})
    write(out / "donate" / "index.html", donate)
    write(out / "index.php" / "projects", json.dumps({"data": site.projects}))


def write_qr_codes(addresses: set[str], codes: dict[str, str], out: Path) -> None:
    for address in sorted(addresses):
        path = out / "qr" / f"{address}.png"
        path.parent.mkdir(exist_ok=True)
        rows = qr.modules(codes[address])
        if rows is None:
            raise ValueError(f"the QR code of {address} does not decode")
        path.write_bytes(qr.png(rows))


def drop_unused(cache: Path, used: set[str]) -> None:
    """Delete the cache files no proposal used, half-written ones too."""
    for old in cache.iterdir():
        if old.name not in used:
            old.unlink()


def main() -> None:
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    in_dir, template_dir, out = (Path(arg) for arg in sys.argv[1:4])
    cache = Path(sys.argv[4]) if len(sys.argv) == 5 else None

    given = inputs.read(in_dir)
    site = content.build(given)
    run = Render(set(given.qr_codes))
    names = ["home", "proposal", "donate", *site.stage_pages]
    pages = templates(template_dir, names, run.printable)
    used = write_proposals(run, pages["proposal"], site.proposals, out, cache)
    write_lists(run, pages, site, out)
    proposals = site.in_stage("funding-required")
    write(out / "feed.xml", feed(proposals, given.site_url, run.listed))
    write_qr_codes(site.addresses_shown, given.qr_codes, out)
    if cache is not None:
        drop_unused(cache, used)


if __name__ == "__main__":
    main()
