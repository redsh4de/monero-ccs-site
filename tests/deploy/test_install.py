"""install.sh: both roles, the push, upgrades, and the releases it refuses."""

import filecmp
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from installing import CCS_BACK, LIB, PUSH_KEY, Machine, push_key, values
from scripts import DEPLOY, run
from web import lay_out

USAGE = "usage: install.sh"
BACKEND = ("backend", "site-v1", "127.0.0.1", CCS_BACK)
PUSHED = Path("/var/lib/ccs/in/values")
SSHD_CONFIG = Path("/etc/ssh/sshd_config.d")


def ownership(path: Path) -> str:
    """Return a file's owner and mode, such as "root 600"."""
    return f"{path.owner()} {path.stat().st_mode & 0o777:o}"


@pytest.fixture(scope="module")
def machine(tmp_path_factory, keys):
    """Return the machine with nothing installed, saved as "fresh"."""
    machine = Machine(tmp_path_factory.mktemp("machine"), keys)
    machine.save("fresh")
    yield machine
    machine.stop()


@pytest.fixture(scope="module")
def backend_install(machine) -> CompletedProcess[str]:
    """Return the backend's first install, of release 1, saved as "backend"."""
    machine.restore("fresh")
    result = machine.install(*BACKEND, first=True, RELEASES=f"file://{machine.host}")
    machine.save("backend")
    return result


@pytest.fixture(scope="module")
def web_install(machine, backend_install) -> None:
    """Install the web server from release 1, add content and release 2, save "web"."""
    machine.restore("backend")
    (SSHD_CONFIG / "50-admin.conf").write_text("PasswordAuthentication no\n")
    machine.install("web", "site-v1", "127.0.0.1", push_key(), first=True)
    lay_out(Path("/var/lib/ccs"))
    PUSHED.write_text(values())
    machine.publish(2)
    with open("/etc/ccs/config", "a") as config:
        config.write(f"RELEASES=file://{machine.host}\n")
    machine.save("web")


@pytest.fixture(scope="module")
def upgrade(machine, web_install) -> None:
    """Upgrade the web server to release 2, saved as "upgraded"."""
    machine.restore("web")
    machine.install("web", "site-v2")
    machine.save("upgraded")


@pytest.fixture
def fresh(machine) -> Machine:
    return machine.restore("fresh")


@pytest.fixture
def installed_backend(machine, backend_install) -> Machine:
    return machine.restore("backend")


@pytest.fixture
def installed_web(machine, web_install) -> Machine:
    return machine.restore("web")


@pytest.fixture
def upgraded(machine, upgrade) -> Machine:
    return machine.restore("upgraded")


def test_without_the_release_signers_keyring_nothing_is_installed(fresh):
    Path("/etc/ccs/release-signers.gpg").unlink()
    reason = "put the release signers' keyring in /etc/ccs/release-signers.gpg first"
    fresh.refuses(*BACKEND, reason=reason, first=True)
    assert not LIB.exists()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(
            ("backend", "v1", "127.0.0.1", str(CCS_BACK)),
            id="a tag that is not site-vN",
        ),
        pytest.param(
            ("both", "site-v1", "127.0.0.1", str(CCS_BACK)), id="an unknown role"
        ),
        pytest.param(
            ("backend", "site-v1", "127.0.0.1 x", str(CCS_BACK)),
            id="a web host with a second word",
        ),
        pytest.param(
            ("backend", "site-v1", "127.0.0.1", "storage|x"),
            id="a ccs-back folder that is not a plain path",
        ),
    ],
)
def test_a_malformed_command_is_refused_with_the_usage(fresh, args):
    fresh.refuses(*args, reason=USAGE, first=True)
    assert not LIB.exists()


def test_the_backend_stops_for_its_settings_before_it_downloads(fresh):
    fresh.refuses("backend", "site-v1", reason=USAGE, first=True)
    assert not LIB.exists()
    config = Path("/etc/ccs/config")
    assert config.read_text() == (DEPLOY / "config.example").read_text()
    assert ownership(config) == "root 600"


def test_the_public_name_as_web_host_is_refused(fresh):
    args = ("backend", "site-v1", "ccs.getmonero.org", CCS_BACK)
    reason = "set WEB_HOST to the web server's own address, not the public name"
    fresh.refuses(*args, reason=reason, first=True)


def test_ccs_backs_json_that_others_cannot_read_is_refused(fresh):
    unreadable = CCS_BACK / "proposals.json"
    unreadable.touch(mode=0o600)
    reason = f"make ccs-back's files readable by all users: {unreadable}"
    fresh.refuses(*BACKEND, reason=reason, first=True)


UNITS = Path("/etc/systemd/system")


def differing(installed: dict[Path, Path]) -> list[str]:
    """Return each installed file whose bytes differ from its source's."""
    return [
        str(path)
        for path, source in installed.items()
        if not filecmp.cmp(path, source, shallow=False)
    ]


def test_the_backend_writes_both_settings_to_its_config_and_no_alert_url(
    installed_backend,
):
    config = Path("/etc/ccs/config").read_text()
    assert "WEB_HOST=127.0.0.1" in config.splitlines()
    assert f"CCS_BACK={CCS_BACK}" in config.splitlines()
    assert "ALERT_URL" not in config


def test_the_alert_url_file_starts_empty_and_root_only_and_the_install_says_so(
    installed_backend, backend_install
):
    alert_url = Path("/etc/ccs/alert_url")
    assert (ownership(alert_url), alert_url.read_text()) == ("root 600", "")
    said = "/etc/ccs/alert_url is empty, so no alert reaches anyone yet."
    assert said in backend_install.stdout.splitlines()


def test_a_release_installed_under_a_strict_umask_is_readable_by_the_services(
    installed_backend,
):
    assert installed_backend.installed() == str(LIB / "releases/1")
    expected = {
        LIB / "releases": "root 755",
        LIB / "releases/1": "root 755",
        LIB / "releases/1/deploy/web/update.sh": "root 755",
        Path("/etc/ccs/known_hosts"): "root 644",
    }
    assert {path: ownership(path) for path in expected} == expected


def test_the_push_key_stays_root_only_and_no_push_user_is_made(installed_backend):
    assert ownership(PUSH_KEY) == "root 600"
    run("id", "ccs-push", ok=False)


def test_the_keyrings_are_made_readable_by_the_services(installed_backend):
    names = ("release-signers.gpg", "address-signers.gpg")
    expected = {Path("/etc/ccs", name): "root 644" for name in names}
    assert {path: ownership(path) for path in expected} == expected


def test_the_web_servers_host_key_is_recorded_and_its_fingerprint_shown(
    installed_backend, backend_install
):
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    recorded = Path("/etc/ccs/known_hosts").read_text().split()
    assert recorded[1:3] == host_key.read_text().split()[:2]
    fingerprint = run("ssh-keygen", "-lf", host_key).stdout.split()[1]
    assert fingerprint in backend_install.stdout


def test_the_values_path_watches_ccs_backs_json(installed_backend):
    expected = f"[Path]\nPathChanged=\nPathChanged={CCS_BACK}/proposals.json\n"
    assert (UNITS / "ccs-values.path.d/path.conf").read_text() == expected


def test_the_backend_units_are_installed_each_service_with_the_sandbox(
    installed_backend,
):
    deploy = LIB / "releases/1/deploy"
    units = ["ccs-values.service", "ccs-values.path", "ccs-push.service"]
    units += ["ccs-monitor.service", "ccs-monitor.timer"]
    installed = {UNITS / unit: deploy / "backend" / unit for unit in units}
    for service in ("ccs-values", "ccs-push", "ccs-monitor"):
        installed[UNITS / f"{service}.service.d/sandbox.conf"] = deploy / "sandbox.conf"
    assert differing(installed) == []
    assert not (UNITS / "ccs-.service.d").exists()


def test_the_backend_prints_the_files_still_to_place_and_the_web_servers_command(
    installed_backend, backend_install
):
    lines = backend_install.stdout.splitlines()
    assert "Still to place by hand: /etc/ccs/addresses.txt" in lines
    assert f"  install.sh web site-v1 <backend IP address> '{push_key()}'" in lines
