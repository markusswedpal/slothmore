"""Menu bar quick panel: the pure helpers in macos/quickpanel.py and how menubar.py wires it (Cocoa is faked)."""
import importlib.util
import io
import json
import os
import sys
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
        self.assertEqual(set(args[3]), {"quit", "open_full", "open_url", "start_server",
                                        "panel_starting", "panel_ok", "panel_failed"})
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


    def test_main_reset_panel_clears_state(self):
        self.mb.write_panel_state(self.mb.DISABLED, reason="x")
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(self.mb.main(["--reset-panel"]), 0)
        self.assertEqual(self.mb.read_panel_state(), {})

    def test_load_quickpanel_once_per_process(self):
        sys.modules.pop("slothmore_quickpanel", None)
        try:
            self.assertIs(self.mb.load_quickpanel(), self.mb.load_quickpanel())
        finally:
            sys.modules.pop("slothmore_quickpanel", None)

    def test_version_matches_server(self):
        self.assertEqual(self.mb.VERSION, server.__version__)


# ---------------------------------------------------------------- crash guard (menubar.state)


class CrashGuardTests(unittest.TestCase):
    def setUp(self):
        self.mb = load("menubar_guard_under_test", "menubar.py")
        self.mb.clear_panel_state()
        self.addCleanup(self.mb.clear_panel_state)
        self.rumps = fake_rumps()
        self.rumps.notes = []
        self.rumps.notification = lambda *a, **k: self.rumps.notes.append(a)
        self.client = mock.Mock(base="http://127.0.0.1:7790")
        self.client.listing.return_value = {"items": [], "groups": []}

    def attach(self, fake_qp):
        app = self.mb.make_app(self.rumps, self.client, panel=True)
        err = io.StringIO()
        with mock.patch.object(self.mb, "load_quickpanel", return_value=fake_qp), redirect_stderr(err):
            app.attach_panel()
        return app, err.getvalue()

    def test_marker_lifecycle(self):
        fake_qp = mock.Mock(available=mock.Mock(return_value=True))
        app, _ = self.attach(fake_qp)
        hooks = fake_qp.attach.call_args[0][3]
        hooks["panel_starting"]()
        st = self.mb.read_panel_state()
        self.assertEqual((st["state"], st["version"]), ("starting-panel", self.mb.VERSION))
        self.assertEqual(oct(os.stat(self.mb.state_path()).st_mode & 0o777), "0o600")
        hooks["panel_ok"]()
        self.assertEqual(self.mb.read_panel_state(), {})

    def test_crash_while_opening_starts_classic_next_time(self):
        self.mb.write_panel_state(self.mb.STARTING)          # helper died before the first page loaded
        fake_qp = mock.Mock(available=mock.Mock(return_value=True))
        app, err = self.attach(fake_qp)
        fake_qp.attach.assert_not_called()
        self.assertIsNone(app.quick)
        self.assertIn("classic menu", err)
        self.assertIn("--reset-panel", err)
        self.assertEqual(self.mb.read_panel_state()["state"], "panel-disabled")
        # The next launch of the same version stays classic (no crash loop under the LaunchAgent) ...
        fake_qp2 = mock.Mock(available=mock.Mock(return_value=True))
        self.attach(fake_qp2)
        fake_qp2.attach.assert_not_called()
        # ... and the server's --status reports it.
        self.assertEqual(server.menubar_panel_state(), "off")

    def test_new_version_tries_the_panel_again(self):
        self.mb.write_panel_state(self.mb.DISABLED, reason="old crash")
        st = self.mb.read_panel_state()
        st["version"] = "0.0.1"
        with open(self.mb.state_path(), "w") as f:
            json.dump(st, f)
        fake_qp = mock.Mock(available=mock.Mock(return_value=True))
        self.attach(fake_qp)
        fake_qp.attach.assert_called_once()

    def test_caught_panel_error_is_not_a_crash(self):
        fake_qp = mock.Mock(available=mock.Mock(return_value=True))
        app, _ = self.attach(fake_qp)
        hooks = fake_qp.attach.call_args[0][3]
        hooks["panel_starting"]()
        hooks["panel_failed"]("navigation start")
        self.assertIsNone(app.quick)
        self.assertEqual(self.mb.read_panel_state(), {})
        self.assertEqual(self.rumps.notes[0][1], "Quick panel turned off")

    def test_clean_quit_clears_marker(self):
        fake_qp = mock.Mock(available=mock.Mock(return_value=True))
        app, _ = self.attach(fake_qp)
        hooks = fake_qp.attach.call_args[0][3]
        hooks["panel_starting"]()
        hooks["quit"]()
        self.assertEqual(self.mb.read_panel_state(), {})
        self.assertEqual(self.rumps.quit_calls, [1])

    def test_unexpected_attach_error_logs_traceback_and_keeps_menu(self):
        fake_qp = mock.Mock(available=mock.Mock(return_value=True), attach=mock.Mock(side_effect=RuntimeError("boom")))
        app, err = self.attach(fake_qp)
        self.assertIsNone(app.quick)
        self.assertIn("Traceback", err)
        self.assertIn("classic menu", err)

    def test_server_reset_panel(self):
        self.mb.write_panel_state(self.mb.DISABLED)
        out = io.StringIO()
        with mock.patch.object(server, "IS_MAC", False), mock.patch("sys.stdout", out):
            self.assertEqual(server.cmd_menubar(["--reset-panel"]), 0)
        self.assertEqual(server.menubar_panel_state(), "on")

    def test_install_login_keeps_classic_and_hotkey(self):
        captured = {}
        with mock.patch.object(server, "IS_MAC", True), mock.patch("server.rumps_available", return_value=True), \
                mock.patch("server.install_menubar_login",
                           side_effect=lambda: captured.update(server.menubar_plist_data()["EnvironmentVariables"])), \
                mock.patch.dict(os.environ, {}), mock.patch("sys.stdout", io.StringIO()):
            os.environ.pop("SLOTHMORE_MENUBAR_CLASSIC", None)
            os.environ.pop("SLOTHMORE_HOTKEY", None)
            self.assertEqual(server.cmd_menubar(["--install-login", "--classic", "--hotkey", "alt+space"]), 0)
        self.assertEqual(captured.get("SLOTHMORE_MENUBAR_CLASSIC"), "1")
        self.assertEqual(captured.get("SLOTHMORE_HOTKEY"), "alt+space")


# ---------------------------------------------------------------- the Cocoa part, against fake PyObjC modules


class Rec:
    """Stand-in for an Objective-C object: every method call is recorded; init* returns self."""
    responds = ()

    def __init__(self, **ret):
        self.calls, self.ret = [], dict(ret)

    @classmethod
    def alloc(cls):
        return cls()

    def respondsToSelector_(self, name):
        return name in self.responds

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, args))
            if name.startswith("init"):
                return self
            r = self.ret.get(name)
            if isinstance(r, BaseException):
                raise r
            return r(*args) if callable(r) else r
        return method

    def called(self, name):
        return [a for n, a in self.calls if n == name]


def fake_cocoa():
    """Minimal objc/AppKit/Foundation/WebKit modules. CGColor does not exist on purpose."""
    objc = types.ModuleType("objc")

    class nosuchclass_error(Exception):
        pass
    objc.nosuchclass_error = nosuchclass_error
    objc.registry = {}

    def lookUpClass(name):
        if name in objc.registry:
            return objc.registry[name]
        raise nosuchclass_error(name)
    objc.lookUpClass = lookUpClass
    objc.protocolNamed = lambda n: "<protocol %s>" % n

    class NSObject(Rec):
        def __init_subclass__(cls, protocols=(), **kw):
            super().__init_subclass__(**kw)
            cls.protocols = list(protocols)
            objc.registry[cls.__name__] = cls

    class NSPanel(NSObject):
        pass

    class NSColor:
        @staticmethod
        def clearColor():
            return "clear"

    class NSView(Rec):
        def __init__(self):
            super().__init__(bounds="BOUNDS")
            self.the_layer = Rec()
            self.ret["layer"] = self.the_layer

    class WKWebView(Rec):
        responds = ("setUnderPageBackgroundColor:",)
        last = None

        def __init__(self):
            super().__init__()
            WKWebView.last = self

    class WKWebViewConfiguration(Rec):
        def __init__(self):
            super().__init__()
            self.ucc = Rec()
            self.ret["userContentController"] = self.ucc

    app = Rec(mainMenu=None, currentEvent=None)
    app.responds = ("activate",)
    monitors = []

    class NSEvent:
        fail_global = False

        @classmethod
        def addGlobalMonitorForEventsMatchingMask_handler_(cls, mask, handler):
            if cls.fail_global:
                raise TypeError("no block signature")
            monitors.append(("global", mask, handler))
            return "mon%d" % len(monitors)

        @classmethod
        def addLocalMonitorForEventsMatchingMask_handler_(cls, mask, handler):
            monitors.append(("local", mask, handler))
            return "mon%d" % len(monitors)

        removed = []

        @classmethod
        def removeMonitor_(cls, m):
            cls.removed.append(m)

    class NSApplication:
        @staticmethod
        def sharedApplication():
            return app

    def rect(*a):
        return a

    AppKit = types.ModuleType("AppKit")
    AppKit.__dict__.update(NSPanel=NSPanel, NSColor=NSColor, NSView=NSView, NSEvent=NSEvent, NSApplication=NSApplication,
                           NSMakeRect=rect, NSBackingStoreBuffered=2, NSMenu=Rec, NSMenuItem=Rec,
                           NSScreen=types.SimpleNamespace(mainScreen=lambda: None))
    Foundation = types.ModuleType("Foundation")

    class NSURL:
        @staticmethod
        def URLWithString_(s):
            return ("NSURL", s)

    class NSURLRequest:
        @staticmethod
        def requestWithURL_(u):
            return ("REQ", u)
    Foundation.__dict__.update(NSObject=NSObject, NSURL=NSURL, NSURLRequest=NSURLRequest)
    WebKit = types.ModuleType("WebKit")
    WebKit.__dict__.update(WKWebView=WKWebView, WKWebViewConfiguration=WKWebViewConfiguration)
    mods = {"objc": objc, "AppKit": AppKit, "Foundation": Foundation, "WebKit": WebKit}
    return mods, types.SimpleNamespace(app=app, monitors=monitors, NSEvent=NSEvent, WKWebView=WKWebView, objc=objc)


class FakeStatusItem(Rec):
    def __init__(self, fail_button=False):
        super().__init__()
        self.menu_obj = "CLASSIC-MENU"
        self.btn = Rec()
        if fail_button:
            self.btn.ret["setAction_"] = RuntimeError("no button")
        self.ret["button"] = self.btn

    def menu(self):
        return self.menu_obj

    def setMenu_(self, m):
        self.calls.append(("setMenu_", (m,)))
        self.menu_obj = m


class CocoaPanelTests(unittest.TestCase):
    def setUp(self):
        mods, self.fx = fake_cocoa()
        patcher = mock.patch.dict(sys.modules, mods)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.qp = load("quickpanel_cocoa_under_test", "quickpanel.py")
        self.hooks = {k: mock.Mock() for k in ("quit", "open_full", "open_url", "start_server",
                                               "panel_starting", "panel_ok", "panel_failed")}
        self.item = FakeStatusItem()
        self.err = io.StringIO()
        ctx = redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def make(self, **kw):
        return self.qp.attach(self.item, lambda: "http://127.0.0.1:7790", lambda: "tok", self.hooks, **kw)

    def test_no_decision_handler_and_no_cgcolor(self):
        src = open(str(server.HERE / "macos" / "quickpanel.py")).read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("decisionHandler_", code.replace("decisionHandler (", ""))
        self.assertNotIn(".CGColor(", code)
        self.assertNotIn("_block_", code)   # no timer/other block APIs called from Python
        p = self.make()
        bridge = type(p.bridge)
        self.assertFalse(hasattr(bridge, "webView_decidePolicyForNavigationAction_decisionHandler_"))
        self.assertIn("<protocol WKNavigationDelegate>", bridge.protocols)

    def test_construction_wires_icon_and_transparent_window(self):
        p = self.make()
        self.assertEqual(self.item.menu_obj, None)
        self.assertEqual(p.classic_menu, "CLASSIC-MENU")
        self.assertEqual(self.item.btn.called("setAction_"), [("statusClicked:",)])
        self.assertEqual(p.panel.called("setOpaque_"), [(False,)])
        self.assertEqual(p.panel.called("setBackgroundColor_"), [("clear",)])
        web = self.fx.WKWebView.last
        self.assertEqual(web.called("setValue_forKey_"), [(False, "drawsBackground")])
        self.assertEqual(web.called("setUnderPageBackgroundColor_"), [("clear",)])
        layer = p.panel.called("setContentView_")[0][0].the_layer
        self.assertEqual([n for n, _ in layer.calls], ["setCornerRadius_", "setMasksToBounds_"])
        self.assertTrue(type(p.panel).canBecomeKeyWindow(p.panel))

    def test_optional_selector_skipped_when_missing(self):
        self.fx.WKWebView.responds = ()
        self.make()
        self.assertEqual(self.fx.WKWebView.last.called("setUnderPageBackgroundColor_"), [])

    def test_wiring_failure_restores_classic_menu_and_raises(self):
        self.item = FakeStatusItem(fail_button=True)
        with self.assertRaises(RuntimeError):
            self.make()
        self.assertEqual(self.item.menu_obj, "CLASSIC-MENU")
        self.assertEqual(self.qp._CONTROLLER, [])

    def nav(self, p, url):
        web = Rec(URL=Rec(absoluteString=url) if url is not None else None)
        p.bridge.webView_didStartProvisionalNavigation_(web, None)
        return web

    def test_navigation_policy_without_blocks(self):
        p = self.make()
        p.loaded_base = "http://127.0.0.1:7790"
        self.assertEqual(self.nav(p, "http://127.0.0.1:7790/compact").called("stopLoading"), [])
        w = self.nav(p, "https://example.com/x")
        self.assertEqual(w.called("stopLoading"), [()])
        self.hooks["open_url"].assert_called_once_with("https://example.com/x")
        w = self.nav(p, "file:///etc/passwd")
        self.assertEqual(w.called("stopLoading"), [()])
        self.assertEqual(self.hooks["open_url"].call_count, 1)
        self.assertFalse(p.failed)

    def test_cancelled_load_is_ignored_and_real_failure_shows_offline(self):
        p = self.make()
        p.bridge.webView_didFailProvisionalNavigation_withError_(None, None, Rec(code=-999))
        self.assertEqual(p.web.called("loadHTMLString_baseURL_"), [])
        p.bridge.webView_didFailProvisionalNavigation_withError_(None, None, Rec(code=-1004))
        self.assertEqual(len(p.web.called("loadHTMLString_baseURL_")), 1)

    def test_exception_in_callback_is_caught_and_falls_back(self):
        p = self.make()
        p.bridge.userContentController_didReceiveScriptMessage_(None, Rec(body=ValueError("bad body")))
        self.assertTrue(p.failed)
        self.assertEqual(self.item.menu_obj, "CLASSIC-MENU")
        self.hooks["panel_failed"].assert_called_once_with("script message")
        self.assertIn("Traceback", self.err.getvalue())
        self.assertEqual(self.qp._CONTROLLER, [])
        # Later callbacks are no-ops instead of errors.
        p.bridge.statusClicked_(None)
        p.bridge.windowDidResignKey_(None)
        self.hooks["panel_failed"].assert_called_once()

    def test_show_marks_start_and_first_load_clears(self):
        p = self.make()
        win = Rec(convertRectToScreen_=lambda r: types.SimpleNamespace(
            origin=types.SimpleNamespace(x=1000, y=875), size=types.SimpleNamespace(width=24, height=25)))
        win.ret["screen"] = Rec(visibleFrame=types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0, y=0), size=types.SimpleNamespace(width=1440, height=875)))
        self.item.btn.ret["window"] = win
        p.panel.ret["isVisible"] = False
        p.bridge.statusClicked_(None)
        self.assertFalse(p.failed, self.err.getvalue())
        self.hooks["panel_starting"].assert_called_once()
        self.assertEqual(p.web.called("loadRequest_"), [(("REQ", ("NSURL", "http://127.0.0.1:7790/compact?token=tok")),)])
        self.assertEqual(self.fx.app.called("activate"), [()])
        self.assertEqual(p.web.called("evaluateJavaScript_completionHandler_")[0][1], None)
        p.bridge.webView_didFinishNavigation_(None, None)
        p.bridge.webView_didFinishNavigation_(None, None)
        self.hooks["panel_ok"].assert_called_once()
        # the click-outside handler is guarded
        kind, _, handler = self.fx.monitors[-1]
        self.assertEqual(kind, "global")
        p.panel.ret["isVisible"] = RuntimeError("gone")
        handler("event")
        self.assertTrue(p.failed)

    def test_missing_monitor_signature_does_not_break_show(self):
        self.fx.NSEvent.fail_global = True
        p = self.make()
        win = Rec(convertRectToScreen_=lambda r: types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0, y=875), size=types.SimpleNamespace(width=24, height=25)))
        win.ret["screen"] = Rec(visibleFrame=types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0, y=0), size=types.SimpleNamespace(width=1440, height=875)))
        self.item.btn.ret["window"] = win
        p.panel.ret["isVisible"] = False
        p.show()
        self.assertIsNone(p.monitor)
        self.assertFalse(p.failed)

    def test_retry_uses_perform_selector_not_a_block(self):
        p = self.make()
        p.bridge = Rec()
        p.on_message({"action": "startServer"})
        self.hooks["start_server"].assert_called_once()
        self.assertEqual(p.bridge.called("performSelector_withObject_afterDelay_"), [("retryLoad:", None, 2.5)])

    def test_hotkey_handlers_are_guarded(self):
        p = self.make(hotkey=(1 << 19, 49))
        handlers = {k: h for k, _, h in self.fx.monitors}
        bad = Rec(modifierFlags=RuntimeError("x"), keyCode=49)
        self.assertIs(handlers["local"](bad), bad)      # event passes through, no exception
        self.assertTrue(p.failed)
        self.assertEqual(set(self.fx.NSEvent.removed) >= {"mon1", "mon2"}, True)

    def test_second_load_reuses_objc_classes(self):
        self.make()
        qp2 = load("quickpanel_cocoa_again", "quickpanel.py")
        C = qp2._classes()
        self.assertIs(C["panel"], self.fx.objc.registry["SlothmoreQuickPanel"])


if __name__ == "__main__":
    unittest.main()
