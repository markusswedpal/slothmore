# Changelog

## 0.2.3 (2026-11-27)

### Added
- **Install and run.** When a Node or Python project is missing its dependencies, the card still has **Install**, and now also **Install and run**. That runs the same install as **Install** (output in the terminal and in history), and only if it exits 0 starts the project the same way **Start** would, including saved args, the chosen script, and the usual port check. If the install fails, the project is left stopped. The work happens on the server, so it still finishes if you close the browser. A second click while it is already in progress is rejected; the first one keeps going. The menu bar quick panel's **Quick start** uses the same chain when dependencies are missing.

## 0.2.2 (2026-11-26)

### Fixed
- **Menu bar helper crashed when the quick panel opened** (`OC_PythonException: cannot call block without a signature`, seen with Homebrew Python 3.15 and pyobjc 13.1). The panel called WebKit's navigation `decisionHandler` block from Python. The navigation policy now runs in `webView:didStartProvisionalNavigation:` and stops anything that isn't this Slothmore server (other http(s) pages still open in the browser), so Python never calls an Objective-C block. The "server is starting" retry uses `performSelector:afterDelay:` instead of a timer block.
- The `ObjCPointerWarning` about `CGColor` is gone. The window and web view are transparent and `compact.css` draws the rounded dark card and its border; the native side only clips the corners.
- Every callback from Cocoa into Python (web view and window delegates, the icon click, event monitors) catches errors. An error logs its traceback to `~/.slothmore/menubar.log`, turns the panel off for the session and gives the icon its classic menu back, with a notification, instead of terminating the helper.
- Optional AppKit/WebKit calls are checked with `respondsToSelector:` first; `NSApplication.activate` is used on macOS 14+.
- The quick panel module is loaded once per process, so its Objective-C classes are never defined twice.

### Added
- **Crash guard.** Before the panel is shown for the first time the helper writes `~/.slothmore/menubar.state`, and clears it once the first page has loaded. If the helper dies in between, the LaunchAgent restart starts with the classic menu instead of crash-looping. The panel stays off for that version until `slothmore menubar --reset-panel` (which also restarts the login item) or the next upgrade. `slothmore menubar --status` shows the panel state.
- `--classic` and `--hotkey` given together with `slothmore menubar --install-login` are now kept in the login item (`SLOTHMORE_MENUBAR_CLASSIC`, `SLOTHMORE_HOTKEY`).

## 0.2.1 (2026-11-26)

### Added
- **Custom icon.** The sloth mark is used for the web app (including a maskable icon), the favicon, the sidebar, the sign-in page, the README, and the macOS menu bar (a template image that follows light and dark appearance).
- **`slothmore menubar`** starts the macOS menu bar icon. It reads the port from `~/.slothmore/server.json` and the token from `~/.slothmore/token`. When the server is not running it shows "Slothmore is not running" with a **Start Slothmore** item.
- **`slothmore menubar --install-login` / `--uninstall-login` / `--status`** manage a LaunchAgent (`~/Library/LaunchAgents/com.slothmore.menubar.plist`) that starts the icon at login (log: `~/.slothmore/menubar.log`). The dashboard has a matching **Menu bar icon** button (`GET`/`POST /api/menubar`, authenticated; macOS only).
- If `rumps` is missing, the command prints how to fix it instead of a traceback.
- **Quick panel.** Clicking the menu bar icon opens a borderless, rounded, dark panel under the icon (420 pt wide, up to 600 pt tall) instead of a menu. It shows the new `/compact` page in a WKWebView: a search box that filters projects, running items and ports (Enter starts or opens the top match), **Running now** (open, restart, stop), **Quick start** (pinned and recently run first), **Ports** (process, PID, ports; multi-port processes expand; Stop asks first and uses the same safe stop as the Ports view), an **AI** summary (local model servers and agents), and **Refresh** (⌘R), **Full view** (⌘O), **Kill all mine** and **Quit** (⌘Q). Arrow keys move, Esc closes, and so do a click outside or a second click on the icon. Right-click (or control-click) the icon for the classic menu. `--classic` or `SLOTHMORE_MENUBAR_CLASSIC=1` keeps the menu on left click, and without `pyobjc-framework-WebKit` the helper falls back to the menu by itself. An optional global shortcut (`--hotkey alt+space` or `SLOTHMORE_HOTKEY`) is off by default.
- `/compact` also works in a normal browser tab. It needs the same sign-in as the dashboard and follows the same CSP (no inline script or style).
- `POST /api/runs/stop-all` with `{"confirm": true}` stops everything Slothmore started ("Kill all mine"). Other processes are never touched.
- **Homebrew formula** installs `rumps` and pyobjc (Cocoa and WebKit) into a private venv from pinned, hash-checked wheels (`macos/requirements-menubar.txt`, installed in `post_install`, macOS only). The `slothmore` wrapper uses that venv and falls back to Homebrew's Python if it is missing.

### Changed
- The formula depends on `python@3` (the current Homebrew Python) instead of `python@3.12`.
- CI runs Python 3.12, 3.13, 3.14 and 3.15 on Ubuntu and macOS (3.15 may fail without blocking).
- Tests resolve their temporary folders, which fixes four README-open tests on macOS where `/var` is a symlink to `/private/var`.

## 0.2.0

Security hardening after a code review.

### Added
- **Access token.** On first start Slothmore writes a random token to `~/.slothmore/token` (mode 0600). Opening `http://127.0.0.1:7777/?token=…` sets an HttpOnly, SameSite=Strict cookie and redirects to strip the token from the address bar. Every `/api/*` request, including the live output stream, needs the cookie or an `Authorization: Bearer <token>` header. The token survives restarts, so an installed web app stays signed in. The Host and Origin checks from 0.1.0 still apply.
- `slothmore open` opens a running dashboard signed in (for `brew services` users), and `slothmore url` prints the signed-in address. `slothmore --reset-token` makes a new token and signs out every browser.
- Starting `slothmore` while it is already running prints the signed-in address (and opens it) instead of failing with "Address already in use".
- A sign-in page for browsers without the cookie.
- **App log.** Events go to `~/.slothmore/slothmore.log` (rotating, 1 MB × 3, mode 0600) and to stderr when you run it in a terminal. `--log-level` and `SLOTHMORE_LOG_LEVEL` set the level (default `info`). The access token and stored env values are redacted. Unexpected errors return a generic `internal error` to the client; the traceback stays in the log. The launchd login item writes stdout/stderr to `~/.slothmore/service.log` so it does not share the app log file.
- **Ports view.** A sidebar entry lists every TCP port in LISTEN on the machine (process, PID, user, address, and the project folder when it can tell). Likely HTTP ports get an Open link. Stop appears only for processes you own, asks for confirmation, and re-checks PID, command and start time just before signalling. Slothmore's own port is marked and cannot be stopped from the list. The listing uses `lsof` or `ss`, never a shell.
- Tests: stdlib `unittest` with fixture folders for all 16 detection rules (plus AI projects), auth, env redaction, file permissions, request validation and CSP; Node checks for the README renderer. GitHub Actions runs them on Ubuntu and macOS with Python 3.12.
- `slothmore.json` remains the override file; `launcher.json` still works.

### Changed
- **Env values are no longer sent with the item list.** `/api/items` returns key names and a `has_value` flag. **Reveal & Edit** fetches values from `/api/items/<id>/env`. Saving keeps any value the browser never received (a bare `KEY` line or a `null` value means "keep").
- **Private files.** `~/.slothmore` is 0700; `config.json`, the token, run logs and history are 0600. Permissions of files from older versions are fixed at startup.
- **README viewer.** Remote images show a "Load image" button and are fetched only after a click (https only, no referrer). Rendering is capped at 200 KB with a notice, and blockquotes and lists at 8 levels of nesting. Inline parsing is now linear: 240 KB of repeated `![a` renders in about 30 ms instead of about 24 seconds.
- **Content-Security-Policy.** Inline JavaScript and CSS moved to `static/app.js` and `static/app.css`. Every response sends `script-src 'self'` (no inline scripts) and `frame-ancestors 'none'`; see the README for the full policy.
- A malformed `Content-Length` now returns 400 (was 500), and bodies over 1 MB return 413.
- Service logs no longer contain the token: when stdout is not a terminal, Slothmore prints the plain address and suggests `slothmore open`.
- Web app manifest colours match the dark UI (`#070B10`).
- **README Open.** On macOS, Open uses `open -t` so an extensionless executable README from an untrusted repo cannot launch in Terminal. On Linux, Open uses `xdg-open` only for known text extensions (`.md`, `.markdown`, `.rst`, `.txt`); otherwise it is refused. Show in folder opens the containing folder. The file path is never passed through a shell.
- **Stopping an AI model server.** Right before sending a stop signal (or running a CLI stop such as `lms server stop`), Slothmore re-probes the port, re-resolves the listening PID, and confirms it is the same process (command and start time). If the process has been replaced, it refuses and asks you to refresh.
- **Live output stream.** SSE socket writes (output chunks, pings and the exit event) happen after the run lock is released, so a slow client cannot stall other work on that run.

## 0.1.0

First public release.
