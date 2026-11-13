"""nginx.conf: what the web server sends, as nginx serves the rendered site."""

import shutil
from pathlib import Path

import pytest

from scripts import DEPLOY, EXAMPLE, REPO, run

CSP = (
    "default-src 'none'; img-src 'self'; style-src 'self'; font-src 'self';"
    " form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)
SITE = Path("/var/lib/ccs/site/1")
ORIGIN = "https://ccs.getmonero.org"


@pytest.fixture(scope="module")
def nginx():
    """Serve the rendered example site with deploy/web/nginx.conf, as installed."""
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.copytree(REPO / "dist", SITE, ignore=shutil.ignore_patterns("templates"))
    templates = REPO / "dist/templates"
    run("python3", "-B", DEPLOY / "render/render.py", EXAMPLE, templates, SITE)
    (SITE / "status.json").write_text("{}")
    shutil.copy(
        SITE / "proposals/example-funding.html", SITE / "proposals/with space.html"
    )
    (SITE / "file.unknown").write_text("x\n")
    (SITE / "leak.html").symlink_to("/etc/passwd")
    (SITE / "inner.html").symlink_to(SITE / "index.html")
    current = Path("/var/lib/ccs/current")
    current.unlink(missing_ok=True)
    current.symlink_to(SITE)
    Path("/etc/ssl/ccs").mkdir(parents=True, exist_ok=True)
    certificate = (
        "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1"
        " -subj /CN=ccs.getmonero.org -keyout /etc/ssl/ccs/privkey.pem"
        " -out /etc/ssl/ccs/fullchain.pem"
    )
    run(*certificate.split())
    Path("/etc/nginx/sites-enabled/default").unlink(missing_ok=True)
    shutil.copy(DEPLOY / "web/nginx.conf", "/etc/nginx/conf.d/ccs.conf")
    acme = Path("/var/www/acme/.well-known/acme-challenge")
    acme.mkdir(parents=True, exist_ok=True)
    (acme / "t1").write_text("token-1\n")
    run("nginx")
    yield SITE
    run("nginx", "-s", "stop")


def head(path, *options):
    """Return the status line and the headers nginx sends for a path, over TLS."""
    resolve = ["--resolve", "ccs.getmonero.org:443:127.0.0.1"]
    sent = run(
        "curl", "-sk", *resolve, "-o", "/dev/null", "-D", "-", *options, ORIGIN + path
    )
    return sent.stdout.replace("\r", "").splitlines()


def status(path):
    return int(head(path)[0].split()[1])


def headers(path, name):
    prefix = f"{name.lower()}: "
    return [
        line[len(prefix) :] for line in head(path) if line.lower().startswith(prefix)
    ]


def built(pattern):
    found = sorted(SITE.glob(pattern))
    assert found, f"the build holds no {pattern}"
    return "/" + str(found[0].relative_to(SITE))


def plain(path, host="ccs.getmonero.org"):
    """Return the headers and body of a request to port 80."""
    sent = run(
        "curl", "-s", "-D", "-", "-H", f"Host: {host}", f"http://127.0.0.1{path}"
    )
    return sent.stdout.replace("\r", "")


def test_the_site_config_loads_with_no_warnings(nginx):
    checked = run("nginx", "-t")
    assert "warn" not in checked.stderr.lower()


@pytest.mark.parametrize(
    "path, code",
    [
        pytest.param("/", 200, id="the home page"),
        pytest.param("/ideas/", 200, id="a folder"),
        pytest.param("/proposals/with%20space.html", 200, id="a name with a space"),
        pytest.param("/missing.html", 404, id="a missing page"),
        pytest.param("_astro/*.css", 200, id="a stylesheet"),
    ],
)
def test_every_response_has_one_of_each_security_header(nginx, path, code):
    path = built(path) if "*" in path else path
    assert status(path) == code
    assert headers(path, "Content-Security-Policy") == [CSP]
    assert headers(path, "X-Content-Type-Options") == ["nosniff"]
    assert headers(path, "Referrer-Policy") == ["no-referrer"]
    assert headers(path, "Strict-Transport-Security") == ["max-age=31536000"]


@pytest.mark.parametrize(
    "path, kind",
    [
        pytest.param("/feed.xml", "application/rss+xml; charset=utf-8", id="the feed"),
        pytest.param(
            "/index.php/projects",
            "application/json; charset=utf-8",
            id="the projects API",
        ),
        pytest.param(
            "/status.json", "application/json; charset=utf-8", id="status.json"
        ),
        pytest.param("_astro/*.avif", "image/avif", id="AVIF"),
        pytest.param("_astro/*.woff2", "font/woff2", id="WOFF2"),
        pytest.param("_astro/*.css", "text/css; charset=utf-8", id="CSS"),
        pytest.param(
            "/file.unknown", "application/octet-stream", id="an unknown extension"
        ),
    ],
)
def test_each_file_has_its_type(nginx, path, kind):
    path = built(path) if "*" in path else path
    assert headers(path, "Content-Type") == [kind]


def test_a_folder_without_its_slash_redirects(nginx):
    assert status("/ideas") == 301
    locations = headers("/ideas", "Location")
    assert len(locations) == 1 and locations[0].endswith("/ideas/"), locations


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/leak.html", id="out of the tree"),
        pytest.param("/inner.html", id="inside it"),
    ],
)
def test_a_symlink_is_refused(nginx, path):
    assert status(path) == 403


def test_only_astro_files_are_cached_for_good(nginx):
    assert headers(built("_astro/*.css"), "Cache-Control") == ["max-age=315360000"]
    assert headers("/", "Cache-Control") == []


def test_port_80_answers_acme_and_redirects_the_rest_to_https(nginx):
    assert plain("/.well-known/acme-challenge/t1").endswith("token-1\n")
    assert plain("/.well-known/other").startswith("HTTP/1.1 301")
    redirect = plain("/ideas/")
    assert redirect.startswith("HTTP/1.1 301")
    assert "\nLocation: https://ccs.getmonero.org/ideas/\n" in redirect


def test_a_forged_host_does_not_steer_the_redirect(nginx):
    assert "\nLocation: https://ccs.getmonero.org/x\n" in plain(
        "/x", host="evil.example"
    )


def tls(*options, ok=True):
    target = ["-connect", "127.0.0.1:443", "-servername", "ccs.getmonero.org"]
    return run("openssl", "s_client", *target, *options, input="", ok=ok)


@pytest.mark.parametrize("version", ["-tls1_3", "-tls1_2"])
def test_tls_1_3_and_1_2_are_offered(nginx, version):
    tls(version)


def test_tls_1_1_is_refused_even_to_a_client_that_offers_it(nginx):
    refused = tls("-tls1_1", "-cipher", "DEFAULT:@SECLEVEL=0", ok=False)
    assert "alert protocol version" in refused.stdout + refused.stderr


def test_no_session_ticket_is_handed_out(nginx):
    assert "TLS session ticket:" not in tls("-tls1_2").stdout
