"""The deploy documents: every path, link and alert they name, and their style."""

import re

import pytest

from scripts import DEPLOY, REPO

DOCS = sorted(DEPLOY.rglob("*.md"))
IDS = [str(doc.relative_to(DEPLOY)) for doc in DOCS]
SCRIPTS = sorted(DEPLOY.glob("*/*.sh"))


def anchors(text):
    """Return the section anchors a Markdown file's headings make, as GitHub does."""
    headings = re.findall(r"^#+ (.+)$", text, re.M)
    return {
        re.sub(r"[^a-z0-9 -]", "", heading.lower().replace("`", "")).replace(" ", "-")
        for heading in headings
    }


def sentences(text):
    """Yield the doc's prose sentences, without code, tables, headings or bold."""
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    lines = prose.splitlines()
    prose = "\n".join(
        line for line in lines if not line.lstrip().startswith(("|", "#"))
    )
    prose = re.sub(r"`[^`]*`", "X", prose)
    prose = re.sub(r"\*\*[^*]*\*\*", "", prose)
    for chunk in re.split(r"\n\s*(?:\d+\.|-)\s+|\n\n", prose):
        yield from re.split(r"(?<=[.:])\s+", " ".join(chunk.split()))


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_every_path_a_doc_names_exists(doc):
    named = set(
        re.findall(
            r"(?<![\w/])(?:deploy|src)/[A-Za-z0-9_./-]*[A-Za-z0-9_]", doc.read_text()
        )
    )
    assert sorted(path for path in named if not (REPO / path).exists()) == []


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_every_link_and_section_anchor_resolves(doc):
    text = doc.read_text()
    broken = []
    for target, anchor in re.findall(
        r"\]\(([A-Za-z0-9_./-]*\.md|)(?:#([a-z0-9-]+))?\)", text
    ):
        linked = (doc.parent / target).resolve() if target else doc
        if not linked.exists():
            broken.append(target)
        elif anchor and anchor not in anchors(linked.read_text()):
            broken.append(f"{target}#{anchor}")
    assert broken == []


def test_the_docs_name_every_alert_the_scripts_raise_and_every_checks_py_command():
    code = "\n".join(
        line
        for script in SCRIPTS
        for line in script.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    alerts = set(
        re.findall(r'\b(?:alert|check|require) ([a-z][a-z-]+) (?:"?\$|[01] )', code)
    )
    assert alerts, "no alert names found in the scripts"
    docs = "\n".join(doc.read_text() for doc in DOCS)
    commands = [
        f"checks.py {name}"
        for name in ("values", "addresses", "apply", "status", "release")
    ]
    unnamed = [
        name for name in [*sorted(alerts), "page-", *commands] if f"`{name}" not in docs
    ]
    assert unnamed == []


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_sentence_is_over_25_words(doc):
    long = [
        sentence
        for sentence in sentences(doc.read_text())
        if len(sentence.split()) > 25
    ]
    assert long == []


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_doc_uses_a_contraction(doc):
    pattern = (
        r"\b(?:don|can|isn|aren|won|doesn|didn|it|that|there|you)'(?:t|s|re|ll|ve)\b"
    )
    assert re.findall(pattern, doc.read_text()) == []
