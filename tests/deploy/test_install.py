"""install.sh: both roles, the push, upgrades, and the releases it refuses."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from installing import CCS_BACK, LIB, Machine, push_key, values
from scripts import DEPLOY
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
