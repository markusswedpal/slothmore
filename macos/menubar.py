#!/usr/bin/env python3
"""Slothmore menu bar helper for macOS (optional).

Click the icon: a floating quick panel (the server's /compact page in a WKWebView) with search,
running items, quick start, ports and AI models. Right-click or control-click: the classic menu, which
shows what is running (with CPU and memory), lets you open, restart or stop each item, start stopped
items and groups, and opens the dashboard. Without pyobjc-framework-WebKit (or with --classic) a click
shows the classic menu.

Homebrew:                  `slothmore menubar` (rumps is installed by the formula)
                           `slothmore menubar --install-login` starts it at every login
From source:               python3 -m pip install --user rumps, then python3 macos/menubar.py
                           or macos/install-login-item.sh install --with-menubar
The dashboard port is read from ~/.slothmore/server.json; --url overrides it.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

VERSION = "0.2.5"  # keep in sync with server.__version__ (a test checks it)

DEFAULT_URL = os.environ.get("SLOTHMORE_URL") or os.environ.get("LAUNCHER_URL") or "http://127.0.0.1:7777"
MAX_STOPPED = 20  # stopped items listed under "Start"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUMPS_HELP = ("The menu bar helper needs the Python package 'rumps'.\n"
              "  Homebrew install:  brew reinstall markusswedpal/tap/slothmore   (0.2.1 or newer installs it)\n"
              "  From source:       %s -m pip install --user rumps")


def home_dir():
    return os.environ.get("SLOTHMORE_HOME") or os.environ.get("LAUNCHER_HOME") or os.path.expanduser("~/.slothmore")


def read_token():
    """The dashboard's access token (written by the server to ~/.slothmore/token, mode 0600)."""
    try:
        with open(os.path.join(home_dir(), "token")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def read_port():
    """Port of the running server, from ~/.slothmore/server.json (None when absent)."""
    try:
        with open(os.path.join(home_dir(), "server.json")) as f:
            return int(json.load(f).get("port"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def server_command():
    """argv that starts the dashboard server in the background (same install as this helper)."""
    exe = os.environ.get("SLOTHMORE_BIN")
    if exe and os.path.isfile(exe):
        return [exe, "--no-open"]
    return [sys.executable, os.path.join(ROOT, "bin", "slothmore"), "--no-open"]


def start_server():
    """Start the server detached from this helper. Returns the Popen (argv list, no shell)."""
    return subprocess.Popen(server_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, shell=False)


class Client:
    def __init__(self, base=None, token=None):
        self._base = base.rstrip("/") if base else None
        self.token = token

    @property
    def base(self):
        """Explicit --url, else the port in server.json (the server may have moved), else the default."""
        if self._base:
            return self._base
        port = read_port()
        return "http://127.0.0.1:%d" % port if port else DEFAULT_URL.rstrip("/")

    def call(self, path, method="GET"):
        tok = self.token or read_token()  # re-read so `slothmore --reset-token` is picked up
        base = self.base
        headers = {"Origin": base}
        if tok:
            headers["Authorization"] = "Bearer " + tok
        req = urllib.request.Request(base + path, method=method, headers=headers)
        if method == "POST":
            req.data = b"{}"
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    def listing(self):
        return self.call("/api/items")

    def item(self, item_id, action):
        return self.call("/api/items/%s/%s" % (item_id, action), "POST")

    def group(self, group_id, action):
        return self.call("/api/groups/%s/%s" % (group_id, action), "POST")


def fmt_bytes(n):
    if not n:
        return "-"
    if n < 1024 * 1024:
        return "%d KB" % (n // 1024)
    if n < 1024 ** 3:
        return "%d MB" % (n // (1024 * 1024))
    return "%.1f GB" % (n / 1024.0 ** 3)


def build_model(listing):
    """Pure description of the menu, so it can be tested without macOS.

    Returns {"title": str, "running": [...], "stopped": [...], "groups": [...]}; each entry
    has a label and the actions it offers.
    """
    if listing is None:
        return {"title": "!", "online": False, "running": [], "stopped": [], "groups": []}
    items = listing.get("items") or []
    running, stopped = [], []
    for it in items:
        if it.get("running"):
            h = it.get("health") or {}
            stats = ""
            if h:
                stats = "  ·  %.0f%% · %s" % (h.get("cpu") or 0, fmt_bytes(h.get("rss")))
            flag = "  ⏳" if it.get("prompt") else ""
            actions = (["open"] if it.get("url") else []) + ["restart", "stop"]
            running.append({"id": it["id"], "label": "● %s%s%s" % (it["name"], stats, flag),
                            "url": it.get("url"), "actions": actions})
        elif it.get("startable"):
            stopped.append({"id": it["id"], "label": it["name"], "pinned": it.get("source") == "pinned",
                            "actions": ["start"]})
    stopped.sort(key=lambda x: (not x["pinned"], x["label"].lower()))
    groups = []
    for g in listing.get("groups") or []:
        groups.append({"id": g["id"], "label": "%s  (%d/%d)" % (g["name"], g.get("running", 0), g.get("total", 0)),
                       "actions": ["start", "restart", "stop"]})
    n = len(running)
    return {"title": ("%d" % n) if n else "", "online": True, "running": running,
            "stopped": stopped[:MAX_STOPPED], "more": max(0, len(stopped) - MAX_STOPPED), "groups": groups}


def dashboard_url(base, token=None, add=None):
    """Signed-in dashboard URL, optionally opening the add-folder dialog (?add=watch or ?add=pin)."""
    q = []
    if token:
        q.append("token=" + token)
    if add in ("watch", "pin"):
        q.append("add=" + add)
    return base.rstrip("/") + "/" + (("?" + "&".join(q)) if q else "")


def open_dashboard(base, add=None):
    # Prefer the installed web app (Dock app named "Slothmore", then a leftover "Launcher"); fall back to the default browser.
    # The installed web app keeps its sign-in cookie; the browser gets the token once in the URL.
    # When opening the add-folder flow we pass the URL so the dialog can open.
    tok = read_token()
    url = dashboard_url(base, token=tok, add=add)
    if add in ("watch", "pin"):
        for app in ("Slothmore", "Launcher"):
            if subprocess.call(["open", "-a", app, url], stderr=subprocess.DEVNULL) == 0:
                return
        subprocess.call(["open", url])
        return
    for app in ("Slothmore", "Launcher"):
        if subprocess.call(["open", "-a", app], stderr=subprocess.DEVNULL) == 0:
            return
    subprocess.call(["open", url])


def load_quickpanel():
    """macos/quickpanel.py, loaded by path once per process (this file is itself loaded by path from `slothmore menubar`).

    Loaded once because its Objective-C class names are global to the process."""
    name = "slothmore_quickpanel"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickpanel.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


# --------------------------------------------------------------------------- crash guard for the quick panel
# Before the panel is shown for the first time the helper writes ~/.slothmore/menubar.state = starting-panel,
# and clears it once the first page has loaded. If the helper dies in between (a native crash Python can't
# catch), the LaunchAgent restarts it, it finds the marker and starts with the classic menu instead of
# crash-looping. The panel stays off for this version; upgrading or `slothmore menubar --reset-panel` retries it.

STATE_FILE = "menubar.state"
STARTING, DISABLED = "starting-panel", "panel-disabled"


def state_path():
    return os.path.join(home_dir(), STATE_FILE)


def read_panel_state():
    try:
        with open(state_path()) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_panel_state(state, **extra):
    st = dict(extra, state=state, version=VERSION, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)
    except OSError as e:
        log("could not write %s: %s" % (path, e))


def clear_panel_state():
    try:
        os.remove(state_path())
    except FileNotFoundError:
        pass
    except OSError as e:
        log("could not remove %s: %s" % (state_path(), e))


def panel_blocked():
    """Reason the quick panel must not be tried in this launch, or None. Turns a leftover marker into 'disabled'."""
    st = read_panel_state()
    if st.get("state") == STARTING:
        write_panel_state(DISABLED, reason="the helper stopped while opening the quick panel (%s)" % st.get("at", "?"),
                          crashed_version=st.get("version"))
        return "the last launch stopped while opening the quick panel"
    if st.get("state") == DISABLED and st.get("version") == VERSION:
        return st.get("reason") or "the quick panel was turned off after a crash"
    return None


def panel_state_summary():
    st = read_panel_state()
    if st.get("state") == DISABLED and st.get("version") == VERSION:
        return "off after a crash (%s); `slothmore menubar --reset-panel` turns it back on" % st.get("at", "?")
    if st.get("state") == STARTING:
        return "was opening when the helper last stopped; the next launch uses the classic menu"
    return "on"


def log(msg):
    try:
        sys.stderr.write("[menubar %s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        sys.stderr.flush()
    except Exception:
        pass


def open_url(url):
    """Open an http(s) page in the default browser (argv list, no shell)."""
    if str(url).startswith(("http://", "https://")):
        subprocess.call(["open", url])


def want_panel(classic=False, env=None):
    env = os.environ if env is None else env
    return not classic and env.get("SLOTHMORE_MENUBAR_CLASSIC", "") not in ("1", "true", "yes")


def make_app(rumps, client, panel=True, hotkey=None):
    class SlothmoreMenu(rumps.App):
        def __init__(self):
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "menubarTemplate.png")
            super(SlothmoreMenu, self).__init__(
                "", icon=icon if os.path.isfile(icon) else None, template=True, quit_button=None)
            self.signature = None
            self.quick = None
            self.refresh(None)
            if panel:
                rumps.events.before_start.register(self.attach_panel)

        def attach_panel(self):
            """Swap the menu click for the quick panel. Any failure keeps the classic menu."""
            blocked = panel_blocked()
            if blocked:
                log("Quick panel off: %s. Using the classic menu; run `slothmore menubar --reset-panel` to try it again." % blocked)
                return
            try:
                qp = load_quickpanel()
                if not qp.available():
                    sys.stderr.write("Quick panel needs pyobjc-framework-WebKit; using the classic menu.\n")
                    return
                self.quick = qp.attach(
                    self._nsapp.nsstatusitem, lambda: client.base, read_token, self.panel_hooks(),
                    hotkey=qp.parse_hotkey(hotkey) if hotkey else None)
            except ImportError as e:  # WebKit/pyobjc missing: the rumps menu still works
                sys.stderr.write("Quick panel unavailable (%s: %s); using the classic menu.\n" % (type(e).__name__, e))
                self.quick = None
            except Exception as e:  # any other Cocoa problem: keep the details in the log
                sys.stderr.write("Quick panel unavailable (%s: %s); using the classic menu.\n%s" % (
                    type(e).__name__, e, traceback.format_exc()))
                self.quick = None

        def panel_hooks(self):
            def quit_helper():
                clear_panel_state()  # a clean quit is not a crash
                rumps.quit_application()

            def failed(where):
                self.quick = None
                clear_panel_state()  # the error was caught: not a crash, so the next launch may try again
                try:
                    rumps.notification("Slothmore", "Quick panel turned off",
                                       "Using the classic menu. Details: ~/.slothmore/menubar.log")
                except Exception:
                    pass

            return {"quit": quit_helper, "open_full": lambda add=None: open_dashboard(client.base, add=add),
                    "open_url": open_url, "start_server": start_server,
                    "panel_starting": lambda: write_panel_state(STARTING),
                    "panel_ok": clear_panel_state, "panel_failed": failed}

        def _quit(self, _sender):
            if read_panel_state().get("state") == STARTING:
                clear_panel_state()  # a clean quit is not a crash
            rumps.quit_application()

        def _safe(self, fn):
            def cb(_sender):
                try:
                    fn()
                except Exception as e:  # server down or error: show it, keep the helper alive
                    rumps.notification("Slothmore", "Action failed", str(e)[:200])
                self.signature = None
                self.refresh(None)
            return cb

        def _start_server(self, _sender):
            try:
                start_server()
                rumps.notification("Slothmore", "Starting", "The dashboard server is starting.")
            except Exception as e:
                rumps.notification("Slothmore", "Could not start", str(e)[:200])
            self.signature = None

        def rebuild(self, model):
            sig = json.dumps(model, sort_keys=True)
            if sig == self.signature:
                return
            self.signature = sig
            self.menu.clear()
            self.menu.add(rumps.MenuItem("Open Slothmore", callback=lambda _: open_dashboard(client.base)))
            self.menu.add(rumps.separator)
            if not model["online"]:
                self.menu.add(rumps.MenuItem("Slothmore is not running"))
                self.menu.add(rumps.MenuItem("Start Slothmore", callback=self._start_server))
            else:
                if not model["running"]:
                    self.menu.add(rumps.MenuItem("Nothing running"))
                for r in model["running"]:
                    sub = rumps.MenuItem(r["label"])
                    if "open" in r["actions"]:
                        sub.add(rumps.MenuItem("Open in browser", callback=self._safe(lambda u=r["url"]: open_url(u))))
                    sub.add(rumps.MenuItem("Restart", callback=self._safe(lambda i=r["id"]: client.item(i, "restart"))))
                    sub.add(rumps.MenuItem("Stop", callback=self._safe(lambda i=r["id"]: client.item(i, "stop"))))
                    self.menu.add(sub)
                if model["groups"]:
                    self.menu.add(rumps.separator)
                    gmenu = rumps.MenuItem("Groups")
                    for g in model["groups"]:
                        sub = rumps.MenuItem(g["label"])
                        for act, label in (("start", "Start all"), ("restart", "Restart all"), ("stop", "Stop all")):
                            sub.add(rumps.MenuItem(label, callback=self._safe(
                                lambda i=g["id"], a=act: client.group(i, a))))
                        gmenu.add(sub)
                    self.menu.add(gmenu)
                if model["stopped"]:
                    smenu = rumps.MenuItem("Start")
                    for s in model["stopped"]:
                        smenu.add(rumps.MenuItem(s["label"], callback=self._safe(lambda i=s["id"]: client.item(i, "start"))))
                    if model.get("more"):
                        smenu.add(rumps.MenuItem("… %d more in the dashboard" % model["more"]))
                    self.menu.add(smenu)
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Quit menu bar helper", callback=self._quit))
            self.title = model["title"]

        @rumps.timer(4)
        def refresh(self, _):
            try:
                listing = client.listing()
            except Exception:
                listing = None
            self.rebuild(build_model(listing))

    return SlothmoreMenu()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Slothmore menu bar helper (macOS)")
    ap.add_argument("--url", default=None, help="dashboard address (default: port from ~/.slothmore/server.json, else %s)" % DEFAULT_URL)
    ap.add_argument("--classic", action="store_true", help="click shows the classic menu, not the quick panel")
    ap.add_argument("--hotkey", default=os.environ.get("SLOTHMORE_HOTKEY") or None,
                    help="global shortcut for the quick panel, e.g. alt+space (off by default)")
    ap.add_argument("--reset-panel", action="store_true",
                    help="turn the quick panel back on after the crash guard switched it off, then exit")
    args = ap.parse_args(argv)
    if args.reset_panel:
        clear_panel_state()
        print("Quick panel re-enabled. Restart the menu bar helper (or log out and in) to use it.")
        return 0
    if args.hotkey:
        try:
            load_quickpanel().parse_hotkey(args.hotkey)
        except ValueError as e:
            sys.stderr.write("--hotkey: %s\n" % e)
            return 2
    try:
        import rumps  # noqa: F401
    except ImportError:
        sys.stderr.write(RUMPS_HELP % sys.executable + "\n")
        return 2
    if sys.platform != "darwin":
        sys.stderr.write("The menu bar helper only works on macOS.\n")
        return 2
    try:  # no Dock icon for this helper
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass
    make_app(rumps, Client(args.url), panel=want_panel(args.classic), hotkey=args.hotkey).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
