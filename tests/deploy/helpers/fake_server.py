"""A fake GitLab API, alert channel and release host, in the test's own process."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT = "/api/v4/projects/monero-project%2Fccs-proposals"


class FakeServer:
    """Serve merge requests, the alert channel and releases from a state folder.

    A file named fail, fail-once, changes/<iid>.fail or alert.fail makes a request fail.
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
        if self.path == "/api/graphql":
            return self.graphql(json.loads(body)["variables"])
        if (self.server.state / "alert.fail").exists():
            return self.send(500)
        with open(self.server.state / "alerts.log", "ab") as log:
            log.write(body.replace(b"\n", b"|") + b"\n")
        self.send(200)

    def graphql(self, variables):
        """Answer mrs.py's query from mrs.json, 100 nodes a page."""
        if self.failed(f"/api/graphql after={variables['after']}"):
            return self.send(500)
        mrs = json.loads((self.server.state / "mrs.json").read_text())
        start = int(variables["after"] or 0)
        nodes = [
            {
                "iid": str(mr["iid"]),
                "title": mr.get("title"),
                "author": mr.get("author"),
                "webUrl": mr.get("web_url"),
                "createdAt": mr.get("created_at"),
                "upvotes": mr.get("upvotes", 0),
                "downvotes": mr.get("downvotes", 0),
                "diffHeadSha": mr.get("sha"),
            }
            for mr in mrs[start : start + 100]
        ]
        info = {"hasNextPage": start + 100 < len(mrs), "endCursor": str(start + 100)}
        found = {"pageInfo": info, "nodes": nodes}
        answer = {"data": {"project": {"mergeRequests": found}}}
        self.send(200, json.dumps(answer).encode())

    def failed(self, line):
        """Log the request, and return True if a fail file says it fails."""
        with open(self.server.state / "access.log", "a") as log:
            log.write(line + "\n")
        if (self.server.state / "fail").exists():
            return True
        if (self.server.state / "fail-once").exists():
            (self.server.state / "fail-once").unlink()
            return True
        return False

    def do_GET(self):
        if self.failed(self.path):
            return self.send(500)
        url = urlsplit(self.path)
        query = parse_qs(url.query)
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
