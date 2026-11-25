#!/usr/bin/env python3
"""Slothmore menu bar helper for macOS (optional).

Shows what is running (with CPU and memory), lets you open, restart or stop each item,
start stopped items and groups, and opens the dashboard.

Needs one extra package:   python3 -m pip install --user rumps
Run it by hand:            python3 macos/menubar.py [--url http://127.0.0.1:7777]
Start it at every login:   macos/install-login-item.sh install --with-menubar
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

DEFAULT_URL = os.environ.get("SLOTHMORE_URL") or os.environ.get("LAUNCHER_URL") or "http://127.0.0.1:7777"
MAX_STOPPED = 20  # stopped items listed under "Start"


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def call(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method, headers={"Origin": self.base})
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
        return {"title": "▸ !", "online": False, "running": [], "stopped": [], "groups": []}
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
    return {"title": "▸ %d" % n if n else "▸", "online": True, "running": running,
            "stopped": stopped[:MAX_STOPPED], "more": max(0, len(stopped) - MAX_STOPPED), "groups": groups}


def open_dashboard(base):
    # Prefer the installed web app (Dock app named "Slothmore", then a leftover "Launcher"); fall back to the default browser.
    for app in ("Slothmore", "Launcher"):
        if subprocess.call(["open", "-a", app], stderr=subprocess.DEVNULL) == 0:
            return
    subprocess.call(["open", base])


def make_app(rumps, client):
    class SlothmoreMenu(rumps.App):
        def __init__(self):
            super(SlothmoreMenu, self).__init__("▸", quit_button=None)
            self.signature = None
            self.refresh(None)

        def _safe(self, fn):
            def cb(_sender):
                try:
                    fn()
                except Exception as e:  # server down or error: show it, keep the helper alive
                    rumps.notification("Slothmore", "Action failed", str(e)[:200])
                self.signature = None
                self.refresh(None)
            return cb

        def rebuild(self, model):
            sig = json.dumps(model, sort_keys=True)
            if sig == self.signature:
                return
            self.signature = sig
            self.menu.clear()
            self.menu.add(rumps.MenuItem("Open Slothmore", callback=lambda _: open_dashboard(client.base)))
            self.menu.add(rumps.separator)
            if not model["online"]:
                self.menu.add(rumps.MenuItem("Slothmore server not reachable"))
            else:
                if not model["running"]:
                    self.menu.add(rumps.MenuItem("Nothing running"))
                for r in model["running"]:
                    sub = rumps.MenuItem(r["label"])
                    if "open" in r["actions"]:
                        sub.add(rumps.MenuItem("Open in browser", callback=self._safe(
                            lambda u=r["url"]: subprocess.call(["open", u]))))
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
            self.menu.add(rumps.MenuItem("Quit menu bar helper", callback=lambda _: rumps.quit_application()))
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
    ap.add_argument("--url", default=DEFAULT_URL, help="dashboard address (default %(default)s)")
    args = ap.parse_args(argv)
    try:
        import rumps  # noqa: F401
    except ImportError:
        sys.stderr.write("The menu bar helper needs rumps:  %s -m pip install --user rumps\n" % sys.executable)
        return 2
    if sys.platform != "darwin":
        sys.stderr.write("The menu bar helper only works on macOS.\n")
        return 2
    try:  # no Dock icon for this helper
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass
    make_app(rumps, Client(args.url)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
