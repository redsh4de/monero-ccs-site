#!/usr/bin/env python3
"""Render the site's data pages from the input folder.

Usage: render.py <in> <templates> <out> [<cache>]

Writes the proposal pages, the home and stage pages, the donate page,
/index.php/projects, and a QR code for every address shown into <out>. The
input folder <in> is described in inputs.py.

The templates are Jinja, written by Astro with [[ ]] and [% %] so they don't
clash with Astro's own braces, and they escape everything they insert.
Proposal bodies are cached by the hash of their Markdown and of the code that
renders them, so a donation only re-fills the templates.
"""

import json
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import jinja2
import linkify_it
import markdown_it
import mdit_py_plugins
import mdurl
from markupsafe import Markup

import bodies
import content
import inputs


def source_hash(*modules: ModuleType) -> bytes:
    """Hash the source of the code that shapes a body.

    A change to bodies.py, or a patched library (Debian keeps the version
    number), then starts the cache afresh.
    """
    digest = sha256()
    for module in modules:
        path = Path(module.__file__)
        package = path.name == "__init__.py"
        files = sorted(path.parent.rglob("*.py")) if package else [path]
        for file in files:
            digest.update(file.read_bytes())
    return digest.digest()


BODY_CODE = source_hash(bodies, markdown_it, mdit_py_plugins, linkify_it, mdurl)


def templates(folder: Path, names: list[str]) -> dict[str, jinja2.Template]:
    """Compile every template up front.

    A broken template then stops the render with its file name, before any page
    is written.
    """
    env = jinja2.Environment(
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
    # Written whole and renamed, so a cache file is never half there.
    cache.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".tmp").write_text(html, encoding="utf-8")
    path.with_suffix(".tmp").rename(path)
    return html


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def qr_code(address: str, out: Path) -> None:
    path = out / "qr" / f"{address}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["qrencode", "-s", "8", "-m", "4", "-o", str(path), f"monero:{address}"],
        check=True,
    )


def main() -> None:
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    in_dir, template_dir, out = (Path(arg) for arg in sys.argv[1:4])
    cache = Path(sys.argv[4]) if len(sys.argv) == 5 else None

    site = content.build(inputs.read(in_dir))
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
    for address in sorted(site.addresses_shown):
        qr_code(address, out)

    # Prune what no proposal used this time, half-written files included.
    if cache is not None:
        for old in cache.iterdir():
            if old.name not in used:
                old.unlink()


if __name__ == "__main__":
    main()
