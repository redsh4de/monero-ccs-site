"""The systemd units, as systemd itself reads them."""

import json
import shutil
from pathlib import Path

import pytest

from scripts import DEPLOY, run

UNITS = sorted(DEPLOY.glob("*/ccs-*"))
SERVICES = [unit.name for unit in UNITS if unit.suffix == ".service"]
SYSTEM = Path("/etc/systemd/system")


@pytest.fixture(scope="module")
def installed():
    """Place the units, each service's sandbox and a release, as install.sh does."""
    release = Path("/usr/local/lib/ccs/releases/1")
    shutil.copytree(DEPLOY, release / "deploy")
    Path("/usr/local/lib/ccs/current").symlink_to(release)
    Path("/etc/ccs").mkdir(exist_ok=True)
    shutil.copy(DEPLOY / "config.example", "/etc/ccs/config")
    for unit in UNITS:
        shutil.copy(unit, SYSTEM)
        if unit.suffix == ".service":
            dropins = SYSTEM / f"{unit.name}.d"
            dropins.mkdir()
            shutil.copy(DEPLOY / "sandbox.conf", dropins)
    return SYSTEM


def security(installed, service):
    """Return each setting the offline security review reports, and whether it is set."""
    command = ["systemd-analyze", "security", "--offline=yes", "--json=short", service]
    report = json.loads(run(*command, cwd=installed).stdout)
    return {entry["name"].removesuffix("="): entry for entry in report}


def test_the_units_load_cleanly(installed):
    checked = run(
        "systemd-analyze", "verify", "--man=no", *[u.name for u in UNITS], cwd=installed
    )
    assert checked.stdout + checked.stderr == ""


@pytest.mark.parametrize("service", SERVICES)
def test_each_service_runs_in_the_sandbox(installed, service):
    settings = security(installed, service)
    for name in ("NoNewPrivileges", "ProtectHome", "PrivateTmp"):
        assert settings[name]["set"], f"{service}: {name} is not set"
    assert "strict" in settings["ProtectSystem"]["description"]


@pytest.mark.parametrize(
    "service", ["ccs-update.service", "ccs-monitor.service", "ccs-values.service"]
)
def test_the_services_that_need_no_network_have_none(installed, service):
    assert security(installed, service)["PrivateNetwork"]["set"]
