"""Slothmore quick panel for the menu bar helper (macOS, optional).

Clicking the menu bar icon opens a borderless floating panel under the icon with a WKWebView
that shows the server's /compact page. Right-click (or control-click) still shows the classic menu.

Everything above the "Cocoa" line is plain Python so it can be tested on any OS. The Cocoa part
needs pyobjc-framework-Cocoa and pyobjc-framework-WebKit; if either is missing, `available()`
returns False and the helper keeps the classic rumps menu.
"""
import importlib.util
import time
import urllib.parse

PANEL_WIDTH = 420
PANEL_MIN_HEIGHT = 140
PANEL_MAX_HEIGHT = 600
PANEL_START_HEIGHT = 520
PANEL_GAP = 6          # points between the menu bar and the panel
SCREEN_MARGIN = 8      # keep the panel this far from the screen edges
CORNER_RADIUS = 14
BG_RGB = (0x0B, 0x10, 0x16)   # #0B1016, same as the page
REOPEN_GUARD = 0.3     # a click on the icon right after the panel closed itself should not reopen it
ACTIONS = ("close", "quit", "openFull", "openURL", "height", "startServer", "retry")

# --------------------------------------------------------------------------- pure helpers


def available():
    """True when PyObjC's AppKit and WebKit bindings are importable (no import side effects)."""
    try:
        return all(importlib.util.find_spec(m) is not None for m in ("AppKit", "WebKit", "objc"))
    except (ImportError, ValueError):
        return False


def compact_url(base, token=None):
    """URL the web view loads. The ?token= is swapped by the server for an HttpOnly cookie and stripped by a redirect."""
    url = base.rstrip("/") + "/compact"
    if token:
        url += "?token=" + urllib.parse.quote(token, safe="")
    return url


def clamp_height(h):
    try:
        h = int(round(float(h)))
    except (TypeError, ValueError):
        return PANEL_START_HEIGHT
    return max(PANEL_MIN_HEIGHT, min(PANEL_MAX_HEIGHT, h))


def panel_frame(button, visible, width=PANEL_WIDTH, height=PANEL_START_HEIGHT, gap=PANEL_GAP, margin=SCREEN_MARGIN):
    """Frame (x, y, w, h) for the panel in Cocoa screen coordinates (origin bottom-left).

    button  = (x, y, w, h) of the status item in screen coordinates
    visible = (x, y, w, h) of the screen's visibleFrame (excludes the menu bar and Dock)
    The panel is centred under the icon, kept inside the screen, and its top sits `gap` below the menu bar.
    """
    bx, by, bw, bh = button
    vx, vy, vw, vh = visible
    w = min(width, vw - 2 * margin)
    h = min(clamp_height(height), vh - 2 * margin)
    x = bx + bw / 2.0 - w / 2.0
    x = max(vx + margin, min(x, vx + vw - margin - w))
    top = min(by, vy + vh) - gap
    y = max(vy + margin, top - h)
    return (round(x), round(y), round(w), round(h))


def resize_keep_top(frame, height):
    """Same frame with a new height, top edge fixed (Cocoa y grows upwards)."""
    x, y, w, h = frame
    nh = clamp_height(height)
    return (x, y + h - nh, w, nh)


def should_open(now, last_hidden_at, guard=REOPEN_GUARD):
    """Clicking the icon makes the panel lose key focus and close itself first; don't reopen it on that same click."""
    return last_hidden_at is None or (now - last_hidden_at) > guard


def nav_decision(url, base):
    """'allow' for pages of this Slothmore server, 'external' for other http(s) pages (open in the browser), else 'block'."""
    try:
        u, b = urllib.parse.urlsplit(url or ""), urllib.parse.urlsplit(base)
    except ValueError:
        return "block"
    if u.scheme in ("about",) and url.startswith("about:blank"):
        return "allow"
    if u.scheme not in ("http", "https"):
        return "block"
    local = ("127.0.0.1", "localhost")
    try:
        same_port = u.port == b.port
    except ValueError:
        return "block"
    if (u.hostname or "") in local and (b.hostname or "") in local and same_port:
        return "allow"
    return "external"


def parse_message(body):
    """Validate a message posted by compact.js (window.webkit.messageHandlers.slothmore).

    Returns (action, value) or None for anything unexpected. Only http(s) URLs may be opened.
    """
    if not isinstance(body, dict):
        try:
            body = dict(body)
        except (TypeError, ValueError):
            return None
    action = str(body.get("action") or "")
    if action not in ACTIONS:
        return None
    if action == "openURL":
        url = str(body.get("url") or "")
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return None
        return ("openURL", url)
    if action == "height":
        return ("height", clamp_height(body.get("value")))
    return (action, None)


MODIFIERS = {"cmd": 1 << 20, "command": 1 << 20, "alt": 1 << 19, "option": 1 << 19, "opt": 1 << 19,
             "ctrl": 1 << 18, "control": 1 << 18, "shift": 1 << 17}
KEYCODES = {"space": 49, "return": 36, "enter": 36, "tab": 48, "escape": 53, "esc": 53, "`": 50, "/": 44, ".": 47, ",": 43, ";": 41}
KEYCODES.update({c: k for c, k in zip("asdfhgzxcv", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9))})
KEYCODES.update({"b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35,
                 "l": 37, "j": 38, "k": 40, "n": 45, "m": 46})
KEYCODES.update({"1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29})
MOD_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)


def parse_hotkey(spec):
    """'alt+space' -> (modifier_flags, keycode). None/''/'off' -> None. Needs at least one modifier."""
    if not spec or str(spec).strip().lower() in ("off", "none", "0", "false"):
        return None
    parts = [p.strip().lower() for p in str(spec).replace("-", "+").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("hotkey needs a modifier and a key, e.g. alt+space")
    mods = 0
    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError("unknown modifier %r (use cmd, alt, ctrl, shift)" % p)
        mods |= MODIFIERS[p]
    key = parts[-1]
    if key not in KEYCODES:
        raise ValueError("unknown key %r" % key)
    return (mods, KEYCODES[key])


def hotkey_matches(hotkey, flags, keycode):
    return bool(hotkey) and (flags & MOD_MASK) == hotkey[0] and keycode == hotkey[1]


def offline_html():
    """Shown in the panel when the server can't be reached (loaded as a string, not from the server)."""
    return ("<!doctype html><meta charset=utf-8><style>html,body{margin:0;background:#0B1016;color:#E8EEF5;"
            "font:13px/1.45 ui-monospace,Menlo,monospace}div{padding:22px 20px}b{color:#3EE0D4}"
            "button{font:inherit;margin:14px 8px 0 0;height:30px;padding:0 12px;border-radius:8px;border:1px solid #3EE0D4;"
            "background:#3EE0D4;color:#042321;cursor:pointer}button.g{background:transparent;color:#E8EEF5;border-color:#2a3a44}</style>"
            "<div><b>Slothmore isn't running.</b><br>Start the dashboard server to use the quick panel."
            "<br><button id=s>Start Slothmore</button><button class=g id=r>Retry</button><button class=g id=q>Quit</button></div>"
            "<script>const p=(a)=>window.webkit.messageHandlers.slothmore.postMessage({action:a});"
            "s.onclick=()=>p('startServer');r.onclick=()=>p('retry');q.onclick=()=>p('quit');"
            "document.onkeydown=(e)=>{if(e.key==='Escape')p('close');if(e.metaKey&&e.key==='q')p('quit');};"
            "window.webkit.messageHandlers.slothmore.postMessage({action:'height',value:150});</script>")


# --------------------------------------------------------------------------- Cocoa (macOS only)

_CLASSES = {}
_CONTROLLER = []


def _classes():
    """Define the Objective-C subclasses once per process."""
    if _CLASSES:
        return _CLASSES
    import objc
    import WebKit  # noqa: F401  (registers the WK* protocols)
    from AppKit import NSPanel
    from Foundation import NSObject

    class SlothmoreQuickPanel(NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

        def cancelOperation_(self, sender):
            if _CONTROLLER:
                _CONTROLLER[0].hide()

    protocols = [objc.protocolNamed(n) for n in ("WKScriptMessageHandler", "WKNavigationDelegate", "NSWindowDelegate")]

    class SlothmoreQuickPanelBridge(NSObject, protocols=protocols):
        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            if _CONTROLLER:
                _CONTROLLER[0].on_message(message.body())

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, webview, action, handler):
            url = action.request().URL()
            ok = _CONTROLLER[0].on_navigate(str(url.absoluteString()) if url else "") if _CONTROLLER else False
            handler(1 if ok else 0)  # WKNavigationActionPolicyAllow / Cancel

        def webView_didFailProvisionalNavigation_withError_(self, webview, nav, error):
            if _CONTROLLER:
                _CONTROLLER[0].show_offline()

        def windowDidResignKey_(self, note):
            if _CONTROLLER:
                _CONTROLLER[0].hide()

        def statusClicked_(self, sender):
            if _CONTROLLER:
                _CONTROLLER[0].status_clicked()

    _CLASSES.update(panel=SlothmoreQuickPanel, bridge=SlothmoreQuickPanelBridge)
    return _CLASSES


def install_edit_menu():
    """Without an Edit menu, ⌘C/⌘V/⌘A don't work in a web view text field. The menu stays invisible (accessory app)."""
    from AppKit import NSApp, NSMenu, NSMenuItem
    if NSApp.mainMenu() is not None:
        return
    main, app_item, edit_item = NSMenu.alloc().init(), NSMenuItem.alloc().init(), NSMenuItem.alloc().init()
    main.addItem_(app_item)
    app_item.setSubmenu_(NSMenu.alloc().init())
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    edit_item.setSubmenu_(edit)
    main.addItem_(edit_item)
    NSApp.setMainMenu_(main)


class QuickPanel:
    """Owns the NSPanel + WKWebView and the status item click. `hooks` supplies quit/open_full/open_url/start_server."""

    def __init__(self, status_item, base_fn, token_fn, hooks, hotkey=None):
        from AppKit import (NSApp, NSBackingStoreBuffered, NSColor, NSMakeRect, NSView)
        from WebKit import WKWebView, WKWebViewConfiguration
        C = _classes()
        _CONTROLLER[:] = [self]
        self.status_item, self.base_fn, self.token_fn, self.hooks = status_item, base_fn, token_fn, hooks
        self.last_hidden_at, self.loaded_base, self.monitor, self.hotkey_monitors = None, None, None, []
        self.bridge = C["bridge"].alloc().init()
        self.frame = (0, 0, PANEL_WIDTH, PANEL_START_HEIGHT)

        style = (1 << 7)  # NSWindowStyleMaskBorderless (0) | NSWindowStyleMaskNonactivatingPanel
        panel = C["panel"].alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_WIDTH, PANEL_START_HEIGHT), style, NSBackingStoreBuffered, False)
        panel.setLevel_(101)                      # NSPopUpMenuWindowLevel: above normal windows
        panel.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 8))  # all Spaces, transient, full-screen auxiliary
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setDelegate_(self.bridge)

        bg = NSColor.colorWithSRGBRed_green_blue_alpha_(BG_RGB[0] / 255.0, BG_RGB[1] / 255.0, BG_RGB[2] / 255.0, 1.0)
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_WIDTH, PANEL_START_HEIGHT))
        root.setWantsLayer_(True)
        root.layer().setCornerRadius_(CORNER_RADIUS)
        root.layer().setMasksToBounds_(True)
        root.layer().setBackgroundColor_(bg.CGColor())
        root.layer().setBorderWidth_(0.5)
        root.layer().setBorderColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.7, 0.86, 0.88, 0.22).CGColor())

        config = WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self.bridge, "slothmore")
        web = WKWebView.alloc().initWithFrame_configuration_(root.bounds(), config)
        web.setAutoresizingMask_(2 | 16)  # width + height sizable
        web.setNavigationDelegate_(self.bridge)
        try:
            web.setValue_forKey_(False, "drawsBackground")  # no white flash before the page paints
        except Exception:
            pass
        try:
            web.setUnderPageBackgroundColor_(bg)
        except Exception:
            pass
        root.addSubview_(web)
        panel.setContentView_(root)
        self.panel, self.web, self.NSApp = panel, web, NSApp
        install_edit_menu()

        # Left click opens the panel; right/control click shows the classic menu.
        self.classic_menu = status_item.menu()
        status_item.setMenu_(None)
        button = status_item.button()
        button.setTarget_(self.bridge)
        button.setAction_("statusClicked:")
        button.sendActionOn_((1 << 2) | (1 << 4))  # left + right mouse up
        if hotkey:
            self._install_hotkey(hotkey)

    # -- status item
    def status_clicked(self):
        ev = self.NSApp.currentEvent()
        right = ev is not None and (ev.type() == 4 or (ev.modifierFlags() & (1 << 18)))
        if right and self.classic_menu is not None:
            self.hide()
            self.status_item.setMenu_(self.classic_menu)
            self.status_item.button().performClick_(None)   # runs the menu modally
            self.status_item.setMenu_(None)
            return
        self.toggle()

    def toggle(self):
        if self.panel.isVisible():
            self.hide()
        elif should_open(time.monotonic(), self.last_hidden_at):
            self.show()

    def _button_rect(self):
        button = self.status_item.button()
        win = button.window()
        r = win.convertRectToScreen_(button.convertRect_toView_(button.bounds(), None))
        screen = win.screen()
        if screen is None:
            from AppKit import NSScreen
            screen = NSScreen.mainScreen()
        v = screen.visibleFrame()
        return (r.origin.x, r.origin.y, r.size.width, r.size.height), (v.origin.x, v.origin.y, v.size.width, v.size.height)

    def show(self):
        from AppKit import NSEvent, NSMakeRect
        base = self.base_fn()
        if self.loaded_base != base:
            self.load(base)
        btn, vis = self._button_rect()
        self.frame = panel_frame(btn, vis, height=self.frame[3])
        self.panel.setFrame_display_(NSMakeRect(*self.frame), True)
        self.NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.web)
        self.panel.invalidateShadow()
        self.web.evaluateJavaScript_completionHandler_("window.slothmoreShown && window.slothmoreShown()", None)
        if self.monitor is None:  # clicks in other apps close the panel
            self.monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_((1 << 1) | (1 << 3), lambda e: self.hide())

    def hide(self):
        from AppKit import NSEvent
        if self.monitor is not None:
            NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        if self.panel.isVisible():
            self.panel.orderOut_(None)
            self.last_hidden_at = time.monotonic()

    def load(self, base):
        from Foundation import NSURL, NSURLRequest
        self.loaded_base = base
        req = NSURLRequest.requestWithURL_(NSURL.URLWithString_(compact_url(base, self.token_fn())))
        self.web.loadRequest_(req)

    def show_offline(self):
        self.loaded_base = None  # reload from the server next time
        self.web.loadHTMLString_baseURL_(offline_html(), None)

    def set_height(self, h):
        from AppKit import NSMakeRect
        self.frame = resize_keep_top(self.frame, h)
        self.panel.setFrame_display_animate_(NSMakeRect(*self.frame), True, False)
        self.panel.invalidateShadow()

    # -- web view callbacks
    def on_navigate(self, url):
        d = nav_decision(url, self.loaded_base or self.base_fn())
        if d == "external":
            self.hooks["open_url"](url)
        return d == "allow"

    def on_message(self, body):
        msg = parse_message(body)
        if not msg:
            return
        action, value = msg
        if action == "close":
            self.hide()
        elif action == "height":
            self.set_height(value)
        elif action == "openURL":
            self.hide()
            self.hooks["open_url"](value)
        elif action == "openFull":
            self.hide()
            self.hooks["open_full"]()
        elif action == "quit":
            self.hide()
            self.hooks["quit"]()
        elif action == "startServer":
            self.hooks["start_server"]()
            self._retry_later(2.5)
        elif action == "retry":
            self.load(self.base_fn())

    def _retry_later(self, seconds):
        from Foundation import NSTimer
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(seconds, False, lambda t: self.load(self.base_fn()))

    # -- optional global hotkey (off by default; macOS asks for Accessibility/Input Monitoring permission)
    def _install_hotkey(self, hotkey):
        from AppKit import NSEvent

        def on_global(e):
            if hotkey_matches(hotkey, e.modifierFlags(), e.keyCode()):
                self.toggle_from_hotkey()

        def on_local(e):
            if hotkey_matches(hotkey, e.modifierFlags(), e.keyCode()):
                self.toggle_from_hotkey()
                return None
            return e
        mask = 1 << 10  # NSEventMaskKeyDown
        self.hotkey_monitors = [NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, on_global),
                                NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, on_local)]

    def toggle_from_hotkey(self):
        if self.panel.isVisible():
            self.hide()
        else:
            self.show()


def attach(status_item, base_fn, token_fn, hooks, hotkey=None):
    """Create the quick panel for a status item. Raises on any Cocoa/WebKit problem (caller falls back to the menu)."""
    return QuickPanel(status_item, base_fn, token_fn, hooks, hotkey=hotkey)
