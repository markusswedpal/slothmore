# Slothmore

<img src="static/icons/icon-192.png" width="96" height="96" alt="Slothmore">

A local dashboard for your code folders. Click a folder to start its web server or run its script, see the output live, and answer Y/N prompts from the page.

Standard library only: no `pip` packages needed for the server. Tested on Python 3.12, 3.13, 3.14 and 3.15 (CI runs all four on Linux and macOS; the suite also passes on 3.9 and 3.10, which are not supported targets). The Homebrew formula depends on `python@3`, so it uses whichever current Python Homebrew ships and does not install a second one if you already have it. Your own `python3` is never changed.

## Install (Homebrew)

```
brew install markusswedpal/tap/slothmore
slothmore
```

That starts the dashboard at http://127.0.0.1:7777 and opens it in your browser, signed in. Keep the Terminal window open while you use it.

To run it in the background and start it at login instead:

```
brew services start slothmore
slothmore open
```

The service starts without a browser, so use `slothmore open` whenever you want the dashboard. It opens the running server signed in (see [Signing in](#signing-in)). If you run `slothmore` while it is already running, it opens the running one instead of failing.

| Command | What it does |
|---|---|
| `slothmore` | start the dashboard and open it |
| `slothmore open` | open an already running dashboard (for example the `brew services` one), signed in |
| `slothmore url` | print the signed-in address |
| `slothmore --port 8888` | use another port (`slothmore open --port 8888` to open it) |
| `slothmore --no-open` | start without opening a browser |
| `slothmore --watch ~/Projects` | add a watched folder (you can also add folders in the page with **Add** or **Watch a folder**) |
| `slothmore --reset-token` | make a new access token, which signs out every browser and installed web app |
| `slothmore menubar` | show Slothmore in the macOS menu bar (see [below](#start-at-login-and-the-menu-bar-icon-macos)) |
| `slothmore menubar --install-login` | start the menu bar icon at every login (`--uninstall-login` removes it) |
| `slothmore --version` | print the version |

## Signing in

The dashboard can run commands on your machine, so it is protected by an access token, the same way Jupyter does it.

- On first start Slothmore creates a random token in `~/.slothmore/token` (readable only by you). The same token is reused after restarts.
- `slothmore` and `slothmore open` open `http://127.0.0.1:7777/?token=…`. The server checks the token, stores it in a cookie (HttpOnly, SameSite=Strict, valid for about a year) and redirects to `http://127.0.0.1:7777/` so the token is no longer in the address bar.
- Every API request and the live output stream need that cookie, or an `Authorization: Bearer <token>` header (used by the menu bar helper).
- A browser without the cookie gets a sign-in page. Run `slothmore open`, or paste the token from `~/.slothmore/token`.
- The installed web app (Dock) keeps the cookie, so it keeps working after restarts. After `slothmore --reset-token`, open it once with `slothmore open` in the same browser.
- When Slothmore runs as a service, the token is not written to the log. The log only shows the plain address.

## Run from source

```
python3 bin/slothmore
```

Or `python3 server.py`. If you used `--no-open`, run `python3 bin/slothmore open` to open it signed in.

Run the tests (standard library only; the README renderer checks also need Node):

```
python3 -m unittest discover -s tests -v
```

## Install as an app (Dock)

- **Chrome / Edge:** open the address above, then use the install icon in the address bar ("Install Slothmore").
- **Safari (macOS Sonoma or later):** File > Add to Dock.

## Start at login, and the menu bar icon (macOS)

### With Homebrew

`brew services start slothmore` starts the server at login. The menu bar icon is a separate helper that the formula sets up for you: it installs `rumps` and its macOS bindings (pyobjc, pinned and hash-checked wheels from `macos/requirements-menubar.txt`) into a private venv inside the keg, so nothing touches your own Python.

```
slothmore menubar                  # show the icon now
slothmore menubar --install-login  # start it at every login (LaunchAgent com.slothmore.menubar)
slothmore menubar --uninstall-login
slothmore menubar --status         # login item installed? rumps and WebKit found? where is the log?
slothmore menubar --classic        # left click shows the classic menu instead of the quick panel
slothmore menubar --hotkey alt+space   # optional global shortcut for the quick panel (off by default)
slothmore menubar --reset-panel    # turn the quick panel back on after a crash switched it off
```

- The icon finds the server through `~/.slothmore/server.json` and signs in with `~/.slothmore/token`. If the server is not running, the menu says **Slothmore is not running** and offers **Start Slothmore**.
- The login item logs to `~/.slothmore/menubar.log`. It restarts after a crash but stays closed after you choose **Quit menu bar helper**. If the helper ever stops while it opens the quick panel, the next start uses the classic menu instead of crash-looping (`--status` shows it; `--reset-panel` or the next upgrade tries the panel again). `--classic` and `--hotkey` given together with `--install-login` are kept in the login item. It runs the `slothmore` wrapper from Homebrew's `bin`, so it keeps working after `brew upgrade`.
- **Quick panel.** Click the icon and a dark, rounded panel opens under it with the `/compact` view: a search box (filters projects, running items and ports; Enter starts or opens the top match), **Running now** (open, restart, stop), **Quick start** (pinned and recently run first), **Ports** (Stop asks first and uses the Ports view's safe stop), an **AI** summary, and **Refresh** ⌘R, **Full view** ⌘O, **Kill all mine** (stops only what Slothmore started; asks first) and **Quit** ⌘Q. Arrow keys move through the list; Esc, a click outside or another click on the icon closes it. **Right-click** (or control-click) the icon for the classic menu. The panel needs `pyobjc-framework-WebKit` (the formula installs it); without it, or with `--classic` / `SLOTHMORE_MENUBAR_CLASSIC=1`, a click shows the menu. `--hotkey` / `SLOTHMORE_HOTKEY` set a global shortcut; macOS asks for Accessibility permission the first time. You can also open `http://127.0.0.1:7777/compact` in a browser.
- You can also toggle it in the dashboard: the **Menu bar icon: on/off** button in the sidebar (shown on macOS only).
- If `rumps` is missing, `slothmore menubar` prints the fix instead of an error trace. With Homebrew, run `brew postinstall slothmore` (retries the package install) or `brew reinstall markusswedpal/tap/slothmore`. After Homebrew upgrades Python itself, the venv can go stale; the wrapper then falls back to Homebrew's Python (the dashboard keeps working) and `brew postinstall slothmore` rebuilds the venv.

### From a source checkout

The scripts below are for a source checkout; `python3 -m pip install --user rumps` provides the helper's one dependency, and `python3 bin/slothmore menubar` works the same as above.

Everything for this lives in `macos/`:

```
macos/install-login-item.sh install ~/Projects                  # server only
macos/install-login-item.sh install --with-menubar ~/Projects   # server + menu bar icon
macos/install-login-item.sh install --port 8888 ~/Projects      # another port
macos/install-login-item.sh status                              # what is installed / running
macos/install-login-item.sh uninstall                           # remove both (settings and logs stay)
```

- **Server login item:** a launchd agent (`~/Library/LaunchAgents/com.slothmore.dashboard.plist`) that starts the server at login and keeps it running, so the Dock app always has something to open. It saves your shell's `PATH`, so `npm`, `node` and Homebrew tools work. Launchd stdout/stderr: `~/.slothmore/service.log`. The app also writes `~/.slothmore/slothmore.log`. Folders are optional; you can add more in the page.
- **Menu bar icon** (`macos/menubar.py`, optional): shows the sloth icon in the menu bar (a macOS template image, so it follows light and dark appearance), with a count when things are running, or `!` when the server can't be reached. The menu has **Open Slothmore**, each running item with **Open in browser**, **Restart** and **Stop**, your **Groups** (Start all / Restart all / Stop all) and a **Start** submenu (pinned items first). It needs one package:

  ```
  python3 -m pip install --user rumps
  ```

  Then use `--with-menubar` above, or run it by hand with `python3 macos/menubar.py` (add `--url http://127.0.0.1:8888` for another port). The installer uses the first `python3` that has rumps; if none does, it stops and tells you the pip command. The menu bar login item restarts after a crash but stays closed after you choose **Quit menu bar helper**. Log: `~/.slothmore/menubar.log`.
- `./install.sh` and `./menubar.py` in the main folder still work and forward to the `macos/` versions (`./install.sh --uninstall` removes the login items).

If your folders are inside Desktop, Documents or Downloads, macOS may ask once whether `python3` may access them. Click Allow. A folder like `~/Projects` avoids the prompt.

**Tested on Linux only.** The installer was checked with a dry run (`SLOTHMORE_DRY_RUN=1 LAUNCH_AGENTS_DIR=/tmp/x …` writes the plists and prints the `launchctl` commands without running them), and the menu bar helper was tested against a stand-in for rumps. Real `launchctl bootstrap`, the rumps menu itself, the quick panel window (NSPanel + WKWebView; its `/compact` page and the panel's sizing and message logic are tested on Linux), and macOS notifications (`osascript`) have not been run on a Mac yet. If something fails, `macos/install-login-item.sh status` and the two log files above are the first places to look.

## How it decides what to run

For each folder, the first match wins:

| # | Found in the folder | Runs | Card type |
|---|---|---|---|
| 1 | `slothmore.json` or `launcher.json` with `cmd` | that command (always wins) | from the file |
| 1b | AI projects: ComfyUI, Open WebUI, `langgraph.json`, CrewAI, MCP servers, Streamlit, Gradio | see [AI](#ai) | ComfyUI, Open WebUI, Agent, MCP, Streamlit, Gradio |
| 2 | `package.json` with a `dev`, `start`, `serve` or `preview` script (in that order) | `npm run dev`, `pnpm run dev`, `yarn run dev`, `bun run dev` … | Node |
| 3 | `Makefile` with a `dev`, `run`, `start` or `serve` target | `make <target>`, with a picker for the other targets | Make |
| 4 | `app.py`, `main.py`, `server.py` or `run.py` | `python <file>` (uses `.venv/` or `venv/` if present) | Python web if it imports Flask, FastAPI etc., otherwise Script |
| 5 | `pyproject.toml` with `[project.scripts]` or `[tool.poetry.scripts]` | `uv run <name>` (with `uv.lock`, or uv installed and no venv), `poetry run <name>` (with `poetry.lock`), otherwise python on the entry module; picker if there are several | Python |
| 6 | `deno.json(c)` with tasks | `deno task dev`/`start`/`serve`, picker for the rest | Deno |
| 7 | `go.mod` / `Cargo.toml` | `go run .` / `cargo run` | Go / Rust |
| 8 | `bin/rails` / `config.ru` | `bin/rails server -p $PORT` / `bundle exec rackup -p $PORT` | Rails / Ruby web |
| 9 | `compose.yaml`, `docker-compose.yml` … | `docker compose up` (Stop sends Ctrl+C first so containers shut down cleanly, then waits up to 15 s) | Compose |
| 10 | `run.sh`, `start.sh` or `dev.sh` | `bash <file>` | Script |
| 11 | `package.json` `main`, or `index.js`/`server.js`/`main.js` | `node <file>` | Node |
| 12 | `index.html` (or static `*.html` files) | `python -m http.server <free port>` (page picker if several) | HTML |
| 13 | `.py` files | one file: run it; several: the one with `if __name__ == "__main__"`, otherwise a **File** picker | Script |
| 14 | `package.json` without the scripts above | **Script** picker (for example `build`, `lint`) | Node |
| 15 | `Makefile` without the targets above | **Target** picker | Make |
| 16 | `*.ipynb` notebooks | `jupyter lab --port $PORT` | Jupyter |

- **Package manager** comes from the lockfile (`pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`/`bun.lock`, `package-lock.json`) or the `packageManager` field, also from a monorepo root up to four levels above. Default is npm.
- **Pickers:** when there's no obvious default, the card shows a dropdown and **Run** appears once you choose. The choice is remembered per item. If there is a default, the dropdown still lets you switch to another script, target, task, entry point or file.
- **Missing tools:** if the command needs a program that isn't on your PATH (pnpm, yarn, bun, uv, poetry, make, docker, go, cargo, deno, bundle, jupyter …), the card says so in red with the install command, and Run is hidden.
- **Install / Install and run:** when `node_modules` is missing (for a monorepo package it checks the package and the workspace root), or a Python project has `requirements.txt`/`pyproject.toml` but no `.venv`, the card shows *Dependencies not installed* with **Install** and **Install and run**. Both run `<pm> install`, `uv sync`, `poetry install` or `python -m venv .venv && .venv/bin/pip install -r requirements.txt` (or `-e .`). Output goes to the terminal panel and to the run history like a normal run. **Install and run** then starts the project (saved args, chosen script, and the usual port check) only if the install exits 0; a failed install is left stopped. The chain runs on the server, so it still finishes if you close the browser. The menu bar quick panel's **Quick start** does the same when dependencies are missing.

Web servers get a free port through the `PORT` environment variable, and the card shows the port it actually printed (for example Vite's 5173) with an **Open** button.

### Subfolders, monorepos and loose files

- **Nested projects:** a subfolder that has nothing runnable of its own is searched further down, by default **3 levels** deep. Runnable folders found there become cards under a small heading with their path (for example `clients/beta/`), and the card shows the relative path under its name. Change the depth (1–6) per watched folder with the **Depth** dropdown in the section header. A watched folder is searched in at most 600 folders. `node_modules`, `__pycache__`, `venv`, `env`, `dist`, `build` and hidden folders are skipped.
- **Monorepos:** a folder with `workspaces` in `package.json`, `pnpm-workspace.yaml`, or `apps/*` and `packages/*` gets one card per workspace package that has something to run. They're grouped under the repo name with a **monorepo** badge. Packages with nothing to run (for example a shared `ui` library) are left out.
- **Loose files:** `.py`, `.sh`, `.js`, `.mjs` and `.html` files lying directly in a watched folder become cards too. Turn this off with the **Loose files** switch in the section header.

### Override with `slothmore.json`

Put this in a folder to choose the command yourself:

```json
{
  "name": "My script",
  "cmd": "python app.py",
  "web": false,
  "port": null,
  "env": { "SOME_VAR": "value" }
}
```

## Custom names

Give any card a name of your own: hover the card and click the pencil next to the title, or double-click the title. **Enter** saves, **Esc** cancels, and saving an empty name goes back to the folder name. Clicking elsewhere also cancels.

The custom name becomes the title, and the real folder name (or the relative path for nested cards) always stays visible underneath in smaller monospace text. Custom names are used everywhere the item appears: Running now, the terminal header, the ⌘K palette (which searches both the custom name and the folder name), groups, notifications, the menu bar icon and run history. Names are saved in `~/.slothmore/config.json`, so they survive restarts. A `name` in `slothmore.json` or `launcher.json` still works as before, and a custom name set in the dashboard takes precedence over it.

## Interactive scripts and Y/N prompts

Scripts run in a pseudo-terminal, so `input()` prompts and live output work just like in Terminal. When the last line of output looks like `[y/N]`, `[Y/n]` or `(y/n)`, the **Yes** and **No** buttons appear. They send `y` or `n` plus Enter to the script. For any other question, type the answer in the reply box under the output.

Tip: if a script shows nothing until it finishes, run it with `python -u` (folder detection already does this) or use `print(..., flush=True)`.

## Ports, arguments, presets, and environment variables

- **Auto-detected ports:** Slothmore watches each process's stdout/stderr (stripping ANSI escape codes) for actual listening URLs and ports—such as Vite's `Local: http://localhost:5173`, Next.js, Flask/uvicorn `Running on`, `http.server` `Serving HTTP on ... port N`, or generic `http://127.0.0.1:N` / `port N` lines—and updates the card, **Running now** row, and **Open** URL automatically (keeping any selected static `.html` page appended).
- **Script arguments & named presets:** Cards for scripts and servers include an **Args** field (for example `-reset`). Click **+ Preset** next to Args to save named argument sets (such as `reset` or `dry`), switch between them from the **Preset** dropdown, or delete unused presets. Saved in `~/.slothmore/config.json`.
- **Per-card environment variables:** Click **Env** on any startable card to open its `KEY=VALUE` editor. Keys are validated (`[A-Za-z_][A-Za-z0-9_]*`), values are merged into the child process environment at launch, masked by default (`••••••••`) with a **Reveal & Edit** toggle, and never printed in the terminal command header. The item list sent to the page contains only the key names. Values are loaded from the server when you click **Reveal & Edit** and dropped again when you mask or close the editor. When saving, a line with just `KEY` keeps the stored value.
- **Static HTML folders:** Folders containing `*.html` files launch on a free local port. If there are multiple `.html` files, choose the entry page from the **Page** picker on the card; your choice is remembered and immediately updates the **Open** URL.
- **Running now box:** Click any active item to select its card and view its output, click **Open** to launch a running web server, use **Restart** (`↻`) or **Stop**, or click a stopped/finished item to re-launch it.

## Restart and auto-restart

- **One-click Restart:** Available on running cards, inside the **Running now** list, and in the terminal header.
- **On crash:** Toggle **On crash** on any card to automatically restart after non-zero exits with exponential backoff (`1s`, `2s`, `4s`), capped at 3 restarts within 60 seconds so crash loops pause safely.
- **On change:** Toggle **On change** to watch the item's folder (ignoring `node_modules`, `.git`, `__pycache__`, `venv`, `.venv`, `dist`, `build`, `.next`, `.cache`, and dotfiles) with debounce and restart automatically on edits. Restart counts (`↺ N`) appear on the card and in **Running now**. Both toggles persist in `~/.slothmore/config.json`.

## Logs and run history

- The app writes its own log to `~/.slothmore/slothmore.log` (rotating, 1 MB × 3, mode 0600). Set `--log-level debug` or `SLOTHMORE_LOG_LEVEL=debug` for more detail. The token and stored env values never appear in the log. When you run it in a terminal, the same messages also go to stderr. The launchd login item writes its stdout/stderr to `~/.slothmore/service.log`, so it does not share the app log file.
- Every run's output is saved under `~/.slothmore/logs/<item>/` (the last 2000 lines and at most 512 KB per run, older lines trimmed). Each item keeps its last 25 runs. History survives restarts of Slothmore; a run that was still going when Slothmore quit shows as **interrupted**.
- Each run records its start and end time, duration, exit code, arguments, preset, what started it (you, a group, a restart or auto-restart) and its port. Environment variable values are never written to logs or history.
- Click **History** on a card or above the output (or press `Shift+H`) to see past runs. **View log** shows that run's output with a yellow "Viewing past run" bar; **Back to live output** returns. **Clear history** (click twice) deletes the saved logs for that item.
- Above the output: **Filter output…** shows only matching lines with the matches highlighted and a count, **Copy log** copies the full output (live or past), and **Clear** empties the view.
- Cards show "Last run … · exit N" when an item isn't running.

## Notifications

- Click **Notifications** in the sidebar. Turn them on, then pick when: **Finished** (exit code 0), **Failed** (non-zero exit) and **Waiting for input** (a script asks a question, such as a `[y/N]` prompt). **Only for runs longer than N seconds** skips quick runs (default 10; use 0 for all). Stopping something yourself never notifies.
- Delivery: while the dashboard tab is visible, a small message appears in the corner (click it to jump to the item). When the tab is in the background, the browser shows a system notification; the browser asks for permission the first time you turn this on. On macOS, when no dashboard tab is open, the server shows a macOS notification instead (`osascript`), so you also get them with only the menu bar icon running. **Send a test** checks both.
- Per item: the **Notify** pill on each card follows the global setting (**Notify · auto**); click it to switch that item off (or on, when "Notify for every item" is off), and click again to go back to auto.

## Groups

- **+ New group** in the sidebar: give it a name, tick the items, put them in start order with ↑ ↓, and set a delay between starts (0–30 s). Saved in `~/.slothmore/config.json`.
- Each group in the sidebar has **Start all**, **Restart all** and **Stop all** buttons and shows how many of its items are running. Click the name to edit or delete it. Groups also appear in the ⌘K palette (Enter starts the group) and in the menu bar icon.
- Start all starts items in order and waits the delay between them; Stop all stops them in reverse order. Items already running are left alone on Start all. If an item can't start (for example, its port is taken), the others still start and a message tells you which one failed.

## Health and port checks

- While something runs, its card and the **Running now** row show CPU, memory, uptime and the number of processes, counting child processes too (for example the real server under `npm run dev`). CPU is a percentage of one core, so busy multi-threaded apps can go above 100%. Values update every 2 seconds. If `psutil` is installed it is used; otherwise Slothmore reads `ps` (no extra install needed).
- Before starting, Slothmore checks the port the app is expected to use: the `port` in `slothmore.json` / `launcher.json`, or a port the app picked itself last time (for example Vite's 5173). If that port is already taken, nothing starts and a message shows what is using it (program and PID when available) and the `kill <PID>` command if you want to free it yourself. **Start anyway** starts it regardless. Slothmore never stops other programs for you. Apps that use the free port Slothmore gives them in `$PORT` are not checked, because that port is free by definition.

### Listening ports

Open **Machine → Ports** in the sidebar. The panel lists every TCP port in LISTEN on your machine: port, address (`127.0.0.1`, `0.0.0.0`, `::`, and so on), process name, PID, user, and the project folder when the process's working directory is in a watched or pinned folder, or when it is something Slothmore itself started. The list refreshes when you open it and about every 5 seconds while it is visible.

- **Open** appears for ports that look like HTTP (common development ports and typical server process names) and opens `http://127.0.0.1:<port>` in a new tab (or `http://[::1]:<port>` when the socket is IPv6-only).
- **Stop** appears only for processes you own, not for Slothmore's own dashboard port. It asks for confirmation, then re-checks the PID, command and start time just before sending SIGTERM, and refuses if anything has changed. Processes Slothmore started are stopped the usual way. Docker and VM helpers are not signalled; stop them there instead.
- Filter the table with the search box. The row for Slothmore itself is marked.

The listing uses `lsof -nP -iTCP -sTCP:LISTEN` on macOS, and `ss` if `lsof` is missing on Linux. It never runs a shell.

## README files


- If a folder has a README at the top level, its card gets a **README** button. The same action appears in the ⌘K palette. Click it to read the file in the app.
- Recognised names, case-insensitive: `README.md` (preferred), `README.markdown`, `README.rst`, `README.txt`, or `README` with no extension. Only the top-level file is used, not one in a subfolder. Files larger than 1 MB are listed but not opened.
- For a pinned single file (for example `app.py`), Slothmore looks in the parent folder only when that folder looks like the file's project: it has a marker such as `package.json`, `slothmore.json`, `launcher.json`, `pyproject.toml` or a `.git` directory, or it is a small folder that already contains a README. Catch-all folders such as Desktop, Documents and Downloads are skipped.
- Markdown is rendered in the app (headings, lists, code, tables, blockquotes, links and images). Raw HTML is shown as text, not executed. Links open in a new tab. Relative images are served only from that item's folder; path traversal and SVG files are rejected. `.rst`, `.txt` and bare README files are shown as plain text.
- **Remote images** (`https://…`) are not loaded automatically. Each one shows a **Load image (host)** button, so opening a README never contacts another server until you click. Images are requested without a referrer. Plain `http://` images are shown as text.
- Very large or deeply nested READMEs are cut short: rendering stops at 200 KB with a notice (**View raw** still shows the whole file), and blockquotes or lists nested more than 8 levels deep are shown as plain text.
- **View raw** shows the source, **Copy path** copies the file path, and **Show in Finder** (or **Show in folder** on Linux) reveals the file. **Open** always uses a text editor (`open -t` on macOS; on Linux, `xdg-open` only for `.md`, `.markdown`, `.rst` and `.txt`). An extensionless README is never handed to the OS opener, so a hostile executable named `README` cannot launch. Esc closes the reader.

## AI

Slothmore finds the AI things on your machine. It never calls a model and has no AI features of its own.

### Local model servers

Open **AI → Models** in the sidebar, or ⌘K and type the server name. The panel checks localhost every 10 seconds, and only while the dashboard tab is visible.

| Server | Default ports | How it's recognised | Models shown |
|---|---|---|---|
| Ollama | 11434 | `/api/tags` | all pulled models; loaded ones from `/api/ps` with VRAM, size and context |
| LM Studio | 1234 | `/api/v0/models` (falls back to `/v1/models`) | loaded vs available, quantisation, context |
| llama.cpp server | 8080 | `/props` + `/v1/models` | the loaded GGUF, its parameters, size and training context |
| vLLM | 8000 | `/v1/models` with `owned_by: "vllm"` | served models and max length |
| Jan | 1337 | OpenAI-style `/v1/models` | models |
| LocalAI | 8080 | `/readyz` + `/v1/models` | models |
| text-generation-webui | 5000, 7860 | `/v1/internal/model/info` or the UI page | loaded vs available |
| Open WebUI | 3000, 8080 | `/api/config` name | needs sign-in, so the card only links to it |
| ComfyUI | 8188 | `/system_stats` | GPU memory in use |

- Ports such as 3000 and 8080 are also used by normal dev servers, so a server is only shown if its reply matches. Ports used by Slothmore's own runs are skipped. An open port that isn't an AI server is checked again only after 2 minutes, so your own dev server isn't hit every 10 seconds.
- Each card shows the URL with an **Open** link, the version, the PID listening on the port, its CPU, memory and uptime (with psutil when installed, otherwise `ps`), and the model list.
- **Start:** if `ollama` or `lms` is installed and the server isn't running, the card offers **Start**. It runs `ollama serve` (with `OLLAMA_HOST` set to the configured port) or `lms server start --port N`. **Output** shows the server log in the terminal panel.
- **Stop:** a server Slothmore started stops right away. For a server Slothmore didn't start, it asks first. LM Studio is stopped with `lms server stop`. Other servers get SIGTERM to the PID that owns the port. Processes behind Docker, OrbStack, Colima or a VM are never signalled; the card tells you to stop them there. If the Ollama menu bar app is running, quit it from the menu bar instead, otherwise it starts the server again.
- **Ports…** in the panel changes the ports per server (up to 4 each) and turns model or agent detection off. Settings are stored in `~/.slothmore/config.json` under `ai`.

### Running agents

**AI → Agents** lists coding agents that are running right now:

- Claude Code (`claude`)
- Codex CLI (`codex`)
- Aider
- Gemini CLI
- Cursor Agent (`cursor-agent`)
- opencode
- Goose (`goose session` / `run` / `web` only, so the goose database migration tool isn't listed)
- OpenHands
- Cline CLI
- Ollama runner processes, shown as a summary line

Matching uses the process name, or the script that node, python or bun is running. Desktop apps (`*.app/Contents/…`) are ignored. When a wrapper starts a child process with the same name (npm shim → native binary), only the parent is shown. The signatures are a list (`AGENT_SIGNATURES` in `server.py`), so adding another agent is one line.

For each agent you get:

- the PID and working folder (psutil, `/proc/<pid>/cwd` on Linux, `lsof -d cwd` on macOS)
- CPU, memory (including child processes), uptime and child process count
- the command name with its flags only; arguments that could be prompts are not shown

If the working folder is inside a watched or pinned item, the row links to that card (**Reveal folder**), and the card shows a small chip such as `claude`. Clicking the chip opens the agent in the panel.

**Stop** always asks first, because Slothmore didn't start these processes. It sends SIGINT (like Ctrl+C), then SIGTERM after 3 seconds if the process is still running, and never SIGKILL. Before stopping, it checks that the PID still belongs to the same agent.

Slothmore only reads the process list. It never opens an agent's files, transcripts or sessions.

### AI projects in folders

These are checked right after `slothmore.json` / `launcher.json`, so they take priority over the generic rules:

| Found | Runs | Card |
|---|---|---|
| ComfyUI checkout (`main.py`, `nodes.py`, `comfy/`) | `python main.py --listen 127.0.0.1 --port $PORT` | ComfyUI (web) |
| Open WebUI (`open-webui` dependency, or the source checkout with `backend/start.sh`) | `open-webui serve --port $PORT` / `bash backend/start.sh` | Open WebUI (web) |
| `langgraph.json` | `langgraph dev --port $PORT --no-browser` | Agent (web) |
| CrewAI project (`crewai` dependency plus `[tool.crewai]` or `src/*/crew.py`) | `crewai run` | Agent |
| MCP bundle `manifest.json` (`server.mcp_config`) | Inspect / Run that command | MCP |
| `package.json` depending on `@modelcontextprotocol/sdk` (and not a client-only import) | Inspect / Run `node <bin or main>` | MCP |
| Python with `mcp` or `fastmcp` and a file that creates a server (`FastMCP(`, `mcp.server`, `@mcp.tool`), at the top level or under `src/` | Inspect / Run `python server.py` (package layout: the normal command, e.g. `uv run <script>`) | MCP |
| `streamlit` dependency or import | `streamlit run app.py --server.port $PORT --server.headless true` | Streamlit (web) |
| `gradio` dependency or import | `python app.py` with `GRADIO_SERVER_PORT=$PORT` | Gradio (web) |

- **MCP servers** that use stdio speak JSON-RPC on stdin/stdout, so a plain Run would just wait for a client. The card explains this, and its **Mode** picker defaults to **Inspect**, which runs `npx @modelcontextprotocol/inspector <server command>`. The Inspector's **Open** link includes its one-time auth token, which Slothmore reads from the Inspector's output. **Run over stdio** is still available. A server that uses the HTTP transport (`streamable-http`, SSE, uvicorn) keeps its normal Run command and gets the MCP badge with a note to point a client at its URL. A folder with only an `mcp.json` / `.mcp.json` gets an **Inspect** picker for each stdio server in it, but only when nothing else in the folder can run. An `.mcp.json` in a normal app doesn't change how the app runs.
- **Agent frameworks:** with LangChain, LangGraph, CrewAI, OpenAI Agents SDK, AutoGen/AG2, Pydantic AI, LlamaIndex, smolagents, Agno, Google ADK, Haystack, Semantic Kernel, DSPy or Mastra as a dependency (or import), the card keeps its normal run command and gets an **Agent** badge plus the framework names. With only the Vercel AI SDK or Genkit, it gets an **AI** badge. The list is `AGENT_FRAMEWORKS` in `server.py`.
- The CLIs come from the project's `.venv` when it has one. Missing tools show the usual red message with an install hint (`npx`, `streamlit`, `langgraph`, `crewai`, `open-webui`).
- The **AI** filter chip above the cards shows only AI projects and folders with an agent running in them.

### Privacy

Everything stays on your Mac:

- The only network traffic is short GET requests (0.6 s timeout) to `127.0.0.1` on the ports above, with any proxy settings ignored.
- Probing happens only while a dashboard tab is open and visible.
- No model is called.
- Nothing about agents is read beyond the process table and the working folder path.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Open the command palette (fuzzy-search, run, open, or select any item or group, plus local model servers and running agents; works anywhere) |
| `/` | Focus folder search |
| `⌘.` / `Ctrl+.` | Stop the selected item |
| `Shift+R` | Restart the selected item |
| `Shift+H` | Show the run history of the selected item |
| `?` | Show or hide the shortcuts help overlay |
| `Esc` | Close the command palette, README reader or an open dialog; cancel renaming |
| Double-click a title | Rename the card (Enter saves, Esc cancels) |

## Adding things

- **Watch a folder:** every subfolder becomes a card, and new ones appear on their own. Nested projects, monorepo packages and loose script files are found too (see *Subfolders, monorepos and loose files*). `node_modules`, `__pycache__` and hidden folders are skipped.
- **Pin by drag and drop:** drag a folder or script from Finder onto the page. Browsers only reveal the *name* of what you drop, so Slothmore looks for that name inside your watched folders (three levels deep). If it finds one match it pins it. If it finds several, it asks which one. If it finds none, it offers **Choose folder…** (the native macOS picker, which gives the real path) or a pasted path.
- **Reorder pinned items** by dragging their cards.

Settings, presets, groups and notification choices live in `~/.slothmore/config.json`; run logs and history in `~/.slothmore/logs/`. Set `SLOTHMORE_HOME` to use another folder. If you already have `~/.launcher` from an earlier build and no `~/.slothmore` yet, settings and logs are copied on first run. `LAUNCHER_HOME` is still honoured.

## Typography

The dashboard is set entirely in **JetBrains Mono** (bundled, SIL Open Font License 1.1). Headings use ExtraBold or Bold with tighter tracking, card titles use SemiBold, buttons and other UI use Medium, hints and descriptive text use Light italic, labels are uppercase with wider tracking, and paths, commands and the terminal use Regular. Ligatures and a slashed zero are on in the terminal and other code, and off in the UI. The variable roman and italic files cover weights 100–800. Notes and the license live in `static/fonts/`. The optional macOS menu bar helper keeps the system UI font.


## Security

- The server listens on `127.0.0.1` only. Nothing on your network can reach it.
- Every API request needs the access token (see [Signing in](#signing-in)).
- It rejects requests with an unexpected `Host` header (blocks DNS rebinding) and any write request that doesn't come from the dashboard's own origin (blocks other websites from sending commands to it).
- `~/.slothmore` is readable only by you (0700). The token, `config.json` (which holds env values), run logs and history are 0600, and permissions of older files are fixed at startup.
- Every response sends a Content-Security-Policy:

  ```
  default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline';
  img-src 'self' data: https:; font-src 'self'; connect-src 'self'; manifest-src 'self';
  worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
  ```

  Scripts and stylesheets load only from Slothmore itself, with no inline scripts and no inline event handlers. `style-src-attr 'unsafe-inline'` allows `style="…"` attributes, which the dashboard uses for layout; they can't run code. `img-src https:` is there for README images, but the README viewer only creates a remote `<img>` after you click **Load image**.
- Even so, the dashboard can run commands in your folders, which is the point. Only watch or pin folders you trust, and never change `127.0.0.1` to `0.0.0.0`.

## Files

```
bin/slothmore                 CLI entry point (`open`, `url`, `--version`, `--port`, `--no-open`, `--watch`, `--reset-token`, `--log-level`, `menubar`)
server.py                     the server (API, auth, process control, folder detection, history, health)
static/index.html             the dashboard page (markup only)
static/app.js, app.css        dashboard script and styles
static/compact.html, compact.js, compact.css   quick panel (/compact) shown by the menu bar icon
static/login.html             sign-in page shown without the access cookie
static/markdown.js            README renderer
static/                       also the web app manifest, service worker, icons, favicon and fonts
static/icons/                 app icons (PWA, Apple, sidebar) and NOTES.md
macos/icons/                  menu bar template images (black + alpha)
tests/                        unittest suite (`python3 -m unittest discover -s tests`)
.github/workflows/test.yml    CI on Ubuntu and macOS, Python 3.12 to 3.15
macos/install-login-item.sh   launchd login items (server, optional menu bar icon)
macos/menubar.py              menu bar icon (needs rumps)
macos/quickpanel.py           quick panel under the icon (NSPanel + WKWebView; needs pyobjc-framework-WebKit)
macos/requirements-menubar.txt  pinned, hash-checked rumps + pyobjc (Cocoa, WebKit) wheels (used by the Homebrew formula)
install.sh, menubar.py        older entry points, forward to macos/
LICENSE                       MIT, copyright 2026 Markus Wulff
CHANGELOG.md                  release notes
static/fonts/                 JetBrains Mono (SIL Open Font License 1.1) and notes
~/.slothmore/config.json      your settings (created on first run)
~/.slothmore/token            access token (0600)
~/.slothmore/server.json      port and PID of the running server (used by `slothmore open`)
~/.slothmore/slothmore.log    app log (rotating, 1 MB × 3, 0600)
~/.slothmore/service.log      launchd stdout/stderr (login item)
~/.slothmore/logs/            run logs and history
```

If `~/.slothmore` does not exist and `~/.launcher` does, the first run copies the old folder. `SLOTHMORE_HOME` (or the older `LAUNCHER_HOME`) selects a different settings folder.
