#!/usr/bin/python3 -I
"""Check the live site against its sources and a render of its release.

Exit: 0 when the run ends, whatever it found; it reports through alerts.
"""

import calendar
import contextlib
import glob
import os
import secrets
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

# the physical path, so a run that overlaps an install stays in one release
MODULES = Path(__file__).resolve().parents[1] / "modules.py"
exec(compile(MODULES.read_bytes(), MODULES, "exec"), {"__file__": str(MODULES)})
import lib  # noqa: E402
from alerts import Alerts  # noqa: E402

# DEPLOY is releases/<N>/deploy, so the release's folder name is its number
INSTALLED_DIR = lib.DEPLOY.parent
INSTALLED_VERSION = INSTALLED_DIR.name
STATE_DIR = Path(os.environ["STATE_DIRECTORY"])
RUN_DIR = STATE_DIR / "run"
EXPECTED_DIR = STATE_DIR / "expected"
# in memory, so a full disk can still alert
ALERTS_DIR = Path(os.environ["RUNTIME_DIRECTORY"], "alerts")
CHECKOUT = Path(os.environ["CCS_BACK"], "proposals")
BACKEND_VALUES = Path("/var/lib/ccs-backend/values")
MESSAGE_PREFIX = "ccs monitor:"
# what Tor Browser asks for a page; keep it to each Tor Browser major release
BROWSER_AGENT = "Mozilla/5.0 (Windows NT 10.0; rv:153.0) Gecko/20100101 Firefox/153.0"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
BROWSER = ["--compressed", "-A", BROWSER_AGENT, "-H", f"Accept: {BROWSER_ACCEPT}"]
BROWSER += ["-H", "Accept-Language: en-US,en;q=0.5"]
DOWNLOAD = "--disable -fsS -L --max-time 60 --max-filesize 200000000".split()
GIT = ["git", "-C", str(CHECKOUT)]
PAGE_DIFFERS = " differs from the expected render"
MRS_DIFFER = "the site's merge requests differ from GitLab's"
NO_GITLAB = (
    "GitLab's merge request API doesn't answer, so the site's merge requests are"
    " unverified"
)
ALWAYS = [
    "index.html",
    "ideas/index.html",
    "funding-required/index.html",
    "work-in-progress/index.html",
    "completed-proposals/index.html",
    "donate/index.html",
    "index.php/projects",
    "feed.xml",
]
NOW = int(time.time())
ALERTS = Alerts(ALERTS_DIR, NOW)


class Ended(Exception):
    """A failed requirement, which ends the run early."""


def check(name: str, failing: bool, text: str, hold: int = 600) -> None:
    """Record a check, which alerts after hold seconds of failing, then once a day.

    The default hold of 10 minutes is the second failing run.
    """
    ALERTS.check(name, failing, hold, repeat=86400, text=f"{MESSAGE_PREFIX} {text}")


def require[T](name: str, value: T | None, text: str) -> T:
    """Record a check that fails on None or False, end the run then, else return value."""
    if value is None or value is False:
        check(name, True, text)
        raise Ended
    check(name, False, text)
    return value


def new_circuit() -> str:
    """Return a new SOCKS user name, for a Tor circuit of its own.

    The web server then cannot tie one set of requests to another by their exit.
    """
    return secrets.token_hex(8)


def fetch_url(url: str, file: Path, circuit: str, *curl: str, **options) -> bool:
    """Download one file on that Tor circuit, through the proxy in ALL_PROXY."""
    proxy = ["--proxy-user", f"{circuit}:x"]
    return lib.succeeds("curl", *DOWNLOAD, *proxy, *curl, "-o", file, url, **options)


def fetch_page(url: str, file: Path, circuit: str) -> bool:
    """Download one page as Tor Browser asks for it.

    The web server then cannot single out the monitor by its requests.
    """
    return fetch_url(url, file, circuit, *BROWSER)


def status_py(release: Path, *arguments: str | Path) -> bytes:
    """Run that release's status.py, and return its output."""
    script = release / "deploy/render/status.py"
    command = ["python3", "-I", "-B", script, *arguments]
    return lib.run(*command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def check_keys() -> None:
    """Alert on a signing key that expires soon.

    It runs first, as it does not depend on the site.
    """
    output = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True}
    checked = lib.checks("expiring", lib.RELEASE_SIGNERS, lib.ADDRESS_SIGNERS, **output)
    text = checked.stdout.rstrip("\n") or "a signing key expires within 30 days"
    check("key-expiry", checked.returncode != 0, text)


def read_site_status(circuit: str) -> str | None:
    """Return the "<release> <commit>" that the site's status.json names, or None.

    The web server is not trusted, so checks.py checks both fields.
    """
    status = RUN_DIR / "status.json"
    with open(RUN_DIR / "certs", "w") as certs:
        url = f"{lib.SITE}/status.json"
        if not fetch_url(url, status, circuit, "-w", "%{certs}", stdout=certs):
            return None
    checked = lib.checks("status", status, stdout=subprocess.PIPE, text=True)
    return checked.stdout if checked.returncode == 0 else None


def read_expiry() -> bytes:
    """Return when the certificate that served status.json expires, as curl says.

    curl lists that certificate first.
    """
    for line in (RUN_DIR / "certs").read_bytes().split(b"\n"):
        if line.startswith(b"Expire date:"):
            return line.removeprefix(b"Expire date:")
    return b""


def check_certificate() -> None:
    """Alert 14 days before the site's TLS certificate expires."""
    expires = read_expiry()
    if not expires:
        return
    try:
        parsed = time.strptime(expires.decode(), "%b %d %H:%M:%S %Y GMT")
        expiry = calendar.timegm(parsed)
    except ValueError:
        # a date that cannot be read counts as expired, so it alerts
        expiry = 0
    day = time.strftime("%Y-%m-%d", time.gmtime(expiry))
    expiring = expiry < NOW + 14 * 86400
    check("cert-expiry", expiring, f"the site's certificate expires on {day}")


def read_status(circuit: str) -> list[str]:
    """Read the site's status.json, and return its release and commit."""
    status = read_site_status(circuit)
    check_certificate()
    return require("status", status, "status.json is missing or malformed").split()


def get_verified_release(version: str) -> Path | None:
    """Return the release's folder, downloaded and verified as the install does.

    Return None when it does not download or verify.
    """
    release = STATE_DIR / "releases" / version
    if release.is_dir():
        return release
    download, tree = RUN_DIR / "download", RUN_DIR / "tree"
    download.mkdir(parents=True, exist_ok=True)
    try:
        lib.get_release(f"site-v{version}", download)
        lib.checks("release", download, lib.RELEASE_SIGNERS, version, tree, check=True)
    except lib.FAILED:
        return None
    tree.rename(release)
    return release


def is_recent_enough(version: int, release: Path) -> bool:
    """Return whether the site's release may run beside this one.

    It may be this one or newer, or older by only backend/, signer/ and the docs.
    So the site cannot fall back to a fixed bug, and no older render runs here.
    """
    if version >= int(INSTALLED_VERSION):
        return True
    differ = ["diff", "-rq", "-x", "backend", "-x", "signer", "-x", "*.md"]
    return lib.succeeds(*differ, release, INSTALLED_DIR, stdout=subprocess.DEVNULL)


def take_release(version: str) -> Path:
    """Verify the site's release, and return its folder."""
    text = f"release {version} doesn't download or verify"
    release = require("release", get_verified_release(version), text)
    lib.keep(f"{STATE_DIR}/releases/", release, release)
    text = (
        f"the site runs release {version}, older than release {INSTALLED_VERSION} here"
    )
    require("old-release", is_recent_enough(int(version), release), text)
    return release


def check_stall(commit: str) -> None:
    """Alert when the site's commit stays behind ccs-back's for over an hour."""
    head = lib.output(*GIT, "rev-parse", "HEAD").strip()
    ancestor = [*GIT, "merge-base", "--is-ancestor", commit, head]
    behind = head != commit and lib.succeeds(*ancestor)
    text = "the site's content is over an hour behind ccs-back"
    check("stall", behind, text, hold=3600)


def check_site_commit(commit: str) -> None:
    """Require the site's commit in ccs-back's checkout."""
    is_commit = [*GIT, "cat-file", "-e", f"{commit}^{{commit}}"]
    found = lib.succeeds(*is_commit, stderr=subprocess.DEVNULL)
    text = f"the site's commit {commit} isn't in ccs-back's checkout"
    require("commit", found, text)
    check_stall(commit)


def read_snapshot(release: Path) -> bytes | None:
    """Restore the site's merge requests from status.json, and return their snapshot.

    Return None when status.json's merge requests are malformed.
    """
    status = RUN_DIR / "status.json"
    if not lib.accepts("snapshot", status):
        return None
    try:
        status_py(release, "restore", status, RUN_DIR / "in")
        return status_py(release, "mrs", RUN_DIR / "in")
    except lib.FAILED:
        return None


def is_recently_verified(snapshot_sum: str) -> bool:
    """Return whether GitLab confirmed this same set of merge requests within the hour."""
    verified = lib.read(STATE_DIR / "mrs-verified")
    verified_sum, _, verified_at = verified.rpartition(" ")
    return verified_sum == snapshot_sum and NOW - int(verified_at) < 3600


def verify_merge_requests(release: Path, snapshot: bytes) -> str:
    """Return why the site's merge requests are unverified, or "" when GitLab's match."""
    snapshot_sum = lib.sha256(snapshot)
    if is_recently_verified(snapshot_sum):
        return ""
    gitlab = RUN_DIR / "gitlab"
    (gitlab / "mr").mkdir(parents=True, exist_ok=True)
    mrs = [lib.DEPLOY / "mrs.py", lib.GITLAB_API, gitlab / "mr", STATE_DIR / "changes"]
    if not lib.succeeds(*mrs, stderr=subprocess.DEVNULL):
        return NO_GITLAB
    if status_py(release, "mrs", gitlab) != snapshot:
        return MRS_DIFFER
    lib.write(STATE_DIR / "mrs-verified", f"{snapshot_sum} {NOW}")
    return ""


def check_snapshot(release: Path) -> None:
    """Restore the site's merge requests, and compare them with GitLab's."""
    text = "status.json's merge requests are malformed"
    snapshot = require("snapshot", read_snapshot(release), text)
    try:
        text = verify_merge_requests(release, snapshot)
    except lib.FAILED:
        text = MRS_DIFFER
    # the web server can lag GitLab by an hour, so the alert waits 75 minutes
    check("merge-requests", bool(text), text or MRS_DIFFER, hold=4500)


def build_values() -> str:
    """Build the render's values from the backend's latest file, and return their hash.

    The web server builds them the same way. A site with other values fails the
    page compare, index.php/projects first.
    """
    verified, checked = RUN_DIR / "addresses", RUN_DIR / "checked"
    lib.verify_list(verified)
    with open(checked, "w") as file:
        values = ["values", BACKEND_VALUES, "/dev/null", verified]
        if not lib.accepts(*values, stdout=file):
            file.truncate(0)
    return lib.render_values(checked, RUN_DIR / "in", verified)


def render_expected(release: Path, commit: str, inputs_hash: str) -> None:
    """Render the expected pages into EXPECTED_DIR, when the render's inputs changed."""
    rendered = STATE_DIR / "rendered"
    if inputs_hash == lib.read(rendered) and EXPECTED_DIR.is_dir():
        return
    # with rendered gone, a render cut short is redone next run
    shutil.rmtree(EXPECTED_DIR, ignore_errors=True)
    rendered.unlink(missing_ok=True)
    lib.export_tree(CHECKOUT, commit, RUN_DIR / "in")
    shutil.copytree(release / "dist", EXPECTED_DIR)
    shutil.rmtree(EXPECTED_DIR / "templates", ignore_errors=True)
    render = ["python3", "-I", "-B", release / "deploy/render/render.py"]
    templates = release / "dist/templates"
    lib.run("timeout", "300", *render, RUN_DIR / "in", templates, EXPECTED_DIR)
    lib.write(rendered, inputs_hash)


def pages_with(text: bytes) -> list[str]:
    """Return each proposal page of the expected render that holds text."""
    pages = sorted(glob.glob("proposals/*.html", root_dir=EXPECTED_DIR))
    return [page for page in pages if text in (EXPECTED_DIR / page).read_bytes()]


def check_addresses() -> None:
    """Alert when the address list does not verify.

    Also alert when a proposal that takes donations shows no address.
    """
    if not lib.has_data(RUN_DIR / "addresses"):
        text = "the address list doesn't verify, so no donation address shows"
        check("addresses", True, text)
        return
    missing = " ".join(pages_with(b'class="no-address"'))
    text = f"proposals take donations with no address on the list: {missing}"
    check("addresses", bool(missing), text)


def check_render(release: Path, commit: str) -> None:
    """Render the site as it should be, and check it."""
    values_sum = build_values()
    status_sum = lib.sha256((RUN_DIR / "status.json").read_bytes())
    inputs_hash = lib.sha256(f"{status_sum}\n{values_sum}\n".encode())
    render_expected(release, commit, inputs_hash)
    check_addresses()
    hosts = release / "deploy/site-hosts"
    listed = lib.accepts("site", EXPECTED_DIR, RUN_DIR / "addresses", hosts)
    check("unlisted", not listed, "the render shows an address or a link it may not")


def list_always() -> set[str]:
    """Return the files compared on every run."""
    always = set(ALWAYS)
    for path in (EXPECTED_DIR / "_astro").rglob("*.css"):
        always.add(str(path.relative_to(EXPECTED_DIR)))
    for qr in glob.glob("qr/*.png", root_dir=EXPECTED_DIR):
        always.add(qr)
        always.update(pages_with(os.fsencode(Path(qr).stem)))
    return always


def list_sample(files: list[str]) -> list[str]:
    """Return the next ten files, from where the last run stopped."""
    if not files:
        return []
    offset_file = STATE_DIR / "sample-offset"
    offset = int(lib.read(offset_file) or 0)
    if offset >= len(files):
        offset = 0
    lib.write(offset_file, str((offset + 10) % len(files)))
    return [files[(offset + i) % len(files)] for i in range(min(10, len(files)))]


def list_failing(listed: set[str]) -> list[str]:
    """Return each failing page still listed, so it is compared again until it passes."""
    again = []
    for state in sorted(ALERTS_DIR.glob("page-*")):
        # compare_page records "<MESSAGE_PREFIX> page <path><PAGE_DIFFERS>"
        _, _, text = lib.read(state).split(" ", 2)
        path = text.removeprefix(f"{MESSAGE_PREFIX} page ").removesuffix(PAGE_DIFFERS)
        if path in listed:
            again.append(path)
        else:
            check(state.name, False, f"page {path} left the site")
    return again


def is_same(fetched: Path, expected: Path) -> bool:
    """Return whether both files hold the same bytes, and False when one is missing."""
    try:
        return fetched.read_bytes() == expected.read_bytes()
    except OSError:
        return False


def compare_page(path: str, circuit: str) -> None:
    """Fetch one file from the live site, and check it against the expected render.

    The two must match byte for byte.
    """
    page = RUN_DIR / "page"
    url = f"{lib.SITE}/{urllib.parse.quote(path)}"
    same = fetch_page(url, page, circuit) and is_same(page, EXPECTED_DIR / path)
    name = lib.sha256(path.encode())[:16]
    check(f"page-{name}", not same, f"page {path}{PAGE_DIFFERS}")


def compare_pages(circuit: str) -> None:
    """Compare the files always compared, ten more in turn, and each failing page."""
    files = sorted(
        str(path.relative_to(EXPECTED_DIR))
        for path in EXPECTED_DIR.rglob("*")
        if path.is_file()
    )
    always = list_always()
    sample = list_sample(files)
    again = list_failing(always.union(files))
    for path in sorted({*always, *sample, *again}):
        compare_page(path, circuit)


def monitor() -> None:
    """Clear the last run's files, then run each check in turn."""
    shutil.rmtree(RUN_DIR, ignore_errors=True)
    RUN_DIR.mkdir(parents=True)
    (STATE_DIR / "releases").mkdir(exist_ok=True)
    check_keys()
    # two new circuits, so the web server cannot tie the pages to the request
    # for status.json, which marks the monitor
    status_circuit, page_circuit = new_circuit(), new_circuit()
    version, commit = read_status(status_circuit)
    release = take_release(version)
    check_site_commit(commit)
    check_snapshot(release)
    check_render(release, commit)
    compare_pages(page_circuit)


def send_heartbeat() -> None:
    """Request the heartbeat URL, when there is one.

    A heartbeat service sees the monitor or Tor stop, which no alert can show.
    """
    if lib.credential("heartbeat_url") and not lib.request_url("heartbeat_url"):
        lib.log("heartbeat not sent")


def main() -> None:
    # ccs-back's checkout belongs to another user, so git is told to trust it
    os.environ.update(
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="safe.directory",
        GIT_CONFIG_VALUE_0=str(CHECKOUT),
    )
    crashed = True
    try:
        with contextlib.suppress(Ended):
            monitor()
        crashed = False
    finally:
        check("monitor", crashed, "the monitor crashed")
        ALERTS.send()
    send_heartbeat()
    shutil.rmtree(RUN_DIR)


if __name__ == "__main__":
    lib.main(main)
