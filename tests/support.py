"""Shared test setup: a private SLOTHMORE_HOME and the server module, imported once per test run."""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="slothmore-tests-")).resolve()
os.environ["SLOTHMORE_HOME"] = str(TMP / "home")
os.environ.pop("LAUNCHER_HOME", None)
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def make_tree(base, files):
    """Create a fixture folder: {relative path: text}."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return base


class LiveServer:
    """Runs the real Handler on a free port in a background thread."""

    def __init__(self, token="t" * 43):
        server.ensure_private_dirs()
        self.token = token
        self.httpd = server.make_server(0, token)
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, method="GET", body=None, auth="bearer", headers=None, raw_body=None):
        h = dict(headers or {})
        if auth == "bearer":
            h["Authorization"] = "Bearer " + self.token
        elif auth == "cookie":
            h["Cookie"] = "%s=%s" % (server.COOKIE_NAME, self.token)
        if method == "POST":
            h.setdefault("Origin", self.base)
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, dict(e.headers), e.read()
            finally:
                e.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None
