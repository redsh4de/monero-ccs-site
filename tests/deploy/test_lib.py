"""lib.sh: the functions the deploy scripts share."""

import os

import pytest

from fetching import merge, new_repository
from scripts import lib_sh, run, stand_in


@pytest.fixture
def merged(tmp_path):
    """Return a repository whose merge commits add files, as GitLab writes them."""
    repo = tmp_path / "repo"
    new_repository(repo)
    for branch, name, request in [
        ("one", "a.md", "monero-project/ccs-proposals!7"),
        ("two", "b c.md", "monero-project/ccs-proposals!8"),
        ("three", "d.md", "other/project!9"),
        ("four", "e\tf.md", "monero-project/ccs-proposals!10"),
    ]:
        merge(repo, branch, name, "x\n", request)
    note = "See merge request monero-project/ccs-proposals!11"
    run("git", "-C", repo, "commit", "-q", "--allow-empty", "-m", note)
    return repo


def test_export_tree_maps_each_file_to_the_merge_request_that_added_it(
    merged, tmp_path
):
    out = tmp_path / "out"
    lib_sh('export_tree "$1" master "$2"', merged / ".git", out)
    assert (out / "proposals/a.md").is_file()
    assert (out / "proposals/b c.md").is_file()
    assert (out / "mr/merged.tsv").read_text() == "7\ta.md\n8\tb c.md\n"
    assert not (out / "mr/log").exists()


def test_export_tree_fails_on_an_unknown_commit(merged, tmp_path):
    unknown, out = "0" * 39 + "1", tmp_path / "out"
    lib_sh('export_tree "$1" "$2" "$3"', merged / ".git", unknown, out, ok=False)


class Alerts:
    """lib.sh's alert rule in a folder of its own, posting to the fake server."""

    def __init__(self, root, server):
        self.dir, self.server = root / "alerts", server
        self.env = {
            "ALERTS_DIR": str(self.dir),
            "CREDENTIALS_DIRECTORY": str(server.credentials),
        }

    def run(self, now, *alerts):
        calls = "".join(f"alert {alert}; " for alert in alerts)
        return lib_sh(f"NOW={now}; alerts_begin; {calls}alerts_send", env=self.env)

    def state(self, name):
        path = self.dir / name
        return path.read_text().strip() if path.exists() else None


@pytest.fixture
def alerts(tmp_path, server):
    return Alerts(tmp_path, server)


def test_an_alert_waits_for_its_hold(alerts):
    alerts.run(1000, "a 1 600 9999 'A broke'")
    assert alerts.server.alerts() == []
    assert alerts.state("a") == "1000 0 A broke"
    alerts.run(1600, "a 1 600 9999 'A broke'")
    assert alerts.server.alerts() == ["A broke (a)"]


def test_an_alert_fires_once_within_its_repeat(alerts):
    alerts.run(1000, "a 1 0 9999 'A broke'")
    alerts.run(1600, "a 1 0 9999 'A broke'")
    assert alerts.server.alerts() == ["A broke (a)"]


def test_a_pass_after_an_alert_sends_resolved_and_clears_the_state(alerts):
    alerts.run(1000, "a 1 0 9999 'A broke'")
    alerts.run(1100, "a 0 0 9999 'A broke'")
    assert alerts.server.alerts() == ["A broke (a)", "resolved: A broke (a)"]
    assert alerts.state("a") is None


def test_a_failed_send_changes_no_state_and_is_retried_as_one_message(alerts):
    refusing = alerts.server.state / "alert.fail"
    refusing.touch()
    alerts.run(3000, "b 1 0 9999 B", "c 1 0 9999 C")
    assert alerts.state("b") == "3000 0 B"
    refusing.unlink()
    alerts.run(3100, "b 1 0 9999 B", "c 1 0 9999 C")
    assert alerts.server.alerts() == ["B (b)|C (c)"]
    assert alerts.state("b") == "3000 3100 B"


def test_a_recovery_before_the_hold_is_silent(alerts):
    alerts.run(3200, "d 1 600 9999 D")
    alerts.run(3300, "d 0 600 9999 D")
    assert alerts.server.alerts() == []
    assert alerts.state("d") is None


def test_an_alert_repeats_after_its_repeat_time_with_the_newest_text(alerts):
    alerts.run(4000, "e 1 0 3600 'release 5 fails'")
    alerts.run(5000, "e 1 0 3600 'release 5 fails'")
    alerts.run(7600, "e 1 0 3600 'release 6 fails'")
    assert alerts.server.alerts() == ["release 5 fails (e)", "release 6 fails (e)"]


def test_nothing_to_send_posts_nothing(alerts):
    alerts.run(8000)
    assert alerts.server.alerts() == []


def test_a_killed_runs_queued_alert_is_not_posted_later(alerts):
    lib_sh("NOW=100; alerts_begin; alert k 1 0 9999 K", env=alerts.env)
    alerts.run(200, "k 0 0 9999 K")
    assert alerts.server.alerts() == []
    assert alerts.state("k") is None


def test_a_pending_mark_for_a_removed_check_is_skipped(alerts):
    alerts.dir.mkdir()
    (alerts.dir / ".pending").write_text("sent gone\n")
    (alerts.dir / ".outbox").write_text("G\n")
    lib_sh("NOW=300; alerts_send", env=alerts.env)
    assert alerts.state("gone") is None


def test_alert_and_alerts_send_leave_the_callers_variables_alone(alerts):
    names = "file since sent text action name due"
    mine = " ".join(f"{name}=mine" for name in names.split())
    shown = " ".join(f"${name}" for name in names.split())
    snippet = f"{mine}; NOW=400; alerts_begin; alert v 1 0 9999 V; alerts_send"
    result = lib_sh(f'{snippet}; echo "{shown}"', env=alerts.env)
    assert result.stdout.splitlines()[-1] == " ".join(["mine"] * 7)


def test_the_alert_url_reaches_curl_on_stdin_never_in_its_arguments(alerts, tmp_path):
    script = f'#!/bin/sh\nprintf "%s\\n" "$@" >{tmp_path}/args\ncat >{tmp_path}/stdin\n'
    env = {**alerts.env, "PATH": stand_in(tmp_path / "bin", "curl", script)}
    queued = "NOW=1; alerts_begin; alert x 1 0 9999 @/etc/hostname; alerts_send"
    lib_sh(queued, env=env)
    arguments = (tmp_path / "args").read_text().splitlines()
    assert not any(alerts.server.url in argument for argument in arguments)
    assert f"url = {alerts.server.url}/alert" in (tmp_path / "stdin").read_text()
    assert f"@{alerts.dir}/.outbox" in arguments


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://127.0.0.1:9/x", id="a URL that refuses"),
        pytest.param("", id="an empty URL"),
        pytest.param(None, id="no credentials at all"),
    ],
)
def test_a_failed_post_is_logged(url, alerts):
    env = dict(alerts.env)
    if url is None:
        del env["CREDENTIALS_DIRECTORY"]
    else:
        (alerts.server.credentials / "alert_url").write_text(f"{url}\n")
    queued = "NOW=1; alerts_begin; alert x 1 0 9999 X; alerts_send"
    assert "alert not sent" in lib_sh(queued, env=env).stderr


def test_alerts_go_through_the_proxy_all_proxy_names(alerts):
    # an alert channel only the proxy reaches; the fake server logs any post
    (alerts.server.credentials / "alert_url").write_text("http://alert.invalid/\n")
    alerts.env["ALL_PROXY"] = alerts.server.url
    alerts.run(1000, "a 1 0 9999 'A broke'")
    assert alerts.server.alerts() == ["A broke (a)"]


def test_a_due_alert_stays_unsent_while_alerts_are_paused(alerts):
    (alerts.server.credentials / "alert_url").write_text("")
    alerts.run(100, "p 1 0 9999 P")
    assert alerts.state("p") == "100 0 P"


def test_an_unsigned_list_counts_as_empty(etc_ccs, tmp_path):
    (etc_ccs / "addresses.txt").write_text(f"{'8' + 'A' * 94} {'A' * 232}\n")
    verified = tmp_path / "addresses"
    lib_sh('verify_list "$1"', verified)
    assert verified.read_text() == ""


def test_render_values_writes_only_the_renders_inputs(tmp_path):
    values, inputs, verified = tmp_path / "values", tmp_path / "in", tmp_path / "list"
    values.write_text("updated 1\n")
    inputs.mkdir()
    verified.write_text("")
    lib_sh('render_values "$1" "$2" "$3" >/dev/null', values, inputs, verified)
    assert sorted(os.listdir(inputs)) == ["addresses", "site", "values"]


def test_release_tag_reads_the_version():
    assert lib_sh('release_tag "$1"', "site-v12").stdout == "12\n"


@pytest.mark.parametrize("tag", ["v12", "site-v", "site-v0", "site-v1x", "site-v-1"])
def test_release_tag_refuses_a_malformed_tag(tag):
    assert lib_sh('release_tag "$1"', tag, ok=False).stdout == ""
