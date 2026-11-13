"""monitor.sh: the backend's checks of the live site."""

import time

import pytest

from monitoring import ALWAYS, VALUES, Monitored, SiteServer, page
from scripts import replaced


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
