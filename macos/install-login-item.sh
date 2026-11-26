#!/bin/bash
# Slothmore for macOS: start the dashboard server (and optionally the menu bar helper) at login.
#
#   macos/install-login-item.sh install [--port 7777] [--with-menubar] [folder ...]
#   macos/install-login-item.sh uninstall          remove both login items (config and logs are kept)
#   macos/install-login-item.sh status             show what is installed and whether it is running
#
# Folders are optional: they are added as watched folders. You can add more later in the dashboard.
# For testing on other systems: SLOTHMORE_DRY_RUN=1 (or LAUNCHER_DRY_RUN=1) writes the plists
# but skips launchctl, and LAUNCH_AGENTS_DIR=/some/dir changes where the plists go.
set -euo pipefail

SERVER_LABEL="com.slothmore.dashboard"
MENUBAR_LABEL="com.slothmore.menubar"
AGENTS="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
SERVER_PLIST="$AGENTS/$SERVER_LABEL.plist"
MENUBAR_PLIST="$AGENTS/$MENUBAR_LABEL.plist"
MACOS_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$MACOS_DIR/.." && pwd)"
LOG_DIR="${SLOTHMORE_HOME:-${LAUNCHER_HOME:-$HOME/.slothmore}}"
UID_NUM="$(id -u)"
DRY="${SLOTHMORE_DRY_RUN:-${LAUNCHER_DRY_RUN:-0}}"

lctl() {
  if [ "$DRY" = "1" ]; then
    echo "[dry run] launchctl $*"
    return 0
  fi
  launchctl "$@"
}

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

uninstall() {
  lctl bootout "gui/$UID_NUM/$MENUBAR_LABEL" 2>/dev/null || true
  lctl bootout "gui/$UID_NUM/$SERVER_LABEL" 2>/dev/null || true
  rm -f "$MENUBAR_PLIST" "$SERVER_PLIST"
  echo "Slothmore login items removed. Your settings and logs in $LOG_DIR are untouched."
}

status() {
  for pair in "$SERVER_LABEL:$SERVER_PLIST" "$MENUBAR_LABEL:$MENUBAR_PLIST"; do
    label="${pair%%:*}"; plist="${pair#*:}"
    if [ -f "$plist" ]; then
      if [ "$DRY" != "1" ] && launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
        echo "$label: installed, loaded"
      else
        echo "$label: installed, not loaded"
      fi
    else
      echo "$label: not installed"
    fi
  done
}

install() {
  local port=7777 with_menubar=0
  local folders=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --port) port="${2:?--port needs a number}"; shift 2 ;;
      --port=*) port="${1#--port=}"; shift ;;
      --with-menubar) with_menubar=1; shift ;;
      -h|--help) usage 0 ;;
      --*) echo "Unknown option: $1" >&2; usage 2 ;;
      *) folders+=("$1"); shift ;;
    esac
  done
  case "$port" in ''|*[!0-9]*) echo "Port must be a number" >&2; exit 2 ;; esac

  local python
  python="$(command -v python3 || true)"
  if [ -z "$python" ]; then
    echo "python3 not found. Install the Xcode command line tools: xcode-select --install" >&2
    exit 1
  fi

  local menubar_python=""
  if [ "$with_menubar" = "1" ]; then
    # The helper needs rumps. Use the first python3 that already has it.
    for cand in "$python" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
      if [ -x "$cand" ] && "$cand" -c "import rumps" >/dev/null 2>&1; then
        menubar_python="$cand"; break
      fi
    done
    if [ -z "$menubar_python" ]; then
      echo "The menu bar helper needs rumps. Install it first:   $python -m pip install --user rumps" >&2
      echo "Then run this command again. (The server login item was not changed.)" >&2
      exit 1
    fi
  fi

  mkdir -p "$AGENTS" "$LOG_DIR"

  # plistlib writes valid XML even for paths with spaces or odd characters.
  # The PATH from this shell is saved so npm, node and Homebrew tools work under launchd too.
  # Prefer bin/slothmore when present so --version / --no-open stay consistent with the CLI.
  "$python" - "$SERVER_PLIST" "$SERVER_LABEL" "$APP_DIR" "$python" "$PATH" "$LOG_DIR/service.log" "$port" ${folders[@]+"${folders[@]}"} <<'PY'
import os, plistlib, sys
plist_path, label, d, py, path, log, port, *watch = sys.argv[1:]
cli = os.path.join(d, "bin", "slothmore")
if os.path.isfile(cli):
    args = [py, cli, "--no-open", "--port", port]
else:
    args = [py, os.path.join(d, "server.py"), "--no-open", "--port", port]
for w in watch:
    args += ["--watch", os.path.abspath(os.path.expanduser(w))]
env = {"PATH": path, "SLOTHMORE_SERVICE": "1"}
home = os.environ.get("SLOTHMORE_HOME") or os.environ.get("LAUNCHER_HOME")
if home:
    env["SLOTHMORE_HOME"] = home
data = {
    "Label": label,
    "ProgramArguments": args,
    "WorkingDirectory": d,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Interactive",
    "EnvironmentVariables": env,
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
with open(plist_path, "wb") as f:
    plistlib.dump(data, f)
PY

  lctl bootout "gui/$UID_NUM/$SERVER_LABEL" 2>/dev/null || true
  lctl bootstrap "gui/$UID_NUM" "$SERVER_PLIST"
  echo "Server login item installed: http://127.0.0.1:$port (log: $LOG_DIR/service.log)"

  if [ "$with_menubar" = "1" ]; then
    "$menubar_python" - "$MENUBAR_PLIST" "$MENUBAR_LABEL" "$MACOS_DIR" "$menubar_python" "$PATH" "$LOG_DIR/menubar.log" "$port" <<'PY'
import os, plistlib, sys
plist_path, label, d, py, path, log, port = sys.argv[1:]
data = {
    "Label": label,
    "ProgramArguments": [py, os.path.join(d, "menubar.py"), "--url", "http://127.0.0.1:%s" % port],
    "WorkingDirectory": d,
    "RunAtLoad": True,
    # Relaunch after a crash, but not after you choose "Quit" in the menu (clean exit).
    "KeepAlive": {"SuccessfulExit": False},
    "LimitLoadToSessionType": "Aqua",
    "ProcessType": "Interactive",
    "EnvironmentVariables": {"PATH": path},
    "StandardOutPath": log,
    "StandardErrorPath": log,
}
with open(plist_path, "wb") as f:
    plistlib.dump(data, f)
PY
    lctl bootout "gui/$UID_NUM/$MENUBAR_LABEL" 2>/dev/null || true
    lctl bootstrap "gui/$UID_NUM" "$MENUBAR_PLIST"
    echo "Menu bar helper installed (log: $LOG_DIR/menubar.log)"
  fi
  echo "Next: run '$APP_DIR/bin/slothmore open' (or 'slothmore open') to open it signed in, then use 'Install Slothmore' in Chrome or File > Add to Dock in Safari."
}

cmd="${1:-}"
[ $# -gt 0 ] && shift
case "$cmd" in
  install) install "$@" ;;
  uninstall|--uninstall) uninstall ;;
  status) status ;;
  -h|--help|help|"") usage 0 ;;
  *) echo "Unknown command: $cmd" >&2; usage 2 ;;
esac
