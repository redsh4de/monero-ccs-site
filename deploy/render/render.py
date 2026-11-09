#!/usr/bin/env python3
"""Render the site's data pages from the input folder.

Usage: render.py <input-dir> <templates-dir> <out-dir> [<cache-dir>]

  <input-dir>      the render's input: proposals/, mr/, values, general-fund and
                   site, as inputs.py describes them
  <templates-dir>  the Jinja templates, dist/templates in a release
  <out-dir>        where the pages go
  <cache-dir>      optional: rendered proposal bodies, kept between runs

Writes the proposal pages, the home and stage pages, the donate page,
/index.php/projects, the Funding Required feed, and a QR code for every address
shown into <out-dir>.

The templates are Jinja, written by Astro with [[ ]] and [% %] so they don't
clash with Astro's own braces, and they escape everything they insert.
Proposal bodies are cached by the hash of their Markdown and of the code that
renders them, so a donation only re-fills the templates.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from email.utils import format_datetime
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import jinja2.sandbox
import linkify_it
import markdown_it
import mdit_py_plugins
import mdurl
from markupsafe import Markup

# python3 -I leaves this folder off the import path, so the render's own
# modules are found from here
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodies  # noqa: E402
import content  # noqa: E402
import inputs  # noqa: E402
import qr  # noqa: E402
import safety  # noqa: E402


def source_hash(*modules: ModuleType) -> bytes:
    """Hash the source of the code that shapes a body.

    A change to bodies.py, to the safety.py rules it redacts by, or a patched
    library (Debian keeps the version number), then starts the cache afresh.
    """
    digest = sha256()
    for module in modules:
        path = Path(module.__file__)
        package = path.name == "__init__.py"
        files = sorted(path.parent.rglob("*.py")) if package else [path]
        for file in files:
            digest.update(file.read_bytes())
    return digest.digest()


BODY_CODE = source_hash(bodies, safety, markdown_it, mdit_py_plugins, linkify_it, mdurl)


def templates(folder: Path, names: list[str]) -> dict[str, jinja2.Template]:
    """Compile every template up front.

    A broken template then stops the render with its file name, before any page
    is written. They run in Jinja's sandbox: the templates come from the build,
    so one that a dependency changed still can't reach Python's internals.
    """
    env = jinja2.sandbox.SandboxedEnvironment(
        loader=jinja2.FileSystemLoader(folder, encoding="utf-8"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        variable_start_string="[[",
        variable_end_string="]]",
        block_start_string="[%",
        block_end_string="%]",
        comment_start_string="[#",
        comment_end_string="#]",
    )
    return {name: env.get_template(f"{name}.html") for name in names}


def body_html(markdown: str, cache: Path | None, used: set[str]) -> str:
    if cache is None:
        return bodies.render(markdown)
    path = cache / f"{sha256(BODY_CODE + markdown.encode()).hexdigest()}.html"
    used.add(path.name)
    if path.exists():
        return path.read_text(encoding="utf-8")
    html = bodies.render(markdown)
    # Written whole, on disk, then renamed: a later render trusts the name, so a
    # crash must never leave a cache file half there or empty.
    cache.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".tmp"), "w", encoding="utf-8") as file:
        file.write(html)
        os.fsync(file.fileno())
    path.with_suffix(".tmp").rename(path)
    return html


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def feed(proposals: list[content.Proposal], site_url: str) -> str:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Monero CCS: Funding Required"
    ET.SubElement(channel, "link").text = f"{site_url}/funding-required/"
    ET.SubElement(channel, "description").text = "Proposals that need funding"
    for proposal in proposals:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = proposal.title
        ET.SubElement(item, "link").text = site_url + proposal.url
        ET.SubElement(item, "guid").text = site_url + proposal.url
        if proposal.day:
            ET.SubElement(item, "pubDate").text = format_datetime(proposal.day)
        ET.SubElement(item, "description").text = (
            f"Raised {proposal.raised} of {proposal.amount} XMR, by {proposal.author}"
        )
    return ET.tostring(rss, encoding="unicode")


def build_site(given: inputs.Inputs) -> content.Site:
    """Build the site, and close each donation box whose code wasn't read back.

    Such a box shows no address, as when its address isn't on the signed list.
    The rest of the site still updates, and the monitor's addresses alert names
    the proposal.
    """
    site = content.build(given)
    hashes = given.qr_hashes
    unread = {a for a in site.addresses_shown if not qr.is_read_back(a, hashes.get(a))}
    if not unread:
        return site
    listed = " ".join(sorted(unread))
    print(f"render: no QR code read back for {listed}", file=sys.stderr)
    wallets = {
        name: replace(wallet, address="") if wallet.address in unread else wallet
        for name, wallet in given.wallets.items()
    }
    fund = "" if given.general_fund in unread else given.general_fund
    return content.build(replace(given, wallets=wallets, general_fund=fund))


def main() -> None:
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    in_dir, template_dir, out = (Path(arg) for arg in sys.argv[1:4])
    cache = Path(sys.argv[4]) if len(sys.argv) == 5 else None

    given = inputs.read(in_dir)
    site = build_site(given)
    names = ["home", "proposal", "donate", *site.stage_pages]
    pages = templates(template_dir, names)
    shared = {"counts": site.counts, "home": site.home}

    used = set()
    for proposal in site.proposals:
        proposal = replace(
            proposal, body=Markup(body_html(proposal.markdown, cache, used))
        )
        write(out / proposal.file, pages["proposal"].render(it=proposal))
    write(out / "index.html", pages["home"].render(**shared))
    for page, cards in site.stage_pages.items():
        write(out / page / "index.html", pages[page].render(cards=cards, **shared))
    donate = pages["donate"].render(it={"address": site.general_fund})
    write(out / "donate" / "index.html", donate)
    write(out / "index.php" / "projects", json.dumps({"data": site.projects}))
    write(out / "feed.xml", feed(site.stage_pages["funding-required"], given.site_url))
    for address in sorted(site.addresses_shown):
        path = out / "qr" / f"{address}.png"
        path.parent.mkdir(exist_ok=True)
        qr.write(address, path, given.qr_hashes.get(address))

    # Prune what no proposal used this time, half-written files included.
    if cache is not None:
        for old in cache.iterdir():
            if old.name not in used:
                old.unlink()


if __name__ == "__main__":
    main()
