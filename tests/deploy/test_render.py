"""render.py and status.py, as update.sh and the monitor run them."""

import json
import re
import shutil

from scripts import DEPLOY, EXAMPLE, lib_sh, replaced, run
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


def test_a_template_cannot_reach_python_internals(tmp_path):
    templates = shutil.copytree(TEMPLATES, tmp_path / "templates")
    page = templates / "proposal.html"
    planted = "[[ cycler.__init__.__globals__.os.getuid() ]]"
    page.write_text(replaced(page.read_text(), "[[ it.title ]]", planted))
    failed = run(
        "python3",
        "-B",
        DEPLOY / "render/render.py",
        EXAMPLE,
        templates,
        tmp_path / "out",
        ok=False,
    )
    assert "unsafe" in failed.stderr


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
