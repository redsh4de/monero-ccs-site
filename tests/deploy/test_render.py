"""render.py and status.py, as update.sh and the monitor run them."""

import hashlib
import html
import json
import re
import shutil
from xml.etree import ElementTree

import pytest

from harness import CHECKS, DEPLOY, EXAMPLE, lib_sh, replaced, run
from web import DIST

TEMPLATES = DIST / "templates"


def render(script, inputs, templates, out, *cache):
    return run("python3", "-B", script, inputs, templates, out, *cache)


def test_a_change_to_safety_py_starts_the_body_cache_afresh(tmp_path):
    """A fix to the address rule applies to every body, not only to new ones."""
    old, new = tmp_path / "old", tmp_path / "new"
    shutil.copytree(DEPLOY / "render", old, ignore=shutil.ignore_patterns("*.pyc"))
    shutil.copytree(DEPLOY / "render", new, ignore=shutil.ignore_patterns("*.pyc"))
    safety = new / "safety.py"
    safety.write_text(replaced(safety.read_text(), "{90,}", "{8,}"))
    cache = tmp_path / "bodies"
    render(old / "render.py", EXAMPLE, TEMPLATES, tmp_path / "with-old", cache)
    render(new / "render.py", EXAMPLE, TEMPLATES, tmp_path / "cached", cache)
    render(new / "render.py", EXAMPLE, TEMPLATES, tmp_path / "fresh")
    run("diff", "-r", tmp_path / "cached", tmp_path / "fresh")


def refused(tmp_path, planted, refusal):
    """Render with planted in place of the proposal's title, which must fail."""
    templates = shutil.copytree(TEMPLATES, tmp_path / "templates")
    page = templates / "proposal.html"
    page.write_text(replaced(page.read_text(), "[[ it.title ]]", planted))
    script = DEPLOY / "render/render.py"
    run("python3", "-B", script, EXAMPLE, templates, tmp_path / "out", refusal=refusal)


def test_a_template_cannot_reach_python_internals(tmp_path):
    refused(tmp_path, "[[ cycler.__init__.__globals__.os ]]", "unsafe")


@pytest.mark.parametrize(
    "planted, reason",
    [
        pytest.param("[[ lipsum() ]]", "uses Call", id="random text"),
        pytest.param("[[ [1, 2]|random ]]", "uses Filter", id="a random choice"),
        pytest.param('[[ "" ~ range ]]', "uses Concat", id="a place in memory"),
        pytest.param("[[ it.title.upper ]]", "prints a builtin", id="a method"),
    ],
)
def test_a_template_cannot_make_two_renders_differ(tmp_path, planted, reason):
    refused(tmp_path, planted, reason)


def test_only_a_proposals_own_text_may_link_elsewhere(tmp_path):
    """A file name may hold any character that prints, a space among them."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    text = proposal.read_text() + "\n[elsewhere](https://elsewhere.example/)\n"
    proposal.unlink()
    (inputs / "proposals/ a name 1 2.md").write_text(text)
    out = tmp_path / "out"
    render(DEPLOY / "render/render.py", inputs, TEMPLATES, out)
    site = [CHECKS, "site", out, inputs / "addresses", DEPLOY / "site-hosts"]
    run(*site)
    name = "proposals/ a name 1 2.html"
    end = json.loads((tmp_path / "out.spans").read_text())[name][1]
    text = (out / name).read_text()
    link = '<a href="https://elsewhere.example/">x</a>'
    (out / name).write_text(text[:end] + link + text[end:])
    run(*site, refusal="links where it may not: https://elsewhere.example/")


def test_a_bidi_control_in_the_data_never_reaches_a_page(tmp_path):
    """checks.py site refuses one, so the render drops it from all it prints."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    proposal.write_text(proposal.read_text() + "\nSend to: &#x202E;abc\n")
    merge_requests = json.loads((inputs / "mr/open.json").read_text())
    for merge_request in merge_requests:
        merge_request["web_url"] += "\N{RIGHT-TO-LEFT OVERRIDE}"
    (inputs / "mr/open.json").write_text(json.dumps(merge_requests))
    checked(inputs, tmp_path / "out")


BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def completed(head):
    """Return a valid address that starts with head, as anyone can make one."""
    first = head + "1" * (88 - len(head))
    values = [0] * 8
    for i, char in enumerate(first):
        values[i // 11] = values[i // 11] * 58 + BASE58.index(char)
    if max(values) >= 256**8:
        return None
    data = b"".join(value.to_bytes(8, "big") for value in values) + b"\0"
    tail = int.from_bytes(data[-1:] + hashlib.new("keccak-256", data).digest()[:4])
    return first + "".join(BASE58[tail // 58**k % 58] for k in range(6, -1, -1))


def opened(inputs, title1, title2):
    """Open two merge requests by one author, a day apart."""
    merge_requests = json.loads((inputs / "mr/open.json").read_text())
    for number, title in ((901, title1), (900, title2)):
        merge_requests.append(
            {
                "iid": number,
                "title": title,
                "author": "a",
                "web_url": f"https://repo.getmonero.org/x/-/merge_requests/{number}",
                "created_at": f"2026-11-{number - 879}T10:00:00.000Z",
                "adds": [f"idea-{number}.md"],
                "upvotes": 0,
                "downvotes": 0,
            }
        )
    (inputs / "mr/open.json").write_text(json.dumps(merge_requests))


def checked(inputs, out):
    """Render as update.sh does, status.json included, and check the site."""
    render(DEPLOY / "render/render.py", inputs, TEMPLATES, out)
    status = [DEPLOY / "render/status.py", "write", inputs, "1", "0" * 40]
    (out / "status.json").write_text(run("python3", "-B", *status).stdout)
    run(CHECKS, "site", out, inputs / "addresses", DEPLOY / "site-hosts")
    return out


def test_a_merged_proposal_cannot_piece_together_an_address_in_the_feed(tmp_path):
    """Its amount and its author, around an &, which a reader decodes twice."""
    pad = next(p for p in range(1, 40) if completed(f"4{'1' * p}XMRby{'1' * 10}"))
    address = completed(f"4{'1' * pad}XMRby{'1' * 10}")
    author = f"{'1' * 10} & {address[pad + 16 :]}"
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    text = re.sub(
        "(?m)^amount: .*$", f"amount: x{address[: pad + 1]}", proposal.read_text()
    )
    proposal.write_text(replaced(text, "author: example", f"author: {author}"))
    checked(inputs, tmp_path / "out")


def test_a_proposal_named_to_end_an_address_with_its_page_is_left_out(tmp_path):
    """The h of .html ends it."""
    heads = ("4" + digit * pad for digit in "123456789" for pad in range(1, 88))
    address = next(a for a in map(completed, heads) if a and a[-1] == "h")
    name = f"{address[:47]}-{address[47:94]}"
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    shutil.copy(
        inputs / "proposals/example-funding.md", inputs / f"proposals/{name}.md"
    )
    checked(inputs, tmp_path / "out")


@pytest.mark.parametrize(
    "title, author",
    [
        pytest.param("Fund " * 2000, "example", id="past its cap"),
        pytest.param("&t Fund Fund", "*t", id="with an alias, which repeats text"),
    ],
)
def test_front_matter_that_could_print_more_than_its_cap_counts_as_none(
    tmp_path, title, author
):
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    text = re.sub("(?m)^title: .*$", f"title: {title}", proposal.read_text())
    proposal.write_text(replaced(text, "author: example", f"author: {author}"))
    out = checked(inputs, tmp_path / "out")
    assert "Fund Fund" not in (out / "proposals/example-funding.html").read_text()


def test_a_body_that_shows_an_address_in_an_attribute_is_removed(tmp_path):
    """A code block's language becomes its class."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    block = f"```{'1' * 95}\nx\n```"
    proposal.write_text(proposal.read_text() + "\n" + block + "\n")
    out = checked(inputs, tmp_path / "out")
    page = (out / "proposals/example-funding.html").read_text()
    assert "<p>[address removed]</p>" in page


def test_a_control_character_in_a_title_never_breaks_the_feed(tmp_path):
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    title = 'title: "Fund \\v\\ud800 this"'
    text = re.sub("(?m)^title: .*$", lambda _: title, proposal.read_text())
    proposal.write_text(text)
    out = checked(inputs, tmp_path / "out")
    ElementTree.parse(out / "feed.xml")


def test_a_cached_body_renders_as_a_fresh_one(tmp_path):
    """Even one that holds a carriage return, which reading as text would change."""
    script = DEPLOY / "render/render.py"
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    proposal.write_text(proposal.read_text() + "\na&#13;b\n")
    cache = tmp_path / "cache"
    for out, *used in (("fresh",), ("first", cache), ("cached", cache)):
        render(script, inputs, TEMPLATES, tmp_path / out, *used)
    page = "proposals/example-funding.html"
    fresh = (tmp_path / "fresh" / page).read_bytes()
    assert b"\r" in fresh and (tmp_path / "cached" / page).read_bytes() == fresh


def test_a_proposal_whose_name_does_not_print_is_left_out(tmp_path):
    """A newline in it would split it in two in the monitor's list of pages."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposals = inputs / "proposals"
    shutil.copy(proposals / "example-funding.md", proposals / "two\nlines.md")
    out = checked(inputs, tmp_path / "out")
    assert not list((out / "proposals").glob("two*"))


@pytest.mark.parametrize(
    "adds",
    [
        pytest.param(["idea-a.md", "idea-b.md"], id="two files"),
        pytest.param(["idea-" + "0" * 1100 + ".md"], id="a name too long for a page"),
    ],
)
def test_a_merge_request_adds_a_file_only_by_one_name_a_proposal_could_have(
    tmp_path, adds
):
    """status.json holds no other, which the snapshot check could refuse."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    opened(inputs, "One idea", "Another idea")
    merge_requests = json.loads((inputs / "mr/open.json").read_text())
    merge_requests[-2]["adds"] = adds
    (inputs / "mr/open.json").write_text(json.dumps(merge_requests))
    out = checked(inputs, tmp_path / "out")
    status = json.loads((out / "status.json").read_text())
    assert [mr["adds"] for mr in status["merge_requests"] if mr["iid"] == 901] == [[]]
    run(CHECKS, "snapshot", out / "status.json")


@pytest.mark.parametrize("where", ["title", "file"])
def test_entities_in_a_merge_request_never_stop_the_update(tmp_path, where):
    """status.json holds them raw, and its values are read with entities decoded."""
    heads = ("4" + digit * pad for digit in "123456789" for pad in range(1, 88))
    address = next(filter(None, map(completed, heads)))
    text = "&#52;" + ".".join(address[i : i + 20] for i in range(1, 95, 20))
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    opened(inputs, text if where == "title" else "One idea", "Another idea")
    if where == "file":
        merge_requests = json.loads((inputs / "mr/open.json").read_text())
        merge_requests[-2]["adds"] = [text + ".md"]
        (inputs / "mr/open.json").write_text(json.dumps(merge_requests))
    checked(inputs, tmp_path / "out")


def test_a_template_cannot_fill_the_disk(tmp_path):
    """The cap counts bytes: these pages hold fewer characters than it."""
    flood = "[% for i in range(100000) %]" + "\U0001f600" * 30 + "[% endfor %]"
    refused(tmp_path, flood, "the pages hold more than 100000000 bytes")


def test_the_feed_shows_a_proposals_text_as_text(tmp_path):
    """A feed reader shows a description as HTML."""
    inputs = shutil.copytree(EXAMPLE, tmp_path / "in")
    proposal = inputs / "proposals/example-funding.md"
    image = 'author: <img src="https://elsewhere.example/x.png">'
    proposal.write_text(replaced(proposal.read_text(), "author: example", image))
    render(DEPLOY / "render/render.py", inputs, TEMPLATES, tmp_path / "out")
    feed = ElementTree.parse(tmp_path / "out/feed.xml")
    descriptions = [item.findtext("description") for item in feed.iter("item")]
    shown = html.escape(image.removeprefix("author: "))
    assert any(text.endswith(f"by {shown}") for text in descriptions)


def test_a_proposal_link_preview_gives_its_stage_and_funds(tmp_path):
    render(DEPLOY / "render/render.py", EXAMPLE, TEMPLATES, tmp_path)
    previews = {
        page: re.findall(
            r'<meta property="og:description" content="([^"]*)"',
            (tmp_path / f"proposals/{page}.html").read_text(),
        )
        for page in ("example-funding", "example-progress", "example-completed")
    }
    assert previews == {
        "example-funding": ["Funding required: 10 of 25 XMR raised. By example."],
        "example-progress": [
            "Work in progress: 60 XMR funded, 1 of 2 milestones done. By example."
        ],
        "example-completed": ["Completed: 9.9 XMR funded. By example."],
    }


def restore(rendered, tmp_path):
    """Return the render's input, rebuilt as the monitor does from status.json."""
    restored = tmp_path / "restored"
    (restored / "mr").mkdir(parents=True)
    status_py = DEPLOY / "render/status.py"
    run(
        "python3",
        "-B",
        status_py,
        "restore",
        rendered.current / "status.json",
        restored,
    )
    return restored


def test_the_merge_request_snapshot_round_trips(rendered, tmp_path):
    restored = restore(rendered, tmp_path)
    served = rendered.served("merge_requests")
    for merge_request in served:
        del merge_request["upvotes"], merge_request["downvotes"]
    expected = json.dumps(
        served, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    mrs = run("python3", "-B", DEPLOY / "render/status.py", "mrs", restored)
    assert mrs.stdout.strip() == expected


def test_the_restored_input_renders_the_same_ideas_and_proposal_pages(
    rendered, tmp_path
):
    restored = restore(rendered, tmp_path)
    (restored / "proposals").symlink_to(rendered.ccs / "in/repo/proposals")
    shutil.copy(rendered.ccs / "in/repo/mr/merged.tsv", restored / "mr")
    verified = tmp_path / "addresses"
    lib_sh('verify_list "$1"', verified)
    values = rendered.ccs / "state/values"
    lib_sh('render_values "$1" "$2" "$3" >/dev/null', values, restored, verified)
    release = rendered.lib / "releases/1"
    out = shutil.copytree(release / "dist", tmp_path / "out")
    shutil.rmtree(out / "templates")
    render(
        release / "deploy/render/render.py", restored, release / "dist/templates", out
    )

    ideas = rendered.page("ideas/index.html").replace("\n", "")
    up = r"icon-thumbs-up[^<]*</span> *<span[^>]*>7</span>"
    down = r"icon-thumbs-down[^<]*</span> *<span[^>]*>1</span>"
    assert re.search(f"{up}.*{down}", ideas), "an idea's votes do not show"
    assert (out / "ideas/index.html").read_text() == rendered.page("ideas/index.html")
    run("diff", "-r", out / "proposals", rendered.current / "proposals")
