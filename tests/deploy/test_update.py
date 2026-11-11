"""update.sh: what the web server serves after each push, release and list."""

import json
import os
import re
import shutil
import stat
import xml.etree.ElementTree as ET

import pytest

from scripts import replaced
from web import A, B, C, COMMIT, DIST

UNLISTED = "4" + "U" * 94


def set_address(proposal, address):
    """Return an edit for WebServer.push that gives the proposal this address."""

    def edit(text):
        found = re.search(rf"^proposal {proposal} .*$", text, flags=re.M)
        line = found[0] if found else f"proposal {proposal} "
        return replaced(text, line, re.sub(r"address=\S*$", f"address={address}", line))

    return edit


def bad_first_line(text):
    return text.replace("\n", " <b>\n", 1)


def broken_release(tmp_path, template, old, new):
    dist = shutil.copytree(DIST, tmp_path / f"dist-{template}")
    page = dist / "templates" / template
    page.write_text(replaced(page.read_text(), old, new))
    return dist


def test_without_a_list_every_donation_box_is_closed(web):
    web.install(1)
    web.push()
    web.update()
    assert web.open_boxes() == 0
    assert "/qr/" not in web.page("donate/index.html")


def test_a_signed_list_opens_the_listed_boxes_and_the_general_fund_qr(rendered):
    assert rendered.open_boxes() >= 1
    assert "/qr/" in rendered.page("donate/index.html")


@pytest.mark.parametrize(
    "path",
    [
        "index.html",
        "funding-required/index.html",
        "proposals/example-funding.html",
        "status.json",
        "index.php/projects",
        "feed.xml",
    ],
)
def test_a_run_serves_every_page_kind(rendered, path):
    assert rendered.page(path)


def test_a_run_serves_the_qr_codes(rendered):
    assert list((rendered.current / "qr").glob("*.png"))


def test_the_feed_lists_the_funding_proposals_newest_first(rendered):
    items = ET.fromstring(rendered.page("feed.xml")).findall("channel/item")
    links = [item.findtext("link").rsplit("/", 1)[1] for item in items]
    assert links == [
        "example-funding.html",
        "example-funded.html",
        "example-no-address.html",
    ]


def test_the_feed_links_to_site(rendered):
    default = "<link>https://ccs.getmonero.org/proposals/example-funding.html</link>"
    assert default in rendered.page("feed.xml")
    rendered.update(SITE="https://staging.example")
    staging = "<link>https://staging.example/proposals/example-funding.html</link>"
    assert staging in rendered.page("feed.xml")


def test_status_json_names_only_the_release_commit_and_merge_requests(rendered):
    status = json.loads(rendered.page("status.json"))
    assert sorted(status) == ["commit", "merge_requests", "release"]
    assert status["commit"] == COMMIT


def test_no_templates_are_served(rendered):
    assert not (rendered.current / "templates").exists()


def test_the_tree_is_world_readable(rendered):
    mode = os.stat(rendered.current.resolve()).st_mode
    assert stat.S_IMODE(mode) == 0o755


def test_a_newer_push_with_the_same_values_makes_no_new_tree(rendered):
    tree, before = rendered.tree(), rendered.applied()
    rendered.push()
    rendered.update()
    assert rendered.tree() == tree
    assert rendered.applied() > before
    rendered.update()
    assert rendered.tree() == tree


def test_an_older_push_is_ignored(rendered):
    applied = rendered.applied()
    rendered.push(
        edit=lambda text: re.sub(r"updated \d+", f"updated {applied - 100}", text)
    )
    rendered.update()
    assert rendered.applied() == applied


def test_a_donation_renders_a_new_tree(rendered):
    tree = rendered.tree()
    rendered.push(raised="12")
    rendered.update()
    assert rendered.tree() != tree
    assert rendered.raised() == 12.0
    assert "Raised 12 of 25 XMR" in rendered.page("feed.xml")


def test_unchanged_files_keep_their_inode(rendered):
    stylesheet = sorted((rendered.current / "_astro").glob("*.css"))[0]
    inode = stylesheet.stat().st_ino
    rendered.push(raised="12")
    rendered.update()
    assert (rendered.current / "_astro" / stylesheet.name).stat().st_ino == inode


def test_two_trees_are_kept(rendered):
    for raised in ("12", "13"):
        rendered.push(raised=raised)
        rendered.update()
        assert rendered.trees() == 2


def test_an_address_the_new_list_drops_disappears_and_the_old_values_stay(
    rendered, sign_list
):
    sign_list(C)
    rendered.push(raised="99", edit=bad_first_line)
    rendered.update()
    assert A not in rendered.page("proposals/example-funding.html")
    assert A not in rendered.page("index.php/projects")
    assert rendered.raised() == 10.0


def test_a_push_giving_an_address_to_another_proposal_is_refused(rendered, sign_list):
    sign_list(A, B)
    rendered.update()
    applied = rendered.applied()
    rendered.push(raised="16", edit=set_address("example-no-address", A))
    refused = rendered.update()
    assert "example-no-address shows the address of example-funding" in refused.stderr
    assert rendered.applied() == applied
    assert A not in rendered.page("proposals/example-no-address.html")


def test_a_push_moving_a_proposal_to_another_listed_address_is_refused(
    rendered, sign_list
):
    sign_list(A, B)
    rendered.update()
    applied = rendered.applied()
    rendered.push(raised="16", edit=set_address("example-funding", B))
    refused = rendered.update()
    assert "example-funding moves off its address" in refused.stderr
    assert rendered.applied() == applied
    assert A in rendered.page("proposals/example-funding.html")


def test_a_new_list_without_the_pinned_address_releases_it(rendered, sign_list):
    sign_list(B)
    rendered.push(raised="16", edit=set_address("example-funding", B))
    rendered.update()
    assert B in rendered.page("proposals/example-funding.html")
    assert rendered.raised() == 16.0


def test_going_back_to_the_old_list_shows_the_old_address_again(rendered, sign_list):
    sign_list(B)
    rendered.push(edit=set_address("example-funding", B))
    rendered.update()
    sign_list(A)
    rendered.push(raised="13")
    rendered.update()
    assert A in rendered.page("proposals/example-funding.html")
    assert rendered.raised() == 13.0


def test_a_new_release_renders_and_status_names_it(rendered):
    tree = rendered.tree()
    rendered.install(2)
    rendered.update()
    assert rendered.served("release") == 2
    assert rendered.tree() != tree


def test_a_release_whose_template_shows_an_unlisted_address_is_refused(
    rendered, tmp_path
):
    tree = rendered.tree()
    dist = broken_release(tmp_path, "home.html", "</body>", f"<p>{UNLISTED}</p></body>")
    rendered.install(3, dist)
    rendered.push(raised="14")
    refused = rendered.update(ok=False)
    assert "address the signed list lacks" in refused.stderr
    assert rendered.tree() == tree
    served = rendered.current.rglob("*")
    assert not any(
        UNLISTED in f.read_text(errors="replace") for f in served if f.is_file()
    )


def test_a_bad_values_line_keeps_the_old_values(rendered):
    rendered.push(raised="99", edit=bad_first_line)
    rendered.update()
    assert rendered.raised() == 10.0


def test_a_release_whose_render_fails_keeps_the_site_and_leaves_the_values_unapplied(
    rendered, tmp_path
):
    tree, applied = rendered.tree(), rendered.applied()
    dist = broken_release(tmp_path, "proposal.html", "[[ it.title ]]", "[[ it.nope ]]")
    rendered.install(4, dist)
    rendered.push(raised="15")
    rendered.update(ok=False)
    assert rendered.applied() == applied
    assert rendered.tree() == tree
    assert rendered.served("release") == 1


def test_values_count_as_applied_only_once_shown(rendered, tmp_path):
    applied = rendered.applied()
    dist = broken_release(tmp_path, "proposal.html", "[[ it.title ]]", "[[ it.nope ]]")
    rendered.install(4, dist)
    rendered.push(raised="15")
    rendered.update(ok=False)
    rendered.install(5)
    rendered.update()
    assert rendered.raised() == 15.0
    assert rendered.applied() > applied
