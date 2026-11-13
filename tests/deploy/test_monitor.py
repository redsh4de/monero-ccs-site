"""monitor.sh: the backend's checks of the live site."""

import shutil
import time

import pytest

from fetching import git
from monitoring import ALWAYS, ETC, VALUES, Monitored, SiteServer, page
from scripts import replaced
from web import DIST, B, C


@pytest.fixture(scope="module")
def baseline(tmp_path_factory, keys):
    """Return a site the monitor has checked once, which every test copies."""
    site = SiteServer()
    yield Monitored.build(tmp_path_factory.mktemp("baseline"), keys, site)
    site.stop()


@pytest.fixture
def monitored(baseline, tmp_path, server) -> Monitored:
    return baseline.copy(tmp_path / "monitored", server)


def test_a_matching_site_raises_no_alert(monitored):
    monitored.monitor()
    assert monitored.failures() == []
    assert monitored.server.alerts() == []


def differs(path: str) -> str:
    """Return the alert for a page that differs from the expected render."""
    return f"ccs monitor: page {path} differs from the expected render ({page(path)})"


def assert_alerts_after_ten_minutes(monitored, path: str) -> None:
    monitored.monitor()
    monitored.age(page(path), 700)
    monitored.monitor()
    assert monitored.server.alerts() == [differs(path)]


def test_a_changed_page_alerts_after_ten_minutes_and_resolves_when_fixed(monitored):
    path = "proposals/example-funding.html"
    monitored.flip(path)
    monitored.monitor()
    assert monitored.server.alerts() == []
    monitored.age(page(path), 700)
    monitored.monitor()
    assert monitored.server.alerts() == [differs(path)]
    monitored.flip(path)
    monitored.monitor()
    assert monitored.server.alerts() == [differs(path), f"resolved: {differs(path)}"]


def test_a_failing_page_reminds_once_a_day(monitored):
    monitored.flip("index.html")
    assert_alerts_after_ten_minutes(monitored, "index.html")
    monitored.monitor()
    assert monitored.server.alerts() == [differs("index.html")]
    monitored.age(page("index.html"), 90000, field=1)
    monitored.monitor()
    assert monitored.server.alerts() == [differs("index.html")] * 2


def test_a_changed_qr_code_alerts(monitored):
    qr = next(path for path in monitored.expected() if path.startswith("qr/"))
    monitored.flip(qr)
    assert_alerts_after_ten_minutes(monitored, qr)


def test_the_fixed_always_checked_pages_alert_in_one_message(monitored):
    for path in ALWAYS:
        monitored.flip(path)
    monitored.monitor()
    for path in ALWAYS:
        monitored.age(page(path), 700)
    monitored.monitor()
    posts = [sorted(post.split("|")) for post in monitored.server.alerts()]
    assert posts == [sorted(differs(path) for path in ALWAYS)]


def test_every_page_with_a_template_of_its_own_is_always_checked():
    templates = {path.stem for path in (DIST / "templates").glob("*.html")}
    pages = {f"{name}/index.html" for name in templates - {"home", "proposal"}}
    assert pages and pages <= set(ALWAYS)


def test_values_unlike_the_backends_show_in_the_projects_page(monitored):
    VALUES.write_text(replaced(VALUES.read_text(), "raised=10 ", "raised=11 "))
    monitored.monitor()
    assert page("index.php/projects") in monitored.failures()


def test_a_vote_count_the_ideas_page_does_not_show_is_found(monitored):
    monitored.edit_merge_requests(lambda mrs: mrs[-1].update(upvotes=99))
    monitored.monitor()
    assert page("ideas/index.html") in monitored.failures()


def test_a_failing_sampled_page_is_compared_again_after_the_sample_moves_on(monitored):
    path, files = "how-to-ccs/index.html", monitored.expected()
    (monitored.state / "sample-offset").write_text(f"{files.index(path)}\n")
    monitored.flip(path)
    assert_alerts_after_ten_minutes(monitored, path)


def test_an_always_checked_page_missing_from_the_render_alerts(monitored):
    path = "donate/index.html"
    (monitored.state / "expected" / path).unlink()
    assert_alerts_after_ten_minutes(monitored, path)
    (monitored.state / "rendered").unlink()
    monitored.monitor()
    assert monitored.failures() == []


def test_a_failing_page_that_left_the_site_resolves(monitored):
    name, text = "page-ffffffffffffffff", "ccs monitor: page gone/away.html"
    state = f"{int(time.time())} 1 {text} differs from the expected render\n"
    (monitored.state / "alerts" / name).write_text(state)
    monitored.monitor()
    assert monitored.server.alerts() == [f"resolved: {text} left the site ({name})"]


def test_every_file_is_compared_within_the_sample_rotation(monitored):
    files = monitored.expected()
    assert len(files) > 10
    for _ in range(0, len(files), 10):
        monitored.monitor()
    assert sorted(set(files) - set(monitored.site_server.requested)) == []


def test_unchanged_inputs_cause_no_render(monitored):
    (monitored.state / "expected/index.html").unlink()
    monitored.monitor()
    assert not (monitored.state / "expected/index.html").exists()


MRS_DIFFER = "ccs monitor: the site's merge requests differ from GitLab's"


def test_merge_requests_unlike_gitlabs_alert_only_after_75_minutes(monitored):
    monitored.edit_merge_requests(lambda mrs: mrs[0].update(title="Not on GitLab"))
    monitored.failing_for("merge-requests", 3000)
    monitored.monitor()
    assert monitored.server.alerts() == []
    monitored.age("merge-requests", 5000)
    monitored.monitor()
    assert monitored.server.alerts() == [f"{MRS_DIFFER} (merge-requests)"]


def test_a_missing_open_merge_request_fails_the_check(monitored):
    monitored.edit_merge_requests(lambda mrs: mrs.pop(0))
    monitored.monitor()
    assert monitored.reason("merge-requests") == MRS_DIFFER


def test_gitlab_not_answering_leaves_the_merge_requests_unverified(monitored):
    monitored.forget_verification()
    (monitored.server.state / "fail").touch()
    monitored.failing_for("merge-requests", 5000)
    monitored.monitor()
    assert monitored.server.alerts() == [
        "ccs monitor: GitLab's merge request API doesn't answer, so the site's"
        " merge requests are unverified (merge-requests)"
    ]


def test_a_verified_set_is_asked_about_again_only_an_hour_on(monitored):
    monitored.server.set_merge_requests([])
    monitored.monitor()
    assert monitored.server.calls("merge_requests") == 0
    monitored.forget_verification()
    monitored.monitor()
    assert monitored.reason("merge-requests") == MRS_DIFFER


def test_votes_cast_since_the_sites_fetch_are_no_mismatch(monitored):
    path = monitored.server.state / "mrs.json"
    path.write_text(replaced(path.read_text(), '"upvotes": 7,', '"upvotes": 12,'))
    monitored.forget_verification()
    monitored.monitor()
    assert monitored.failures() == []
    assert monitored.server.calls("merge_requests") > 0


@pytest.mark.parametrize(
    "edit, reason",
    [
        (lambda mrs: mrs[0].clear(), "snapshot: a merge request has other fields"),
        (lambda mrs: mrs[0].update(upvotes="7"), "snapshot: a vote count is malformed"),
    ],
)
def test_malformed_merge_requests_in_status_json_are_refused_not_a_crash(
    monitored, edit, reason
):
    monitored.edit_merge_requests(edit)
    assert reason in monitored.monitor().stderr
    assert monitored.failures() == ["snapshot"]


def test_an_address_list_that_does_not_verify_alerts(monitored):
    (ETC / "addresses.txt").write_text("an unsigned line\n")
    monitored.failing_for("addresses", 700)
    monitored.monitor()
    assert monitored.server.alerts() == [
        "ccs monitor: the address list doesn't verify, so no donation address"
        " shows (addresses)"
    ]


def test_a_proposal_whose_address_is_not_listed_alerts_naming_it(monitored):
    VALUES.write_text(replaced(VALUES.read_text(), f"address={B}\n", f"address={C}\n"))
    monitored.failing_for("addresses", 700)
    monitored.monitor()
    assert monitored.server.alerts() == [
        "ccs monitor: proposals take donations with no address on the list:"
        " proposals/example-no-address.html (addresses)"
    ]


def test_a_render_that_shows_an_unlisted_address_alerts(monitored):
    index = monitored.state / "expected/index.html"
    index.write_text(replaced(index.read_text(), "</body>", f"<p>{C}</p></body>"))
    monitored.failing_for("unlisted", 700)
    monitored.monitor()
    assert monitored.server.alerts() == [
        "ccs monitor: the render shows an address the signed list lacks (unlisted)"
    ]


def test_a_signing_key_that_expires_soon_alerts_naming_it_and_its_day(monitored, keys):
    keys.gpg("--quick-gen-key", "soon-expiring", "ed25519", "sign", "10d")
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 10 * 86400))
    keys.keyring(ETC / "address-signers.gpg", "addresser", "soon-expiring")
    monitored.failing_for("key-expiry", 700)
    monitored.monitor()
    assert monitored.server.alerts() == [
        f"ccs monitor: keys expire: soon-expiring: {day} (key-expiry)"
    ]


def test_a_site_that_does_not_answer_is_recorded_with_curls_reason(monitored):
    assert "curl: (7)" in monitored.monitor(SITE="http://127.0.0.1:1").stderr
    assert monitored.failures() == ["status"]


def test_a_site_over_an_hour_behind_ccs_back_alerts(monitored):
    trust = ("-c", "safe.directory=*")
    git(monitored.checkout, *trust, "commit", "-q", "--allow-empty", "-m", "newer")
    monitored.monitor()
    assert monitored.server.alerts() == []
    monitored.age("stall", 3700)
    monitored.monitor()
    assert monitored.server.alerts() == [
        "ccs monitor: the site's content is over an hour behind ccs-back (stall)"
    ]


def install_newer(monitored, *changed: str) -> None:
    """Install release 2: release 1 with a newline added to each file changed names."""
    shutil.copytree(monitored.release(1), monitored.release(2))
    for path in changed:
        with open(next(monitored.release(2).glob(path)), "a") as file:
            file.write("\n")


def test_an_older_site_release_differing_only_in_backend_files_and_docs_is_checked(
    monitored,
):
    install_newer(monitored, "deploy/backend/values.py", "deploy/README.md")
    monitored.monitor(release=2)
    assert monitored.failures() == []
    assert set(ALWAYS) <= set(monitored.site_server.requested)


@pytest.mark.parametrize(
    "changed",
    [
        "deploy/web/update.sh",
        "deploy/checks.py",
        "deploy/render/content.py",
        "dist/_astro/*.css",
    ],
)
def test_an_older_site_release_with_other_site_files_alerts_and_compares_no_page(
    monitored, changed
):
    install_newer(monitored, changed)
    monitored.failing_for("old-release", 700)
    monitored.monitor(release=2)
    assert monitored.site_server.requested == ["status.json"]
    assert monitored.server.alerts() == [
        "ccs monitor: the site runs release 1, older than release 2 here (old-release)"
    ]


def test_a_crash_alerts_and_a_good_run_clears_it(monitored):
    shutil.rmtree(monitored.state / "releases/1/dist/templates")
    (monitored.state / "rendered").unlink()
    monitored.failing_for("monitor", 700)
    monitored.monitor(ok=False)
    assert monitored.server.alerts() == ["ccs monitor: the monitor crashed (monitor)"]
    shutil.rmtree(monitored.state / "releases/1")
    monitored.monitor()
    assert monitored.failures() == []
