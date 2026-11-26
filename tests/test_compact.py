"""The quick panel page (/compact): auth, CSP, static assets, and the "Kill all mine" endpoint."""
import re
import unittest
from unittest import mock

from support import LiveServer, server


class CompactPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_requires_auth(self):
        for path in ("/compact", "/compact.html"):
            code, _, body = self.srv.request(path, auth=None)
            self.assertEqual(code, 401, path)
            self.assertNotIn(b"/compact.js", body)

    def test_wrong_token_is_rejected(self):
        code, _, body = self.srv.request("/compact", auth=None, headers={"Authorization": "Bearer nope"})
        self.assertEqual(code, 401)
        self.assertNotIn(b"/compact.js", body)

    def test_renders_with_bearer_and_cookie(self):
        for auth in ("bearer", "cookie"):
            code, headers, body = self.srv.request("/compact", auth=auth)
            self.assertEqual(code, 200, auth)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            html = body.decode()
            self.assertIn('<script src="/compact.js"></script>', html)
            self.assertIn('href="/compact.css"', html)
            self.assertIn('id="q"', html)
            for el in ("refreshBtn", "fullBtn", "killBtn", "quitBtn", "list"):
                self.assertIn('id="%s"' % el, html)

    def test_token_handshake_sets_cookie_and_strips_token(self):
        code, headers, _ = self.srv.request("/compact?token=" + self.srv.token, auth=None)
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/compact")
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_csp_header(self):
        _, headers, _ = self.srv.request("/compact", auth="cookie")
        csp = headers.get("Content-Security-Policy", "")
        script = csp.split("script-src")[1].split(";")[0]
        self.assertIn("'self'", script)
        self.assertNotIn("unsafe-inline", script)
        self.assertNotIn("unsafe-eval", script)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_static_assets(self):
        for path, ctype in (("/compact.js", "javascript"), ("/compact.css", "text/css"), ("/icons/mark-64.png", "image/png"),
                            ("/icons/mark-128.png", "image/png")):
            code, headers, body = self.srv.request(path, auth="cookie")
            self.assertEqual(code, 200, path)
            self.assertIn(ctype, headers.get("Content-Type", ""), path)
            self.assertTrue(body)

    def test_html_is_csp_clean(self):
        html = (server.STATIC / "compact.html").read_text()
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), "inline <script>")
        self.assertIsNone(re.search(r"<style", html), "inline <style>")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), "inline event handler")
        self.assertNotIn("javascript:", html)
        self.assertNotIn("http://", html.replace("http-equiv", ""))

    def test_js_has_no_eval_and_uses_safe_stop(self):
        js = (server.STATIC / "compact.js").read_text()
        self.assertNotRegex(js, r"\beval\(|new Function\(|setTimeout\(\s*['\"]")
        self.assertIn('"/api/ports/stop"', js)
        self.assertIn("confirm: true", js)
        self.assertIn('"/api/runs/stop-all"', js)
        self.assertIn("messageHandlers", js)
        # Every dynamic value that reaches innerHTML goes through esc(); the name fields must.
        for field in ("esc(i.name)", "esc(name)", "esc(s.name)", "esc(r.name)"):
            self.assertIn(field, js)

    def test_css_style(self):
        css = (server.STATIC / "compact.css").read_text()
        self.assertIn("JetBrains Mono", css)
        self.assertIn("#0B1016", css)
        self.assertIn("max-height:600px", css)


class StopAllMineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_requires_auth(self):
        code, _, _ = self.srv.request("/api/runs/stop-all", "POST", {"confirm": True}, auth=None)
        self.assertEqual(code, 401)

    def test_requires_confirm(self):
        code, _, body = self.srv.request("/api/runs/stop-all", "POST", {})
        self.assertEqual(code, 409)
        self.assertIn(b"confirm_required", body)

    def test_stops_only_slothmore_runs(self):
        alive = mock.Mock(alive=True)
        alive.name = "web"
        dead = mock.Mock(alive=False)
        dead.name = "old"
        with mock.patch.dict(server.RUNS, {"a": alive, "b": dead}, clear=True), \
                mock.patch("server.stop_run") as stop, mock.patch("server.os.kill") as kill:
            code, _, body = self.srv.request("/api/runs/stop-all", "POST", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(server.json.loads(body), {"stopped": 1, "names": ["web"]})
        stop.assert_called_once_with(alive)
        kill.assert_not_called()

    def test_nothing_running(self):
        with mock.patch.dict(server.RUNS, {}, clear=True):
            code, _, body = self.srv.request("/api/runs/stop-all", "POST", {"confirm": True})
        self.assertEqual(code, 200)
        self.assertEqual(server.json.loads(body)["stopped"], 0)


if __name__ == "__main__":
    unittest.main()
