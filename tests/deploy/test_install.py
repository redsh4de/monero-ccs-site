"""install.sh: both roles, the push, upgrades, and the releases it refuses."""

import filecmp
import os
import re
import shutil
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from installing import (
    CCS_BACK,
    LIB,
    MERGE,
    PUSH_KEY,
    Machine,
    make_certificate,
    push_key,
    values,
)
from scripts import DEPLOY, REPO, replaced, run, stand_in
from signing import publish
from web import lay_out

USAGE = "usage: install.sh"
INSTALL = (DEPLOY / "install.sh").read_text()


def test_a_push_holds_one_byte_more_than_the_values_cap():
    checks = (DEPLOY / "checks.py").read_text()
    cap = int(re.search(r"^VALUES_CAP = ([0-9_]+)$", checks, re.M)[1])
    assert f"head -c {cap + 1} " in INSTALL


def test_fetch_deletes_the_temporary_files_that_a_push_makes():
    made = re.search(r"mktemp /var/lib/ccs/in/values\.(X+)\)", INSTALL)[1]
    fetch = (DEPLOY / "web/fetch.sh").read_text()
    assert f"-name 'values.{'?' * len(made)}' " in fetch


def test_install_names_every_package_of_the_render_image():
    image = (REPO / "dev/Containerfile.render").read_text()
    packages = re.findall(r"python3-[a-z0-9-]+", image)
    assert packages
    assert [name for name in packages if name not in INSTALL.split()] == []


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


AUTHORIZED_KEYS = Path("/home/ccs-fetch/.ssh/authorized_keys")
PASSWORDS = (
    "sshd may take passwords: turn PasswordAuthentication and"
    " KbdInteractiveAuthentication off in /etc/ssh/sshd_config.d/, reload ssh,"
    " then run this again"
)


def test_an_sshd_that_takes_passwords_stops_the_web_install(installed_backend):
    args = ("web", "site-v1", "127.0.0.1", push_key())
    installed_backend.refuses(*args, reason=PASSWORDS, first=True)


def test_the_web_install_leaves_sshds_config_as_it_was(installed_web):
    assert os.listdir(SSHD_CONFIG) == ["50-admin.conf"]


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(
            "KbdInteractiveAuthentication yes\n"
            "AuthenticationMethods publickey,keyboard-interactive\n",
            id="a key and a second factor",
        ),
        pytest.param(
            "AuthenticationMethods publickey password\n",
            id="both password methods off",
        ),
    ],
)
def test_an_sshd_that_takes_only_keys_passes_the_web_install(installed_web, config):
    (SSHD_CONFIG / "40-totp.conf").write_text(config)
    installed_web.install("web", "site-v1")


@pytest.mark.parametrize(
    "config, reason",
    [
        pytest.param(
            "PasswordAuthentication yes\nAuthenticationMethods publickey password\n",
            PASSWORDS,
            id="a method list without a key",
        ),
        pytest.param(
            "KbdInteractiveAuthentication yes\n",
            PASSWORDS,
            id="keyboard-interactive, which takes passwords through PAM",
        ),
        pytest.param("NoSuchOption yes\n", "NoSuchOption", id="a config sshd refuses"),
    ],
)
def test_an_sshd_that_may_take_passwords_stops_the_web_install(
    installed_web, config, reason
):
    (SSHD_CONFIG / "40-totp.conf").write_text(config)
    installed_web.refuses("web", "site-v1", reason=reason)


def test_the_web_users_are_made_as_sysusers_conf_declares(installed_web):
    shell = run("getent", "passwd", "ccs-fetch").stdout.strip().split(":")[6]
    assert shell == "/bin/sh"
    expiry = run("getent", "shadow", "ccs-update").stdout.split(":")[7]
    assert expiry != "", "ccs-update's account has no expiry date, so it is not locked"


def test_the_web_folders_are_made_and_a_changed_mode_is_put_back(installed_web):
    ccs = Path("/var/lib/ccs")
    expected = {
        ccs: "ccs-update 755",
        ccs / "in": "ccs-fetch 755",
        ccs / "fetch": "ccs-fetch 755",
        Path("/home/ccs-fetch"): "root 755",
        Path("/home/ccs-fetch/.ssh"): "root 755",
    }
    assert {path: ownership(path) for path in expected} == expected
    assert Path("/var/lib/systemd/linger/ccs-fetch").exists()
    (ccs / "in").chmod(0o700)
    run("systemd-tmpfiles", "--create", "/etc/tmpfiles.d/ccs-web.conf")
    assert ownership(ccs / "in") == "ccs-fetch 755"


def test_authorized_keys_is_roots_and_holds_only_the_backends_key(installed_web):
    assert ownership(AUTHORIZED_KEYS) == "root 644"
    lines = AUTHORIZED_KEYS.read_text().splitlines()
    assert len(lines) == 1, lines
    assert lines[0].endswith(f" {push_key()}")


def test_the_web_units_and_nginx_site_are_installed_each_service_with_the_sandbox(
    installed_web,
):
    deploy = LIB / "releases/1/deploy"
    units = ["ccs-fetch.service", "ccs-fetch.path", "ccs-fetch.timer"]
    units += ["ccs-update.service"]
    installed = {UNITS / unit: deploy / "web" / unit for unit in units}
    for service in ("ccs-fetch", "ccs-update"):
        installed[UNITS / f"{service}.service.d/sandbox.conf"] = deploy / "sandbox.conf"
    installed[Path("/etc/nginx/sites-enabled/ccs.conf")] = deploy / "web/nginx.conf"
    nginx_sandbox = UNITS / "nginx.service.d/ccs-sandbox.conf"
    installed[nginx_sandbox] = deploy / "web/nginx-sandbox.conf"
    assert differing(installed) == []
    assert os.listdir("/etc/nginx/sites-enabled") == ["ccs.conf"]


def test_a_push_from_the_backend_replaces_the_values(installed_web):
    PUSHED.unlink()
    text = values()
    installed_web.push(text)
    assert PUSHED.read_text() == text
    assert ownership(PUSHED) == "ccs-fetch 644"


def test_the_push_key_runs_nothing_but_the_values_write(installed_web):
    assert "root:" not in installed_web.push("", "cat", "/etc/passwd").stdout


def test_the_push_key_works_only_from_the_backends_address(installed_web):
    logged = installed_web.sshd_log.read_text()
    installed_web.push("pushed\n", source="127.0.0.2", ok=False)
    refusal = installed_web.sshd_log.read_text().removeprefix(logged)
    assert "with correct key but not from a permitted host" in refusal
    assert PUSHED.read_text() == installed_web.saved("web", PUSHED)


@pytest.mark.parametrize(
    "address, key, reason",
    [
        pytest.param(
            "127.0.0.1",
            'ssh-ed25519 AAAA" evil',
            "the push key must be the one line",
            id="a key with a quote",
        ),
        pytest.param(
            "127.0.0.1",
            "{key}\n{key}",
            "the push key must be the one line",
            id="a key with a second line",
        ),
        pytest.param("1.2.3.4 x", "{key}", USAGE, id="an address with a space"),
        pytest.param(
            "127.0.0.1\n10.0.0.1", "{key}", USAGE, id="an address with a second line"
        ),
        pytest.param(
            "backend.example",
            "{key}",
            USAGE,
            id="a host name, which sshd's from= never matches",
        ),
    ],
)
def test_a_malformed_push_key_or_address_is_refused(
    installed_web, address, key, reason
):
    key = key.format(key=push_key())
    installed_web.refuses("web", "site-v1", address, key, reason=reason)
    assert AUTHORIZED_KEYS.read_text() == installed_web.saved("web", AUTHORIZED_KEYS)


def test_an_upgrade_is_announced_on_the_alert_channel(installed_web, server):
    shutil.copy(server.credentials / "alert_url", "/etc/ccs/alert_url")
    result = installed_web.install("web", "site-v2")
    assert server.alerts() == ["ccs install: site-v2, web role"]
    assert "The alert channel has the install." in result.stdout.splitlines()


@pytest.fixture
def mktemp_log(tmp_path) -> Path:
    """Write a mktemp in tmp_path that logs its arguments, and return the log."""
    log = tmp_path / "mktemp.log"
    script = f'#!/bin/sh\necho "$*" >>{log}\nexec /usr/bin/mktemp "$@"\n'
    stand_in(tmp_path, "mktemp", script)
    return log


def test_the_download_goes_beside_releases_and_leaves_nothing_behind(
    installed_web, mktemp_log
):
    path = f"{mktemp_log.parent}:{installed_web.env['PATH']}"
    installed_web.install("web", "site-v2", PATH=path)
    assert f"-d -p {LIB}" in mktemp_log.read_text().splitlines()
    assert list(LIB.glob("tmp.*")) == []


def test_the_installed_copy_upgrades_from_the_release_host_in_the_config(upgraded):
    assert upgraded.installed() == str(LIB / "releases/2")
    assert upgraded.served() == 2
    assert sorted(os.listdir(LIB / "releases")) == ["1", "2"]
    assert AUTHORIZED_KEYS.read_text() == upgraded.saved("web", AUTHORIZED_KEYS)


def test_the_backend_upgrade_keeps_the_push_key(upgraded):
    upgraded.install("backend", "site-v2")
    assert push_key() == upgraded.saved("upgraded", Path(f"{PUSH_KEY}.pub")).strip()


def test_installing_the_installed_release_again_changes_nothing(upgraded):
    upgraded.install("web", "site-v2")
    assert upgraded.installed() == str(LIB / "releases/2")
    assert sorted(os.listdir(LIB / "releases")) == ["1", "2"]


def unrenderable(tree: Path) -> None:
    """Make the proposal template fail to render, and mark three deploy files."""
    template = tree / "dist/templates/proposal.html"
    template.write_text(replaced(template.read_text(), "it.title", "it.nope"))
    for name in ("web/ccs-fetch.service", "sandbox.conf", "web/nginx.conf"):
        with open(tree / "deploy" / name, "a") as file:
            file.write("# a release that fails to render\n")


def test_a_release_that_fails_to_render_is_refused_and_the_old_one_comes_back(
    upgraded,
):
    upgraded.publish(3, edit=unrenderable)
    site = os.readlink("/var/lib/ccs/current")
    reason = "site-v3 failed to render, or nginx refused its site"
    upgraded.refuses("web", "site-v3", reason=reason)
    assert upgraded.installed() == str(LIB / "releases/2")
    assert not (LIB / "releases/3").exists()
    assert os.readlink("/var/lib/ccs/current") == site
    deploy = LIB / "releases/2/deploy"
    installed = {
        UNITS / "ccs-fetch.service": deploy / "web/ccs-fetch.service",
        UNITS / "ccs-fetch.service.d/sandbox.conf": deploy / "sandbox.conf",
        Path("/etc/nginx/sites-enabled/ccs.conf"): deploy / "web/nginx.conf",
    }
    assert differing(installed) == []


def test_a_failed_render_is_refused_even_when_its_first_start_joins_a_run_already_going(
    upgraded,
):
    MERGE.touch()
    upgraded.publish(4, edit=unrenderable)
    upgraded.refuses("web", "site-v4", reason="site-v4 failed to render")
    assert upgraded.installed() == str(LIB / "releases/2")


def publish_one_signature(machine: Machine) -> None:
    publish(machine.host, 5, machine.keys, signers=["signer-a"])


def publish_cut_off(machine: Machine) -> None:
    tar = machine.publish(6)
    tar.write_bytes(tar.read_bytes()[:1000])


def publish_release_2_as_7(machine: Machine) -> None:
    release_2, release_7 = (machine.host / f"download/site-v{n}" for n in (2, 7))
    shutil.copytree(release_2, release_7)
    (release_7 / "ccs-site-v2.tar").rename(release_7 / "ccs-site-v7.tar")


@pytest.mark.parametrize(
    "version, make, reason",
    [
        pytest.param(
            5,
            publish_one_signature,
            "needs good signatures from two release signers",
            id="one signature",
        ),
        pytest.param(
            6,
            publish_cut_off,
            "the archive doesn't match its hash",
            id="a cut-off archive",
        ),
        pytest.param(
            7,
            publish_release_2_as_7,
            "this isn't release 7",
            id="release 2 named site-v7",
        ),
    ],
)
def test_a_release_that_does_not_verify_is_refused(upgraded, version, make, reason):
    make(upgraded)
    upgraded.refuses("web", f"site-v{version}", reason=f"release: {reason}")
    assert upgraded.installed() == str(LIB / "releases/2")
    assert sorted(os.listdir(LIB / "releases")) == ["1", "2"]


def test_an_older_release_is_refused(upgraded):
    shutil.rmtree(LIB / "releases/1")
    reason = "site-v1 is older than the installed release"
    upgraded.refuses("web", "site-v1", reason=reason)
    assert upgraded.installed() == str(LIB / "releases/2")


def test_a_damaged_release_is_fetched_again_and_a_killed_download_cleared(upgraded):
    (LIB / "tmp.killed").mkdir()
    shutil.rmtree(LIB / "releases/2")
    upgraded.install("web", "site-v2", first=True)
    assert os.access(LIB / "releases/2/deploy/web/update.sh", os.X_OK)
    assert not (LIB / "tmp.killed").exists()


@pytest.fixture
def certificate(upgraded) -> Path:
    return make_certificate()


def test_a_tls_key_that_others_can_read_is_refused(upgraded, certificate):
    certificate.chmod(0o644)
    reason = f"make {certificate} readable by root only"
    upgraded.refuses("web", "site-v2", reason=reason)


def test_with_a_certificate_the_release_passes_nginxs_check(upgraded, certificate):
    upgraded.install("web", "site-v2")


def unloadable_nginx_site(tree: Path) -> None:
    with open(tree / "deploy/web/nginx.conf", "a") as site:
        site.write("not_a_directive;\n")


def test_a_release_whose_nginx_site_does_not_load_is_refused(upgraded, certificate):
    upgraded.publish(8, edit=unloadable_nginx_site)
    site = os.readlink("/var/lib/ccs/current")
    upgraded.refuses("web", "site-v8", reason='unknown directive "not_a_directive"')
    assert upgraded.installed() == str(LIB / "releases/2")
    assert os.readlink("/var/lib/ccs/current") == site
    nginx_site = Path("/etc/nginx/sites-enabled/ccs.conf")
    assert differing({nginx_site: LIB / "releases/2/deploy/web/nginx.conf"}) == []
