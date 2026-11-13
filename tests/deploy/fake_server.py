"""A fake GitLab API, alert channel and release host, in the test's own process."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT = "/api/v4/projects/monero-project%2Fccs-proposals"


class FakeServer:
    """Serve merge requests, the alert channel and releases from a state folder.

    set_merge_requests() sets the open merge requests, and changes() the diffs of
    one. A file named fail makes the API answer 500, fail-once makes it do so
    once, changes/<iid>.fail fails that merge request, and alert.fail refuses
    alerts. Each request goes to access.log, and each alert to alerts.log. The
    credentials folder holds the alert URL, as systemd's LoadCredential= gives it.
    The releases that signing.publish() writes into the folder are served too.
    """

    def __init__(self, state: Path):
        self.state = state
        (state / "changes").mkdir(parents=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.state = state
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.api = self.url + PROJECT
        self.credentials = state / "credentials"
        self.credentials.mkdir()
        (self.credentials / "alert_url").write_text(f"{self.url}/alert\n")
        serve = self.httpd.serve_forever
        threading.Thread(target=serve, args=(0.05,), daemon=True).start()

    def set_merge_requests(self, merge_requests: list[dict]) -> None:
        (self.state / "mrs.json").write_text(json.dumps(merge_requests))

    def changes(self, iid: int, files: list[tuple[str, bool]]) -> None:
        """Give merge request `iid` these files, each a path and whether it is new."""
        diffs = [{"new_path": path, "new_file": new} for path, new in files]
        (self.state / "changes" / f"{iid}.json").write_text(json.dumps(diffs))

    def calls(self, fragment: str) -> int:
        return sum(fragment in line for line in self.log("access.log"))

    def alerts(self) -> list[str]:
        """Return each post, its messages joined by |."""
        return [post.removesuffix("|") for post in self.log("alerts.log")]

    def log(self, name: str) -> list[str]:
        path = self.state / name
        return path.read_text().splitlines() if path.exists() else []

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body=b"", kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if (self.server.state / "alert.fail").exists():
            return self.send(500)
        with open(self.server.state / "alerts.log", "ab") as log:
            log.write(body.replace(b"\n", b"|") + b"\n")
        self.send(200)

    def do_GET(self):
        with open(self.server.state / "access.log", "a") as log:
            log.write(self.path + "\n")
        url = urlsplit(self.path)
        if (self.server.state / "fail").exists():
            return self.send(500)
        if (self.server.state / "fail-once").exists():
            (self.server.state / "fail-once").unlink()
            return self.send(500)
        query = parse_qs(url.query)
        if url.path == f"{PROJECT}/merge_requests":
            mrs = json.loads((self.server.state / "mrs.json").read_text())
            return self.send(200, json.dumps(page(mrs, query)).encode())
        if url.path.startswith(f"{PROJECT}/merge_requests/") and url.path.endswith(
            "/diffs"
        ):
            iid = url.path.split("/")[-2]
            if (self.server.state / "changes" / f"{iid}.fail").exists():
                return self.send(500)
            path = self.server.state / "changes" / f"{iid}.json"
            diffs = json.loads(path.read_text()) if path.exists() else []
            return self.send(200, json.dumps(page(diffs, query)).encode())
        release = self.server.state / url.path[1:]
        if url.path.startswith("/download/") and release.is_file():
            return self.send(200, release.read_bytes(), "application/octet-stream")
        self.send(404)


def page(items, query):
    number, size = int(query["page"][0]), int(query["per_page"][0])
    return items[(number - 1) * size : number * size]
