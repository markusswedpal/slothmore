"""HTTP-level tests: auth, env redaction, file permissions, request validation, CSP."""
import json
import os
import socket
import stat
import tempfile
import time
import unittest
from pathlib import Path

from support import LiveServer, make_tree, server


def mode(p):
    return stat.S_IMODE(os.stat(str(p)).st_mode)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()
        cls.tmp = Path(tempfile.mkdtemp(prefix="srv-"))

    @classmethod
    def tearDownClass(cls):
        server.stop_all()
        cls.srv.close()

    # ---- auth
    def test_api_requires_token(self):
        code, _, body = self.srv.request("/api/items", auth=None)
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")

    def test_wrong_token_rejected(self):
        code, _, _ = self.srv.request("/api/items", auth=None, headers={"Authorization": "Bearer nope"})
        self.assertEqual(code, 401)
        code, _, _ = self.srv.request("/api/items", auth=None,
                                      headers={"Cookie": server.COOKIE_NAME + "=nope"})
        self.assertEqual(code, 401)

    def test_post_requires_token(self):
        code, _, _ = self.srv.request("/api/settings", "POST", body={}, auth=None)
        self.assertEqual(code, 401)

    def test_stream_requires_token(self):
        code, _, _ = self.srv.request("/api/items/0123456789/stream", auth=None)
        self.assertEqual(code, 401)

    def test_cookie_and_bearer_work(self):
        self.assertEqual(self.srv.request("/api/items", auth="cookie")[0], 200)
        self.assertEqual(self.srv.request("/api/items", auth="bearer")[0], 200)

    def test_dashboard_page_needs_sign_in(self):
        code, headers, body = self.srv.request("/", auth=None)
        self.assertEqual(code, 401)
        self.assertIn(b"slothmore open", body)
        code, _, body = self.srv.request("/", auth="cookie")
        self.assertEqual(code, 200)
        self.assertIn(b'src="/app.js"', body)

    def test_static_assets_are_public(self):
        for path in ("/app.js", "/app.css", "/markdown.js", "/manifest.webmanifest", "/sw.js"):
            self.assertEqual(self.srv.request(path, auth=None)[0], 200, path)

    def test_token_link_sets_cookie_and_strips_token(self):
        code, headers, _ = self.srv.request("/?token=" + self.srv.token, auth=None)
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/")
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(server.COOKIE_NAME + "=" + self.srv.token, cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_bad_token_link(self):
        code, headers, body = self.srv.request("/?token=wrong", auth=None)
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn(b"not valid", body)

    def test_host_and_origin_checks_still_apply(self):
        code, _, _ = self.srv.request("/api/items", headers={"Host": "evil.example:80"})
        self.assertEqual(code, 403)
        code, _, _ = self.srv.request("/api/settings", "POST", body={}, headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)

    # ---- request validation (finding 8)
    def raw_post(self, content_length, body=b""):
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=10)
        req = ("POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: %s\r\n"
               "Authorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %s\r\n\r\n"
               % (self.srv.port, self.srv.base, self.srv.token, content_length)).encode() + body
        s.sendall(req)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return int(data.split(b" ", 2)[1])

    def test_bad_content_length(self):
        self.assertEqual(self.raw_post("abc"), 400)
        self.assertEqual(self.raw_post("-5"), 400)
        self.assertEqual(self.raw_post("99999999"), 413)

    def test_bad_json(self):
        self.assertEqual(self.raw_post("5", b"{nope"), 400)

    # ---- CSP and headers
    def test_csp_header(self):
        _, headers, _ = self.srv.request("/", auth="cookie")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline';", csp.split("script-src")[1].split(";")[0] + ";")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_no_inline_script_or_handlers(self):
        html = (server.STATIC / "index.html").read_text()
        import re
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html))
        self.assertIsNone(re.search(r"<style", html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))

    # ---- env redaction
    def pinned_item(self, name, files):
        d = make_tree(self.tmp / name, files)
        server.pin_path(str(d))
        iid = server.item_id(server.expand(str(d)))
        return iid

    def test_env_values_redacted(self):
        iid = self.pinned_item("envapp", {"run.sh": "echo hi\n"})
        code, _, _ = self.srv.request("/api/items/%s/prefs" % iid, "POST",
                                      body={"env_text": "API_KEY=s3cret-value\nDEBUG=1"})
        self.assertEqual(code, 200)
        code, _, body = self.srv.request("/api/items")
        self.assertEqual(code, 200)
        self.assertNotIn(b"s3cret-value", body)
        item = [i for i in json.loads(body)["items"] if i["id"] == iid][0]
        self.assertEqual([v["key"] for v in item["env_vars"]], ["API_KEY", "DEBUG"])
        self.assertTrue(all("value" not in v for v in item["env_vars"]))
        self.assertTrue(item["env_vars"][0]["has_value"])
        # the reveal endpoint needs auth and returns the value
        self.assertEqual(self.srv.request("/api/items/%s/env" % iid, auth=None)[0], 401)
        code, _, body = self.srv.request("/api/items/%s/env" % iid)
        self.assertEqual(json.loads(body)["env"], {"API_KEY": "s3cret-value", "DEBUG": "1"})

    def test_env_edit_keeps_unseen_values(self):
        iid = self.pinned_item("envkeep", {"run.sh": "echo hi\n"})
        self.srv.request("/api/items/%s/prefs" % iid, "POST", body={"env_text": "TOKEN=abc\nMODE=dev"})
        # a bare KEY line keeps the stored value
        self.srv.request("/api/items/%s/prefs" % iid, "POST", body={"env_text": "TOKEN\nMODE=prod\nNEW=1"})
        env = json.loads(self.srv.request("/api/items/%s/env" % iid)[2])["env"]
        self.assertEqual(env, {"TOKEN": "abc", "MODE": "prod", "NEW": "1"})
        # null in the dict form keeps it too
        self.srv.request("/api/items/%s/prefs" % iid, "POST", body={"env": {"TOKEN": None, "MODE": "x"}})
        env = json.loads(self.srv.request("/api/items/%s/env" % iid)[2])["env"]
        self.assertEqual(env, {"TOKEN": "abc", "MODE": "x"})
        # other prefs edits don't touch env
        self.srv.request("/api/items/%s/prefs" % iid, "POST", body={"args": "--verbose"})
        env = json.loads(self.srv.request("/api/items/%s/env" % iid)[2])["env"]
        self.assertEqual(env, {"TOKEN": "abc", "MODE": "x"})

    # ---- file permissions
    def test_config_and_home_permissions(self):
        server.save_config(server.load_config())
        self.assertEqual(mode(server.HOME_DIR), 0o700)
        self.assertEqual(mode(server.CONFIG_PATH), 0o600)

    def test_existing_files_fixed_at_startup(self):
        loose = server.HOME_DIR / "logs" / "old"
        loose.mkdir(parents=True, exist_ok=True)
        f = loose / "x.log"
        f.write_text("old")
        os.chmod(str(f), 0o644)
        os.chmod(str(loose), 0o755)
        os.chmod(str(server.HOME_DIR), 0o755)
        server.ensure_private_dirs()
        self.assertEqual(mode(f), 0o600)
        self.assertEqual(mode(loose), 0o700)
        self.assertEqual(mode(server.HOME_DIR), 0o700)

    def test_token_file(self):
        tok_path = server.TOKEN_PATH
        tok = server.load_token()
        self.assertEqual(mode(tok_path), 0o600)
        self.assertGreaterEqual(len(tok), 32)
        self.assertEqual(server.load_token(), tok)          # persists across restarts
        new = server.load_token(reset=True)                  # --reset-token
        self.assertNotEqual(new, tok)
        self.assertEqual(tok_path.read_text().strip(), new)

    def test_run_log_is_private(self):
        iid = self.pinned_item("runlog", {"run.sh": "echo hello-from-test\n"})
        code, _, body = self.srv.request("/api/items/%s/start" % iid, "POST", body={})
        self.assertEqual(code, 200, body)
        deadline = time.time() + 10
        logs = []
        while time.time() < deadline:
            logs = list((server.LOGS_DIR / iid).glob("*.log"))
            if logs and server.RUNS.get(iid) and not server.RUNS[iid].alive:
                break
            time.sleep(0.1)
        self.assertTrue(logs)
        self.assertEqual(mode(logs[0]), 0o600)
        self.assertEqual(mode(server.LOGS_DIR / iid), 0o700)
        hist = server.LOGS_DIR / iid / "history.json"
        if hist.exists():
            self.assertEqual(mode(hist), 0o600)


class CliTests(unittest.TestCase):
    def test_version(self):
        import subprocess
        import sys
        out = subprocess.run([sys.executable, str(server.HERE / "bin" / "slothmore"), "--version"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "slothmore " + server.__version__)


if __name__ == "__main__":
    unittest.main()
