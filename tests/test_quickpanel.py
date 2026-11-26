"""Menu bar quick panel: the pure helpers in macos/quickpanel.py and how menubar.py wires it (Cocoa is faked)."""
import importlib.util
import io
import os
import types
import unittest
from contextlib import redirect_stderr
from unittest import mock

from support import server


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(server.HERE / "macos" / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qp = load("quickpanel_under_test", "quickpanel.py")


class HelperTests(unittest.TestCase):
    def test_compact_url_quotes_token(self):
        self.assertEqual(qp.compact_url("http://127.0.0.1:7777/"), "http://127.0.0.1:7777/compact")
        self.assertEqual(qp.compact_url("http://127.0.0.1:7790", "a+b/c=="), "http://127.0.0.1:7790/compact?token=a%2Bb%2Fc%3D%3D")

    def test_frame_is_centred_under_the_icon(self):
        # 1440x900 screen, menu bar 25pt, icon at x=1000..1024.
        x, y, w, h = qp.panel_frame((1000, 875, 24, 25), (0, 0, 1440, 875), height=500)
        self.assertEqual((w, h), (420, 500))
        self.assertEqual(x, round(1012 - 210))
        self.assertEqual(y + h, 875 - qp.PANEL_GAP)

    def test_frame_stays_on_screen(self):
        x, _, w, _ = qp.panel_frame((1420, 875, 20, 25), (0, 0, 1440, 875))
        self.assertEqual(x + w, 1440 - qp.SCREEN_MARGIN)
        x, _, _, _ = qp.panel_frame((2, 875, 20, 25), (0, 0, 1440, 875))
        self.assertEqual(x, qp.SCREEN_MARGIN)
        # Second display to the right of the first one.
        x, _, w, _ = qp.panel_frame((3000, 1415, 24, 25), (1440, 0, 2560, 1415))
        self.assertTrue(1440 <= x and x + w <= 4000)

    def test_frame_height_is_capped(self):
        _, y, _, h = qp.panel_frame((1000, 875, 24, 25), (0, 0, 1440, 875), height=5000)
        self.assertEqual(h, qp.PANEL_MAX_HEIGHT)
        _, y, _, h = qp.panel_frame((100, 400, 24, 22), (0, 0, 800, 400), height=600)  # tiny screen
        self.assertGreaterEqual(y, qp.SCREEN_MARGIN)
        self.assertLessEqual(h, 400 - 2 * qp.SCREEN_MARGIN)

    def test_resize_keeps_top_edge(self):
        x, y, w, h = qp.resize_keep_top((10, 300, 420, 500), 320)
        self.assertEqual((x, w, h), (10, 420, 320))
        self.assertEqual(y + h, 800)
        self.assertEqual(qp.resize_keep_top((0, 0, 420, 500), "junk")[3], qp.PANEL_START_HEIGHT)
        self.assertEqual(qp.clamp_height(10), qp.PANEL_MIN_HEIGHT)

    def test_icon_click_right_after_auto_close_does_not_reopen(self):
        self.assertTrue(qp.should_open(100.0, None))
        self.assertFalse(qp.should_open(100.1, 100.0))
        self.assertTrue(qp.should_open(101.0, 100.0))

    def test_navigation_policy(self):
        base = "http://127.0.0.1:7777"
        self.assertEqual(qp.nav_decision("http://127.0.0.1:7777/compact?token=x", base), "allow")
        self.assertEqual(qp.nav_decision("http://localhost:7777/", base), "allow")
        self.assertEqual(qp.nav_decision("about:blank", base), "allow")
        self.assertEqual(qp.nav_decision("http://127.0.0.1:5173/", base), "external")
        self.assertEqual(qp.nav_decision("https://example.com/", base), "external")
        self.assertEqual(qp.nav_decision("http://127.0.0.1.evil.com:7777/", base), "external")
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "slothmore://x", "", "http://127.0.0.1:99999/"):
            self.assertEqual(qp.nav_decision(bad, base), "block", bad)

    def test_parse_message(self):
        self.assertEqual(qp.parse_message({"action": "close"}), ("close", None))
        self.assertEqual(qp.parse_message({"action": "quit"}), ("quit", None))
        self.assertEqual(qp.parse_message({"action": "openFull"}), ("openFull", None))
        self.assertEqual(qp.parse_message({"action": "height", "value": 9999}), ("height", qp.PANEL_MAX_HEIGHT))
        self.assertEqual(qp.parse_message({"action": "openURL", "url": "http://localhost:8001"}), ("openURL", "http://localhost:8001"))
        self.assertIsNone(qp.parse_message({"action": "openURL", "url": "file:///Applications/Calculator.app"}))
        self.assertIsNone(qp.parse_message({"action": "rm -rf"}))
        self.assertIsNone(qp.parse_message("close"))
        self.assertIsNone(qp.parse_message(None))

    def test_hotkey(self):
        self.assertIsNone(qp.parse_hotkey(None))
        self.assertIsNone(qp.parse_hotkey("off"))
        self.assertEqual(qp.parse_hotkey("alt+space"), (1 << 19, 49))
        self.assertEqual(qp.parse_hotkey("Cmd+Shift+K"), ((1 << 20) | (1 << 17), 40))
        for bad in ("space", "hyper+space", "alt+f13"):
            with self.assertRaises(ValueError):
                qp.parse_hotkey(bad)
        hk = qp.parse_hotkey("alt+space")
        self.assertTrue(qp.hotkey_matches(hk, (1 << 19) | 0x100, 49))   # device-dependent low bits ignored
        self.assertFalse(qp.hotkey_matches(hk, (1 << 19) | (1 << 17), 49))
        self.assertFalse(qp.hotkey_matches(None, 1 << 19, 49))

    def test_offline_page_posts_only_known_actions(self):
        html = qp.offline_html()
        for a in ("startServer", "retry", "quit", "close", "height"):
            self.assertIn("'%s'" % a, html)
            self.assertIn(a, qp.ACTIONS)

    def test_available_is_a_bool(self):
        self.assertIsInstance(qp.available(), bool)
        with mock.patch.object(qp.importlib.util, "find_spec", return_value=None):
            self.assertFalse(qp.available())


# ---------------------------------------------------------------- menubar.py wiring, with a fake rumps


def fake_rumps():
    r = types.ModuleType("rumps")
    r.events = types.SimpleNamespace(before_start=types.SimpleNamespace(callbacks=[]))
    r.events.before_start.register = lambda fn: r.events.before_start.callbacks.append(fn) or fn
    r.separator = object()
    r.quit_calls = []
    r.quit_application = lambda *a: r.quit_calls.append(1)
    r.notification = lambda *a, **k: None

    class Menu(list):
        def add(self, x):
            self.append(x)

    class MenuItem:
        def __init__(self, title, callback=None):
            self.title, self.callback, self.items = title, callback, []

        def add(self, x):
            self.items.append(x)

    class App:
        def __init__(self, name, icon=None, template=None, quit_button=None):
            self.menu, self.title = Menu(), name
            self._nsapp = types.SimpleNamespace(nsstatusitem="STATUS-ITEM")

    r.App, r.MenuItem = App, MenuItem
    r.timer = lambda s: (lambda f: f)
    return r


class MenubarWiringTests(unittest.TestCase):
    def setUp(self):
        self.mb = load("menubar_qp_under_test", "menubar.py")
        self.rumps = fake_rumps()
        self.client = mock.Mock(base="http://127.0.0.1:7790")
        self.client.listing.return_value = {"items": [], "groups": []}

    def test_panel_registered_and_attached(self):
        app = self.mb.make_app(self.rumps, self.client, panel=True, hotkey="alt+space")
        self.assertEqual(len(self.rumps.events.before_start.callbacks), 1)
        fake_qp = mock.Mock(available=mock.Mock(return_value=True), parse_hotkey=qp.parse_hotkey)
        with mock.patch.object(self.mb, "load_quickpanel", return_value=fake_qp):
            self.rumps.events.before_start.callbacks[0]()
        args, kwargs = fake_qp.attach.call_args
        self.assertEqual(args[0], "STATUS-ITEM")
        self.assertEqual(args[1](), "http://127.0.0.1:7790")
        self.assertIs(args[2], self.mb.read_token)
        self.assertEqual(set(args[3]), {"quit", "open_full", "open_url", "start_server"})
        self.assertEqual(kwargs["hotkey"], (1 << 19, 49))
        args[3]["quit"]()
        self.assertEqual(self.rumps.quit_calls, [1])
        self.assertIs(app.quick, fake_qp.attach.return_value)

    def test_classic_menu_when_disabled(self):
        app = self.mb.make_app(self.rumps, self.client, panel=False)
        self.assertEqual(self.rumps.events.before_start.callbacks, [])
        self.assertIsNone(app.quick)
        self.assertTrue(any(getattr(i, "title", "") == "Open Slothmore" for i in app.menu))

    def test_falls_back_when_webkit_missing_or_broken(self):
        app = self.mb.make_app(self.rumps, self.client, panel=True)
        for fake in (mock.Mock(available=mock.Mock(return_value=False)),
                     mock.Mock(available=mock.Mock(return_value=True), attach=mock.Mock(side_effect=ImportError("WebKit")))):
            err = io.StringIO()
            with mock.patch.object(self.mb, "load_quickpanel", return_value=fake), redirect_stderr(err):
                app.attach_panel()
            self.assertIsNone(app.quick)
            self.assertIn("classic menu", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_want_panel(self):
        self.assertTrue(self.mb.want_panel(False, {}))
        self.assertFalse(self.mb.want_panel(True, {}))
        self.assertFalse(self.mb.want_panel(False, {"SLOTHMORE_MENUBAR_CLASSIC": "1"}))

    def test_open_url_only_http(self):
        with mock.patch.object(self.mb.subprocess, "call") as call:
            self.mb.open_url("http://localhost:8001/")
            self.mb.open_url("file:///etc/passwd")
            self.mb.open_url("-a Terminal")
        call.assert_called_once_with(["open", "http://localhost:8001/"])

    def test_bad_hotkey_is_a_clear_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = self.mb.main(["--hotkey", "hyper+space"])
        self.assertEqual(rc, 2)
        self.assertIn("--hotkey", err.getvalue())

    def test_cli_forwards_classic_and_hotkey(self):
        loaded = mock.Mock()
        loaded.main.return_value = 0
        with mock.patch.object(server, "IS_MAC", True), mock.patch("server.rumps_available", return_value=True), \
                mock.patch("importlib.util.module_from_spec", return_value=loaded), \
                mock.patch("importlib.util.spec_from_file_location", return_value=mock.Mock()):
            rc = server.cmd_menubar(["--classic", "--hotkey", "alt+space"])
        self.assertEqual(rc, 0)
        loaded.main.assert_called_once_with(["--classic", "--hotkey", "alt+space"])

    def test_login_item_keeps_hotkey_env(self):
        with mock.patch.dict(os.environ, {"SLOTHMORE_HOTKEY": "alt+space"}):
            env = server.menubar_plist_data()["EnvironmentVariables"]
        self.assertEqual(env.get("SLOTHMORE_HOTKEY"), "alt+space")


if __name__ == "__main__":
    unittest.main()
