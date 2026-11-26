# Changelog

## 0.2.0 (unreleased)

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
