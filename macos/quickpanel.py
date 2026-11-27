"""Slothmore quick panel for the menu bar helper (macOS, optional).

Clicking the menu bar icon opens a borderless floating panel under the icon with a WKWebView
that shows the server's /compact page. Right-click (or control-click) still shows the classic menu.

Everything above the "Cocoa" line is plain Python so it can be tested on any OS. The Cocoa part
needs pyobjc-framework-Cocoa and pyobjc-framework-WebKit; if either is missing, `available()`
returns False and the helper keeps the classic rumps menu.
"""
import importlib.util
import sys
import time
import traceback
import urllib.parse

PANEL_WIDTH = 420
PANEL_MIN_HEIGHT = 140
PANEL_MAX_HEIGHT = 600
PANEL_START_HEIGHT = 520
PANEL_GAP = 6          # points between the menu bar and the panel
SCREEN_MARGIN = 8      # keep the panel this far from the screen edges
CORNER_RADIUS = 14
REOPEN_GUARD = 0.3     # a click on the icon right after the panel closed itself should not reopen it
ACTIONS = ("close", "quit", "openFull", "openURL", "height", "startServer", "retry", "pickFolder")

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
    if action == "pickFolder":
        mode = str(body.get("mode") or "")
        if mode not in ("watch", "pin"):
            return None
        return ("pickFolder", mode)
    if action == "openFull":
        add = str(body.get("add") or "") or None
        if add not in (None, "watch", "pin"):
            return None
        return ("openFull", add)
    return (action, None)


def js_string(s):
    """A single-quoted JavaScript string literal (no raw newlines)."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\r", "\\r").replace("\n", "\\n") + "'"


def picked_js(mode, path):
    """JS the panel evaluates after a native folder pick. mode is 'watch' or 'pin'."""
    if mode not in ("watch", "pin") or not path:
        return None
    return "window.slothmorePicked && window.slothmorePicked(%s, %s)" % (js_string(mode), js_string(path))


def pick_folder_prompt(mode):
    if mode == "watch":
        return ("Add folder to watch", "Projects in this folder and its subfolders will show up in Slothmore.")
    return ("Add single project", "This folder will appear as one project.")


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
#
# Rules that keep this part crash-free under PyObjC. 0.2.1 aborted with
# "OC_PythonException: cannot call block without a signature": Python called the WKNavigationDelegate
# decisionHandler block, and PyObjC had no signature for it.
#  * Python never calls an Objective-C block. The navigation policy runs in
#    webView:didStartProvisionalNavigation: (no decisionHandler), and the retry timer uses
#    performSelector:withObject:afterDelay: instead of a timer block. Blocks we hand to Cocoa
#    (event monitor handlers) are plain Python callables wrapped in _guarded().
#  * No Core Graphics value (CGColorRef) crosses into Python. The window and the web view are
#    transparent and compact.css draws the rounded dark card and its border.
#  * Everything Cocoa calls into (delegate methods, the icon action, monitor handlers) runs inside
#    _guard(), which catches every Python exception. An exception that escapes into Cocoa's run loop
#    terminates the whole app. A caught exception turns the panel off for the rest of the session
#    and gives the icon its classic menu back.
#  * Optional selectors are checked with respondsToSelector_ before they are called.

NAV_CANCELLED = -999   # NSURLErrorCancelled: we stopped a navigation ourselves
MOUSE_DOWN_MASK = (1 << 1) | (1 << 3)   # NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown
KEY_DOWN_MASK = 1 << 10                 # NSEventMaskKeyDown
STYLE_MASK = 1 << 7                     # NSWindowStyleMaskBorderless (0) | NSWindowStyleMaskNonactivatingPanel
POPUP_LEVEL = 101                       # NSPopUpMenuWindowLevel
COLLECTION = (1 << 0) | (1 << 3) | (1 << 8)   # CanJoinAllSpaces | Transient | FullScreenAuxiliary (a valid combination)
AUTORESIZE_WH = 2 | 16                  # NSViewWidthSizable | NSViewHeightSizable

_CLASSES = {}
_CONTROLLER = []


def _log(msg):
    try:
        sys.stderr.write("[quick panel] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _guard(where, fn, *args, **kwargs):
    """Run fn; never let a Python exception reach Cocoa. On error: log the traceback and switch the panel off."""
    default = kwargs.pop("default", None)
    try:
        return fn(*args)
    except Exception:
        _log("error in %s; switching to the classic menu for this session.\n%s" % (where, traceback.format_exc()))
        ctl = _CONTROLLER[0] if _CONTROLLER else None
        if ctl is not None:
            ctl.fail(where)
        return default


def _guarded(where, fn, default=None):
    """fn wrapped by _guard, for callables handed to Cocoa (event monitor handlers)."""
    def wrapper(*args):
        return _guard(where, fn, *args, default=default)
    return wrapper


def _ctl(where, method, *args, **kwargs):
    """Call a QuickPanel method from an Objective-C callback (no-op once the panel is switched off)."""
    if not _CONTROLLER:
        return kwargs.get("default")
    return _guard(where, getattr(_CONTROLLER[0], method), *args, **kwargs)


def _responds(obj, name):
    try:
        return bool(obj.respondsToSelector_(name))
    except Exception:
        return False


def run_native_open_panel(mode):
    """Modal macOS folder picker. Returns a POSIX path, or None if the user cancelled.

    Uses NSOpenPanel.runModal (no completion-handler block) so PyObjC never has to call a block.
    """
    from AppKit import NSModalResponseOK, NSOpenPanel

    title, message = pick_folder_prompt(mode)
    panel = NSOpenPanel.openPanel()
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setCanCreateDirectories_(False)
    if _responds(panel, "setTitle:"):
        panel.setTitle_(title)
    if _responds(panel, "setMessage:"):
        panel.setMessage_(message)
    if _responds(panel, "setPrompt:"):
        panel.setPrompt_("Add")
    if panel.runModal() != NSModalResponseOK:
        return None
    url = panel.URL()
    path = url.path() if url is not None else None
    if not path:
        return None
    return str(path).rstrip("/")


def _classes():
    """Define the Objective-C subclasses once per process (the class names are global to the runtime)."""
    if _CLASSES:
        return _CLASSES
    import objc
    import WebKit  # noqa: F401  (registers the WK* protocols)
    from AppKit import NSPanel
    from Foundation import NSObject

    try:  # this module was loaded a second time in the same process: reuse the classes
        _CLASSES.update(panel=objc.lookUpClass("SlothmoreQuickPanel"), bridge=objc.lookUpClass("SlothmoreQuickPanelBridge"))
        return _CLASSES
    except objc.nosuchclass_error:
        pass

    class SlothmoreQuickPanel(NSPanel):
        # A borderless panel only takes keyboard focus (the search field) when it says it can.
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

        def cancelOperation_(self, sender):
            _ctl("Esc", "hide")

    names = ("WKScriptMessageHandler", "WKNavigationDelegate", "NSWindowDelegate")
    protocols = []
    for n in names:
        try:
            protocols.append(objc.protocolNamed(n))
        except Exception:
            _log("protocol %s not found; continuing without it" % n)

    class SlothmoreQuickPanelBridge(NSObject, protocols=protocols):
        # WKScriptMessageHandler: messages from compact.js
        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            _ctl("script message", "on_message_obj", message)

        # WKNavigationDelegate. Deliberately no webView:decidePolicyForNavigationAction:decisionHandler:
        # (calling its block from Python is what crashed 0.2.1).
        def webView_didStartProvisionalNavigation_(self, webview, nav):
            _ctl("navigation start", "on_provisional", webview)

        def webView_didReceiveServerRedirectForProvisionalNavigation_(self, webview, nav):
            _ctl("navigation redirect", "on_provisional", webview)

        def webView_didFinishNavigation_(self, webview, nav):
            _ctl("navigation finish", "on_loaded")

        def webView_didFailProvisionalNavigation_withError_(self, webview, nav, error):
            _ctl("navigation failure", "on_load_failed", error)

        def webView_didFailNavigation_withError_(self, webview, nav, error):
            _ctl("navigation failure", "on_load_failed", error, False)

        def webViewWebContentProcessDidTerminate_(self, webview):
            _ctl("web content process ended", "on_web_process_ended")

        # NSWindowDelegate: clicking anywhere else takes key focus away, which closes the panel
        def windowDidResignKey_(self, note):
            _ctl("panel lost focus", "hide")

        # Target/action of the status item button, and the delayed retry
        def statusClicked_(self, sender):
            _ctl("icon click", "status_clicked")

        def retryLoad_(self, sender):
            _ctl("retry", "retry")

    _CLASSES.update(panel=SlothmoreQuickPanel, bridge=SlothmoreQuickPanelBridge)
    return _CLASSES


def _shared_app():
    from AppKit import NSApplication
    return NSApplication.sharedApplication()


def install_edit_menu(app=None):
    """Without an Edit menu, ⌘C/⌘V/⌘A don't work in a web view text field. The menu stays invisible (accessory app)."""
    from AppKit import NSMenu, NSMenuItem
    app = app or _shared_app()
    if app.mainMenu() is not None:
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
    app.setMainMenu_(main)


class QuickPanel:
    """Owns the NSPanel + WKWebView and the status item click.

    hooks: quit, open_full, open_url, start_server (required) and, optionally, panel_starting (before the
    first show), panel_ok (first page shown) and panel_failed(where) (panel switched off after an error).
    """

    def __init__(self, status_item, base_fn, token_fn, hooks, hotkey=None):
        from AppKit import NSBackingStoreBuffered, NSColor, NSMakeRect, NSView
        from WebKit import WKWebView, WKWebViewConfiguration
        C = _classes()
        self.status_item, self.base_fn, self.token_fn, self.hooks = status_item, base_fn, token_fn, hooks
        self.last_hidden_at, self.loaded_base, self.monitor, self.hotkey_monitors = None, None, None, []
        self.failed, self.first_show_pending, self.classic_menu, self.wired = False, True, None, False
        self.picking = False  # True while NSOpenPanel is up, so losing key focus does not close us
        self.app = _shared_app()
        self.bridge = C["bridge"].alloc().init()
        self.frame = (0, 0, PANEL_WIDTH, PANEL_START_HEIGHT)

        panel = C["panel"].alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_WIDTH, PANEL_START_HEIGHT), STYLE_MASK, NSBackingStoreBuffered, False)
        panel.setLevel_(POPUP_LEVEL)
        panel.setCollectionBehavior_(COLLECTION)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())   # transparent window; compact.css draws the card
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setDelegate_(self.bridge)

        # Rounded clipping only (cornerRadius/masksToBounds are plain numbers/bools; no CGColor is used).
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_WIDTH, PANEL_START_HEIGHT))
        root.setWantsLayer_(True)
        layer = root.layer()
        if layer is not None:
            layer.setCornerRadius_(CORNER_RADIUS)
            layer.setMasksToBounds_(True)

        config = WKWebViewConfiguration.alloc().init()
        config.userContentController().addScriptMessageHandler_name_(self.bridge, "slothmore")
        web = WKWebView.alloc().initWithFrame_configuration_(root.bounds(), config)
        web.setAutoresizingMask_(AUTORESIZE_WH)
        web.setNavigationDelegate_(self.bridge)
        try:  # no white flash before the page paints (private KVC key; harmless if it ever goes away)
            web.setValue_forKey_(False, "drawsBackground")
        except Exception:
            _log("web view has no drawsBackground; the background may flash white")
        if _responds(web, "setUnderPageBackgroundColor:"):   # macOS 12+
            web.setUnderPageBackgroundColor_(NSColor.clearColor())
        root.addSubview_(web)
        panel.setContentView_(root)
        self.panel, self.web = panel, web
        try:
            install_edit_menu(self.app)
        except Exception:
            _log("could not add the Edit menu (copy/paste shortcuts may not work):\n" + traceback.format_exc())

        _CONTROLLER[:] = [self]
        self._wire_status_item()
        if hotkey:
            try:
                self._install_hotkey(hotkey)
            except Exception:
                _log("global shortcut unavailable:\n" + traceback.format_exc())

    def _wire_status_item(self):
        """Left click opens the panel; right/control click shows the classic menu. Restores the menu if wiring fails."""
        self.classic_menu = self.status_item.menu()
        try:
            button = self.status_item.button()
            button.setTarget_(self.bridge)
            button.setAction_("statusClicked:")
            button.sendActionOn_((1 << 2) | (1 << 4))  # NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp
            self.status_item.setMenu_(None)
            self.wired = True
        except Exception:
            _CONTROLLER[:] = []
            self._restore_classic()
            raise

    def _restore_classic(self):
        try:
            button = self.status_item.button()
            button.setAction_(None)
            button.setTarget_(None)
        except Exception:
            pass
        try:
            if self.classic_menu is not None:
                self.status_item.setMenu_(self.classic_menu)
        except Exception:
            pass

    def fail(self, where):
        """Switch the panel off for this session and give the icon its classic menu back. Never raises."""
        if self.failed:
            return
        self.failed = True
        if _CONTROLLER and _CONTROLLER[0] is self:
            _CONTROLLER[:] = []
        for m in [self.monitor] + list(self.hotkey_monitors):
            if m is not None:
                try:
                    from AppKit import NSEvent
                    NSEvent.removeMonitor_(m)
                except Exception:
                    pass
        self.monitor, self.hotkey_monitors = None, []
        try:
            self.panel.orderOut_(None)
        except Exception:
            pass
        self._restore_classic()
        cb = self.hooks.get("panel_failed")
        if cb:
            try:
                cb(where)
            except Exception:
                _log("panel_failed hook raised:\n" + traceback.format_exc())

    # -- status item
    def status_clicked(self):
        ev = self.app.currentEvent()
        right = ev is not None and (ev.type() == 4 or (ev.modifierFlags() & (1 << 18)))  # NSEventTypeRightMouseUp / ctrl
        if right and self.classic_menu is not None:
            self.hide()
            self.status_item.setMenu_(self.classic_menu)
            try:
                self.status_item.button().performClick_(None)   # runs the menu modally
            finally:
                self.status_item.setMenu_(None)
            return
        self.toggle()

    def toggle(self):
        if self.panel.isVisible():
            self.hide()
        elif should_open(time.monotonic(), self.last_hidden_at):
            self.show()

    def _button_rect(self):
        from AppKit import NSScreen
        button = self.status_item.button()
        win = button.window()
        r = win.convertRectToScreen_(button.convertRect_toView_(button.bounds(), None))
        screen = win.screen() or NSScreen.mainScreen()
        v = screen.visibleFrame()
        return (r.origin.x, r.origin.y, r.size.width, r.size.height), (v.origin.x, v.origin.y, v.size.width, v.size.height)

    def _activate(self):
        if _responds(self.app, "activate"):        # macOS 14+
            self.app.activate()
        else:
            self.app.activateIgnoringOtherApps_(True)

    def show(self):
        from AppKit import NSMakeRect
        if self.first_show_pending:
            cb = self.hooks.get("panel_starting")
            if cb:
                cb()
        base = self.base_fn()
        if self.loaded_base != base:
            self.load(base)
        btn, vis = self._button_rect()
        self.frame = panel_frame(btn, vis, height=self.frame[3])
        self.panel.setFrame_display_(NSMakeRect(*self.frame), True)
        self._activate()
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.web)
        self.panel.invalidateShadow()
        self.web.evaluateJavaScript_completionHandler_("window.slothmoreShown && window.slothmoreShown()", None)
        if self.monitor is None:  # clicks in other apps close the panel (losing key focus does too)
            try:
                from AppKit import NSEvent
                self.monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    MOUSE_DOWN_MASK, _guarded("outside click", lambda e: self.hide()))
            except Exception:
                _log("click-outside monitor unavailable (the panel still closes when it loses focus):\n" + traceback.format_exc())
                self.monitor = None

    def hide(self):
        if self.picking:
            return
        if self.monitor is not None:
            m, self.monitor = self.monitor, None
            try:
                from AppKit import NSEvent
                NSEvent.removeMonitor_(m)
            except Exception:
                pass
        if self.panel.isVisible():
            self.panel.orderOut_(None)
            self.last_hidden_at = time.monotonic()

    def load(self, base):
        from Foundation import NSURL, NSURLRequest
        url = NSURL.URLWithString_(compact_url(base, self.token_fn()))
        if url is None:
            self.show_offline()
            return
        self.loaded_base = base
        self.web.loadRequest_(NSURLRequest.requestWithURL_(url))

    def retry(self):
        self.load(self.base_fn())

    def show_offline(self):
        self.loaded_base = None  # reload from the server next time
        self.web.loadHTMLString_baseURL_(offline_html(), None)

    def set_height(self, h):
        from AppKit import NSMakeRect
        self.frame = resize_keep_top(self.frame, h)
        self.panel.setFrame_display_animate_(NSMakeRect(*self.frame), True, False)
        self.panel.invalidateShadow()

    # -- web view callbacks
    def on_provisional(self, webview):
        """Navigation policy without a decisionHandler: stop anything that isn't this server's page."""
        url = webview.URL()
        s = str(url.absoluteString()) if url is not None else ""
        d = nav_decision(s, self.loaded_base or self.base_fn())
        if d == "allow":
            return
        webview.stopLoading()   # the page that was showing stays; didFailProvisional reports "cancelled" (ignored)
        if d == "external":
            self.hooks["open_url"](s)

    def on_loaded(self):
        self.panel.invalidateShadow()
        if self.first_show_pending:
            self.first_show_pending = False
            cb = self.hooks.get("panel_ok")
            if cb:
                cb()

    def on_load_failed(self, error, provisional=True):
        code = error.code() if error is not None else 0
        if code == NAV_CANCELLED:
            return
        if provisional:
            self.show_offline()
        else:
            _log("page failed to load: %s" % (error.localizedDescription() if error is not None else "unknown error"))

    def on_web_process_ended(self):
        self.loaded_base = None   # reload on the next show
        if self.panel.isVisible():
            self.retry()

    def on_message_obj(self, message):
        self.on_message(message.body())

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
            self.hooks["open_full"](value)
        elif action == "pickFolder":
            self.pick_folder(value)
        elif action == "quit":
            self.hide()
            self.hooks["quit"]()
        elif action == "startServer":
            self.hooks["start_server"]()
            self._retry_later(2.5)
        elif action == "retry":
            self.retry()

    def _eval_js(self, script):
        # completionHandler is nil, so Python never calls an Objective-C block.
        if script:
            self.web.evaluateJavaScript_completionHandler_(script, None)

    def pick_folder(self, mode):
        """Show a native folder picker (NSOpenPanel.runModal, no completion-handler block)."""
        if mode not in ("watch", "pin"):
            return
        self.picking = True
        try:
            path = run_native_open_panel(mode)
            js = picked_js(mode, path)
            if js:
                self._eval_js(js)
        finally:
            self.picking = False
            try:
                self.panel.makeKeyAndOrderFront_(None)
            except Exception:
                pass

    def _retry_later(self, seconds):
        # performSelector:afterDelay: runs on the main run loop and needs no block.
        self.bridge.performSelector_withObject_afterDelay_("retryLoad:", None, float(seconds))

    # -- optional global hotkey (off by default). The global key monitor only receives events once the user
    # allows Slothmore under Privacy & Security > Accessibility; without that it is silently inactive.
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

        g = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(KEY_DOWN_MASK, _guarded("hotkey", on_global))
        local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(KEY_DOWN_MASK, _local_guard(on_local))
        self.hotkey_monitors = [m for m in (g, local) if m is not None]

    def toggle_from_hotkey(self):
        if self.panel.isVisible():
            self.hide()
        else:
            self.show()


def _local_guard(fn):
    """A local event monitor must return the event (or None to swallow it); on error, pass the event through."""
    def wrapper(e):
        return _guard("hotkey", fn, e, default=e)
    return wrapper


def attach(status_item, base_fn, token_fn, hooks, hotkey=None):
    """Create the quick panel for a status item. Raises on any Cocoa/WebKit problem (caller falls back to the menu)."""
    return QuickPanel(status_item, base_fn, token_fn, hooks, hotkey=hotkey)
