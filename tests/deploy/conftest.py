"""Shared setup for the deploy tests."""

import os
import shutil
from pathlib import Path

import pytest

from fake_server import FakeServer
from fetching import Fetcher
from signing import Keys, place_keyrings, write_list
from web import A, WebServer


def pytest_configure(config):
    # the tests delete /etc/ccs and run the deploy scripts as root
    if os.environ.get("container") != "podman":
        pytest.exit("run them with tests/deploy/run.sh, in a container", returncode=2)


@pytest.fixture(scope="session")
def keys(tmp_path_factory) -> Keys:
    return Keys.create(tmp_path_factory.mktemp("gnupg"))


@pytest.fixture
def etc_ccs(keys) -> Path:
    """Return /etc/ccs, made afresh with the keyrings and an empty config."""
    etc = Path("/etc/ccs")
    shutil.rmtree(etc, ignore_errors=True)
    etc.mkdir()
    (etc / "config").write_text("")
    place_keyrings(etc, keys)
    return etc


@pytest.fixture
def web(tmp_path, etc_ccs) -> WebServer:
    return WebServer(tmp_path)


@pytest.fixture
def sign_list(etc_ccs, keys):
    """Return sign(*addresses), which installs a signed list of those addresses."""

    def sign(*addresses: str) -> None:
        write_list(etc_ccs / "addresses.txt", addresses, keys)

    return sign


@pytest.fixture
def rendered(web, sign_list) -> WebServer:
    """Return a web server that has rendered release 1, listing A, once."""
    web.install(1)
    sign_list(A)
    web.push()
    web.update()
    return web


@pytest.fixture
def server(tmp_path):
    server = FakeServer(tmp_path / "server")
    yield server
    server.stop()


@pytest.fixture
def fetcher(tmp_path, server) -> Fetcher:
    return Fetcher(tmp_path, server)
