# Serves dist/ on port 4321 with the headers that nginx.conf sends, so a page
# that breaks the CSP breaks in the preview too. dev/preview.sh runs it.
import functools
import http.server
import re

with open("deploy/web/nginx.conf") as conf:
    HEADERS = re.findall(
        r'^\s*add_header (\S+) "?([^"]*?)"? always;$', conf.read(), re.M
    )
if "Content-Security-Policy" not in dict(HEADERS):
    raise SystemExit("deploy/web/nginx.conf sets no Content-Security-Policy")


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        for name, value in HEADERS:
            self.send_header(name, value)
        super().end_headers()


handler = functools.partial(Handler, directory="dist")
print("Serving dist/ on http://localhost:4321, Ctrl+C stops it", flush=True)
try:
    http.server.ThreadingHTTPServer(("", 4321), handler).serve_forever()
except KeyboardInterrupt:
    pass
