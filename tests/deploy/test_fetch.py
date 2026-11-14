"""fetch.sh and mrs.py: the content the web server fetches from GitLab."""

import json
import os
import re
import socket
import time

import pytest

from scripts import DEPLOY, EXAMPLE, run, stand_in


@pytest.fixture
def fetched(fetcher):
    """Return a fetcher after one good first fetch, with fresh applied values."""
    applied(fetcher, int(time.time()))
    fetcher.values(1)
    fetcher.fetch()
    return fetcher


def applied(fetcher, updated):
    (fetcher.ccs / "state").mkdir(exist_ok=True)
    (fetcher.ccs / "state/values").write_text(f"updated {updated}\n")


def read(fetcher, path):
    return (fetcher.ccs / "in" / path).read_text()


def adds(fetcher):
    """Return the files that each open merge request adds, by its number."""
    merge_requests = json.loads(read(fetcher, "mr/open.json"))
    return {mr["iid"]: mr["adds"] for mr in merge_requests if mr["adds"]}


def requests(fetcher):
    return len(fetcher.server.log("access.log"))


def test_a_first_fetch_links_the_head_tree_and_the_merge_requests(fetcher):
    fetcher.values(1)
    fetcher.fetch()
    repo, _ = fetcher.links()
    assert repo == str(fetcher.ccs / "fetch/trees" / fetcher.tip())
    proposals = list((fetcher.ccs / "in/repo/proposals").glob("*.md"))
    assert len(proposals) == len(list((EXAMPLE / "proposals").glob("*.md")))
    assert read(fetcher, "repo/mr/merged.tsv").startswith("1\t")
    merge_requests = json.loads(read(fetcher, "mr/open.json"))
    assert len(merge_requests) == 3
    assert set(merge_requests[0]) == {
        "iid",
        "title",
        "author",
        "web_url",
        "created_at",
        "adds",
        "upvotes",
        "downvotes",
    }
    assert adds(fetcher) == {3: ["idea.md"]}


def test_the_same_gitlab_line_within_the_hour_makes_no_call(fetched):
    before = requests(fetched)
    fetched.fetch()
    assert fetched.server.log("access.log")[before:] == []


def test_a_new_gitlab_line_calls_the_api_and_reuses_cached_diffs(fetched):
    links, before = fetched.links(), requests(fetched)
    fetched.values(2)
    fetched.fetch()
    assert requests(fetched) > before
    assert fetched.links() == links
    assert fetched.server.calls("merge_requests/3/diffs") == 1


def test_fetch_follows_a_new_commit_and_a_force_push(fetched):
    fetched.commit("new-one.md")
    fetched.values(3)
    fetched.fetch()
    assert fetched.links()[0] == str(fetched.ccs / "fetch/trees" / fetched.tip())
    fetched.rewrite()
    fetched.values(4)
    fetched.fetch()
    assert fetched.links()[0] == str(fetched.ccs / "fetch/trees" / fetched.tip())
    assert len(list((fetched.ccs / "fetch/trees").iterdir())) == 2


@pytest.fixture
def many(fetched):
    """Return a fetcher that has fetched 150 merge requests, over two pages."""
    fetched.mrs(150)
    files = [("tab\there.md", True), ("nl\nhere.md", True), ("ok.md", True)]
    fetched.server.changes(150, files)
    fetched.values(5)
    fetched.fetch()
    return fetched


def test_150_merge_requests_arrive_in_api_order_over_two_pages(many):
    iids = [mr["iid"] for mr in json.loads(read(many, "mr/open.json"))]
    assert iids == list(range(150, 0, -1))
    assert many.server.calls("page=2") >= 1


def test_paths_with_a_tab_or_newline_are_left_out(many):
    assert adds(many) == {150: ["ok.md"], 3: ["idea.md"]}


def test_a_merge_requests_101_files_arrive_over_two_pages(many):
    many.server.changes(151, [(f"f{number:03}.md", True) for number in range(101)])
    many.mrs(151)
    many.values(6)
    many.fetch()
    assert len(adds(many)[151]) == 101
    assert many.server.calls("merge_requests/151/diffs") == 2


def test_two_merge_request_sets_are_kept(many):
    many.mrs(151)
    many.values(6)
    many.fetch()
    assert len(list((many.ccs / "fetch").glob("mr-*"))) == 2


def test_over_1000_open_merge_requests_fails(fetched):
    fetched.mrs(1001)
    fetched.values(7)
    failed = fetched.fetch(ok=False)
    assert "more than 1000 entries" in failed.stderr


def test_one_failing_merge_request_is_left_out_and_retried(fetched):
    fetched.server.changes(2, [("two.md", True)])
    for cached in (fetched.ccs / "fetch/changes").glob("2.*"):
        cached.unlink()
    (fetched.server.state / "changes/2.fail").touch()
    fetched.values(11)
    fetched.fetch()
    assert adds(fetched) == {3: ["idea.md"]}
    assert not list((fetched.ccs / "fetch/changes").glob("2.*"))
    (fetched.server.state / "changes/2.fail").unlink()
    fetched.values(12)
    fetched.fetch()
    assert adds(fetched) == {3: ["idea.md"], 2: ["two.md"]}


def test_a_failing_api_fails_the_fetch_and_keeps_the_links(fetched):
    (fetched.server.state / "fail").touch()
    fetched.values(16)
    links = fetched.links()
    failed = fetched.fetch(ok=False)
    assert "GitLab's list didn't load" in failed.stderr
    assert "fetch: content failed" in failed.stderr
    assert fetched.links() == links


def test_one_500_is_retried(fetched):
    (fetched.server.state / "fail-once").touch()
    fetched.values(40)
    fetched.fetch()
    assert not (fetched.server.state / "fail-once").exists()


def test_a_failed_git_rev_parse_fails_the_fetch(fetched, tmp_path):
    script = (
        '#!/bin/sh\ncase "$*" in *rev-parse*) exit 1 ;; esac\nexec /usr/bin/git "$@"\n'
    )
    links = fetched.links()
    fetched.values(30)
    failed = fetched.fetch(ok=False, PATH=stand_in(tmp_path / "bin", "git", script))
    assert "fetch: content failed" in failed.stderr
    assert fetched.links() == links


SLOW = {
    "git": '#!/bin/sh\ncase "$*" in *fetch*) sleep 60 ;; esac\nexec /usr/bin/git "$@"\n',
    "curl": '#!/bin/sh\ncase "$*" in */merge_requests*) sleep 60 ;; esac\n'
    'exec /usr/bin/curl "$@"\n',
}


@pytest.mark.parametrize("command", ["git", "curl"])
def test_a_gitlab_slower_than_the_steps_limit_fails_the_fetch(
    fetched, tmp_path, command
):
    """A stand-in cuts each limit to 1 s, and logs the real ones."""
    limits = tmp_path / "limits"
    short = f'#!/bin/sh\necho "$1" >>{limits}\nshift\nexec /usr/bin/timeout 1 "$@"\n'
    stand_in(tmp_path / "bin", "timeout", short)
    fetched.values(32)
    path = stand_in(tmp_path / "bin", command, SLOW[command])
    failed = fetched.fetch(ok=False, PATH=path)
    assert "fetch: content failed" in failed.stderr
    assert (fetched.alerts / "fetch").exists()
    unit = (DEPLOY / "web/ccs-fetch.service").read_text()
    units_limit = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M)[1])
    assert sum(map(int, limits.read_text().split())) < units_limit


def alert_state(fetcher, name, since, sent, text):
    """Write a check's alert state, as an earlier run would have left it."""
    fetcher.alerts.mkdir(parents=True, exist_ok=True)
    (fetcher.alerts / name).write_text(f"{since} {sent} {text}\n")


FAILED = "ccs: content fetches have failed for 50 minutes"
STALE = "ccs: no values from the backend applied for over 10 minutes"


def test_no_fetch_alert_without_a_fetch_attempt(fetched):
    """A run with no attempt keeps the alert's state, so its hold keeps counting."""
    since = int(time.time()) - 3100
    alert_state(fetched, "fetch", since, 0, FAILED)
    fetched.fetch()
    assert fetched.server.alerts() == []
    state = fetched.alerts / "fetch"
    assert (state.read_text() if state.exists() else None) == f"{since} 0 {FAILED}\n"


def test_a_first_failed_fetch_does_not_alert_at_once(fetched):
    (fetched.server.state / "fail").touch()
    fetched.values(18)
    fetched.fetch(ok=False)
    assert fetched.server.alerts() == []


def test_fetches_failing_for_50_minutes_alert_once_an_hour_then_resolve(fetched):
    (fetched.server.state / "fail").touch()
    alert_state(fetched, "fetch", int(time.time()) - 3100, 0, FAILED)
    fetched.values(18)
    fetched.fetch(ok=False)
    assert fetched.server.alerts() == [f"{FAILED} (fetch)"]
    fetched.values(19)
    fetched.fetch(ok=False)
    assert len(fetched.server.alerts()) == 1
    an_hour_ago = int(time.time()) - 3100
    alert_state(fetched, "fetch", an_hour_ago - 100, an_hour_ago, FAILED)
    fetched.values(21)
    fetched.fetch(ok=False)
    assert len(fetched.server.alerts()) == 2
    (fetched.server.state / "fail").unlink()
    fetched.values(20)
    fetched.fetch()
    assert fetched.server.alerts()[-1] == f"resolved: {FAILED} (fetch)"
    assert not (fetched.alerts / "fetch").exists()


def test_stale_values_alert_at_once_once_an_hour_then_resolve(fetched):
    applied(fetched, int(time.time()) - 700)
    fetched.fetch()
    assert fetched.server.alerts() == [f"{STALE} (stale-values)"]
    fetched.fetch()
    assert len(fetched.server.alerts()) == 1
    an_hour_ago = int(time.time()) - 3100
    alert_state(fetched, "stale-values", an_hour_ago - 100, an_hour_ago, STALE)
    fetched.fetch()
    assert len(fetched.server.alerts()) == 2
    applied(fetched, int(time.time()))
    fetched.fetch()
    assert fetched.server.alerts()[-1] == f"resolved: {STALE} (stale-values)"


def test_a_full_disk_still_sends_the_alert_that_is_due(fetched):
    applied(fetched, int(time.time()) - 700)
    (fetched.ccs / "fetch/last-fetch").unlink()
    (fetched.ccs / "fetch/last-fetch").symlink_to("/dev/full")
    fetched.values(40)
    fetched.fetch(ok=False)
    assert fetched.server.alerts() == [f"{STALE} (stale-values)"]


def test_an_old_push_leftover_is_cleared_and_one_in_flight_is_left(fetched):
    old, in_flight = fetched.ccs / "in/values.Ab12Cd", fetched.ccs / "in/values.Ef34Gh"
    old.touch()
    twenty_minutes_ago = time.time() - 1200
    os.utime(old, (twenty_minutes_ago, twenty_minutes_ago))
    in_flight.touch()
    fetched.fetch()
    assert not old.exists()
    assert in_flight.exists()


@pytest.fixture
def stalling():
    """Return a port that takes connections and never answers."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        yield listener.getsockname()[1]


def test_a_gitlab_that_stalls_makes_the_fetch_give_up_in_time(fetched, stalling):
    fetched.values(31)
    env = {**fetched.env, "PROPOSALS_GIT": f"http://127.0.0.1:{stalling}/p.git"}
    failed = run(
        "timeout", "150", DEPLOY / "web/fetch.sh", ok=False, env=env, timeout=200
    )
    assert failed.returncode == 1, "fetch.sh did not give up within 150 s"
    assert (fetched.alerts / "fetch").exists()
