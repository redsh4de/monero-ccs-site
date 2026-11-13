"""Under systemd: both roles installed, the pipeline, nginx and the monitor."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from fake_server import FakeServer
from fetching import Fetcher
from installing import CCS_BACK, LIB, PUSH_KEY, make_certificate, push_key
from monitoring import publish_merge_requests
from scripts import DEPLOY, TAIL, run
from signing import RELEASE_SIGNERS, publish, write_list
from web import A, B, C

BOOTED = Path("/proc/1/comm").read_text() == "systemd\n"
REASON = (
    "systemd is not PID 1 here: run the file with tests/deploy/run.sh, under "
    "rootless podman. If systemd still does not boot, raise the host's inotify "
    "limit: sudo sysctl -w fs.inotify.max_user_instances=1024"
)
if not BOOTED and os.environ.get("CI"):
    pytest.fail(REASON, pytrace=False)
pytestmark = pytest.mark.skipif(not BOOTED, reason=REASON)

ORIGIN = Path("/srv/origin.git")
PATHS_AND_TIMERS = """ccs-values.path ccs-monitor.timer ccs-fetch.path
    ccs-fetch.timer""".split()
PIPELINE = """ccs-values.service ccs-push.service ccs-fetch.service
    ccs-update.service""".split()
FUNDING = "proposals/example-funding.html"


def systemctl(*args: str) -> str:
    return run("systemctl", *args).stdout


def journal(*units: str) -> str:
    names = [arg for unit in units for arg in ("-u", unit)]
    return run("journalctl", "--no-pager", "-q", "-n", str(TAIL), *names).stdout


def get(path: str) -> tuple[int, str]:
    """Return the status and body of a page, as nginx serves it."""
    resolve = "ccs.getmonero.org:443:127.0.0.1"
    url = f"https://ccs.getmonero.org/{path}"
    out = run("curl", "-sk", "--resolve", resolve, "-w", "%{http_code}", url).stdout
    return int(out[-3:]), out[:-3]


def outcomes(since: int) -> dict[str, str]:
    """Return the result of each pipeline unit's run after since, or "no run since".

    since is in microseconds of the monotonic clock, as systemd records starts.
    A check of the result alone passes for ccs-update: the install already ran it.
    """
    found = {}
    for unit in PIPELINE:
        shown = systemctl("show", "-p", "Result,ExecMainStartTimestampMonotonic", unit)
        properties = dict(line.split("=", 1) for line in shown.splitlines())
        started = int(properties["ExecMainStartTimestampMonotonic"])
        found[unit] = properties["Result"] if started > since else "no run since"
    return found


@pytest.fixture(scope="module")
def gitlab(tmp_path_factory):
    server = FakeServer(tmp_path_factory.mktemp("gitlab"))
    yield server
    server.stop()


@pytest.fixture(scope="module")
def machine(tmp_path_factory, keys, gitlab) -> None:
    """Set up both servers' outside world, nginx and ssh."""
    root = tmp_path_factory.mktemp("machine")
    fetcher = Fetcher(root, gitlab)
    publish_merge_requests(gitlab)
    shutil.copytree(fetcher.origin, ORIGIN)
    shutil.copytree(fetcher.work, CCS_BACK / "proposals")
    slugs = ["example-funding", "example-funded", "example-no-address"]
    rows = [
        {
            "local-url": f"/proposals/{slug}.html",
            "amount-funded": 1.5,
            "contributions": 2,
            "donate-address": address,
        }
        for slug, address in zip(slugs, [A, B, C])
    ]
    stages = [{"stage": "Funding Required", "proposals": rows}]
    (CCS_BACK / "proposals.json").write_text(json.dumps(stages))
    (CCS_BACK / "complete.json").write_text("[]")

    publish(gitlab.state, 1, keys)
    etc = Path("/etc/ccs")
    etc.mkdir()
    keys.keyring(etc / "release-signers.gpg", *RELEASE_SIGNERS)
    keys.keyring(etc / "address-signers.gpg", "addresser")
    write_list(etc / "addresses.txt", [A, B, C], keys)
    config = (DEPLOY / "config.example").read_text()
    config += f"RELEASES={gitlab.url}\nGITLAB_API={gitlab.api}\n"
    config += f"PROPOSALS_GIT=file://{ORIGIN}\n"
    (etc / "config").write_text(config)
    make_certificate()
    systemctl("start", "ssh", "nginx")


@pytest.fixture(scope="module")
def installed(machine) -> None:
    """Install the backend, then the web server, as an operator does."""
    run(DEPLOY / "install.sh", "backend", "site-v1", "127.0.0.1", CCS_BACK, timeout=300)
    Path("/etc/ssh/sshd_config.d/50-admin.conf").write_text(
        "PasswordAuthentication no\n"
    )
    web = ("web", "site-v1", "127.0.0.1", push_key())
    run(LIB / "current/deploy/install.sh", *web, timeout=300)
    # git refuses a local repository owned by another user; an https:// one,
    # as live, never is
    run("chown", "-R", "ccs-fetch:", ORIGIN)
    # a rootless container sets up no systemd credentials, so the push key is
    # bound where LoadCredential= puts it
    key = Path("/run/ccs-test-push-key")
    shutil.copy(PUSH_KEY, key)
    key.chmod(0o644)
    credential = Path("/etc/systemd/system/ccs-push.service.d/test-credential.conf")
    credential.parent.mkdir(exist_ok=True)
    target = "/run/credentials/ccs-push.service/push_key"
    credential.write_text(f"[Service]\nBindReadOnlyPaths={key}:{target}\n")
    systemctl("daemon-reload")


def test_both_roles_install_and_start_their_paths_and_timers(installed):
    states = {
        unit: systemctl("show", "-P", "ActiveState", unit) for unit in PATHS_AND_TIMERS
    }
    assert states == dict.fromkeys(PATHS_AND_TIMERS, "active\n"), journal(
        *PATHS_AND_TIMERS
    )


@pytest.fixture(scope="module")
def pushed(installed) -> int:
    """Change ccs-back's JSON, and wait at most 60 s for the page to show A.

    Return when the JSON changed, as outcomes() takes it.
    """
    since = time.monotonic_ns() // 1000
    (CCS_BACK / "proposals.json").touch()
    deadline = time.monotonic() + 60
    while A not in get(FUNDING)[1] and time.monotonic() < deadline:
        time.sleep(1)
    return since


def test_ccs_backs_json_reaches_the_served_page(pushed):
    assert A in get(FUNDING)[1], f"{outcomes(pushed)}\n{journal(*PIPELINE)}"


def test_each_unit_of_the_pipeline_ran_and_succeeded(pushed):
    success = dict.fromkeys(PIPELINE, "success")
    assert outcomes(pushed) == success, journal(*PIPELINE)


def test_status_json_names_the_installed_release(pushed):
    code, body = get("status.json")
    assert code == 200, body
    assert json.loads(body)["release"] == 1


def test_the_push_lands_as_in_values_and_nowhere_else(pushed):
    assert set(os.listdir("/var/lib/ccs/in")) == {"mr", "repo", "values"}
