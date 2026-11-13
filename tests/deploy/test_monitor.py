"""monitor.sh: the backend's checks of the live site."""

import pytest

from monitoring import Monitored, SiteServer


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
