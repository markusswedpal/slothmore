#!/usr/bin/env python3
"""Slothmore: a local dashboard that starts web servers and runs scripts from folders.

Standard library only (tested on Python 3.12 to 3.15). Listens on 127.0.0.1 only.

    slothmore
    slothmore --watch ~/Projects
    open http://127.0.0.1:7777
"""
import argparse
import codecs
import collections
import fcntl
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import pty
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

try:  # optional: better CPU/memory numbers and port owners. Everything works without it.
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on the machine
    psutil = None

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
__version__ = "0.2.2"
APP_NAME = "Slothmore"
DEFAULT_PORT = 7777


def _resolve_home():
    """Prefer ~/.slothmore. Copy from ~/.launcher once if that is all that exists."""
    env = os.environ.get("SLOTHMORE_HOME") or os.environ.get("LAUNCHER_HOME")
    if env:
        return Path(env)
    new = Path.home() / ".slothmore"
    old = Path.home() / ".launcher"
    if new.exists() or not (old / "config.json").exists():
        return new
    try:
        shutil.copytree(old, new, dirs_exist_ok=True)
        sys.stderr.write("Slothmore: copied settings from %s to %s\n" % (old, new))
    except OSError as e:
        sys.stderr.write(
            "Slothmore: could not copy %s to %s (%s); using the old folder.\n" % (old, new, e)
        )
        return old
    return new


HOME_DIR = _resolve_home()
CONFIG_PATH = HOME_DIR / "config.json"
IGNORED = {"node_modules", "__pycache__"}
MAX_CHARS = 400000
LOGS_DIR = HOME_DIR / "logs"
TOKEN_PATH = HOME_DIR / "token"
STATE_PATH = HOME_DIR / "server.json"
LOG_PATH = HOME_DIR / "slothmore.log"
LOG = logging.getLogger("slothmore")
_ACTIVE_TOKEN = None          # current access token; never written to the log
_SECRET_VALUES = set()        # env values (and the token) stripped from log lines
COOKIE_NAME = "slothmore_token"
COOKIE_MAX_AGE = 400 * 24 * 3600   # long-lived so an installed PWA stays signed in
MAX_BODY = 1000000
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; "
       "img-src 'self' data: https:; font-src 'self'; connect-src 'self'; manifest-src 'self'; "
       "worker-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _chmod(path, mode):
    try:
        os.chmod(str(path), mode)
    except OSError:
        LOG.debug("could not chmod %s to %o", path, mode, exc_info=True)


def private_open(path, mode="w"):
    """Open a text file that only the current user can read (0600), creating it if needed."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(str(path), flags, 0o600)
    _chmod(path, 0o600)
    return os.fdopen(fd, mode, encoding="utf-8", errors="replace", buffering=1)


def _remember_secret(value):
    """Remember a secret so it is stripped from later log lines (skips very short values)."""
    s = str(value or "")
    if len(s) >= 6:
        _SECRET_VALUES.add(s)


def _redact(text):
    """Remove the access token, cookie/Bearer values, and known env values from a log line."""
    s = str(text)
    tok = _ACTIVE_TOKEN
    if tok:
        s = s.replace(tok, "[redacted]")
    for secret in list(_SECRET_VALUES):
        if secret and secret in s:
            s = s.replace(secret, "[redacted]")
    s = re.sub(r"([?&]token=)[^\s&]+", r"\1[redacted]", s, flags=re.I)
    s = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-]+", r"\1[redacted]", s, flags=re.I)
    s = re.sub(r"(" + re.escape(COOKIE_NAME) + r"=)[^;\s]+", r"\1[redacted]", s)
    return s


def _refresh_secret_values(cfg=None):
    """Track env values currently in the config so they never appear in the log."""
    try:
        if cfg is None:
            cfg = load_config()
        for prefs in (cfg.get("prefs") or {}).values():
            env = prefs.get("env") if isinstance(prefs, dict) else None
            if isinstance(env, dict):
                for v in env.values():
                    _remember_secret(v)
    except Exception:
        LOG.debug("could not refresh redacted env values", exc_info=True)


class _RedactFormatter(logging.Formatter):
    def format(self, record):
        return _redact(super().format(record))


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the log and its backups at 0600."""

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(self.baseFilename, flags, 0o600)
        _chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)

    def doRollover(self):
        super().doRollover()
        _chmod(self.baseFilename, 0o600)
        for i in range(1, self.backupCount + 1):
            bak = "%s.%d" % (self.baseFilename, i)
            if os.path.exists(bak):
                _chmod(bak, 0o600)


def setup_logging(level=None, interactive=None):
    """Log to ~/.slothmore/slothmore.log (rotating, 1 MB x 3, 0600) and to stderr when interactive.

    Level comes from `level`, then SLOTHMORE_LOG_LEVEL, then INFO. Safe to call more than once.
    """
    raw = (level or os.environ.get("SLOTHMORE_LOG_LEVEL") or "info")
    name = str(raw).upper()
    if name not in logging._nameToLevel or name == "NOTSET":
        name = "INFO"
    LOG.handlers.clear()
    LOG.setLevel(getattr(logging, name))
    LOG.propagate = False
    fmt = _RedactFormatter("%(asctime)s %(levelname)s %(message)s")
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        _chmod(HOME_DIR, 0o700)
        fh = _PrivateRotatingFileHandler(str(LOG_PATH), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError as e:
        sys.stderr.write("Slothmore: could not open log file %s (%s)\n" % (LOG_PATH, e))
    if interactive is None:
        interactive = sys.stderr.isatty() and not os.environ.get("SLOTHMORE_SERVICE")
    if interactive:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)


def private_write(path, data):
    """Write bytes or text to path with 0600 permissions (atomic replace)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = Path(str(path) + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    _chmod(tmp, 0o600)
    os.replace(str(tmp), str(path))


def ensure_private_dirs():
    """~/.slothmore is 0700; config, token and logs are 0600 (also fixes files from older versions)."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    _chmod(HOME_DIR, 0o700)
    for root, dirs, files in os.walk(str(HOME_DIR)):
        for d in dirs:
            _chmod(os.path.join(root, d), 0o700)
        for f in files:
            fp = os.path.join(root, f)
            if not os.path.islink(fp):
                _chmod(fp, 0o600)


def load_token(reset=False, create=True):
    """Return the access token from ~/.slothmore/token, creating it if missing (or if reset)."""
    global _ACTIVE_TOKEN
    if not reset:
        try:
            tok = TOKEN_PATH.read_text().strip()
            if re.match(r"^[A-Za-z0-9_\-]{32,128}$", tok):
                _chmod(TOKEN_PATH, 0o600)
                _ACTIVE_TOKEN = tok
                _remember_secret(tok)
                return tok
        except OSError:
            LOG.debug("could not read token file", exc_info=True)
    if not create:
        return None
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    _chmod(HOME_DIR, 0o700)
    tok = secrets.token_urlsafe(32)
    private_write(TOKEN_PATH, tok + "\n")
    _ACTIVE_TOKEN = tok
    _remember_secret(tok)
    return tok


def dashboard_url(port, token=None):
    base = "http://127.0.0.1:%d/" % port
    return base + ("?token=" + quote(token) if token else "")
LOG_MAX_LINES = 2000          # per stored run log (ring buffer, oldest lines dropped)
LOG_MAX_BYTES = 512 * 1024    # per stored run log
HISTORY_KEEP = 25             # runs kept per item; older logs are deleted
IS_MAC = sys.platform == "darwin"
ITEM_ID_RE = re.compile(r"^[0-9a-f]{10}$")
RUN_ID_RE = re.compile(r"^[0-9]{10,16}-[0-9]{1,9}$")
GROUP_ID_RE = re.compile(r"^[0-9a-f]{8}$")
NOTIFY_DEFAULTS = {
    "enabled": False, "all_items": True, "on_finish": True, "on_fail": True, "on_prompt": True,
    "min_seconds": 10, "browser": True, "macos": True,
}

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]"
)
URL_PORT_RE = re.compile(
    r"(https?)://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d{2,5})", re.I
)
LOCAL_URL_RE = re.compile(
    r"Local:\s*(https?)://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d{2,5})", re.I
)
HTTP_SERVER_PORT_RE = re.compile(
    r"Serving HTTP on\s+(?:\S+)\s+port\s+(\d{2,5})", re.I
)
TOKEN_URL_RE = re.compile(
    r"(https?)://(?:localhost|127\.0\.0\.1):(\d{2,5})/?[^\s\"'<>]*?[?&][A-Za-z_]*(?:TOKEN|token)=[A-Za-z0-9_\-]+"
)
LISTEN_PORT_RE = re.compile(
    r"(?:listening|running|serving|started|ready)\s+(?:on|at)\s+(?:[^\n]*?\bport\s+|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):)(\d{2,5})\b",
    re.I,
)
PORT_BANNER_HINT_RE = re.compile(
    r"\b(local|running|listening|serving|uvicorn|werkzeug|flask|vite|next|ready|address|server)\b", re.I
)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WATCH_IGNORED = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "env",
    "dist", "build", ".next", ".cache", ".pytest_cache",
}
YN_RE = re.compile(
    r"[\[(]\s*(?:[yY](?:es)?\s*[/|]\s*[nN](?:o)?|[nN](?:o)?\s*[/|]\s*[yY](?:es)?)\s*[\])]"
)
WEB_PY_RE = re.compile(
    r"\b(flask|fastapi|uvicorn|http\.server|aiohttp|bottle|socketserver|cherrypy|tornado)\b", re.I
)
README_RANK = {
    "readme.md": 0,
    "readme.markdown": 1,
    "readme.rst": 2,
    "readme.txt": 3,
    "readme": 4,
}
README_MAX_BYTES = 1024 * 1024
README_ASSET_MAX = 5 * 1024 * 1024
README_ASSET_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon",
}
# SVG is rejected: it can carry scripts.
PROJECT_MARKERS = {
    "package.json", "slothmore.json", "launcher.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "Gemfile", "Makefile", "CMakeLists.txt",
    "composer.json", "Pipfile", "poetry.lock", "requirements.txt", "tsconfig.json",
}
CATCHALL_DIRS = {
    "desktop", "documents", "downloads", "pictures", "movies", "music",
    "library", "applications", "public", "tmp", "temp",
}


mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("font/ttf", ".ttf")


# --------------------------------------------------------------------------- config

CFG_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, error, **extra):
        Exception.__init__(self, error)
        self.status = status
        self.payload = dict(error=error, **extra)


def tilde(p):
    p = str(p)
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def expand(p):
    return os.path.abspath(os.path.expanduser(str(p)))


def load_config():
    try:
        with open(str(CONFIG_PATH)) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("watch", [])
    cfg.setdefault("pinned", [])
    if not isinstance(cfg.get("prefs"), dict):
        cfg["prefs"] = {}
    return cfg


def save_config(cfg):
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    _chmod(HOME_DIR, 0o700)
    private_write(CONFIG_PATH, json.dumps(cfg, indent=2))
    SCAN_CACHE.update(key=None, items=None)
    _refresh_secret_values(cfg)


# --------------------------------------------------------------------------- detection


def item_id(path):
    return hashlib.sha1(str(path).encode()).hexdigest()[:10]


def read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def find_python_exe(folder):
    for rel in (".venv/bin/python", "venv/bin/python", "env/bin/python"):
        p = Path(folder) / rel
        if p.exists():
            return str(p)
    return sys.executable


def find_python(folder):
    return shlex.quote(find_python_exe(folder))


def find_html_files(folder):
    try:
        names = [
            p.name for p in Path(folder).iterdir()
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() == ".html"
        ]
    except OSError:
        return []
    return sorted(names, key=lambda n: (0 if n.lower() == "index.html" else 1, n.lower()))


def lang_of(kind, cmd):
    c = (cmd or "").strip()
    if kind == "Node" or c.startswith(("npm ", "node ", "npx ", "yarn ", "pnpm ", "bun ")):
        return "node"
    if "python" in c or ".py" in c:
        return "python"
    return ""


TOOL_HINTS = {
    "npm": "Install Node.js (it includes npm): https://nodejs.org or `brew install node`.",
    "node": "Install Node.js: https://nodejs.org or `brew install node`.",
    "pnpm": "Install pnpm: `npm install -g pnpm` or `brew install pnpm`.",
    "yarn": "Install Yarn: `npm install -g yarn` or `corepack enable`.",
    "bun": "Install Bun: `brew install oven-sh/bun/bun`.",
    "uv": "Install uv: `brew install uv`.",
    "poetry": "Install Poetry: `brew install poetry` or `pipx install poetry`.",
    "make": "Install the Xcode command line tools: `xcode-select --install`.",
    "docker": "Install Docker Desktop (or OrbStack) and make sure it is running.",
    "go": "Install Go: `brew install go`.",
    "cargo": "Install Rust: `brew install rustup && rustup-init`.",
    "deno": "Install Deno: `brew install deno`.",
    "bundle": "Install Ruby and Bundler: `brew install ruby` then `gem install bundler`.",
    "jupyter": "Install JupyterLab: `pip3 install jupyterlab` or `brew install jupyterlab`.",
    "bash": "bash was not found on PATH.",
    "ruby": "Install Ruby: `brew install ruby`.",
}
PKG_PREFERRED = ("dev", "start", "serve", "preview")
MAKE_PREFERRED = ("dev", "run", "start", "serve")
SHELL_ENTRY = ("run.sh", "start.sh", "dev.sh")
NODE_ENTRY = ("index.js", "server.js", "main.js")
PY_MAIN_RE = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)
MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-/]*)\s*:(?![=:])", re.M)
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
COMPOSE_PORT_RE = re.compile(r"""^\s*-\s*["']?(?:[\d.]+:)?(\d{2,5}):\d{1,5}(?:/tcp)?["']?\s*$""", re.M)
TOML_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")
TOML_KV_RE = re.compile(r"""^\s*["']?([A-Za-z0-9_.\-]+)["']?\s*=\s*["']([^"']+)["']""")


def which(tool):
    return shutil.which(tool)


def _read_text(p, limit=200000):
    try:
        with open(str(p), "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def toml_section(text, section):
    """Return {key: value} string pairs of one TOML table. Small parser: enough for [project.scripts]."""
    out, inside = {}, False
    for line in text.splitlines():
        m = TOML_SECTION_RE.match(line)
        if m:
            inside = m.group(1).strip().replace(" ", "") == section
            continue
        if inside:
            kv = TOML_KV_RE.match(line)
            if kv:
                out[kv.group(1)] = kv.group(2)
    return out


def detect_pm(folder, stop_at=None):
    """Package manager from packageManager or a lockfile, looking up a few parent folders (monorepos)."""
    d = Path(folder)
    for _ in range(4):
        pkg = read_json(d / "package.json") if (d / "package.json").exists() else None
        if isinstance(pkg, dict) and isinstance(pkg.get("packageManager"), str):
            name = pkg["packageManager"].split("@", 1)[0].strip().lower()
            if name in ("npm", "pnpm", "yarn", "bun"):
                return name
        if (d / "pnpm-lock.yaml").exists() or (d / "pnpm-workspace.yaml").exists():
            return "pnpm"
        if (d / "yarn.lock").exists():
            return "yarn"
        if (d / "bun.lockb").exists() or (d / "bun.lock").exists():
            return "bun"
        if (d / "package-lock.json").exists():
            return "npm"
        if stop_at and str(d) == str(stop_at):
            break
        if d.parent == d:
            break
        d = d.parent
    return "npm"


def pm_run_argv(pm, script):
    if pm == "npm":
        if script == "start":
            return ["npm", "start"], "npm start"
        return ["npm", "run", script], "npm run %s" % script
    if pm == "yarn":
        return ["yarn", "run", script], "yarn %s" % script
    return [pm, "run", script], "%s run %s" % (pm, script)


def _node_modules_present(folder, levels=4):
    d = Path(folder)
    for _ in range(levels):
        if (d / "node_modules").is_dir():
            return True
        if d.parent == d:
            break
        d = d.parent
    return False


def _workspace_root(folder):
    d = Path(folder).parent
    for _ in range(4):
        if (d / "pnpm-workspace.yaml").exists():
            return d
        pkg = read_json(d / "package.json") if (d / "package.json").exists() else None
        if isinstance(pkg, dict) and pkg.get("workspaces"):
            return d
        if d.parent == d:
            break
        d = d.parent
    return None


def _has_venv(folder):
    return any((Path(folder) / v / "bin" / "python").exists() for v in (".venv", "venv", "env"))


def _choice(key, label, argv, display, web=False, kind=None, port=None):
    return {"key": key, "label": label, "argv": argv, "display": display, "web": web, "kind": kind, "port": port}


def _apply_choice(info, choices, default_key, choice_label, prefs, kind, lang, tool=None):
    """Set run command from a list of choices: saved pick > default > needs a pick."""
    info["choices"] = [{"key": c["key"], "label": c["label"]} for c in choices]
    info["choice_label"] = choice_label
    saved = (prefs or {}).get("choice")
    keys = [c["key"] for c in choices]
    key = saved if saved in keys else (default_key if default_key in keys else None)
    info["choice"] = key
    info["kind"] = kind
    info["lang"] = lang
    info["detected"] = True
    if key is None:
        info["needs_choice"] = True
        _require_tool(info, tool or (choices[0]["argv"][0] if choices and choices[0]["argv"] else None))
        return info
    c = choices[keys.index(key)]
    info.update(cmd=c["display"], argv=c["argv"], display_cmd=c["display"], web=bool(c["web"]))
    if c.get("kind"):
        info["kind"] = c["kind"]
    if c.get("port"):
        info["port"] = c["port"]
    _require_tool(info, tool or (c["argv"][0] if c["argv"] else None))
    return info


def _require_tool(info, tool):
    if not tool or "/" in tool:
        return info
    if which(tool):
        return info
    info["missing_tool"] = tool
    info["error"] = "%s isn't installed or isn't on PATH. %s" % (tool, TOOL_HINTS.get(tool, ""))
    info["error"] = info["error"].strip()
    return info


def _python_install(folder):
    folder = Path(folder)
    has_req = (folder / "requirements.txt").exists()
    has_py = (folder / "pyproject.toml").exists()
    if not (has_req or has_py) or _has_venv(folder):
        return None
    if (folder / "uv.lock").exists() or (has_py and not has_req and which("uv") and not (folder / "poetry.lock").exists()):
        return {"argv": ["uv", "sync"], "display": "uv sync", "tool": "uv", "cwd": str(folder)}
    if (folder / "poetry.lock").exists():
        return {"argv": ["poetry", "install"], "display": "poetry install", "tool": "poetry", "cwd": str(folder)}
    py = sys.executable
    step = ".venv/bin/pip install -r requirements.txt" if has_req else ".venv/bin/pip install -e ."
    script = "set -e; %s -m venv .venv; .venv/bin/pip install --upgrade pip; %s" % (shlex.quote(py), step)
    return {"argv": ["/bin/sh", "-c", script], "display": "python -m venv .venv && " + step.replace(".venv/bin/", ""),
            "tool": None, "cwd": str(folder)}


def _node_install(folder, pm):
    if _node_modules_present(folder):
        return None
    root = _workspace_root(folder)
    cwd = str(root) if root else str(folder)
    return {"argv": [pm, "install"], "display": "%s install" % pm, "tool": pm, "cwd": cwd}


def _attach_install(info, inst):
    if inst:
        info["install"] = inst
        info["deps_missing"] = True
    return info


def _py_files(path):
    try:
        return sorted(p.name for p in path.glob("*.py") if not p.name.startswith("."))
    except OSError:
        return []


def _pkg_finish(info, path, choices, default, pm, prefs):
    _apply_choice(info, choices, default, "Script", prefs, "Node", "node", tool=pm)
    info["pm"] = pm
    _attach_install(info, _node_install(path, pm))
    if len(choices) == 1 and not info["needs_choice"]:
        info["choices"] = None
    return info


def detect(path, prefs=None):
    """Work out how to run a folder (or a single script file). slothmore.json / launcher.json always wins,
    then AI project types (MCP servers, agent apps, model UIs), then the generic checks."""
    p = Path(path)
    if p.is_file():
        return _detect_base(p, prefs)
    try:
        sig = _ai_signals(p)
    except Exception:
        LOG.debug("folder signature failed for %s", path, exc_info=True)
        sig = None
    info = _detect_base(p, prefs, sig)
    if sig is not None:
        try:
            _ai_annotate(info, p, sig, prefs)
        except Exception:
            LOG.debug("AI annotation failed for %s", p, exc_info=True)
    return info


def _detect_base(path, prefs=None, sig=None):
    """Work out how to run a folder (or a single script file). slothmore.json / launcher.json always wins."""
    path = Path(path)
    prefs = prefs or {}
    info = {
        "name": path.name,
        "kind": "Other",
        "cmd": None,
        "argv": None,
        "display_cmd": None,
        "web": False,
        "static": False,
        "html_files": [],
        "html_file": None,
        "cwd": str(path),
        "port": None,
        "env": {},
        "error": None,
        "detected": False,
        "choices": None,
        "choice": None,
        "choice_label": None,
        "needs_choice": False,
        "missing_tool": None,
        "install": None,
        "deps_missing": False,
        "stop_signal": None,
    }

    if path.is_file():
        info["cwd"] = str(path.parent)
        q = shlex.quote(path.name)
        ext = path.suffix.lower()
        info["detected"] = True
        if ext == ".py":
            info.update(
                kind="Script",
                cmd="%s -u %s" % (find_python(path.parent), q),
                argv=[find_python_exe(path.parent), "-u", path.name],
                display_cmd="python %s" % path.name,
            )
        elif ext == ".sh":
            info.update(
                kind="Script",
                cmd="bash %s" % q,
                argv=["bash", path.name],
                display_cmd="bash %s" % path.name,
            )
            _require_tool(info, "bash")
        elif ext in (".js", ".mjs", ".cjs"):
            info.update(
                kind="Script",
                cmd="node %s" % q,
                argv=["node", path.name],
                display_cmd="node %s" % path.name,
            )
            _require_tool(info, "node")
        elif ext == ".html":
            info.update(
                kind="HTML",
                web=True,
                static=True,
                html_files=[path.name],
                html_file=path.name,
                display_cmd="python -m http.server",
            )
        else:
            info["detected"] = False
            info["error"] = "Don't know how to run this file type."
        info["lang"] = lang_of(info["kind"], info["cmd"])
        return info

    # 1. slothmore.json (or launcher.json)
    override = None
    for _ov_name in ("slothmore.json", "launcher.json"):
        if (path / _ov_name).exists():
            override = read_json(path / _ov_name)
            if isinstance(override, dict):
                break
    if isinstance(override, dict) and override.get("cmd"):
        web = bool(override.get("web", False))
        info.update(
            name=str(override.get("name", path.name)),
            cmd=str(override["cmd"]),
            display_cmd=str(override["cmd"]),
            web=web,
            kind=str(override.get("kind", "Web" if web else "Script")),
            port=override.get("port"),
            env={str(k): str(v) for k, v in (override.get("env") or {}).items()},
            detected=True,
        )
        info["lang"] = lang_of(info["kind"], info["cmd"])
        return info

    # 1b. AI projects: MCP servers, LangGraph / CrewAI apps, Streamlit / Gradio, ComfyUI, Open WebUI
    if sig is not None:
        try:
            ai = _detect_ai(path, info, prefs, sig)
        except Exception:
            LOG.debug("AI project detection failed for %s", path, exc_info=True)
            ai = None
        if ai:
            return ai

    # 2. package.json with dev / start / serve / preview (other scripts: picker, used only if nothing else matches)
    pkg = read_json(path / "package.json") if (path / "package.json").exists() else None
    pkg_choices, pkg_pm = None, None
    if isinstance(pkg, dict):
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        pkg_pm = detect_pm(path)
        if scripts:
            pkg_choices = []
            for name in list(scripts.keys())[:60]:
                argv, disp = pm_run_argv(pkg_pm, str(name))
                pkg_choices.append(_choice(str(name), "%s  ·  %s" % (name, str(scripts[name])[:60]), argv, disp,
                                           web=str(name) in PKG_PREFERRED))
            default = next((k for k in PKG_PREFERRED if k in scripts), None)
            if default:
                return _pkg_finish(info, path, pkg_choices, default, pkg_pm, prefs)

    # 3. Makefile with a dev/run/start/serve target
    makefile = next((path / n for n in ("Makefile", "makefile", "GNUmakefile") if (path / n).is_file()), None)
    make_targets = []
    if makefile:
        seen = set()
        for m in MAKE_TARGET_RE.finditer(_read_text(makefile)):
            t = m.group(1)
            if t.startswith(".") or "%" in t or t in seen:
                continue
            seen.add(t)
            make_targets.append(t)
    if make_targets and any(t in MAKE_PREFERRED for t in make_targets):
        choices = [_choice(t, "make " + t, ["make", t], "make " + t, web=t in ("dev", "serve")) for t in make_targets[:40]]
        default = next(t for t in MAKE_PREFERRED if t in make_targets)
        return _apply_choice(info, choices, default, "Target", prefs, "Make", "", tool="make")

    # 4. Python entry files
    for f in ("app.py", "main.py", "server.py", "run.py"):
        if (path / f).exists():
            try:
                web = bool(WEB_PY_RE.search((path / f).read_text(errors="ignore")[:30000]))
            except OSError:
                web = False
            info.update(
                kind="Python web" if web else "Script",
                web=web,
                cmd="%s -u %s" % (find_python(path), shlex.quote(f)),
                argv=[find_python_exe(path), "-u", f],
                display_cmd="python %s" % f,
                detected=True,
            )
            info["lang"] = "python"
            _attach_install(info, _python_install(path))
            return info

    # 5. pyproject.toml entry points
    if (path / "pyproject.toml").exists():
        text = _read_text(path / "pyproject.toml")
        eps = toml_section(text, "project.scripts")
        eps.update({k: v for k, v in toml_section(text, "tool.poetry.scripts").items() if k not in eps})
        if eps:
            if (path / "uv.lock").exists():
                runner = "uv"
            elif (path / "poetry.lock").exists() or ("[tool.poetry]" in text and which("poetry")):
                runner = "poetry"
            elif not _has_venv(path) and which("uv"):
                runner = "uv"
            else:
                runner = "python"
            choices = []
            for name, target in list(eps.items())[:30]:
                if runner in ("uv", "poetry"):
                    argv, disp = [runner, "run", name], "%s run %s" % (runner, name)
                else:
                    venv_bin = next((Path(path) / v / "bin" / name for v in (".venv", "venv", "env") if (Path(path) / v / "bin" / name).exists()), None)
                    if venv_bin:
                        argv, disp = [str(venv_bin)], name
                    else:
                        mod, _, func = target.partition(":")
                        mod = mod.strip()
                        func = func.strip().split(".")[0] or None
                        if func:
                            code = "import sys; from %s import %s as _m; sys.exit(_m())" % (mod, func)
                            argv, disp = [find_python_exe(path), "-u", "-c", code], "python -c 'from %s import %s; %s()'" % (mod, func, func)
                        else:
                            argv, disp = [find_python_exe(path), "-u", "-m", mod], "python -m %s" % mod
                choices.append(_choice(name, "%s  ·  %s" % (name, target), argv, disp))
            default = choices[0]["key"] if len(choices) == 1 else None
            _apply_choice(info, choices, default, "Entry point", prefs, "Python", "python",
                          tool=runner if runner != "python" else None)
            if len(choices) == 1:
                info["choices"] = None
            _attach_install(info, _python_install(path))
            return info

    # 6. Deno
    deno_cfg = next((path / n for n in ("deno.json", "deno.jsonc") if (path / n).is_file()), None)
    if deno_cfg:
        raw = re.sub(r"(?m)^\s*//.*$", "", _read_text(deno_cfg))
        try:
            dj = json.loads(raw)
        except ValueError:
            dj = {}
        tasks = dj.get("tasks") if isinstance(dj, dict) and isinstance(dj.get("tasks"), dict) else {}
        if tasks:
            choices = [_choice(t, "%s  ·  %s" % (t, str(v)[:60]), ["deno", "task", t], "deno task %s" % t, web=t in ("dev", "start", "serve"))
                       for t, v in list(tasks.items())[:40]]
            default = next((t for t in ("dev", "start", "serve") if t in tasks), None)
            _apply_choice(info, choices, default, "Task", prefs, "Deno", "", tool="deno")
            if len(choices) == 1 and not info["needs_choice"]:
                info["choices"] = None
            return info

    # 7. Go, Rust
    if (path / "go.mod").exists():
        info.update(kind="Go", cmd="go run .", argv=["go", "run", "."], display_cmd="go run .", detected=True, lang="")
        info["web"] = bool(re.search(r"net/http|gin-gonic|labstack/echo|gofiber", _read_text(path / "go.mod") + _read_text(path / "main.go")))
        return _require_tool(info, "go")
    if (path / "Cargo.toml").exists():
        info.update(kind="Rust", cmd="cargo run", argv=["cargo", "run"], display_cmd="cargo run", detected=True, lang="")
        info["web"] = bool(re.search(r"\b(axum|actix-web|rocket|warp|hyper)\b", _read_text(path / "Cargo.toml")))
        return _require_tool(info, "cargo")

    # 8. Ruby
    if (path / "bin" / "rails").is_file():
        info.update(kind="Rails", web=True, cmd="bin/rails server", argv=["bin/rails", "server", "-p", "{PORT}"],
                    display_cmd="bin/rails server", detected=True, lang="")
        return _require_tool(info, "ruby")
    if (path / "config.ru").is_file():
        info.update(kind="Ruby web", web=True, cmd="bundle exec rackup", argv=["bundle", "exec", "rackup", "-p", "{PORT}"],
                    display_cmd="bundle exec rackup", detected=True, lang="")
        return _require_tool(info, "bundle")

    # 9. docker compose
    compose = next((path / n for n in COMPOSE_FILES if (path / n).is_file()), None)
    if compose:
        m = COMPOSE_PORT_RE.search(_read_text(compose))
        port = int(m.group(1)) if m else None
        info.update(kind="Compose", cmd="docker compose up", argv=["docker", "compose", "-f", compose.name, "up"],
                    display_cmd="docker compose up", detected=True, lang="", web=bool(port), port=port,
                    stop_signal="INT")
        return _require_tool(info, "docker")

    # 10. shell entry scripts
    for f in SHELL_ENTRY:
        if (path / f).is_file():
            info.update(kind="Script", cmd="bash %s" % f, argv=["bash", f], display_cmd="bash %s" % f, detected=True, lang="")
            return _require_tool(info, "bash")

    # 11. node entry files (package.json "main", index.js, server.js, main.js) when no script matched
    if not pkg_choices:
        main = pkg.get("main") if isinstance(pkg, dict) and isinstance(pkg.get("main"), str) else None
        for f in ([main] if main else []) + list(NODE_ENTRY):
            if f and ".." not in f and (path / f).is_file():
                web = bool(re.search(r"\b(express|fastify|koa|http\.createServer|listen\()", _read_text(path / f)))
                info.update(kind="Node", cmd="node %s" % f, argv=["node", f], display_cmd="node %s" % f,
                            detected=True, lang="node", web=web)
                _require_tool(info, "node")
                if isinstance(pkg, dict) and (pkg.get("dependencies") or pkg.get("devDependencies")):
                    _attach_install(info, _node_install(path, pkg_pm or "npm"))
                return info

    # 12. static index.html
    htmls = find_html_files(path)
    if (path / "index.html").exists():
        info.update(
            kind="HTML",
            web=True,
            static=True,
            html_files=htmls or ["index.html"],
            html_file="index.html",
            display_cmd="python -m http.server",
            detected=True,
        )
        info["lang"] = ""
        return info

    # 13. Python files
    pys = _py_files(path)
    if len(pys) == 1:
        info.update(
            kind="Script",
            cmd="%s -u %s" % (find_python(path), shlex.quote(pys[0])),
            argv=[find_python_exe(path), "-u", pys[0]],
            display_cmd="python %s" % pys[0],
            detected=True,
        )
        info["lang"] = "python"
        _attach_install(info, _python_install(path))
        return info
    if htmls:
        info.update(
            kind="HTML",
            web=True,
            static=True,
            html_files=htmls,
            html_file=htmls[0],
            display_cmd="python -m http.server",
            detected=True,
        )
        info["lang"] = ""
        return info
    if len(pys) > 1:
        mains = [f for f in pys[:40] if PY_MAIN_RE.search(_read_text(path / f))]
        choices = [_choice(f, f + ("  ·  has __main__" if f in mains else ""),
                           [find_python_exe(path), "-u", f], "python %s" % f) for f in pys[:40]]
        default = mains[0] if len(mains) == 1 else None
        _apply_choice(info, choices, default, "File", prefs, "Script", "python")
        _attach_install(info, _python_install(path))
        return info

    # 14. package.json scripts without a usual name: let the user pick one
    if pkg_choices:
        return _pkg_finish(info, path, pkg_choices, None, pkg_pm, prefs)

    # 15. Makefile with other targets
    if make_targets:
        choices = [_choice(t, "make " + t, ["make", t], "make " + t) for t in make_targets[:40]]
        return _apply_choice(info, choices, None, "Target", prefs, "Make", "", tool="make")

    # 16. Jupyter notebooks
    try:
        nbs = sorted(p.name for p in path.glob("*.ipynb"))
    except OSError:
        nbs = []
    if nbs:
        info.update(kind="Jupyter", web=True, cmd="jupyter lab", detected=True, lang="python",
                    argv=["jupyter", "lab", "--no-browser", "--port={PORT}", "--ServerApp.port_retries=0"],
                    display_cmd="jupyter lab")
        return _require_tool(info, "jupyter")

    info["error"] = "No run command found. Add a slothmore.json (or launcher.json)."
    info["lang"] = lang_of(info["kind"], info["cmd"])
    return info



# --------------------------------------------------------------------------- AI project detection
# Everything here only reads files in the folder. Nothing calls a model or the network.

AI_DEP_FILES = ("requirements.txt", "requirements-dev.txt", "requirements.in")
PY_DEP_RE = re.compile(r"""^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;@ (].*)?$""")
TOML_ARRAY_RE = re.compile(r"""^\s*([A-Za-z0-9_.\-"']+)\s*=\s*\[(.*?)\]\s*$""", re.M | re.S)
AI_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*$", re.M)
TOML_STR_RE = re.compile(r'"([^"\n]*)"|\'([^\'\n]*)\'')
PY_REQ_LINE_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)", re.M)
PY_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", re.M)
POETRY_DEP_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*=", re.M)
MCP_PY_SERVER_RE = re.compile(r"FastMCP\s*\(|from\s+mcp\.server|import\s+mcp\.server|@mcp\.tool|@server\.(?:call_tool|list_tools)|fastmcp")
MCP_HTTP_RE = re.compile(r"""streamable[-_]http|transport\s*=\s*["'](?:sse|http)|uvicorn\.run|\.sse_app\(|\.streamable_http_app\(""")
MCP_HTTP_NOTE = ("MCP server over HTTP: Run starts it as usual. Point a client or the MCP Inspector at its URL "
                 "(usually /mcp).")
STREAMLIT_RE = re.compile(r"^\s*(?:import\s+streamlit|from\s+streamlit\b)", re.M)
GRADIO_RE = re.compile(r"^\s*(?:import\s+gradio|from\s+gradio\b)", re.M)
STREAMLIT_NAMES = ("streamlit_app.py", "app.py", "main.py", "Home.py", "home.py", "dashboard.py")
GRADIO_NAMES = ("app.py", "main.py", "demo.py", "ui.py", "gradio_app.py")

TOOL_HINTS.update({
    "npx": "Install Node.js (it includes npx): https://nodejs.org or `brew install node`.",
    "streamlit": "Install Streamlit in the project: `pip install streamlit` (or `uv add streamlit`).",
    "langgraph": "Install the LangGraph CLI: `pip install -U \"langgraph-cli[inmem]\"`.",
    "crewai": "Install CrewAI: `uv tool install crewai` or `pip install crewai`.",
    "open-webui": "Install Open WebUI: `pip install open-webui` (or run it with Docker).",
    "ollama": "Install Ollama: https://ollama.com/download or `brew install ollama`.",
    "lms": "Install LM Studio and run `~/.lmstudio/bin/lms bootstrap` to put lms on PATH.",
})

# Data-driven: add a row to recognise another agent framework. Matched against Python dependency
# names (normalised to lower-case with dashes), JS dependency names and top-level Python imports.
AGENT_FRAMEWORKS = [
    {"name": "LangGraph", "py": ["langgraph"], "js": ["@langchain/langgraph"], "imports": ["langgraph"]},
    {"name": "LangChain", "py_prefix": ["langchain"], "js": ["langchain"], "js_prefix": ["@langchain/"], "imports": ["langchain", "langchain_core", "langchain_community", "langchain_openai"]},
    {"name": "CrewAI", "py": ["crewai"], "imports": ["crewai"]},
    {"name": "OpenAI Agents", "py": ["openai-agents"], "js": ["@openai/agents"]},
    {"name": "AutoGen", "py": ["autogen-agentchat", "autogen-core", "pyautogen", "ag2", "autogen"], "imports": ["autogen", "autogen_agentchat", "autogen_core"]},
    {"name": "Pydantic AI", "py": ["pydantic-ai", "pydantic-ai-slim"], "imports": ["pydantic_ai"]},
    {"name": "LlamaIndex", "py_prefix": ["llama-index"], "js": ["llamaindex"], "imports": ["llama_index"]},
    {"name": "smolagents", "py": ["smolagents"], "imports": ["smolagents"]},
    {"name": "Agno", "py": ["agno"], "imports": ["agno"]},
    {"name": "Google ADK", "py": ["google-adk"], "js": ["@google/adk"]},
    {"name": "Haystack", "py": ["haystack-ai"], "imports": ["haystack"]},
    {"name": "Semantic Kernel", "py": ["semantic-kernel"], "imports": ["semantic_kernel"]},
    {"name": "DSPy", "py": ["dspy", "dspy-ai"], "imports": ["dspy"]},
    {"name": "Mastra", "js": ["@mastra/core"]},
    {"name": "AI SDK", "js": ["ai"], "sdk": True},
    {"name": "Genkit", "js": ["genkit"], "py": ["genkit"], "sdk": True},
]
SDK_FRAMEWORKS = {fw["name"] for fw in AGENT_FRAMEWORKS if fw.get("sdk")}


def _fw_label(frameworks):
    """'Agent' for agent frameworks; plain 'AI' when only an SDK (Vercel AI SDK, Genkit) is used."""
    return "Agent" if any(f not in SDK_FRAMEWORKS for f in frameworks) else "AI"


def _norm_dep(n):
    return n.strip().lower().replace("_", "-").replace(".", "-")


AI_SIG_CACHE = {}


def _ai_signals(path):
    """Read dependency files and top-level imports once (cached on file mtimes)."""
    path = Path(path)
    stamp = []
    for fn in AI_DEP_FILES + ("pyproject.toml", "package.json") + tuple(_py_files(path)[:30]):
        try:
            st = os.stat(str(path / fn))
            stamp.append((fn, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    stamp = tuple(stamp)
    hit = AI_SIG_CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    sig = _ai_signals_read(path)
    if len(AI_SIG_CACHE) > 4000:
        AI_SIG_CACHE.clear()
    AI_SIG_CACHE[str(path)] = (stamp, sig)
    return sig


def _ai_signals_read(path):
    py, js, imports, files = set(), set(), set(), {}
    for fn in AI_DEP_FILES:
        t = _read_text(path / fn, 100000)
        for m in PY_REQ_LINE_RE.finditer(t):
            py.add(_norm_dep(m.group(1)))
    pyproject = _read_text(path / "pyproject.toml", 200000) if (path / "pyproject.toml").is_file() else ""
    if pyproject:
        py.update(_pyproject_deps(pyproject))
        for sec in ("tool.poetry.dependencies", "tool.poetry.group.dev.dependencies"):
            for k in toml_section(pyproject, sec).keys():
                py.add(_norm_dep(k))
    pkg = read_json(path / "package.json") if (path / "package.json").is_file() else None
    if isinstance(pkg, dict):
        for sec in ("dependencies", "devDependencies", "peerDependencies"):
            d = pkg.get(sec)
            if isinstance(d, dict):
                js.update(str(k).lower() for k in d.keys())
    for fn in _py_files(path)[:30]:
        t = _read_text(path / fn, 65536)
        files[fn] = t
        imports.update(m.group(1).lower() for m in PY_IMPORT_RE.finditer(t))
    return {"py": py, "js": js, "imports": imports, "files": files, "pyproject": pyproject, "pkg": pkg}


def _pyproject_deps(text):
    """Dependency names from [project] dependencies, optional-dependencies and dependency-groups arrays."""
    out = set()
    headers = [(m.start(), m.group(1).strip()) for m in AI_SECTION_RE.finditer(text)]
    for m in TOML_ARRAY_RE.finditer(text):
        key = m.group(1).strip("\"'")
        sec = ""
        for pos, name in headers:
            if pos < m.start():
                sec = name
        if not (key == "dependencies" or "dependencies" in sec or "dependency-groups" in sec):
            continue
        for sm in TOML_STR_RE.finditer(m.group(2)):
            dm = PY_DEP_RE.match(sm.group(1) if sm.group(1) is not None else sm.group(2))
            if dm:
                out.add(_norm_dep(dm.group(1)))
    return out


def _agent_frameworks(sig):
    found = []
    for fw in AGENT_FRAMEWORKS:
        hit = any(d in sig["py"] for d in fw.get("py", ())) or \
            any(d.startswith(p) for d in sig["py"] for p in fw.get("py_prefix", ())) or \
            any(d in sig["js"] for d in fw.get("js", ())) or \
            any(d.startswith(p) for d in sig["js"] for p in fw.get("js_prefix", ())) or \
            any(i in sig["imports"] for i in fw.get("imports", ()))
        if hit:
            found.append(fw["name"])
    if "LangGraph" in found and "LangChain" in found:
        found.remove("LangChain")
    return found


def _src_py_texts(path, cap=80):
    """Python sources under src/ (package layout), for MCP server markers. Bounded."""
    out = []
    src = Path(path) / "src"
    if not src.is_dir():
        return out
    for root, dirs, fns in os.walk(str(src)):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules")]
        for fn in fns:
            if fn.endswith(".py"):
                out.append(_read_text(os.path.join(root, fn), 65536))
                if len(out) >= cap:
                    return out
    return out


def _has_crew_py(path):
    src = Path(path) / "src"
    try:
        return src.is_dir() and any((p / "crew.py").is_file() for p in src.iterdir() if p.is_dir())
    except OSError:
        return False


def _venv_tool(path, tool):
    """Prefer the project's own venv copy of a CLI (streamlit, langgraph, ...)."""
    for v in (".venv", "venv", "env"):
        p = Path(path) / v / "bin" / tool
        if p.exists():
            return str(p)
    return tool


MCP_STDIO_NOTE = ("stdio MCP server: it talks JSON-RPC over stdin/stdout, so a plain Run just sits there "
                  "waiting for a client. Inspect opens the MCP Inspector in the browser and connects to it.")


def _mcp_choices(server_argv, server_disp):
    insp = ["npx", "-y", "@modelcontextprotocol/inspector"] + list(server_argv)
    return [
        _choice("inspect", "Inspect (MCP Inspector)", insp, "npx @modelcontextprotocol/inspector " + server_disp,
                web=True, kind="MCP"),
        _choice("run", "Run over stdio", list(server_argv), server_disp, web=False, kind="MCP"),
    ]


def _mcp_finish(info, server_argv, server_disp, prefs, lang, source):
    _apply_choice(info, _mcp_choices(server_argv, server_disp), "inspect", "Mode", prefs, "MCP", lang, tool=None)
    info["env"] = dict(info.get("env") or {}, CLIENT_PORT="{PORT}", MCP_AUTO_OPEN_ENABLED="false")
    info["token_url"] = True
    info["note"] = MCP_STDIO_NOTE
    info["ai"] = {"type": "mcp", "label": "MCP", "source": source, "tags": []}
    return info


def _mcp_js_entry(path, pkg):
    """Find the server entry of a Node MCP server: bin > main > common build paths."""
    cands = []
    b = pkg.get("bin")
    if isinstance(b, str):
        cands.append(b)
    elif isinstance(b, dict):
        cands.extend(str(v) for v in b.values())
    if isinstance(pkg.get("main"), str):
        cands.append(pkg["main"])
    cands += ["build/index.js", "dist/index.js", "index.js", "server.js", "src/index.js"]
    for c in cands:
        c = c[2:] if c.startswith("./") else c
        if c and ".." not in c and (path / c).is_file():
            return c
    return None


def _mcp_manifest(path):
    """MCP bundle manifest (manifest.json with server.mcp_config): command + args."""
    m = read_json(path / "manifest.json") if (path / "manifest.json").is_file() else None
    if not isinstance(m, dict) or not isinstance(m.get("server"), dict):
        return None
    cfg = m["server"].get("mcp_config")
    if not isinstance(cfg, dict) or not cfg.get("command"):
        return None
    args = [str(a).replace("${__dirname}/", "").replace("${__dirname}", ".") for a in (cfg.get("args") or [])]
    return [str(cfg["command"])] + args


def _mcp_config_servers(path):
    """Servers listed in mcp.json / .mcp.json (stdio ones only: they have a command)."""
    out = []
    for fn in ("mcp.json", ".mcp.json"):
        d = read_json(path / fn) if (path / fn).is_file() else None
        if not isinstance(d, dict):
            continue
        servers = d.get("mcpServers") or d.get("servers") or {}
        if not isinstance(servers, dict):
            continue
        for name, s in list(servers.items())[:30]:
            if isinstance(s, dict) and s.get("command"):
                argv = [str(s["command"])] + [str(a) for a in (s.get("args") or [])]
                out.append((str(name), argv))
    return out


def _detect_ai(path, info, prefs, sig):
    """AI-specific project types. Runs after slothmore.json / launcher.json and before the generic checks.
    Returns the filled info, or None to fall through to normal detection."""
    files = sig["files"]
    py, js, imports = sig["py"], sig["js"], sig["imports"]
    frameworks = _agent_frameworks(sig)

    def done(kind, label, argv, disp, web, tool, lang="python", ai_type="ui", note=None, env=None):
        info.update(kind=kind, cmd=disp, argv=argv, display_cmd=disp, web=web, detected=True, lang=lang)
        if env:
            info["env"] = dict(info.get("env") or {}, **env)
        info["ai"] = {"type": ai_type, "label": label, "tags": frameworks}
        if note:
            info["note"] = note
        _require_tool(info, tool)
        if lang == "python":
            _attach_install(info, _python_install(path))
        return info

    # ComfyUI checkout: main.py + comfy/ package
    if (path / "main.py").is_file() and (path / "comfy").is_dir() and (path / "nodes.py").is_file():
        return done("ComfyUI", "AI UI", [find_python_exe(path), "-u", "main.py", "--listen", "127.0.0.1", "--port", "{PORT}"],
                    "python main.py", True, None)

    # Open WebUI: installed into this folder's venv, or the source checkout
    if (path / "backend" / "open_webui").is_dir() and (path / "backend" / "start.sh").is_file():
        info["cwd"] = str(path / "backend")
        return done("Open WebUI", "AI UI", ["bash", "start.sh"], "bash backend/start.sh", True, "bash", lang="")
    if "open-webui" in py:
        tool = _venv_tool(path, "open-webui")
        return done("Open WebUI", "AI UI", [tool, "serve", "--port", "{PORT}"], "open-webui serve", True, tool)

    # LangGraph app
    if (path / "langgraph.json").is_file():
        tool = _venv_tool(path, "langgraph")
        return done("Agent", "Agent", [tool, "dev", "--port", "{PORT}", "--no-browser"], "langgraph dev", True, tool,
                    ai_type="agent")

    # CrewAI project (crewai create crew layout)
    if "crewai" in py and ("[tool.crewai]" in sig["pyproject"] or _has_crew_py(path)):
        tool = _venv_tool(path, "crewai")
        return done("Agent", "Agent", [tool, "run"], "crewai run", False, tool, ai_type="agent")

    # MCP bundle manifest
    man = _mcp_manifest(path)
    if man:
        lang = "node" if man[0] in ("node", "npx", "bun") else ("python" if "python" in man[0] or man[0] in ("uv", "uvx") else "")
        _mcp_finish(info, man, " ".join(shlex.quote(a) for a in man), prefs, lang, "manifest.json")
        info["ai"]["tags"] = frameworks
        return info

    # Node MCP server
    pkg = sig["pkg"]
    if isinstance(pkg, dict) and "@modelcontextprotocol/sdk" in js:
        entry = _mcp_js_entry(path, pkg)
        text = _read_text(path / entry, 200000) if entry else ""
        client_only = bool(text) and "sdk/client" in text and "sdk/server" not in text
        if not client_only:
            scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
            pm = detect_pm(path)
            if entry:
                server_argv, disp = ["node", entry], "node " + entry
            elif "start" in scripts:
                server_argv, disp = pm_run_argv(pm, "start"), "%s run start" % pm
            else:
                server_argv = None
            if server_argv:
                _mcp_finish(info, server_argv, disp, prefs, "node", "package.json")
                info["ai"]["tags"] = frameworks
                info["pm"] = pm
                _attach_install(info, _node_install(path, pm))
                if not entry:
                    info["note"] += " Build it first if the entry file isn't there yet."
                return info

    # Python MCP server
    if {"mcp", "fastmcp"} & (py | imports):
        servers = [f for f, t in files.items() if MCP_PY_SERVER_RE.search(t)]
        pref = [f for f in ("server.py", "main.py", "app.py") if f in servers]
        entry = (pref or servers or [None])[0]
        if entry and MCP_HTTP_RE.search(files[entry]):
            entry = None   # HTTP transport: normal detection runs it; _ai_annotate adds the MCP badge
        if entry:
            server_argv = [find_python_exe(path), "-u", entry]
            _mcp_finish(info, server_argv, "python " + entry, prefs, "python", "python")
            info["ai"]["tags"] = frameworks
            _attach_install(info, _python_install(path))
            return info

    # Streamlit
    if "streamlit" in py or "streamlit" in imports:
        hits = [f for f, t in files.items() if STREAMLIT_RE.search(t)]
        entry = next((f for f in STREAMLIT_NAMES if f in hits), hits[0] if hits else None)
        if entry:
            tool = _venv_tool(path, "streamlit")
            return done("Streamlit", "AI UI" if frameworks else "", [tool, "run", entry, "--server.port", "{PORT}",
                        "--server.headless", "true", "--browser.gatherUsageStats", "false"],
                        "streamlit run " + entry, True, tool, ai_type="ui")

    # Gradio
    if "gradio" in py or "gradio" in imports:
        hits = [f for f, t in files.items() if GRADIO_RE.search(t)]
        entry = next((f for f in GRADIO_NAMES if f in hits), hits[0] if hits else None)
        if entry:
            return done("Gradio", "AI UI" if frameworks else "", [find_python_exe(path), "-u", entry],
                        "python " + entry, True, None, ai_type="ui", env={"GRADIO_SERVER_PORT": "{PORT}"})
    return None


def _ai_annotate(info, path, sig, prefs=None):
    """After normal detection: tag agent projects and offer mcp.json servers when nothing else runs."""
    if info.get("ai"):
        if not info["ai"].get("label"):
            tags = info["ai"].get("tags") or []
            info["ai"] = None if not tags else dict(info["ai"], label=_fw_label(tags), type="agent")
        return info
    frameworks = _agent_frameworks(sig)
    if {"mcp", "fastmcp"} & (sig["py"] | sig["imports"]):
        texts = [t for t in sig["files"].values() if MCP_PY_SERVER_RE.search(t)] or \
            [t for t in _src_py_texts(path) if MCP_PY_SERVER_RE.search(t)]
        if texts:
            if any(MCP_HTTP_RE.search(t) for t in texts):
                info["ai"] = {"type": "mcp", "label": "MCP", "source": "python", "tags": frameworks, "http": True}
                info["note"] = MCP_HTTP_NOTE
            elif info.get("argv") and not info.get("needs_choice") and not info.get("choices") and not info.get("static"):
                inst = info.get("install")
                _mcp_finish(info, list(info["argv"]), info.get("display_cmd") or info.get("cmd") or "", prefs, "python", "python")
                info["ai"]["tags"] = frameworks
                if inst:
                    _attach_install(info, inst)
            else:
                info["ai"] = {"type": "mcp", "label": "MCP", "source": "python", "tags": frameworks}
            return info
    cfg_servers = _mcp_config_servers(path)
    if not info.get("cmd") and not info.get("static") and not info.get("needs_choice") and cfg_servers:
        choices = []
        for name, argv in cfg_servers:
            disp = " ".join(shlex.quote(a) for a in argv)
            choices.append(_choice(name, "Inspect " + name, ["npx", "-y", "@modelcontextprotocol/inspector"] + argv,
                                   "npx @modelcontextprotocol/inspector " + disp, web=True, kind="MCP"))
        info["error"] = None
        _apply_choice(info, choices, choices[0]["key"] if len(choices) == 1 else None, "Server", prefs, "MCP", "", tool="npx")
        info["env"] = dict(info.get("env") or {}, CLIENT_PORT="{PORT}", MCP_AUTO_OPEN_ENABLED="false")
        info["token_url"] = True
        info["note"] = "Servers from mcp.json. " + MCP_STDIO_NOTE
        info["ai"] = {"type": "mcp", "label": "MCP", "source": "mcp.json", "tags": frameworks}
        return info
    if frameworks:
        info["ai"] = {"type": "agent", "label": _fw_label(frameworks), "tags": frameworks}
    elif cfg_servers:
        info["ai"] = {"type": "config", "label": "", "tags": ["mcp.json"]}
    return info


WATCH_DEPTH_DEFAULT = 3
WATCH_DEPTH_MAX = 6
SCAN_DIR_CAP = 600            # folders looked at per watched folder
LOOSE_EXTS = {".py", ".sh", ".js", ".mjs", ".html"}
SCAN_CACHE = {"key": None, "at": 0.0, "items": None}
SCAN_TTL = 1.0


def make_item(path, source, root, prefs=None, rel=None, parent=None, workspace=None):
    info = detect(path, prefs)
    info["id"] = item_id(path)
    info["path"] = str(path)
    info["source"] = source
    info["root"] = root
    info["rel"] = rel
    info["parent"] = parent
    info["workspace"] = workspace
    info["folder"] = Path(path).name
    return info


def watch_opts(cfg, root):
    o = (cfg.get("watch_opts") or {}).get(root) or {}
    try:
        depth = int(o.get("depth", WATCH_DEPTH_DEFAULT))
    except (TypeError, ValueError):
        depth = WATCH_DEPTH_DEFAULT
    return {"depth": max(1, min(WATCH_DEPTH_MAX, depth)), "loose": bool(o.get("loose", True))}


def _glob_workspace(base, pattern):
    """Expand a workspace pattern like apps/*, packages/**, tools/cli (one level of *)."""
    pattern = pattern.strip().strip("'\"")
    if not pattern or pattern.startswith("!") or ".." in pattern or pattern.startswith("/"):
        return []
    pattern = pattern.rstrip("/").replace("/**", "/*")
    try:
        return sorted(p for p in Path(base).glob(pattern) if p.is_dir() and not p.name.startswith(".")
                      and p.name not in IGNORED)
    except (OSError, ValueError, NotImplementedError):
        return []


def workspace_packages(folder):
    """Package folders of a JS monorepo (package.json workspaces, pnpm-workspace.yaml, or apps/* + packages/*)."""
    folder = Path(folder)
    patterns = []
    pkg = read_json(folder / "package.json") if (folder / "package.json").exists() else None
    if isinstance(pkg, dict):
        ws = pkg.get("workspaces")
        if isinstance(ws, dict):
            ws = ws.get("packages")
        if isinstance(ws, list):
            patterns += [str(x) for x in ws]
    pw = folder / "pnpm-workspace.yaml"
    if pw.exists():
        inside = False
        for line in _read_text(pw).splitlines():
            if re.match(r"^packages\s*:", line):
                inside = True
                continue
            if inside:
                m = re.match(r"^\s*-\s*(.+?)\s*$", line)
                if m:
                    patterns.append(m.group(1))
                elif line.strip() and not line.startswith((" ", "\t", "#")):
                    inside = False
    if not patterns and isinstance(pkg, dict) and ((folder / "apps").is_dir() or (folder / "packages").is_dir()):
        patterns = ["apps/*", "packages/*"]
    out, seen = [], set()
    for pat in patterns:
        for d in _glob_workspace(folder, pat):
            if str(d) in seen:
                continue
            if any((d / f).exists() for f in ("package.json", "deno.json", "pyproject.toml", "go.mod", "Cargo.toml")):
                seen.add(str(d))
                out.append(d)
    return out[:80]


def _runnable(info):
    return bool(info.get("detected"))


def _scan_watch(root, cfg, prefs_all, pinned_set):
    """Cards for one watched folder: runnable subfolders (recursing into plain folders), monorepo packages,
    and loose runnable files at the top level."""
    opts = watch_opts(cfg, root)
    items = []
    budget = [SCAN_DIR_CAP]
    rootp = Path(root)

    def rel_of(p):
        try:
            return str(Path(p).relative_to(rootp))
        except ValueError:
            return Path(p).name

    def add(path, parent=None, workspace=None):
        iid = item_id(str(path))
        it = make_item(path, "watch", root, prefs_all.get(iid), rel=rel_of(path), parent=parent, workspace=workspace)
        items.append(it)
        return it

    def visit(d, depth, parent_rel):
        if budget[0] <= 0:
            return
        budget[0] -= 1
        if str(d) in pinned_set:
            return
        info = detect(d, prefs_all.get(item_id(str(d))))
        ws = workspace_packages(d) if (d / "package.json").exists() or (d / "pnpm-workspace.yaml").exists() else []
        if ws:
            rel = rel_of(d)
            if _runnable(info) and not (info.get("needs_choice") and not info.get("missing_tool")):
                add(d, parent=parent_rel)
            for w in ws:
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                if str(w) in pinned_set:
                    continue
                winfo = detect(w, prefs_all.get(item_id(str(w))))
                if _runnable(winfo):
                    add(w, parent=rel, workspace=rel)
            return
        if _runnable(info):
            add(d, parent=parent_rel)
            return
        if depth >= opts["depth"]:
            if depth == 1:
                add(d, parent=parent_rel)  # keep the old "nothing to run" card for direct children
            return
        try:
            kids = sorted((c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")
                           and c.name not in IGNORED and c.name not in WATCH_IGNORED), key=lambda c: c.name.lower())
        except OSError:
            kids = []
        before = len(items)
        for k in kids:
            visit(k, depth + 1, rel_of(d))
        if len(items) == before and depth == 1:
            add(d, parent=parent_rel)  # nothing runnable inside: show the folder with its hint, as before

    try:
        children = sorted(rootp.iterdir(), key=lambda c: c.name.lower())
    except OSError:
        return items
    for child in children:
        if child.name.startswith(".") or child.name in IGNORED:
            continue
        if str(child) in pinned_set:
            continue
        try:
            is_dir = child.is_dir()
            is_file = child.is_file()
        except OSError:
            continue
        if is_dir:
            visit(child, 1, None)
        elif is_file and opts["loose"] and child.suffix.lower() in LOOSE_EXTS:
            it = add(child, parent=None)
            it["loose"] = True
    return items


def build_items(cfg=None):
    if cfg is None:
        cfg = load_config()
    prefs_all = cfg.get("prefs") or {}
    key = json.dumps([cfg.get("watch"), cfg.get("pinned"), cfg.get("watch_opts"),
                      {k: v.get("choice") for k, v in prefs_all.items() if isinstance(v, dict) and v.get("choice")}],
                     sort_keys=True, default=str)
    now = time.time()
    if SCAN_CACHE["key"] == key and SCAN_CACHE["items"] is not None and now - SCAN_CACHE["at"] < SCAN_TTL:
        return [dict(i) for i in SCAN_CACHE["items"]]
    items = []
    pinned_set = set()
    for p in cfg["pinned"]:
        pinned_set.add(p)
        if os.path.exists(p):
            items.append(make_item(Path(p), "pinned", None, prefs_all.get(item_id(p))))
        else:
            items.append(
                {
                    "id": item_id(p), "name": os.path.basename(p), "path": p, "source": "pinned",
                    "root": None, "kind": "Missing", "cmd": None, "display_cmd": None, "web": False,
                    "static": False, "cwd": p, "port": None, "env": {}, "lang": "",
                    "error": "This path no longer exists.", "folder": os.path.basename(p),
                }
            )
    for root in cfg["watch"]:
        if not Path(root).is_dir():
            continue
        items.extend(_scan_watch(root, cfg, prefs_all, pinned_set))
    SCAN_CACHE.update(key=key, at=now, items=items)
    return [dict(i) for i in items]


def _resolved_under(root, path):
    """Return path.resolve() if it is a file inside root (after following symlinks)."""
    try:
        base = Path(root).resolve()
        target = Path(path).resolve()
    except OSError:
        return None
    if not target.is_file():
        return None
    if target == base or base in target.parents:
        return target
    return None


def find_readme_file(folder):
    """Pick a top-level README in folder. Case-insensitive; .md wins over .txt / no extension."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    best, best_rank = None, 99
    try:
        entries = list(folder.iterdir())
    except OSError:
        return None
    for p in entries:
        rank = README_RANK.get(p.name.lower())
        if rank is None or rank >= best_rank:
            continue
        safe = _resolved_under(folder, p)
        if safe is None or safe.parent != Path(folder).resolve():
            continue
        best, best_rank = safe, rank
    return best


def readme_search_dir(path):
    """Folder to look in for a README. For a pinned file, only the parent if it looks like that file's project."""
    path = Path(path)
    if path.is_dir():
        return path
    if not path.is_file():
        return None
    parent = path.parent
    try:
        home = Path.home().resolve()
        pref = parent.resolve()
    except OSError:
        return None
    if pref == home or pref.name.lower() in CATCHALL_DIRS:
        return None
    if any((parent / m).exists() for m in PROJECT_MARKERS) or (parent / ".git").is_dir():
        return parent
    # A small folder that already has a README next to this file is almost certainly the project.
    try:
        entries = [p for p in parent.iterdir() if not p.name.startswith(".")]
    except OSError:
        return None
    if len(entries) <= 12 and find_readme_file(parent) is not None:
        return parent
    return None


def readme_meta(path):
    folder = readme_search_dir(path)
    f = find_readme_file(folder) if folder else None
    if not f:
        return None
    try:
        size = f.stat().st_size
    except OSError:
        return None
    return {"name": f.name, "size": size, "path": str(f), "folder": str(folder.resolve())}


def readme_payload(it):
    meta = readme_meta(it["path"])
    if not meta:
        raise ApiError(404, "no readme")
    if meta["size"] > README_MAX_BYTES:
        raise ApiError(413, "readme too large", name=meta["name"], size=meta["size"], max=README_MAX_BYTES)
    try:
        text = Path(meta["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise ApiError(404, "no readme")
    lower = meta["name"].lower()
    if lower.endswith(".md") or lower.endswith(".markdown"):
        fmt = "md"
    elif lower.endswith(".rst"):
        fmt = "rst"
    elif lower.endswith(".txt"):
        fmt = "txt"
    else:
        fmt = "plain"
    return {
        "name": meta["name"],
        "path": tilde(meta["path"]),
        "folder": tilde(meta["folder"]),
        "size": meta["size"],
        "format": fmt,
        "text": text,
        "can_open": _can_open_readme(meta["name"]),
        "can_reveal": _can_reveal_readme(),
        "platform": "mac" if IS_MAC else ("linux" if sys.platform.startswith("linux") else sys.platform),
    }


def readme_asset(it, rel):
    meta = readme_meta(it["path"])
    if not meta:
        raise ApiError(404, "no readme")
    rel = unquote(rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or rel.startswith("\\\\") or "\x00" in rel:
        raise ApiError(400, "invalid path")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.endswith("~") for p in parts):
        raise ApiError(400, "invalid path")
    folder = Path(meta["folder"])
    target = _resolved_under(folder, folder.joinpath(*parts))
    if target is None:
        raise ApiError(404, "not found")
    suffix = target.suffix.lower()
    ctype = README_ASSET_TYPES.get(suffix)
    if not ctype:
        raise ApiError(415, "unsupported file type")
    try:
        size = target.stat().st_size
    except OSError:
        raise ApiError(404, "not found")
    if size > README_ASSET_MAX:
        raise ApiError(413, "file too large")
    try:
        data = target.read_bytes()
    except OSError:
        raise ApiError(404, "not found")
    return data, ctype


# README files that Open may hand to a text editor / xdg-open. An extensionless
# README is still shown in the viewer, but is never passed to the OS "open" so a
# hostile executable named README cannot be launched.
README_OPEN_EXTS = (".md", ".markdown", ".rst", ".txt")


def _readme_text_extension(name):
    lower = (name or "").lower()
    return any(lower.endswith(ext) for ext in README_OPEN_EXTS)


def _can_reveal_readme():
    if IS_MAC:
        return bool(shutil.which("open") or os.path.exists("/usr/bin/open"))
    return bool(shutil.which("xdg-open"))


def _can_open_readme(name):
    if IS_MAC:
        return _can_reveal_readme()
    return bool(shutil.which("xdg-open")) and _readme_text_extension(name)


def _spawn_no_shell(args):
    """Start a helper process from an argv list. Never uses a shell."""
    try:
        subprocess.Popen(list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, shell=False)
    except OSError:
        raise ApiError(500, "could not open the file")


def open_readme(it, mode="reveal"):
    """Reveal the README's folder, or open the file in a text editor.

    On macOS, Open uses `open -t` (always a text editor), never plain `open`, so an
    extensionless executable README cannot launch in Terminal. Reveal uses `open -R`.
    On Linux, Open uses `xdg-open` only for known text extensions; otherwise it is
    refused. Reveal opens the containing folder. Arguments are always an argv list.
    """
    meta = readme_meta(it["path"])
    if not meta:
        raise ApiError(404, "no readme")
    mode = str(mode or "reveal")
    if mode not in ("open", "reveal"):
        raise ApiError(400, "unknown mode")
    path, folder = meta["path"], meta["folder"]
    if IS_MAC:
        opener = shutil.which("open") or ("/usr/bin/open" if os.path.exists("/usr/bin/open") else None)
        if not opener:
            raise ApiError(500, "could not open the file")
        args = [opener, "-t", path] if mode == "open" else [opener, "-R", path]
        _spawn_no_shell(args)
        return {"ok": True, "mode": mode, "path": tilde(path)}
    opener = shutil.which("xdg-open")
    if not opener:
        raise ApiError(501, "Opening files from the dashboard needs xdg-open.", path=tilde(path))
    if mode == "reveal":
        _spawn_no_shell([opener, folder])
        return {"ok": True, "mode": mode, "path": tilde(path), "folder": tilde(folder)}
    if not _readme_text_extension(meta["name"]):
        raise ApiError(
            409,
            "This README has no text extension, so it isn't opened as a file. Use Show in folder instead.",
            path=tilde(path), folder=tilde(folder),
        )
    _spawn_no_shell([opener, path])
    return {"ok": True, "mode": mode, "path": tilde(path)}




def invalidate_scan():
    SCAN_CACHE.update(key=None, items=None)


def display_name(it, prefs=None):
    label = str((prefs or {}).get("label") or "").strip()
    return label or it["name"]


def find_item(iid):
    for it in build_items():
        if it["id"] == iid:
            return it
    return None


def public(it, cfg=None):
    if cfg is None:
        cfg = load_config()
    prefs = (cfg.get("prefs") or {}).get(it["id"]) or {}
    run = RUNS.get(it["id"])
    alive = bool(run and run.alive)
    port = run.port if run else None
    is_web = bool(it["web"] or (run and run.detected_web))
    scheme = (run.scheme if run else None) or "http"
    html_files = it.get("html_files") or []
    html_file = None
    if it.get("static") and html_files:
        saved_hf = prefs.get("html_file")
        html_file = saved_hf if saved_hf in html_files else (it.get("html_file") or html_files[0])
    active_hf = (run.html_file if (alive and run and run.html_file in html_files) else None) or html_file
    url = None
    if alive and is_web and port:
        if it.get("static") and active_hf:
            if active_hf.lower() != "index.html" or len(html_files) > 1:
                url = "%s://localhost:%d/%s" % (scheme, port, quote(active_hf))
            else:
                url = "%s://localhost:%d/" % (scheme, port)
        else:
            url = "%s://localhost:%d" % (scheme, port)
    if alive and run and run.open_url:
        url = run.open_url
    saved_args = str(prefs.get("args") or "")
    raw_presets = prefs.get("presets") if isinstance(prefs.get("presets"), dict) else {}
    presets = [{"name": str(k), "args": str(v)} for k, v in raw_presets.items()]
    raw_env = prefs.get("env") if isinstance(prefs.get("env"), dict) else {}
    env_vars = [{"key": str(k), "has_value": str(v) != ""} for k, v in raw_env.items()]  # values only via /env
    supports_args = bool((it["cmd"] or it.get("needs_choice")) and not it.get("static"))
    custom = str(prefs.get("label") or "").strip() or None
    d = {
        "id": it["id"],
        "name": custom or it["name"],
        "base_name": it["name"],
        "custom_name": custom,
        "folder": it.get("folder") or os.path.basename(str(it["path"]).rstrip("/")),
        "rel": it.get("rel"),
        "parent": it.get("parent"),
        "workspace": it.get("workspace"),
        "loose": bool(it.get("loose")),
        "choices": it.get("choices"),
        "choice": it.get("choice"),
        "choice_label": it.get("choice_label"),
        "needs_choice": bool(it.get("needs_choice")),
        "missing_tool": it.get("missing_tool"),
        "deps_missing": bool(it.get("deps_missing")),
        "install_cmd": (it.get("install") or {}).get("display"),
        "install_missing_tool": (lambda t: t if t and not which(t) else None)((it.get("install") or {}).get("tool")),
        "pm": it.get("pm"),
        "kind": it["kind"],
        "web": is_web,
        "static": bool(it.get("static")),
        "lang": it.get("lang", ""),
        "source": it["source"],
        "path": tilde(it["path"]),
        "root": tilde(it["root"]) if it["root"] else None,
        "cmd": it["display_cmd"],
        "run_cmd": run.display_cmd if (run and getattr(run, "display_cmd", None)) else it["display_cmd"],
        "args": saved_args,
        "presets": presets,
        "env_vars": env_vars,
        "supports_args": supports_args,
        "html_files": html_files,
        "html_file": active_hf if alive else html_file,
        "auto_restart_crash": bool(prefs.get("auto_restart_crash", False)),
        "auto_restart_watch": bool(prefs.get("auto_restart_watch", False)),
        "restart_count": RESTART_COUNTS.get(it["id"], 0),
        "auto_restart_status": run.auto_restart_status if run else None,
        "error": it["error"],
        "startable": bool((it["cmd"] or it["static"]) and not it.get("missing_tool") and not it.get("needs_choice")),
        "running": alive,
        "has_run": bool(run),
        "run_seq": run.seq if run else 0,
        "port": port if alive else None,
        "detected_port": run.detected_port if (alive and run) else None,
        "url": url,
        "exit_code": run.exit_code if (run and not alive) else None,
        "prompt": run.prompt() if run else None,
        "notify": prefs.get("notify") if isinstance(prefs.get("notify"), bool) else None,
        "started_at": run.started_at if (alive and run) else None,
        "health": dict(run.health) if (alive and run and run.health) else None,
        "last_run": _last_run_summary(it["id"]),
        "readme": (lambda m: {"name": m["name"], "size": m["size"]} if m else None)(readme_meta(it["path"])),
        "ai": it.get("ai"),
        "note": it.get("note"),
        "agents": AGENTS_BY_ITEM.get(it["id"]) or [],
    }
    return d


# --------------------------------------------------------------------------- processes

RUNS = {}
RUNS_LOCK = threading.Lock()
RUN_SEQ = 0
CRASH_HISTORY = {}
RESTART_COUNTS = {}


def _detect_port(tail):
    """Parse local server port/scheme from clean terminal text. Returns (port, scheme, is_web_banner) or None."""
    clean = tail.replace("\r", "\n")
    m = LOCAL_URL_RE.search(clean)
    if m:
        p = int(m.group(2))
        if 1 <= p <= 65535:
            return p, m.group(1).lower(), True, True
    m = HTTP_SERVER_PORT_RE.search(clean)
    if m:
        p = int(m.group(1))
        if 1 <= p <= 65535:
            return p, "http", True, False
    m = URL_PORT_RE.search(clean)
    if m:
        p = int(m.group(2))
        if 1 <= p <= 65535:
            line_start = clean.rfind("\n", 0, m.start()) + 1
            line_end = clean.find("\n", m.end())
            line = clean[line_start: line_end if line_end != -1 else len(clean)]
            return p, m.group(1).lower(), bool(PORT_BANNER_HINT_RE.search(line)), False
    m = LISTEN_PORT_RE.search(clean)
    if m:
        p = int(m.group(1))
        if 1 <= p <= 65535:
            return p, "http", True, False
    return None


class Run:
    def __init__(self, item, seq=1):
        self.seq = seq
        self.item_id = item["id"]
        self.static = bool(item.get("static"))
        self.cmd = item["cmd"]
        self.display_cmd = item.get("display_cmd")
        self.html_file = item.get("html_file")
        self.cond = threading.Condition()
        self.chunks = []
        self.base = 0
        self.chars = 0
        self.alive = False
        self.exit_code = None
        self.stopping = False
        self.restarting = False
        self.cancel_auto_restart = False
        self.auto_restart_status = None
        self.proc = None
        self.master = None
        self.port = None
        self.detected_port = None
        self.detected_web = False
        self.scheme = "http"
        self.local_locked = False
        self.watch_snapshot = None
        self.pending_change_at = None
        self.line_tail = ""
        self.port_tail = ""
        self.last_output = time.time()
        self.name = item.get("name") or item["id"]
        self.started_at = time.time()
        self.args_str = ""
        self.preset = None
        self.trigger = "manual"
        self.log_fh = None
        self.log_path = None
        self.log_bytes = 0
        self.hist_entry = None
        self.health = None
        self.cpu_prev = None
        self.prompt_notified = ""
        self.assigned_port = None
        self.stop_signal = None
        self.token_url = bool(item.get("token_url"))
        self.open_url = None

    def append(self, text):
        with self.cond:
            if self.log_fh is not None:
                self._log_write(text)
            self.chunks.append(text)
            self.chars += len(text)
            while self.chars > MAX_CHARS and len(self.chunks) > 1:
                self.chars -= len(self.chunks.pop(0))
                self.base += 1
            self.last_output = time.time()
            if "\n" in text:
                self.line_tail = text.rsplit("\n", 1)[1]
            else:
                self.line_tail = (self.line_tail + text)[-400:]
            self.port_tail = (self.port_tail + text)[-800:]
            if self.token_url and not self.open_url:
                m = TOKEN_URL_RE.search(ANSI_RE.sub("", self.port_tail))
                if m:
                    self.open_url = m.group(0)
                    self.port = int(m.group(2))
                    self.detected_port = self.port
                    self.detected_web = True
                    self.local_locked = True
            if not self.local_locked:
                found = _detect_port(self.port_tail)
                if found:
                    p, scheme, is_banner, is_local = found
                    if is_local or self.detected_port is None:
                        self.detected_port = p
                        if not self.static:
                            self.port = p
                            self.scheme = scheme
                        if is_banner:
                            self.detected_web = True
                        if is_local:
                            self.local_locked = True
            self.cond.notify_all()

    def _log_write(self, text):
        try:
            self.log_fh.write(text)
            self.log_bytes += len(text.encode("utf-8", "replace"))
            if self.log_bytes > 2 * LOG_MAX_BYTES:
                self.log_fh.close()
                _trim_log(self.log_path)
                self.log_fh = private_open(self.log_path, "a")
                self.log_bytes = self.log_path.stat().st_size
        except (OSError, ValueError):
            self.log_fh = None

    def finish(self, code):
        with self.cond:
            if self.stopping:
                self.append("\n[stopped]\n")
            else:
                self.append("\n[finished]\n" if code == 0 else "\n[exited with code %s]\n" % code)
            _finalize_history(self, code)
            self.alive = False
            self.exit_code = code
            self.cond.notify_all()

    def prompt(self):
        """'yn' when the script waits on a y/n question, 'text' for other prompts."""
        if not self.alive:
            return None
        tail = self.line_tail
        if not tail.strip():
            return None
        if YN_RE.search(tail):
            return "yn"
        if time.time() - self.last_output > 0.5 and re.search(r"[:?>]\s*$", tail):
            return "text"
        return None


def free_port(start=8001):
    used = {r.port for r in RUNS.values() if r.alive and r.port}
    for p in range(start, start + 300):
        if p == DEFAULT_PORT or p in used:
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise ApiError(500, "no_free_port")


def _snapshot_item(it):
    p = it["path"]
    snap = {}
    try:
        if os.path.isfile(p):
            st = os.stat(p)
            snap[p] = (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size)
            return snap
        count = 0
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in WATCH_IGNORED)
            for fn in sorted(files):
                if fn.startswith(".") or fn.endswith(("~", ".swp", ".tmp")):
                    continue
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    snap[os.path.relpath(fp, p)] = (
                        getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size
                    )
                except OSError:
                    continue
                count += 1
                if count >= 5000:
                    return snap
    except OSError:
        pass
    return snap


def _delayed_crash_restart(run, delay, attempt):
    steps = max(1, int(delay / 0.1))
    for _ in range(steps):
        time.sleep(0.1)
        if run.cancel_auto_restart or RUNS.get(run.item_id) is not run:
            return
    cfg = load_config()
    prefs = (cfg.get("prefs") or {}).get(run.item_id) or {}
    if not prefs.get("auto_restart_crash"):
        return
    it = find_item(run.item_id)
    if not it:
        return
    try:
        restart_run(
            it,
            reason="\n--- [auto-restarted after crash (%d/3)] ---\n" % attempt,
            carry_chunks=list(run.chunks),
        )
    except Exception:
        LOG.exception("auto-restart after crash failed for %s", run.item_id)


def _reader(run):
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        try:
            data = os.read(run.master, 4096)
        except OSError:
            break
        if not data:
            break
        text = ANSI_RE.sub("", dec.decode(data))
        if text:
            run.append(text)
    code = run.proc.wait()
    try:
        os.close(run.master)
    except OSError:
        pass
    run.finish(code)
    if not run.stopping and not run.restarting:
        dur = _fmt_duration(time.time() - run.started_at)
        if code == 0:
            _maybe_notify(run, "finish", "%s finished" % run.name, "Exited normally after %s." % dur)
        else:
            _maybe_notify(run, "fail", "%s failed" % run.name, "Exit code %s after %s." % (code, dur))

    if not run.stopping and not run.restarting and not run.cancel_auto_restart and code != 0:
        cfg = load_config()
        prefs = (cfg.get("prefs") or {}).get(run.item_id) or {}
        if prefs.get("auto_restart_crash"):
            with RUNS_LOCK:
                if run.cancel_auto_restart or RUNS.get(run.item_id) is not run:
                    return
                now = time.time()
                hist = [t for t in CRASH_HISTORY.get(run.item_id, []) if now - t <= 60.0]
                if len(hist) >= 3:
                    run.auto_restart_status = "paused (3 crashes in 60s)"
                    run.append("\n[auto-restart paused: crashed 3 times in 60s]\n")
                else:
                    delay = [1.0, 2.0, 4.0][len(hist)]
                    attempt = len(hist) + 1
                    hist.append(now)
                    CRASH_HISTORY[run.item_id] = hist
                    run.auto_restart_status = "restarting in %.0fs (%d/3)" % (delay, attempt)
                    run.append("\n[crashed (code %s) — auto-restarting in %.0fs (%d/3)...]\n" % (code, delay, attempt))
                    threading.Thread(
                        target=_delayed_crash_restart, args=(run, delay, attempt), daemon=True
                    ).start()


def _parse_env_text(text, keep=None):
    """KEY=VALUE lines. A bare KEY line keeps the stored value from `keep` (used when values were never revealed)."""
    out = {}
    keep = keep or {}
    for idx, raw_line in enumerate(str(text).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line and line in keep:
            out[line] = str(keep[line])
            continue
        if "=" not in line:
            raise ApiError(400, "Env line %d must be KEY=VALUE" % idx)
        k, v = line.split("=", 1)
        k = k.strip()
        if not ENV_KEY_RE.match(k):
            raise ApiError(400, "Invalid env variable name: %s" % k)
        v = v.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        if "\0" in v:
            raise ApiError(400, "Invalid null byte in env value for %s" % k)
        out[k] = v
    return out


def update_prefs(it, body):
    body = body or {}
    with CFG_LOCK:
        cfg = load_config()
        prefs = cfg["prefs"].setdefault(it["id"], {})
        if "args" in body and body["args"] is not None:
            args_str = str(body["args"])
            if args_str.strip():
                try:
                    shlex.split(args_str)
                except ValueError as e:
                    raise ApiError(400, "Invalid arguments: %s" % e)
            prefs["args"] = args_str
        if "save_preset" in body and isinstance(body["save_preset"], dict):
            pname = str(body["save_preset"].get("name") or "").strip()
            pargs = str(body["save_preset"].get("args") or "")
            if not pname:
                raise ApiError(400, "Preset name cannot be empty")
            if len(pname) > 40:
                raise ApiError(400, "Preset name is too long (max 40 chars)")
            if pargs.strip():
                try:
                    shlex.split(pargs)
                except ValueError as e:
                    raise ApiError(400, "Invalid arguments: %s" % e)
            presets = prefs.setdefault("presets", {})
            if not isinstance(presets, dict):
                presets = {}
                prefs["presets"] = presets
            presets[pname] = pargs
            prefs["args"] = pargs
        if "delete_preset" in body and body["delete_preset"] is not None:
            pname = str(body["delete_preset"]).strip()
            if isinstance(prefs.get("presets"), dict):
                prefs["presets"].pop(pname, None)
        old_env = prefs.get("env") if isinstance(prefs.get("env"), dict) else {}
        if "env_text" in body and body["env_text"] is not None:
            prefs["env"] = _parse_env_text(body["env_text"], old_env)
        elif "env" in body and isinstance(body["env"], dict):
            cleaned = {}
            for k, v in body["env"].items():
                ks = str(k).strip()
                if not ENV_KEY_RE.match(ks):
                    raise ApiError(400, "Invalid env variable name: %s" % ks)
                if v is None:  # null = keep the stored value (the client never saw it)
                    if ks in old_env:
                        cleaned[ks] = str(old_env[ks])
                    continue
                vs = str(v)
                if "\0" in vs:
                    raise ApiError(400, "Invalid null byte in env value for %s" % ks)
                cleaned[ks] = vs
            prefs["env"] = cleaned
        if "notify" in body:
            if body["notify"] is None:
                prefs.pop("notify", None)
            else:
                prefs["notify"] = bool(body["notify"])
        if "auto_restart_crash" in body:
            prefs["auto_restart_crash"] = bool(body["auto_restart_crash"])
            if not prefs["auto_restart_crash"]:
                run = RUNS.get(it["id"])
                if run:
                    run.cancel_auto_restart = True
                    run.auto_restart_status = None
        if "auto_restart_watch" in body:
            prefs["auto_restart_watch"] = bool(body["auto_restart_watch"])
            run = RUNS.get(it["id"])
            if run and run.alive:
                run.watch_snapshot = _snapshot_item(it) if prefs["auto_restart_watch"] else None
                run.pending_change_at = None
        if "label" in body:
            label = re.sub(r"\s+", " ", str(body["label"] or "")).strip()
            if len(label) > 80:
                raise ApiError(400, "Name is too long (max 80 characters)")
            if label:
                prefs["label"] = label
            else:
                prefs.pop("label", None)
        if "choice" in body:
            ch = body["choice"]
            if ch in (None, ""):
                prefs.pop("choice", None)
            else:
                keys = [c["key"] for c in (it.get("choices") or [])]
                if not keys and it.get("choice"):
                    keys = [it["choice"]]
                if str(ch) not in keys:
                    raise ApiError(400, "Unknown choice: %s" % ch)
                prefs["choice"] = str(ch)
        if "html_file" in body and body["html_file"] is not None:
            if not it.get("static"):
                raise ApiError(400, "Not a static HTML folder")
            hf = str(body["html_file"])
            if hf not in (it.get("html_files") or []):
                raise ApiError(400, "Unknown HTML file: %s" % hf)
            prefs["html_file"] = hf
            run = RUNS.get(it["id"])
            if run and run.alive:
                run.html_file = hf
        save_config(cfg)


def install_item(it):
    """Run the detected dependency install (npm/pnpm/yarn/bun install, uv sync, poetry install, venv + pip)
    as a normal run of this item, so its output shows in the terminal and in history."""
    inst = it.get("install")
    if not inst:
        raise ApiError(400, "Nothing to install for this item.")
    tool = inst.get("tool")
    if tool and not which(tool):
        raise ApiError(400, "%s isn't installed or isn't on PATH. %s" % (tool, TOOL_HINTS.get(tool, "")))
    cur = RUNS.get(it["id"])
    if cur and cur.alive:
        raise ApiError(409, "Stop it first, then install.")
    pseudo = dict(it)
    pseudo.update(argv=list(inst["argv"]), cmd=inst["display"], display_cmd=inst["display"], web=False,
                  static=False, port=None, cwd=inst.get("cwd") or it["cwd"], missing_tool=None,
                  needs_choice=False, stop_signal=None)
    run = start_run(pseudo, {"args": None}, trigger="install", check_port=False, no_args=True)
    run.cancel_auto_restart = True
    run.watch_snapshot = None
    return run


def start_run(it, body=None, carry_chunks=None, banner=None, trigger="manual", check_port=True, no_args=False):
    global RUN_SEQ
    if it.get("missing_tool"):
        raise ApiError(400, it.get("error") or "%s isn't installed." % it["missing_tool"], missing_tool=it["missing_tool"])
    if it.get("needs_choice"):
        raise ApiError(400, "Choose a %s to run first." % (it.get("choice_label") or "command").lower(), needs_choice=True)
    if not (it["cmd"] or it["static"]):
        raise ApiError(400, it["error"] or "No run command found. Add a slothmore.json (or launcher.json).")
    body = body or {}
    cfg = load_config()
    saved_prefs = (cfg.get("prefs") or {}).get(it["id"]) or {}
    if no_args:
        saved_prefs = dict(saved_prefs, args="", auto_restart_watch=False)

    hf = None
    extra_argv = []
    if it.get("static"):
        html_files = it.get("html_files") or []
        if "html_file" in body and body["html_file"] is not None:
            hf = str(body["html_file"])
            if hf not in html_files:
                raise ApiError(400, "Unknown HTML file: %s" % hf)
            with CFG_LOCK:
                c = load_config()
                c["prefs"].setdefault(it["id"], {})["html_file"] = hf
                save_config(c)
        else:
            saved_hf = saved_prefs.get("html_file")
            hf = saved_hf if saved_hf in html_files else (it.get("html_file") or (html_files[0] if html_files else None))
    else:
        if "args" in body and body["args"] is not None:
            args_str = str(body["args"])
        else:
            args_str = str(saved_prefs.get("args") or "")
        if args_str.strip():
            try:
                extra_argv = shlex.split(args_str)
            except ValueError as e:
                raise ApiError(400, "Invalid arguments: %s" % e)
        if "args" in body and body["args"] is not None:
            with CFG_LOCK:
                c = load_config()
                c["prefs"].setdefault(it["id"], {})["args"] = args_str
                save_config(c)

    cur = RUNS.get(it["id"])
    if check_port and not body.get("force") and not (cur and cur.alive):
        _check_expected_port(it)

    with RUNS_LOCK:
        old = RUNS.get(it["id"])
        if old and old.alive:
            if it.get("static") and hf:
                old.html_file = hf
            return old
        if old:
            old.cancel_auto_restart = True
        RUN_SEQ += 1
        run = Run(it, seq=RUN_SEQ)
        run.trigger = trigger
        run.name = display_name(it, saved_prefs)
        run.stop_signal = it.get("stop_signal")
        if not it.get("static"):
            run.args_str = " ".join(shlex.quote(a) for a in extra_argv)
            presets = saved_prefs.get("presets") if isinstance(saved_prefs.get("presets"), dict) else {}
            for pname, pargs in presets.items():
                if str(pargs).strip() and str(pargs).strip() == args_str.strip():
                    run.preset = str(pname)
                    break
        if carry_chunks:
            for ch in carry_chunks:
                run.chunks.append(ch)
                run.chars += len(ch)
            while run.chars > MAX_CHARS and len(run.chunks) > 1:
                run.chars -= len(run.chunks.pop(0))
                run.base += 1
        if banner:
            run.append(banner)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1", "TERM": "xterm-256color", "NO_COLOR": "1",
                "BROWSER": "none", "FORCE_COLOR": "0",
            }
        )
        env.update(it["env"])
        saved_env = saved_prefs.get("env") if isinstance(saved_prefs.get("env"), dict) else {}
        for k, v in saved_env.items():
            if ENV_KEY_RE.match(str(k)):
                env[str(k)] = str(v)
        if it["static"]:
            port = it["port"] or free_port()
            env["PORT"] = str(port)
            argv = [sys.executable, "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"]
            run.port = port
            run.html_file = hf
            run.display_cmd = "python -m http.server %d" % port
        else:
            if it["web"]:
                port = it["port"] or free_port()
                env["PORT"] = str(port)
                run.assigned_port = port
                if it["port"]:
                    run.port = int(it["port"])
            if it.get("argv"):
                port_s = env.get("PORT") or str(it.get("port") or free_port())
                argv = [a.replace("{PORT}", port_s) for a in it["argv"]] + extra_argv
                for k, v in list(env.items()):
                    if "{PORT}" in v:
                        env[k] = v.replace("{PORT}", port_s)
            else:
                raw_cmd = it["cmd"]
                if any(op in raw_cmd for op in ("|", "&", ";", "<", ">", "$", "`", "\n")):
                    shell_cmd = raw_cmd + (' "$@"' if extra_argv else "")
                    argv = ["/bin/sh", "-c", shell_cmd, "launcher"] + extra_argv
                else:
                    try:
                        argv = shlex.split(raw_cmd) + extra_argv
                    except ValueError as e:
                        raise ApiError(400, "Invalid command: %s" % e)
            base_disp = it["display_cmd"] or it["cmd"]
            run.display_cmd = base_disp + (" " + " ".join(shlex.quote(a) for a in extra_argv) if extra_argv else "")
        run.cmd = " ".join(shlex.quote(a) for a in argv)
        if saved_prefs.get("auto_restart_watch"):
            run.watch_snapshot = _snapshot_item(it)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        try:
            run.proc = subprocess.Popen(
                argv, shell=False, cwd=it["cwd"], env=env,
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True,
            )
        except OSError as e:
            os.close(master)
            os.close(slave)
            raise ApiError(500, "Could not start: %s" % e)
        os.close(slave)
        run.master = master
        run.started_at = time.time()
        _open_history(run, banner)
        run.alive = True
        RUNS[it["id"]] = run
        threading.Thread(target=_reader, args=(run,), daemon=True).start()
        return run


def restart_run(it, body=None, reason=None, carry_chunks=None):
    iid = it["id"]
    with RUNS_LOCK:
        old = RUNS.get(iid)
        if old:
            old.cancel_auto_restart = True
            if old.alive:
                old.restarting = True
                old.stopping = True
                try:
                    os.killpg(old.proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
    if old and old.alive:
        for _ in range(25):
            if not old.alive:
                break
            time.sleep(0.1)
        if old.alive:
            try:
                os.killpg(old.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(10):
                if not old.alive:
                    break
                time.sleep(0.05)
    if reason is not None:
        RESTART_COUNTS[iid] = RESTART_COUNTS.get(iid, 0) + 1
        trigger = "auto-restart (crash)" if "crash" in reason else "auto-restart (file change)"
    else:
        CRASH_HISTORY[iid] = []
        trigger = "restart"
    return start_run(it, body=body, carry_chunks=carry_chunks, banner=reason, trigger=trigger, check_port=False)


def _watch_loop():
    restarting_ids = set()
    while True:
        time.sleep(0.8)
        try:
            cfg = load_config()
            all_prefs = cfg.get("prefs") or {}
            items_by_id = None
            for iid, run in list(RUNS.items()):
                if not run.alive or run.stopping or run.restarting or iid in restarting_ids:
                    continue
                if not (all_prefs.get(iid) or {}).get("auto_restart_watch"):
                    run.watch_snapshot = None
                    run.pending_change_at = None
                    continue
                if items_by_id is None:
                    items_by_id = {x["id"]: x for x in build_items(cfg)}
                it = items_by_id.get(iid)
                if not it:
                    continue
                if run.watch_snapshot is None:
                    run.watch_snapshot = _snapshot_item(it)
                    continue
                snap = _snapshot_item(it)
                now = time.time()
                if snap != run.watch_snapshot:
                    run.watch_snapshot = snap
                    run.pending_change_at = now
                if run.pending_change_at is not None and now - run.pending_change_at >= 0.6:
                    run.pending_change_at = None
                    restarting_ids.add(iid)
                    def _do_watch_restart(item=it, prev_run=run, item_id_key=iid):
                        try:
                            if RUNS.get(item_id_key) is prev_run and prev_run.alive:
                                restart_run(
                                    item,
                                    reason="\n--- [auto-restarted: file changed] ---\n",
                                    carry_chunks=list(prev_run.chunks),
                                )
                        finally:
                            restarting_ids.discard(item_id_key)
                    threading.Thread(target=_do_watch_restart, daemon=True).start()
        except Exception:
            LOG.exception("file-watch loop error")


# --------------------------------------------------------------------------- logs and run history


HIST_LOCK = threading.RLock()
HISTORY = {}  # item id -> list of run entries, oldest first


def _item_log_dir(iid):
    return LOGS_DIR / iid


def load_history(iid):
    with HIST_LOCK:
        if iid in HISTORY:
            return HISTORY[iid]
        lst = []
        try:
            with open(str(_item_log_dir(iid) / "history.json"), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                lst = [e for e in data if isinstance(e, dict) and RUN_ID_RE.match(str(e.get("run_id", "")))]
        except (OSError, ValueError):
            pass
        HISTORY[iid] = lst
        return lst


def _save_history(iid):
    with HIST_LOCK:
        d = _item_log_dir(iid)
        try:
            d.mkdir(parents=True, exist_ok=True)
            _chmod(d, 0o700)
            private_write(d / "history.json", json.dumps(HISTORY.get(iid, []), indent=1))
        except OSError:
            LOG.warning("could not save history for %s", iid, exc_info=True)


def _trim_log(path):
    """Keep only the last LOG_MAX_LINES lines / LOG_MAX_BYTES bytes of a log file."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) <= LOG_MAX_BYTES and data.count(b"\n") <= LOG_MAX_LINES:
        return
    tail = data[-LOG_MAX_BYTES:]
    lines = tail.split(b"\n")
    if len(tail) < len(data):
        lines = lines[1:]  # first line was cut in the middle
    if len(lines) > LOG_MAX_LINES:
        lines = lines[-LOG_MAX_LINES:]
    out = b"[... older output trimmed ...]\n" + b"\n".join(lines)
    try:
        private_write(path, out)
    except OSError:
        pass


def _open_history(run, banner=None):
    """Create the history entry and on-disk log for a run that just started. Env values are never stored."""
    iid = run.item_id
    run_id = "%d-%d" % (int(run.started_at * 1000), run.seq)
    d = _item_log_dir(iid)
    try:
        d.mkdir(parents=True, exist_ok=True)
        run.log_path = d / (run_id + ".log")
        _chmod(d, 0o700)
        run.log_fh = private_open(run.log_path, "w")
        if banner:
            run.log_fh.write(banner.lstrip("\n"))
    except OSError:
        run.log_fh = None
        run.log_path = None
    entry = {
        "run_id": run_id, "started": round(run.started_at, 3), "ended": None, "duration": None,
        "exit_code": None, "status": "running", "args": run.args_str, "preset": run.preset,
        "cmd": run.display_cmd, "trigger": run.trigger, "port": None,
        "log": (run_id + ".log") if run.log_path else None,
    }
    run.hist_entry = entry
    with HIST_LOCK:
        lst = load_history(iid)
        lst.append(entry)
        while len(lst) > HISTORY_KEEP:
            old = lst.pop(0)
            if old.get("log"):
                try:
                    (d / old["log"]).unlink()
                except OSError:
                    pass
        _save_history(iid)


def _finalize_history(run, code):
    e = run.hist_entry
    if not e or e.get("ended"):
        return
    if run.log_fh is not None:
        try:
            run.log_fh.close()
        except OSError:
            pass
        run.log_fh = None
        if run.log_path:
            _trim_log(run.log_path)
    now = time.time()
    with HIST_LOCK:
        e["ended"] = round(now, 3)
        e["duration"] = round(now - run.started_at, 2)
        e["exit_code"] = code
        e["status"] = "stopped" if run.stopping else ("finished" if code == 0 else "failed")
        e["port"] = run.detected_port or run.port
        # The app chose its own port (e.g. Vite's 5173) instead of the $PORT we handed it.
        e["own_port"] = bool(run.detected_port and run.detected_port != run.assigned_port)
        _save_history(run.item_id)


def recover_history():
    """Runs that were still going when Slothmore itself stopped are marked as interrupted."""
    if not LOGS_DIR.is_dir():
        return
    for d in LOGS_DIR.iterdir():
        if not (d.is_dir() and ITEM_ID_RE.match(d.name)):
            continue
        changed = False
        for e in load_history(d.name):
            if e.get("ended") is None:
                ended = e.get("started") or time.time()
                if e.get("log"):
                    try:
                        ended = max(ended, (d / e["log"]).stat().st_mtime)
                    except OSError:
                        pass
                e["ended"] = round(ended, 3)
                e["duration"] = round(ended - (e.get("started") or ended), 2)
                e["status"] = "interrupted"
                changed = True
        if changed:
            _save_history(d.name)


def _last_run_summary(iid):
    lst = load_history(iid)
    if not lst:
        return None
    e = lst[-1]
    return {"started": e.get("started"), "ended": e.get("ended"), "exit_code": e.get("exit_code"),
            "status": e.get("status"), "count": len(lst)}


def history_public(iid):
    return [dict(e) for e in reversed(load_history(iid))]


def read_run_log(iid, run_id):
    for e in load_history(iid):
        if e.get("run_id") == run_id:
            if not e.get("log"):
                return e, ""
            try:
                return e, (_item_log_dir(iid) / e["log"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return e, ""
    raise ApiError(404, "unknown run")


def clear_history(iid):
    """Delete stored logs and history for an item (the run that is going right now is kept)."""
    d = _item_log_dir(iid)
    with HIST_LOCK:
        keep = []
        for e in load_history(iid):
            if e.get("ended") is None:
                keep.append(e)
                continue
            if e.get("log"):
                try:
                    (d / e["log"]).unlink()
                except OSError:
                    pass
        HISTORY[iid] = keep
        _save_history(iid)


def _expected_port(it):
    if it.get("port"):
        try:
            return int(it["port"])
        except (TypeError, ValueError):
            return None
    if it.get("static"):
        return None
    for e in reversed(load_history(it["id"])):
        if e.get("port") and e.get("own_port"):
            return int(e["port"])
        if e.get("ended") and e.get("port"):
            return None
    return None


# --------------------------------------------------------------------------- notifications


EVENTS = collections.deque(maxlen=200)
EVENT_LOCK = threading.Lock()
EVENT_SEQ = 0
LAST_BROWSER_POLL = 0.0  # last time a dashboard tab with browser notifications allowed asked for events


def notify_settings(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    s = dict(NOTIFY_DEFAULTS)
    raw = ((cfg.get("settings") or {}).get("notify")) if isinstance(cfg.get("settings"), dict) else None
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in NOTIFY_DEFAULTS:
                s[k] = v
    return s


def update_settings(body):
    n = body.get("notify")
    if not isinstance(n, dict):
        raise ApiError(400, "expected {notify: {...}}")
    with CFG_LOCK:
        cfg = load_config()
        settings = cfg.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = cfg["settings"] = {}
        cur = notify_settings(cfg)
        for k, v in n.items():
            if k not in NOTIFY_DEFAULTS:
                continue
            if k == "min_seconds":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ApiError(400, "min_seconds must be a number")
                if not 0 <= v <= 86400:
                    raise ApiError(400, "min_seconds must be between 0 and 86400")
                cur[k] = v
            else:
                cur[k] = bool(v)
        settings["notify"] = cur
        save_config(cfg)


def settings_public():
    return {
        "notify": notify_settings(),
        "platform": sys.platform,
        "macos_notifications": bool(IS_MAC and shutil.which("osascript")),
        "psutil": psutil is not None,
    }


def _fmt_duration(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm %02ds" % (sec // 60, sec % 60)
    return "%dh %02dm" % (sec // 3600, (sec % 3600) // 60)


def mac_notify(title, body):
    """Show a macOS notification with osascript. Text goes in as argv, never into the script source."""
    if not IS_MAC:
        return False
    exe = shutil.which("osascript")
    if not exe:
        return False
    try:
        subprocess.Popen(
            [exe, "-e", "on run argv", "-e",
             "display notification (item 2 of argv) with title \"Slothmore\" subtitle (item 1 of argv)",
             "-e", "end run", str(title)[:200], str(body)[:500]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        return True
    except OSError:
        return False


def push_event(item_id, name, kind, title, body, force_mac=False):
    global EVENT_SEQ
    s = notify_settings()
    with EVENT_LOCK:
        EVENT_SEQ += 1
        ev = {"seq": EVENT_SEQ, "id": item_id, "name": name, "kind": kind, "title": title, "body": body,
              "ts": round(time.time(), 3)}
        EVENTS.append(ev)
    browser_open = s["browser"] and (time.time() - LAST_BROWSER_POLL) < 8
    if s["macos"] and (force_mac or not browser_open):
        ev["macos"] = mac_notify(title, body)
    return ev


def _maybe_notify(run, kind, title, body):
    try:
        cfg = load_config()
        s = notify_settings(cfg)
        if not s["enabled"] or not s.get("on_" + kind):
            return None
        pref = ((cfg.get("prefs") or {}).get(run.item_id) or {}).get("notify")
        if not (pref if isinstance(pref, bool) else s["all_items"]):
            return None
        if time.time() - run.started_at < s["min_seconds"]:
            return None
        return push_event(run.item_id, run.name, kind, title, body)
    except Exception:
        LOG.debug("notification failed", exc_info=True)
        return None


def events_since(after, browser):
    global LAST_BROWSER_POLL
    if browser:
        LAST_BROWSER_POLL = time.time()
    with EVENT_LOCK:
        evs = [] if after < 0 else [e for e in EVENTS if e["seq"] > after]
        return {"seq": EVENT_SEQ, "events": evs}


# --------------------------------------------------------------------------- health (CPU, memory, ports)


PSUTIL_CACHE = {}


def _parse_cputime(t):
    """ps 'time' column: [[dd-]hh:]mm:ss[.cc] on both macOS and Linux."""
    days = 0
    if "-" in t:
        dpart, t = t.split("-", 1)
        days = int(dpart or 0)
    parts = t.split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p or 0)
    return days * 86400 + secs


def _ps_table():
    """{pid: (ppid, rss_bytes, cpu_seconds)} from `ps` (works without any pip install)."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,rss=,time="], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        try:
            table[int(f[0])] = (int(f[1]), int(f[2]) * 1024, _parse_cputime(f[3]))
        except ValueError:
            continue
    return table


def _tree_pids(root, table):
    kids = {}
    for pid, (ppid, _r, _c) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        if p in table and p not in out:
            out.append(p)
            todo.extend(kids.get(p, []))
    return out


def _sample_psutil(run):
    root = psutil.Process(run.proc.pid)
    procs = [root] + root.children(recursive=True)
    cpu, rss = 0.0, 0
    for pr in procs:
        try:
            key = (pr.pid, pr.create_time())
            cached = PSUTIL_CACHE.get(key)
            if cached is None:
                cached = PSUTIL_CACHE[key] = pr
                cached.cpu_percent(None)
            else:
                cpu += cached.cpu_percent(None)
            rss += pr.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return {"cpu": round(cpu, 1), "rss": rss, "procs": len(procs), "source": "psutil", "at": round(time.time(), 2)}


def sample_health():
    alive = [r for r in list(RUNS.values()) if r.alive and r.proc]
    if not alive:
        return
    table = None
    for run in alive:
        try:
            if psutil is not None:
                run.health = _sample_psutil(run)
                continue
        except Exception:
            LOG.debug("psutil health sample failed", exc_info=True)
        if table is None:
            table = _ps_table()
        pids = _tree_pids(run.proc.pid, table)
        if not pids:
            continue
        rss = sum(table[p][1] for p in pids)
        cpu_total = sum(table[p][2] for p in pids)
        now = time.time()
        cpu = 0.0
        if run.cpu_prev:
            dt = now - run.cpu_prev[0]
            if dt > 0:
                cpu = max(0.0, (cpu_total - run.cpu_prev[1]) / dt * 100.0)
        run.cpu_prev = (now, cpu_total)
        run.health = {"cpu": round(cpu, 1), "rss": rss, "procs": len(pids), "source": "ps", "at": round(now, 2)}


def port_in_use(port):
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            if s.connect_ex((host, port)) == 0:
                return True
        except OSError:
            pass
        finally:
            s.close()
    return False


def port_owner(port):
    """Best effort: which process listens on the port. Returns {pid, command} or None."""
    if psutil is not None:
        try:
            for c in psutil.net_connections(kind="tcp"):
                if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                    return {"pid": c.pid, "command": psutil.Process(c.pid).name()}
        except Exception:
            LOG.debug("psutil net_connections unavailable, falling back to lsof", exc_info=True)
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof:
        try:
            out = subprocess.run([lsof, "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fpc"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4).stdout.decode()
            pid, cmd = None, None
            for line in out.splitlines():
                if line.startswith("p") and pid is None:
                    pid = int(line[1:])
                elif line.startswith("c") and cmd is None:
                    cmd = line[1:]
            if pid:
                return {"pid": pid, "command": cmd}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    ss = shutil.which("ss")
    if ss:
        try:
            out = subprocess.run([ss, "-ltnpH", "sport = :%d" % port], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=4).stdout.decode()
            m = re.search(r'\("([^"]+)",pid=(\d+)', out)
            if m:
                return {"pid": int(m.group(2)), "command": m.group(1)}
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _check_expected_port(it):
    port = _expected_port(it)
    if not port:
        return
    if any(r.alive and r.port == port for r in RUNS.values()):
        return
    if port_in_use(port):
        owner = port_owner(port) or {}
        raise ApiError(409, "port_in_use", port=port, pid=owner.get("pid"), command=owner.get("command"))


def _monitor_loop():
    """Every second: notice scripts waiting for input; every 2 s: CPU/memory samples."""
    tick = 0
    while True:
        time.sleep(1.0)
        tick += 1
        try:
            for run in list(RUNS.values()):
                if not run.alive:
                    continue
                e = run.hist_entry
                if e is not None and run.detected_port and e.get("port") != run.detected_port:
                    with HIST_LOCK:
                        e["port"] = run.detected_port
                        e["own_port"] = run.detected_port != run.assigned_port
                        _save_history(run.item_id)
                p = run.prompt()
                if p and run.prompt_notified != run.line_tail:
                    run.prompt_notified = run.line_tail
                    tail = run.line_tail.strip()[:160] or "It is waiting for input."
                    _maybe_notify(run, "prompt", "%s is waiting for input" % run.name, tail)
                elif not p:
                    run.prompt_notified = ""
            if tick % 2 == 0:
                sample_health()
        except Exception:
            LOG.exception("monitor loop error")


# --------------------------------------------------------------------------- groups (stacks)


GROUP_STATUS = {}


def _groups(cfg):
    gs = cfg.get("groups")
    return [g for g in gs if isinstance(g, dict) and GROUP_ID_RE.match(str(g.get("id", "")))] if isinstance(gs, list) else []


def groups_public(cfg, items_by_id):
    out = []
    for g in _groups(cfg):
        ids = [str(i) for i in (g.get("items") or [])]
        present = [i for i in ids if i in items_by_id]
        out.append({
            "id": g["id"], "name": g.get("name") or "Group", "items": ids,
            "delay": float(g.get("delay") or 0),
            "names": [items_by_id[i]["name"] for i in present],
            "missing": [i for i in ids if i not in items_by_id],
            "running": sum(1 for i in present if items_by_id[i]["running"]),
            "total": len(present),
            "status": GROUP_STATUS.get(g["id"]),
        })
    return out


def save_group(body):
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60:
        raise ApiError(400, "Group name must be 1-60 characters")
    ids = []
    for i in body.get("items") or []:
        i = str(i)
        if ITEM_ID_RE.match(i) and i not in ids:
            ids.append(i)
    if not ids:
        raise ApiError(400, "Pick at least one item")
    try:
        delay = float(body.get("delay") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "Delay must be a number")
    if not 0 <= delay <= 30:
        raise ApiError(400, "Delay must be between 0 and 30 seconds")
    gid = str(body.get("id") or "")
    with CFG_LOCK:
        cfg = load_config()
        groups = _groups(cfg)
        g = next((x for x in groups if x["id"] == gid), None) if gid else None
        if gid and g is None:
            raise ApiError(404, "unknown group")
        if g is None:
            g = {"id": secrets.token_hex(4)}
            groups.append(g)
        g.update({"name": name, "items": ids, "delay": delay})
        cfg["groups"] = groups
        save_config(cfg)
    return g


def delete_group(gid):
    with CFG_LOCK:
        cfg = load_config()
        cfg["groups"] = [g for g in _groups(cfg) if g["id"] != gid]
        save_config(cfg)
    GROUP_STATUS.pop(gid, None)


def _group_worker(g, action):
    gid, name = g["id"], g.get("name") or "Group"
    ids = [str(i) for i in g.get("items") or []]
    delay = float(g.get("delay") or 0)
    errors = []
    try:
        if action in ("stop", "restart"):
            GROUP_STATUS[gid] = {"busy": True, "text": "stopping...", "errors": []}
            targets = []
            for iid in reversed(ids):
                run = RUNS.get(iid)
                if run and run.alive:
                    stop_run(run)
                    targets.append(run)
            for _ in range(60):
                if not any(r.alive for r in targets):
                    break
                time.sleep(0.1)
        if action in ("start", "restart"):
            for n, iid in enumerate(ids):
                GROUP_STATUS[gid] = {"busy": True, "text": "starting %d/%d" % (n + 1, len(ids)), "errors": errors}
                it = find_item(iid)
                if not it:
                    errors.append("%s: no longer found" % iid)
                    continue
                run = RUNS.get(iid)
                if run and run.alive:
                    continue
                try:
                    start_run(it, {}, trigger="group: %s" % name)
                except ApiError as e:
                    msg = e.payload.get("error")
                    if msg == "port_in_use":
                        msg = "port %s already in use%s" % (
                            e.payload.get("port"), " by PID %s" % e.payload["pid"] if e.payload.get("pid") else "")
                    errors.append("%s: %s" % (display_name(it, (load_config().get("prefs") or {}).get(it["id"])), msg))
                    continue
                if delay and n < len(ids) - 1:
                    time.sleep(delay)
    finally:
        done = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
        GROUP_STATUS[gid] = {"busy": False, "text": done if not errors else "%s with %d problem(s)" % (done, len(errors)),
                             "errors": errors, "at": round(time.time(), 2)}


def group_action(gid, action):
    cfg = load_config()
    g = next((x for x in _groups(cfg) if x["id"] == gid), None)
    if g is None:
        raise ApiError(404, "unknown group")
    st = GROUP_STATUS.get(gid)
    if st and st.get("busy"):
        raise ApiError(409, "This group is busy, try again in a moment")
    GROUP_STATUS[gid] = {"busy": True, "text": "working...", "errors": []}
    threading.Thread(target=_group_worker, args=(dict(g), action), daemon=True).start()


def stop_run(run):
    if not run:
        return
    run.cancel_auto_restart = True
    run.auto_restart_status = None
    if not run.alive:
        return
    run.stopping = True
    graceful = run.stop_signal == "INT"   # docker compose: Ctrl+C stops the containers cleanly
    try:
        os.killpg(run.proc.pid, signal.SIGINT if graceful else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    def reaper():
        if graceful:
            for _ in range(150):
                if not run.alive:
                    return
                time.sleep(0.1)
            try:
                os.killpg(run.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(40):
            if not run.alive:
                return
            time.sleep(0.1)
        try:
            os.killpg(run.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    threading.Thread(target=reaper, daemon=True).start()


def stop_all_mine():
    """Stop everything Slothmore started (the quick panel's "Kill all mine"). Never touches other processes."""
    alive = [r for r in list(RUNS.values()) if r.alive]
    for run in alive:
        stop_run(run)
    LOG.info("stopped all %d Slothmore runs from the quick panel", len(alive))
    return {"stopped": len(alive), "names": [r.name for r in alive]}


def stop_all():
    for run in list(RUNS.values()):
        stop_run(run)
    for _ in range(40):
        if not any(r.alive for r in RUNS.values()):
            break
        time.sleep(0.1)


# --------------------------------------------------------------------------- pinning and watching


def walk_limited(root, depth):
    stack = [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in IGNORED:
                continue
            yield e
            if lvl + 1 < depth:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, lvl + 1))
                except OSError:
                    pass


def find_by_name(name):
    cfg = load_config()
    found = []
    for root in cfg["watch"]:
        if os.path.isdir(root):
            for e in walk_limited(root, 3):
                if e.name == name:
                    found.append(e.path)
    return sorted(set(found))


def pin_path(p):
    p = expand(p)
    if not os.path.exists(p):
        raise ApiError(404, "not_found", name=os.path.basename(p))
    with CFG_LOCK:
        cfg = load_config()
        if p not in cfg["pinned"]:
            cfg["pinned"].append(p)
            save_config(cfg)
    return p


def pin_name(name):
    name = (name or "").strip()
    if not name or "/" in name:
        raise ApiError(400, "bad_name")
    matches = find_by_name(name)
    if not matches:
        raise ApiError(404, "not_found", name=name)
    if len(matches) > 1:
        raise ApiError(409, "ambiguous", name=name, candidates=[tilde(m) for m in matches],
                       raw=matches)
    return pin_path(matches[0])


def unpin(iid):
    with CFG_LOCK:
        cfg = load_config()
        cfg["pinned"] = [p for p in cfg["pinned"] if item_id(p) != iid]
        save_config(cfg)


def reorder(ids):
    with CFG_LOCK:
        cfg = load_config()
        by_id = {item_id(p): p for p in cfg["pinned"]}
        new = [by_id[i] for i in ids if i in by_id]
        new += [p for p in cfg["pinned"] if p not in new]
        cfg["pinned"] = new
        save_config(cfg)


def add_watch(p):
    p = expand(p)
    if not os.path.isdir(p):
        raise ApiError(404, "not_a_folder", name=tilde(p))
    with CFG_LOCK:
        cfg = load_config()
        if p not in cfg["watch"]:
            cfg["watch"].append(p)
            save_config(cfg)


def set_watch_opts(body):
    p = expand(str(body.get("path") or ""))
    with CFG_LOCK:
        cfg = load_config()
        if p not in cfg["watch"]:
            raise ApiError(404, "not watched")
        opts = cfg.setdefault("watch_opts", {}).setdefault(p, {})
        if "depth" in body:
            try:
                d = int(body["depth"])
            except (TypeError, ValueError):
                raise ApiError(400, "depth must be a number")
            if not 1 <= d <= WATCH_DEPTH_MAX:
                raise ApiError(400, "depth must be 1-%d" % WATCH_DEPTH_MAX)
            opts["depth"] = d
        if "loose" in body:
            opts["loose"] = bool(body["loose"])
        save_config(cfg)


def remove_watch(p):
    p = expand(p)
    with CFG_LOCK:
        cfg = load_config()
        cfg["watch"] = [w for w in cfg["watch"] if w != p]
        save_config(cfg)


def pick_folder():
    if sys.platform != "darwin":
        raise ApiError(501, "The folder picker only works on macOS. Paste a path instead.")
    script = (
        'tell application "System Events"\n'
        "activate\n"
        'set f to choose folder with prompt "Pick a folder"\n'
        "return POSIX path of f\n"
        "end tell"
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        raise ApiError(500, "The folder picker did not open.")
    if r.returncode != 0:
        raise ApiError(499, "cancelled")
    return r.stdout.strip().rstrip("/")


# --------------------------------------------------------------------------- http


# --------------------------------------------------------------------------- AI: local model servers and running agents
# Only localhost is ever contacted (short timeouts), and only while a dashboard tab asks (/api/ai is polled
# by visible pages). Agent processes are only listed: their files and transcripts are never read.


AI_LOCK = threading.Lock()
AI_CACHE = {"at": 0.0, "data": None}
AI_TTL = 4.0
AI_PORT_STATE = {}           # port -> {"open": bool, "result": dict|None, "at": float}
AI_NEG_TTL = 120.0           # an open generic port (3000, 8080, ...) that isn't an AI server: re-check after this long
AI_NEG_TTL_SPECIFIC = 10.0   # same for ports only AI servers use (11434, 1234, ...)
AI_GENERIC_PORTS = {3000, 5000, 5173, 7860, 8000, 8080, 8888}
AI_OWNER_CACHE = {}          # port -> (at, owner)
AI_CPU_PREV = {}             # pid -> (at, cpu_seconds)
AGENTS_BY_ITEM = {}          # item id -> [agent chip names]
AGENT_LAST = {}              # pid -> agent dict from the last scan (only these may be stopped)
AI_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
AI_FOREIGN_GUARD_RE = re.compile(r"docker|com\.docker|vpnkit|orbstack|limactl|qemu|colima|podman|ssh", re.I)

# Data-driven: which servers to look for and on which default ports (ports are editable in AI settings).
MODEL_SERVERS = [
    {"key": "ollama", "name": "Ollama", "ports": [11434], "cli": "ollama",
     "start": ["ollama", "serve"], "start_env": {"OLLAMA_HOST": "127.0.0.1:{PORT}"}, "docs": "https://ollama.com"},
    {"key": "lmstudio", "name": "LM Studio", "ports": [1234], "cli": "lms",
     "start": ["lms", "server", "start", "--port", "{PORT}"], "stop": ["lms", "server", "stop"]},
    {"key": "llamacpp", "name": "llama.cpp server", "ports": [8080]},
    {"key": "vllm", "name": "vLLM", "ports": [8000]},
    {"key": "jan", "name": "Jan", "ports": [1337]},
    {"key": "localai", "name": "LocalAI", "ports": [8080]},
    {"key": "textgen", "name": "text-generation-webui", "ports": [5000, 7860]},
    {"key": "openwebui", "name": "Open WebUI", "ports": [3000, 8080]},
    {"key": "comfyui", "name": "ComfyUI", "ports": [8188]},
]
MODEL_SERVER_BY_KEY = {m["key"]: m for m in MODEL_SERVERS}

# Data-driven: how to recognise a coding agent in the process list.
#   names:   executable basenames (argv[0], or the script run by node/python/bun)
#   paths:   substrings of the command line (npm package paths)
#   args_any: when set, the first argument must be one of these (to tell agents from same-named tools)
AGENT_SIGNATURES = [
    {"key": "claude", "name": "Claude Code", "chip": "claude", "names": ["claude"], "paths": ["@anthropic-ai/claude-code/"]},
    {"key": "codex", "name": "Codex CLI", "chip": "codex", "names": ["codex"], "paths": ["@openai/codex/"]},
    {"key": "aider", "name": "Aider", "chip": "aider", "names": ["aider"], "paths": ["/aider/__main__.py"]},
    {"key": "gemini", "name": "Gemini CLI", "chip": "gemini", "names": ["gemini"], "paths": ["@google/gemini-cli/"]},
    {"key": "cursor-agent", "name": "Cursor Agent", "chip": "cursor-agent", "names": ["cursor-agent"], "paths": ["cursor-agent/"]},
    {"key": "opencode", "name": "opencode", "chip": "opencode", "names": ["opencode"], "paths": ["opencode-ai/"]},
    {"key": "goose", "name": "Goose", "chip": "goose", "names": ["goose"], "args_any": ["session", "s", "run", "web", ""]},
    {"key": "openhands", "name": "OpenHands", "chip": "openhands", "names": ["openhands"], "paths": ["-m openhands", "openhands.core"]},
    {"key": "cline", "name": "Cline CLI", "chip": "cline", "names": ["cline"], "paths": ["@cline/cli/", "/cline-core"]},
    {"key": "ollama-runner", "name": "Ollama runner", "chip": None, "names": ["ollama_llama_server"],
     "args_first": {"ollama": ["runner"]}, "model": True},
]
INTERPRETERS = {"node", "python", "python3", "bun", "deno", "nodejs"}


def _ai_cfg(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    a = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    ports = a.get("ports") if isinstance(a.get("ports"), dict) else {}
    out = {}
    for m in MODEL_SERVERS:
        v = ports.get(m["key"])
        if isinstance(v, list) and all(isinstance(p, int) and 1 <= p <= 65535 for p in v):
            out[m["key"]] = v[:4]
        else:
            out[m["key"]] = list(m["ports"])
    return {"enabled": a.get("enabled", True) is not False, "agents": a.get("agents", True) is not False, "ports": out}


def ai_settings_public():
    c = _ai_cfg()
    return {"enabled": c["enabled"], "agents": c["agents"],
            "servers": [{"key": m["key"], "name": m["name"], "ports": c["ports"][m["key"]], "default": m["ports"]}
                        for m in MODEL_SERVERS]}


def update_ai_settings(body):
    with CFG_LOCK:
        cfg = load_config()
        a = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
        if "enabled" in body:
            a["enabled"] = bool(body["enabled"])
        if "agents" in body:
            a["agents"] = bool(body["agents"])
        if isinstance(body.get("ports"), dict):
            ports = a.get("ports") if isinstance(a.get("ports"), dict) else {}
            for k, v in body["ports"].items():
                if k not in MODEL_SERVER_BY_KEY:
                    raise ApiError(400, "Unknown server: %s" % k)
                if isinstance(v, str):
                    v = [x for x in re.split(r"[,\s]+", v) if x]
                try:
                    v = [int(x) for x in (v or [])]
                except (TypeError, ValueError):
                    raise ApiError(400, "Ports for %s must be numbers." % MODEL_SERVER_BY_KEY[k]["name"])
                if not v or any(p < 1 or p > 65535 for p in v) or len(v) > 4:
                    raise ApiError(400, "Give 1 to 4 ports between 1 and 65535 for %s." % MODEL_SERVER_BY_KEY[k]["name"])
                if v == MODEL_SERVER_BY_KEY[k]["ports"]:
                    ports.pop(k, None)
                else:
                    ports[k] = v
            a["ports"] = ports
        cfg["ai"] = a
        save_config(cfg)
    AI_PORT_STATE.clear()
    AI_CACHE.update(at=0.0, data=None)
    return ai_settings_public()


def _http_get(port, path, timeout=0.6, limit=2000000):
    """GET http://127.0.0.1:port/path. Returns (status, bytes) or (None, None). Never follows to other hosts."""
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 headers={"Accept": "application/json, text/html;q=0.5", "User-Agent": "Slothmore-local-probe"})
    try:
        with AI_OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(limit)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        LOG.debug("HTTP probe of 127.0.0.1:%s%s failed", port, path, exc_info=True)
        return None, None


def _get_json(port, path, timeout=0.6):
    st, body = _http_get(port, path, timeout)
    if st != 200 or not body:
        return None
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _oai_models(port):
    d = _get_json(port, "/v1/models")
    if isinstance(d, dict) and isinstance(d.get("data"), list):
        return [m for m in d["data"] if isinstance(m, dict) and m.get("id")]
    return None


def _m(name, loaded=None, size=None, vram=None, ctx=None, quant=None, params=None, kind=None):
    return {"name": str(name), "loaded": loaded, "size": size, "vram": vram, "ctx": ctx,
            "quant": quant, "params": params, "type": kind}


def _probe_ollama(port):
    tags = _get_json(port, "/api/tags")
    if not (isinstance(tags, dict) and isinstance(tags.get("models"), list)):
        return None
    ps = _get_json(port, "/api/ps") or {}
    ver = (_get_json(port, "/api/version") or {}).get("version")
    loaded = {m.get("name"): m for m in (ps.get("models") or []) if isinstance(m, dict)}
    models = []
    for m in tags["models"]:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        det = m.get("details") or {}
        lm = loaded.get(m["name"])
        models.append(_m(m["name"], loaded=bool(lm), size=(lm or m).get("size"), vram=(lm or {}).get("size_vram"),
                         ctx=(lm or {}).get("context_length"), quant=det.get("quantization_level"),
                         params=det.get("parameter_size")))
    for name, lm in loaded.items():
        if name and not any(x["name"] == name for x in models):
            models.append(_m(name, loaded=True, size=lm.get("size"), vram=lm.get("size_vram")))
    return {"key": "ollama", "version": ver, "models": models,
            "memory": sum((x["vram"] or 0) for x in models if x["loaded"]) or None, "memory_label": "VRAM in use"}


def _probe_lmstudio(port):
    d = _get_json(port, "/api/v0/models")
    if isinstance(d, dict) and isinstance(d.get("data"), list):
        models = [_m(m.get("id"), loaded=(m.get("state") == "loaded"), ctx=m.get("loaded_context_length") or m.get("max_context_length"),
                     quant=m.get("quantization"), kind=m.get("type"))
                  for m in d["data"] if isinstance(m, dict) and m.get("id")]
        return {"key": "lmstudio", "models": models}
    return None


def _probe_llamacpp(port):
    props = _get_json(port, "/props")
    if not (isinstance(props, dict) and ("default_generation_settings" in props or "model_path" in props)):
        return None
    models = []
    for m in _oai_models(port) or []:
        meta = m.get("meta") or {}
        models.append(_m(m["id"], loaded=True, size=meta.get("size"), ctx=meta.get("n_ctx_train"),
                         params=(("%.1fB" % (meta["n_params"] / 1e9)) if isinstance(meta.get("n_params"), (int, float)) else None)))
    if not models and props.get("model_path"):
        models.append(_m(os.path.basename(str(props["model_path"])), loaded=True))
    return {"key": "llamacpp", "models": models}


def _probe_vllm(port):
    ms = _oai_models(port)
    if not ms or not any(str(m.get("owned_by")) == "vllm" for m in ms):
        return None
    ver = (_get_json(port, "/version") or {}).get("version")
    return {"key": "vllm", "version": ver,
            "models": [_m(m["id"], loaded=True, ctx=m.get("max_model_len")) for m in ms]}


def _probe_openwebui(port):
    d = _get_json(port, "/api/config")
    if isinstance(d, dict) and "open webui" in str(d.get("name", "")).lower():
        return {"key": "openwebui", "version": d.get("version"), "models": [], "ui": True,
                "hint": "Models are listed after you sign in to Open WebUI."}
    return None


def _probe_textgen(port):
    info = _get_json(port, "/v1/internal/model/info")
    if isinstance(info, dict) and "model_name" in info:
        lst = (_get_json(port, "/v1/internal/model/list") or {}).get("model_names") or []
        cur = info.get("model_name")
        names = list(dict.fromkeys([n for n in lst if n] + ([cur] if cur and cur != "None" else [])))
        return {"key": "textgen", "models": [_m(n, loaded=(n == cur)) for n in names]}
    st, body = _http_get(port, "/", 0.6, 300000)
    if st == 200 and body and re.search(rb"text-generation-webui|Text generation web UI", body, re.I):
        return {"key": "textgen", "models": [], "ui": True}
    return None


def _probe_comfyui(port):
    d = _get_json(port, "/system_stats")
    if isinstance(d, dict) and isinstance(d.get("system"), dict) and isinstance(d.get("devices"), list):
        dev = d["devices"][0] if d["devices"] and isinstance(d["devices"][0], dict) else {}
        used = (dev.get("vram_total") or 0) - (dev.get("vram_free") or 0)
        return {"key": "comfyui", "version": d["system"].get("comfyui_version"), "models": [], "ui": True,
                "memory": used or None, "memory_label": "VRAM in use"}
    return None


def _probe_localai(port):
    st, _b = _http_get(port, "/readyz")
    ms = _oai_models(port)
    if st == 200 and ms is not None:
        ver = (_get_json(port, "/version") or {}).get("version")
        return {"key": "localai", "version": ver, "models": [_m(m["id"]) for m in ms]}
    return None


def _probe_jan(port):
    ms = _oai_models(port)
    if ms is None:
        return None
    return {"key": "jan", "models": [_m(m["id"]) for m in ms]}


PROBES = {
    "ollama": _probe_ollama, "lmstudio": _probe_lmstudio, "llamacpp": _probe_llamacpp, "vllm": _probe_vllm,
    "openwebui": _probe_openwebui, "textgen": _probe_textgen, "comfyui": _probe_comfyui,
    "localai": _probe_localai, "jan": _probe_jan,
}
# Specific signatures first; Jan (plain OpenAI API) last.
PROBE_ORDER = ["ollama", "lmstudio", "comfyui", "openwebui", "textgen", "llamacpp", "vllm", "localai", "jan"]


def _probe_port(port, keys):
    for k in PROBE_ORDER:
        if k in keys:
            try:
                r = PROBES[k](port)
            except Exception:
                LOG.debug("probe %s on port %s failed", k, port, exc_info=True)
                r = None
            if r:
                return r
    if "lmstudio" in keys:   # older LM Studio: only the OpenAI API
        ms = _oai_models(port)
        if ms is not None:
            return {"key": "lmstudio", "models": [_m(m["id"]) for m in ms]}
    return None


def _owner_cached(port):
    hit = AI_OWNER_CACHE.get(port)
    now = time.time()
    if hit and now - hit[0] < 15:
        return hit[1]
    o = port_owner(port)
    AI_OWNER_CACHE[port] = (now, o)
    return o


def _proc_stats(pid, table, etimes=None):
    """CPU% (since last poll), tree RSS and child count for any PID, via psutil or ps."""
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            procs = [root] + root.children(recursive=True)
            rss, cpu_s = 0, 0.0
            for pr in procs:
                try:
                    rss += pr.memory_info().rss
                    t = pr.cpu_times()
                    cpu_s += t.user + t.system
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            up = max(0.0, time.time() - root.create_time())
            return _cpu_delta(pid, cpu_s, up, rss, len(procs) - 1)
        except Exception:
            LOG.debug("psutil process stats failed for pid %s", pid, exc_info=True)
    pids = _tree_pids(pid, table)
    if not pids:
        return None
    rss = sum(table[p][1] for p in pids)
    cpu_s = sum(table[p][2] for p in pids)
    up = (etimes or {}).get(pid)
    return _cpu_delta(pid, cpu_s, up, rss, len(pids) - 1)


def _cpu_delta(pid, cpu_s, up, rss, children):
    now = time.time()
    prev = AI_CPU_PREV.get(pid)
    if prev and now - prev[0] > 0.2:
        cpu = max(0.0, (cpu_s - prev[1]) / (now - prev[0]) * 100.0)
    else:
        cpu = (cpu_s / up * 100.0) if up else 0.0
    AI_CPU_PREV[pid] = (now, cpu_s)
    return {"cpu": round(cpu, 1), "rss": rss, "children": children,
            "started_at": round(now - up, 1) if up is not None else None}


def _ai_run_id(key):
    return hashlib.sha1(("ai-server:" + key).encode()).hexdigest()[:10]


def process_identity(pid):
    """Best-effort {pid, command, started_at} for a live process, or None if it is gone.

    started_at is the process start time as a Unix timestamp when it can be read
    (psutil, Linux /proc, or `ps`). Used to detect PID reuse before sending a signal.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    ident = {"pid": pid, "command": None, "started_at": None}
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            ident["command"] = p.name() or None
            ident["started_at"] = p.create_time()
            return ident
        except Exception:
            LOG.debug("psutil process identity failed for pid %s", pid, exc_info=True)
    try:
        ident["command"] = Path("/proc/%d/comm" % pid).read_text().strip() or None
        raw = Path("/proc/%d/stat" % pid).read_text()
        fields = raw[raw.rfind(")") + 1:].split()
        startticks = int(fields[19])
        clk = float(os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)
        boot = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
        if boot is not None:
            ident["started_at"] = boot + startticks / clk
        if ident["command"] or ident["started_at"] is not None:
            return ident
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime=", "-o", "comm="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False,
        ).stdout.decode("utf-8", "replace").strip()
        if out:
            parts = out.split(None, 1)
            ident["started_at"] = time.time() - _parse_cputime(parts[0])
            if len(parts) > 1:
                ident["command"] = ident["command"] or parts[1].strip() or None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ident if ident["command"] or ident["started_at"] is not None else ident


def fresh_port_identity(port):
    """Who is listening on `port` right now (bypasses the owner cache)."""
    AI_OWNER_CACHE.pop(port, None)
    owner = port_owner(port) or {}
    pid = owner.get("pid")
    if not pid:
        return None
    ident = process_identity(pid) or {"pid": int(pid), "command": None, "started_at": None}
    if not ident.get("command") and owner.get("command"):
        ident["command"] = owner["command"]
    return ident


def _identities_match(old, new):
    """True if new is the same process as old (same PID, and command/start time when both known)."""
    if not old or not new:
        return False
    if int(old["pid"]) != int(new["pid"]):
        return False
    oc = os.path.basename(str(old.get("command") or "")).lower()
    nc = os.path.basename(str(new.get("command") or "")).lower()
    if oc and nc and oc != nc:
        return False
    a, b = old.get("started_at"), new.get("started_at")
    if a is not None and b is not None and abs(float(a) - float(b)) > 1.5:
        return False
    return True


def _ai_cli(key):
    m = MODEL_SERVER_BY_KEY[key]
    tool = m.get("cli")
    if not tool:
        return None
    w = which(tool)
    if not w and tool == "lms":
        p = os.path.expanduser("~/.lmstudio/bin/lms")
        w = p if os.path.exists(p) else None
    return w


def probe_model_servers(cfg=None):
    c = _ai_cfg(cfg)
    if not c["enabled"]:
        return []
    by_port = {}
    for k, ports in c["ports"].items():
        for p in ports:
            by_port.setdefault(p, []).append(k)
    own_ports = {}
    for iid, r in list(RUNS.items()):
        if r.alive and r.port:
            own_ports[r.port] = iid
    ai_ids = {_ai_run_id(m["key"]): m["key"] for m in MODEL_SERVERS}
    results = {}

    def work(port, keys):
        st = AI_PORT_STATE.get(port) or {}
        now = time.time()
        if port in own_ports and own_ports[port] not in ai_ids:
            results[port] = None          # one of our own dev servers: don't poke it
            return
        if not port_in_use(port):
            AI_PORT_STATE[port] = {"open": False, "result": None, "at": now}
            results[port] = None
            return
        ttl = AI_NEG_TTL if port in AI_GENERIC_PORTS else AI_NEG_TTL_SPECIFIC
        if st.get("open") and st.get("result") is None and now - st.get("at", 0) < ttl:
            results[port] = None          # open but not an AI server: don't keep poking it
            return
        r = _probe_port(port, keys)
        AI_PORT_STATE[port] = {"open": True, "result": r, "at": now}
        results[port] = r

    threads = [threading.Thread(target=work, args=(p, ks), daemon=True) for p, ks in by_port.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3.0)
    rows = _ps_full()
    table = {r[0]: (r[1], r[2], r[3]) for r in rows}
    etimes = {r[0]: r[4] for r in rows}
    out, seen_keys = [], set()
    order = {m["key"]: n for n, m in enumerate(MODEL_SERVERS)}
    for port in sorted(by_port, key=lambda p: (order.get((results.get(p) or {}).get("key"), 99), p)):
        r = results.get(port)
        if not r:
            continue
        m = MODEL_SERVER_BY_KEY[r["key"]]
        seen_keys.add(m["key"])
        run = RUNS.get(_ai_run_id(m["key"]))
        ours = bool(run and run.alive)
        owner = _owner_cached(port) or {}
        pid = owner.get("pid")
        ident = process_identity(pid) if pid else None
        if ident and not ident.get("command") and owner.get("command"):
            ident["command"] = owner["command"]
        st = AI_PORT_STATE.get(port) or {}
        st["identity"] = ident
        AI_PORT_STATE[port] = st
        stats = _proc_stats(pid, table, etimes) if pid else None
        guard = bool(owner.get("command") and AI_FOREIGN_GUARD_RE.search(owner["command"]))
        models = r.get("models") or []
        models.sort(key=lambda x: (not x["loaded"], x["name"].lower()))
        stop_how = "ours" if ours else ("cli" if m.get("stop") and _ai_cli(m["key"]) else ("signal" if pid and not guard else None))
        out.append({
            "id": "%s:%d" % (m["key"], port), "key": m["key"], "name": m["name"], "port": port, "running": True,
            "url": "http://localhost:%d" % port, "ui": bool(r.get("ui")), "version": r.get("version"),
            "models": models[:200], "loaded": sum(1 for x in models if x["loaded"]),
            "memory": r.get("memory"), "memory_label": r.get("memory_label"), "hint": r.get("hint"),
            "pid": pid, "command": owner.get("command"), "health": stats, "ours": ours,
            "run_id": _ai_run_id(m["key"]) if ours else None,
            "can_stop": bool(stop_how), "stop_how": stop_how,
            "stop_note": ("Runs inside %s; stop it there." % owner.get("command")) if guard else None,
        })
    for m in MODEL_SERVERS:
        if m["key"] in seen_keys or not m.get("start"):
            continue
        cli = _ai_cli(m["key"])
        run = RUNS.get(_ai_run_id(m["key"]))
        if not cli and not (run and run.alive):
            continue
        port = c["ports"][m["key"]][0]
        out.append({"id": "%s:%d" % (m["key"], port), "key": m["key"], "name": m["name"], "port": port,
                    "running": False, "starting": bool(run and run.alive), "url": None, "models": [], "loaded": 0,
                    "can_start": bool(cli) and not (run and run.alive), "cli": cli,
                    "start_cmd": " ".join(a.replace("{PORT}", str(port)) for a in m["start"]),
                    "run_id": _ai_run_id(m["key"]) if run else None, "ours": bool(run and run.alive)})
    return out


def _ps_full():
    """[(pid, ppid, rss, cpu_s, etime_s, args)] from one ps call."""
    try:
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,rss=,time=,etime=,args="], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5, check=False).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        f = line.split(None, 5)
        if len(f) < 6:
            continue
        try:
            rows.append((int(f[0]), int(f[1]), int(f[2]) * 1024, _parse_cputime(f[3]), _parse_cputime(f[4]), f[5]))
        except ValueError:
            continue
    return rows


def _match_agent(args):
    try:
        argv = shlex.split(args)
    except ValueError:
        argv = args.split()
    if not argv:
        return None
    if ".app/Contents/" in argv[0]:
        return None               # desktop apps (Claude.app, Cursor.app helpers) aren't CLI agents
    base0 = os.path.basename(argv[0]).lower()
    if base0.startswith("python"):
        base0 = "python"
    script, rest = None, argv[1:]
    if base0 in INTERPRETERS:
        skip = [a for a in argv[1:] if not a.startswith("-")]
        if base0 == "python" and len(argv) > 2 and argv[1] == "-m":
            script, rest = argv[2], argv[3:]
        elif skip:
            script = skip[0]
            rest = argv[argv.index(script) + 1:]
    names = {base0}
    if script:
        sb = os.path.basename(script).lower()
        names.add(sb)
        names.add(re.sub(r"\.(js|mjs|cjs|py)$", "", sb))
    for sig in AGENT_SIGNATURES:
        hit = any(n in names for n in sig.get("names", ())) or any(p in args for p in sig.get("paths", ()))
        for exe, firsts in (sig.get("args_first") or {}).items():
            if base0 == exe and rest and rest[0] in firsts:
                hit = True
        if not hit:
            continue
        if "args_any" in sig:
            first = next((a for a in rest if not a.startswith("-")), "")
            if first not in sig["args_any"]:
                continue
        return sig
    return None


def _cwds(pids):
    out = {}
    if not pids:
        return out
    for pid in pids:
        if psutil is not None:
            try:
                out[pid] = psutil.Process(pid).cwd()
                continue
            except Exception:
                LOG.debug("psutil cwd failed for pid %s", pid, exc_info=True)
        try:
            out[pid] = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            pass
    missing = [p for p in pids if p not in out]
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if missing and lsof:
        try:
            res = subprocess.run([lsof, "-a", "-d", "cwd", "-p", ",".join(str(p) for p in missing), "-Fpn"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4).stdout.decode("utf-8", "replace")
            cur = None
            for line in res.splitlines():
                if line.startswith("p"):
                    cur = int(line[1:])
                elif line.startswith("n") and cur:
                    out[cur] = line[1:]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return out


def _item_for_path(cwd, items):
    best = None
    cwd = os.path.realpath(cwd)
    for it in items:
        p = os.path.realpath(str(it["path"]))
        if it.get("loose") or os.path.isfile(p):
            continue
        if cwd == p or cwd.startswith(p.rstrip("/") + "/"):
            if best is None or len(p) > best[1]:
                best = (it, len(p))
    return best[0] if best else None


def scan_agents(cfg=None):
    c = _ai_cfg(cfg)
    if not c["agents"]:
        AGENTS_BY_ITEM.clear()
        AGENT_LAST.clear()
        return []
    rows = _ps_full()
    me = os.getpid()
    table = {r[0]: (r[1], r[2], r[3]) for r in rows}
    etimes = {r[0]: r[4] for r in rows}
    matched = {}
    for pid, ppid, _rss, _cpu, _et, args in rows:
        if pid == me:
            continue
        sig = _match_agent(args)
        if sig:
            matched[pid] = (sig, ppid, args)
    # A child with the same signature (npm wrapper -> native binary, sub-agents) folds into its top parent.
    tops = {}
    for pid, (sig, ppid, args) in matched.items():
        p, top = ppid, pid
        seen = 0
        while p in table and seen < 50:
            if p in matched and matched[p][0]["key"] == sig["key"]:
                top = p
            p = table[p][0]
            seen += 1
        if top == pid:
            tops[pid] = (sig, args)
    cwds = _cwds(list(tops.keys()))
    items = build_items(cfg)
    ours = {}
    for iid, r in list(RUNS.items()):
        if r.alive and r.proc:
            for p in _tree_pids(r.proc.pid, table):
                ours[p] = iid
    out, by_item, last = [], {}, {}
    for pid, (sig, args) in sorted(tops.items()):
        cwd = cwds.get(pid)
        it = _item_for_path(cwd, items) if cwd else None
        stats = _proc_stats(pid, table, etimes) or {}
        a = {"pid": pid, "key": sig["key"], "name": sig["name"], "model": bool(sig.get("model")),
             "cwd": tilde(cwd) if cwd else None, "item_id": it["id"] if it else None,
             "item_name": display_name(it, (load_config().get("prefs") or {}).get(it["id"])) if it else None,
             "cpu": stats.get("cpu"), "rss": stats.get("rss"), "children": stats.get("children", 0),
             "started_at": stats.get("started_at"), "launched_by": ours.get(pid),
             "cmd": _short_cmd(args)}
        out.append(a)
        last[pid] = a
        if it and sig.get("chip"):
            by_item.setdefault(it["id"], []).append({"chip": sig["chip"], "name": sig["name"], "pid": pid})
    AGENTS_BY_ITEM.clear()
    AGENTS_BY_ITEM.update(by_item)
    AGENT_LAST.clear()
    AGENT_LAST.update(last)
    for pid in list(AI_CPU_PREV):
        if pid not in table:
            AI_CPU_PREV.pop(pid, None)
    return out


def _short_cmd(args):
    """Program name plus flags only: prompts passed as arguments are not shown."""
    try:
        argv = shlex.split(args)
    except ValueError:
        argv = args.split()
    keep = [os.path.basename(argv[0])] if argv else []
    for a in argv[1:6]:
        if a.startswith("-") and len(a) < 40:
            keep.append(a.split("=", 1)[0])
        elif "/" in a and len(keep) == 1:
            keep.append(os.path.basename(a))
    return " ".join(keep)


def ai_state(force=False):
    with AI_LOCK:
        now = time.time()
        if not force and AI_CACHE["data"] is not None and now - AI_CACHE["at"] < AI_TTL:
            return AI_CACHE["data"]
        cfg = load_config()
        servers = probe_model_servers(cfg)
        agents = scan_agents(cfg)
        data = {"servers": servers, "agents": agents, "settings": ai_settings_public(), "at": round(now, 2)}
        AI_CACHE.update(at=time.time(), data=data)
        return data


def ai_server_start(key):
    m = MODEL_SERVER_BY_KEY.get(key)
    if not m or not m.get("start"):
        raise ApiError(400, "Slothmore can't start %s." % (m["name"] if m else key))
    cli = _ai_cli(key)
    if not cli:
        raise ApiError(400, "%s isn't installed. %s" % (m["cli"], TOOL_HINTS.get(m["cli"], "")))
    port = _ai_cfg()["ports"][key][0]
    if port_in_use(port):
        raise ApiError(409, "Port %d is already in use." % port)
    rid = _ai_run_id(key)
    cur = RUNS.get(rid)
    if cur and cur.alive:
        return cur
    argv = [cli] + [a.replace("{PORT}", str(port)) for a in m["start"][1:]]
    env = {k: v.replace("{PORT}", str(port)) for k, v in (m.get("start_env") or {}).items()}
    pseudo = {"id": rid, "name": m["name"], "cmd": " ".join(m["start"]).replace("{PORT}", str(port)), "argv": argv,
              "display_cmd": " ".join(m["start"]).replace("{PORT}", str(port)), "web": False, "static": False,
              "port": None, "cwd": os.path.expanduser("~"), "env": env, "missing_tool": None, "needs_choice": False,
              "stop_signal": None, "html_file": None, "html_files": [], "error": None}
    run = start_run(pseudo, {"args": None}, trigger="ai", check_port=False, no_args=True)
    run.cancel_auto_restart = True
    AI_CACHE.update(at=0.0, data=None)
    return run


def _ai_server_stop_foreign(m, key, port):
    """Stop a model server Slothmore did not start, after a fresh check of port, process and probe.

    Re-probes the HTTP signature, re-resolves the listening PID, and (when we recorded
    one earlier) confirms it is the same process by PID, command and start time. If
    anything has changed, refuses rather than signalling a different process.
    """
    prev = (AI_PORT_STATE.get(port) or {}).get("identity")
    try:
        fresh = _probe_port(port, [key])
    except Exception:
        LOG.warning("re-probe of port %s failed before stop", port, exc_info=True)
        fresh = None
    if not fresh or fresh.get("key") != key:
        raise ApiError(409, "%s is no longer running on port %d. Refresh and try again." % (m["name"], port))
    ident = fresh_port_identity(port)
    if not ident or not ident.get("pid"):
        raise ApiError(409, "Couldn't find the process listening on port %d." % port)
    if prev and not _identities_match(prev, ident):
        raise ApiError(
            409,
            "The process on port %d has changed (it is no longer the same %s process). Refresh and try again."
            % (port, m["name"]),
        )
    cmd = ident.get("command")
    if cmd and AI_FOREIGN_GUARD_RE.search(cmd):
        raise ApiError(409, "Port %d belongs to %s; stop the container or VM there." % (port, cmd))
    cli = _ai_cli(key)
    if m.get("stop") and cli:
        try:
            res = subprocess.run([cli] + m["stop"][1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=15, shell=False)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError(500, "Could not run %s: %s" % (" ".join(m["stop"]), e))
        AI_PORT_STATE.pop(port, None)
        AI_OWNER_CACHE.pop(port, None)
        AI_CACHE.update(at=0.0, data=None)
        return {"stopped": "cli", "output": res.stdout.decode("utf-8", "replace")[-2000:], "pid": ident["pid"]}
    pid = ident["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise ApiError(403, "Not allowed to stop PID %d (owned by another user)." % pid)
    AI_PORT_STATE.pop(port, None)
    AI_OWNER_CACHE.pop(port, None)
    AI_CACHE.update(at=0.0, data=None)
    return {"stopped": "signal", "pid": pid}


def ai_server_stop(key, port, confirm):
    m = MODEL_SERVER_BY_KEY.get(key)
    if not m:
        raise ApiError(404, "Unknown server.")
    run = RUNS.get(_ai_run_id(key))
    if run and run.alive:
        stop_run(run)
        AI_CACHE.update(at=0.0, data=None)
        return {"stopped": "ours"}
    if not confirm:
        raise ApiError(409, "confirm_required", detail="%s wasn't started by Slothmore." % m["name"])
    return _ai_server_stop_foreign(m, key, port)


def ai_agent_stop(pid, confirm):
    if not confirm:
        raise ApiError(409, "confirm_required")
    a = AGENT_LAST.get(pid)
    if not a:
        raise ApiError(404, "PID %d isn't in the list of running agents." % pid)
    # Re-check: the PID must still be the same agent (PIDs get reused).
    rows = {r[0]: r for r in _ps_full()}
    row = rows.get(pid)
    sig = _match_agent(row[5]) if row else None
    if not sig or sig["key"] != a["key"]:
        AGENT_LAST.pop(pid, None)
        raise ApiError(409, "PID %d is no longer %s." % (pid, a["name"]))
    try:
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return {"stopped": True}
    except PermissionError:
        raise ApiError(403, "Not allowed to stop PID %d (owned by another user)." % pid)

    def later():
        for _ in range(30):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return
        try:
            os.kill(pid, signal.SIGTERM)   # never SIGKILL a foreign process
        except OSError:
            pass
    threading.Thread(target=later, daemon=True).start()
    AI_CACHE.update(at=0.0, data=None)
    return {"stopped": True, "pid": pid}


def sse_poll(run, idx, timeout=15):
    """Wait for the next SSE payload. Returns (new_idx, kind, payload).

    kind is 'chunks' (payload is a list of texts), 'ping', or 'exit' (payload is the exit code).
    The condition lock is released before this returns, so the caller can write to a socket safely.
    """
    with run.cond:
        if idx < run.base:
            idx = run.base
        pending = list(run.chunks[idx - run.base:])
        if pending:
            return idx + len(pending), "chunks", pending
        if not run.alive:
            return idx, "exit", run.exit_code
        run.cond.wait(timeout)
        pending = list(run.chunks[idx - run.base:])
        if pending:
            return idx + len(pending), "chunks", pending
        if not run.alive:
            return idx, "exit", run.exit_code
        return idx, "ping", None


class Handler(BaseHTTPRequestHandler):
    server_version = "Slothmore"
    timeout = 60  # a client that promises a body and never sends it can't hold a thread forever

    def log_message(self, fmt, *args):
        try:
            LOG.debug("%s", fmt % args if args else fmt)
        except Exception:
            LOG.debug("access log failed", exc_info=True)

    def _internal_error(self, exc):
        # Log the traceback; never send it (or the token) to the client.
        LOG.exception("internal error while handling %s %s", self.command, urlparse(self.path).path)
        self._send(500, {"error": "internal error"})

    # -- plumbing
    def _security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # -- auth: token in ~/.slothmore/token, sent as a cookie (browser) or Bearer header (CLI, menu bar)
    def _token_ok(self, candidate):
        tok = getattr(self.server, "token", None)
        return bool(tok and candidate) and hmac.compare_digest(str(candidate).encode(), tok.encode())

    def _authed(self):
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer " and self._token_ok(auth[7:].strip()):
            return True
        raw = self.headers.get("Cookie")
        if raw:
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME and self._token_ok(v.strip()):
                    return True
        return False

    def _cookie_header(self):
        return ("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
                % (COOKIE_NAME, self.server.token, COOKIE_MAX_AGE))

    def _login_page(self, code=401, bad_token=False):
        try:
            html = (STATIC / "login.html").read_text(encoding="utf-8")
        except OSError:
            html = "<!doctype html><title>Slothmore</title><p>Run <code>slothmore open</code> to sign in.</p>"
        html = html.replace("{{ERROR}}", "That token is not valid. Run <code>slothmore open</code> again."
                            if bad_token else "")
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    def _guard(self, write):
        """Reject DNS rebinding (Host) and cross-site requests (Origin, Sec-Fetch-Site)."""
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed_hosts:
            self._send(403, {"error": "forbidden host"})
            return False
        if write:
            origin = (self.headers.get("Origin") or "").lower()
            if origin not in self.server.allowed_origins:
                self._send(403, {"error": "forbidden origin"})
                return False
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            self._send(403, {"error": "cross-site request"})
            return False
        return True

    def _body(self):
        raw = (self.headers.get("Content-Length") or "").strip()
        if not raw:
            return {}
        if not raw.isdigit():
            raise ApiError(400, "bad Content-Length")
        n = int(raw)
        if n == 0:
            return {}
        if n > MAX_BODY:
            raise ApiError(413, "request body too large")
        try:
            data = json.loads(self.rfile.read(n))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad json")
        return data if isinstance(data, dict) else {}

    # -- GET
    def do_GET(self):
        if not self._guard(False):
            return
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        is_api = u.path.startswith("/api/")
        q_all = parse_qs(u.query, keep_blank_values=True)
        if not is_api and "token" in q_all:
            # Jupyter-style sign-in: trade ?token= for an HttpOnly cookie, then strip it from the URL.
            if not self._token_ok((q_all.get("token") or [""])[0]):
                return self._login_page(401, bad_token=True)
            rest = [(k, v) for k, vs in q_all.items() if k != "token" for v in vs]
            loc = u.path + ("?" + "&".join("%s=%s" % (quote(k), quote(v)) for k, v in rest) if rest else "")
            return self._send(302, b"", "text/plain; charset=utf-8",
                              headers=[self._cookie_header(), ("Location", loc or "/")])
        if is_api and not self._authed():
            return self._send(401, {"error": "unauthorized",
                                    "detail": "Open Slothmore with `slothmore open` to sign in."})
        if u.path in ("/", "/index.html", "/compact", "/compact.html") and not self._authed():
            return self._login_page(401)
        if u.path == "/compact":  # the quick panel shown by the menu bar icon
            u = u._replace(path="/compact.html")
        try:
            if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "env":
                if not ITEM_ID_RE.match(parts[2]):
                    raise ApiError(404, "unknown item")
                it = find_item(parts[2])
                if not it:
                    raise ApiError(404, "unknown item")
                prefs = (load_config().get("prefs") or {}).get(it["id"]) or {}
                env = prefs.get("env") if isinstance(prefs.get("env"), dict) else {}
                return self._send(200, {"env": {str(k): str(v) for k, v in env.items()}})
            if u.path == "/api/items":
                return self._send(200, self._listing())
            if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "stream":
                return self._stream(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "history":
                if not ITEM_ID_RE.match(parts[2]):
                    raise ApiError(404, "unknown item")
                return self._send(200, {"history": history_public(parts[2])})
            if len(parts) == 6 and parts[:2] == ["api", "items"] and parts[3] == "history" and parts[5] == "log":
                if not (ITEM_ID_RE.match(parts[2]) and RUN_ID_RE.match(parts[4])):
                    raise ApiError(404, "unknown run")
                entry, text = read_run_log(parts[2], parts[4])
                return self._send(200, {"entry": entry, "text": text})
            if u.path == "/api/events":
                q = parse_qs(u.query)
                try:
                    after = int((q.get("after") or ["-1"])[0])
                except ValueError:
                    after = -1
                return self._send(200, events_since(after, (q.get("browser") or ["0"])[0] == "1"))
            if u.path == "/api/settings":
                return self._send(200, settings_public())
            if u.path == "/api/ai":
                return self._send(200, ai_state(force=(parse_qs(u.query).get("force") or ["0"])[0] == "1"))
            if u.path == "/api/menubar":
                return self._send(200, menubar_login_status())
            if u.path == "/api/ports":
                return self._send(200, {"ports": list_listening_ports(own_port=self.server.server_address[1])})
            if u.path == "/api/health":
                return self._send(200, {"items": [
                    {"id": iid, "name": r.name, "pid": r.proc.pid if r.proc else None, "started_at": r.started_at,
                     "health": r.health} for iid, r in list(RUNS.items()) if r.alive]})
            if len(parts) == 4 and parts[:2] == ["api", "items"] and parts[3] == "readme":
                if not ITEM_ID_RE.match(parts[2]):
                    raise ApiError(404, "unknown item")
                it = find_item(parts[2])
                if not it:
                    raise ApiError(404, "unknown item")
                return self._send(200, readme_payload(it))
            if len(parts) == 5 and parts[:2] == ["api", "items"] and parts[3] == "readme" and parts[4] == "asset":
                if not ITEM_ID_RE.match(parts[2]):
                    raise ApiError(404, "unknown item")
                it = find_item(parts[2])
                if not it:
                    raise ApiError(404, "unknown item")
                rel = (parse_qs(u.query).get("path") or [""])[0]
                data, ctype = readme_asset(it, rel)
                return self._send(200, data, ctype)
            if u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            return self._static(u.path)
        except ApiError as e:
            self._send(e.status, e.payload)
        except Exception as e:
            self._internal_error(e)

    def _listing(self):
        cfg = load_config()
        items = [public(it, cfg) for it in build_items(cfg)]
        watch = []
        for w in cfg["watch"]:
            watch.append(
                {
                    "raw": w, "path": tilde(w), "name": os.path.basename(w) or w,
                    "exists": os.path.isdir(w),
                    "depth": watch_opts(cfg, w)["depth"], "loose": watch_opts(cfg, w)["loose"],
                    "count": sum(1 for i in items if i["source"] == "watch" and i["root"] == tilde(w)),
                }
            )
        return {
            "items": items, "watch": watch,
            "pinned": sum(1 for i in items if i["source"] == "pinned"),
            "port": self.server.server_address[1],
            "groups": groups_public(cfg, {i["id"]: i for i in items}),
        }

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        root = STATIC.resolve()
        p = (STATIC / rel).resolve()
        if root not in p.parents or not p.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("json") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)

    def _stream(self, iid):
        run = RUNS.get(iid)
        if not run:
            return self._send(404, {"error": "no output yet"})
        q = parse_qs(urlparse(self.path).query)
        try:
            idx = int(self.headers.get("Last-Event-ID") or (q.get("from") or [0])[0])
        except ValueError:
            idx = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        try:
            while True:
                idx, kind, payload = sse_poll(run, idx, 15)
                if kind == "exit":
                    self.wfile.write(("event: exit\ndata: %s\n\n" % json.dumps({"code": payload})).encode())
                    self.wfile.flush()
                    return
                if kind == "ping":
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                start = idx - len(payload)
                for i, text in enumerate(payload):
                    ev_id = start + i + 1
                    self.wfile.write(("id: %d\ndata: %s\n\n" % (ev_id, json.dumps({"t": text}))).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("SSE client disconnected", exc_info=True)
            return

    # -- POST
    def do_POST(self):
        if not self._guard(True):
            return
        if not self._authed():
            return self._send(401, {"error": "unauthorized",
                                    "detail": "Open Slothmore with `slothmore open` to sign in."})
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        try:
            body = self._body()
            if len(parts) == 4 and parts[:2] == ["api", "items"]:
                self._item_action(parts[2], parts[3], body)
            elif u.path == "/api/pin":
                if body.get("path"):
                    pin_path(body["path"])
                else:
                    pin_name(body.get("name"))
                self._send(200, self._listing())
            elif u.path == "/api/unpin":
                unpin(str(body.get("id", "")))
                self._send(200, self._listing())
            elif u.path == "/api/reorder":
                reorder([str(i) for i in body.get("ids", [])])
                self._send(200, self._listing())
            elif u.path == "/api/watch":
                add_watch(body.get("path", ""))
                self._send(200, self._listing())
            elif u.path == "/api/watch-opts":
                set_watch_opts(body)
                self._send(200, self._listing())
            elif u.path == "/api/unwatch":
                remove_watch(body.get("path", ""))
                self._send(200, self._listing())
            elif u.path == "/api/settings":
                update_settings(body)
                self._send(200, settings_public())
            elif u.path == "/api/ai/settings":
                self._send(200, update_ai_settings(body))
            elif len(parts) == 5 and parts[:3] == ["api", "ai", "servers"] and parts[3] in MODEL_SERVER_BY_KEY \
                    and parts[4] in ("start", "stop"):
                if parts[4] == "start":
                    run = ai_server_start(parts[3])
                    self._send(200, {"run_id": run.item_id, "ai": ai_state(force=True)})
                else:
                    try:
                        port = int(body.get("port"))
                    except (TypeError, ValueError):
                        raise ApiError(400, "port is required")
                    res = ai_server_stop(parts[3], port, body.get("confirm") is True)
                    res["ai"] = ai_state(force=True)
                    self._send(200, res)
            elif len(parts) == 5 and parts[:3] == ["api", "ai", "agents"] and parts[3].isdigit() and parts[4] == "stop":
                res = ai_agent_stop(int(parts[3]), body.get("confirm") is True)
                self._send(200, res)
            elif u.path == "/api/notify-test":
                ev = push_event(None, "Slothmore", "test", "Test notification",
                                "Notifications from Slothmore are working.", force_mac=bool(body.get("macos")))
                self._send(200, {"event": ev, "settings": settings_public()})
            elif u.path == "/api/groups":
                save_group(body)
                self._send(200, self._listing())
            elif len(parts) == 4 and parts[:2] == ["api", "groups"] and GROUP_ID_RE.match(parts[2]):
                if parts[3] == "delete":
                    delete_group(parts[2])
                elif parts[3] in ("start", "stop", "restart"):
                    group_action(parts[2], parts[3])
                else:
                    raise ApiError(404, "unknown action")
                self._send(200, self._listing())
            elif u.path == "/api/menubar":
                if "enabled" not in body or not isinstance(body["enabled"], bool):
                    raise ApiError(400, "enabled (true or false) is required")
                self._send(200, install_menubar_login() if body["enabled"] else uninstall_menubar_login())
            elif u.path == "/api/runs/stop-all":
                if body.get("confirm") is not True:
                    raise ApiError(409, "confirm_required", detail="Stop everything Slothmore started?")
                self._send(200, stop_all_mine())
            elif u.path == "/api/ports/stop":
                res = stop_listening_port(
                    body.get("port"), body.get("pid"), body.get("confirm") is True,
                    own_port=self.server.server_address[1],
                )
                self._send(200, res)
            elif u.path == "/api/pick":
                picked = pick_folder()
                if body.get("mode") == "watch":
                    add_watch(picked)
                else:
                    pin_path(picked)
                self._send(200, self._listing())
            else:
                self._send(404, {"error": "not found"})
        except ApiError as e:
            self._send(e.status, e.payload)
        except Exception as e:  # keep the server alive on unexpected errors
            self._internal_error(e)

    def _item_action(self, iid, action, body):
        it = find_item(iid)
        if not it:
            raise ApiError(404, "unknown item")
        run = RUNS.get(iid)
        if action == "start":
            start_run(it, body)
        elif action == "restart":
            restart_run(it, body)
        elif action == "stop":
            stop_run(run)
        elif action == "prefs":
            update_prefs(it, body)
        elif action == "history-clear":
            clear_history(iid)
        elif action == "install":
            install_item(it)
        elif action == "readme-open":
            self._send(200, open_readme(it, body.get("mode", "reveal")))
            return
        elif action == "input":
            if not run or not run.alive:
                raise ApiError(409, "not running")
            text = str(body.get("text", ""))
            try:
                os.write(run.master, text.encode())
            except OSError:
                raise ApiError(409, "not running")
        else:
            raise ApiError(404, "unknown action")
        self._send(200, public(find_item(iid) or it))



# --------------------------------------------------------------------------- listening ports (Ports view)

LIKELY_HTTP_PORTS = {
    80, 81, 443, 3000, 3001, 4000, 4173, 5000, 5173, 8000, 8001, 8080, 8081,
    8443, 8888, 9000, 9443, 10443, 11434, 1234, 1337, 7860, 8188, 8787, 7777,
}
LIKELY_HTTP_CMDS = {
    "node", "nodejs", "python", "python3", "uvicorn", "gunicorn", "hypercorn",
    "ruby", "puma", "rackup", "php", "nginx", "caddy", "httpd", "apache2",
    "deno", "bun", "next", "vite", "java", "caddy", "traefik", "code-server",
    "grafana", "streamlit", "gradio", "jupyter", "jupyter-lab", "jupyter-labtop",
    "ollama", "comfyui", "lm-studio", "lms",
}
PORTS_IDENTITY = {}  # (port, pid) -> identity from the last list, used by Stop


def _split_host_port(name):
    """Parse an lsof/ss local address into (host, port) or (None, None)."""
    raw = re.sub(r"\s+\(.*\)$", "", str(name or "").strip())
    if not raw:
        return None, None
    if raw.startswith("[") and "]" in raw:
        br = raw.find("]")
        host, rest = raw[1:br], raw[br + 1:]
        if rest.startswith(":"):
            try:
                return _norm_addr(host), int(rest[1:])
            except ValueError:
                return None, None
        return None, None
    if ":" not in raw:
        return None, None
    host, _, port = raw.rpartition(":")
    try:
        return _norm_addr(host or "*"), int(port)
    except ValueError:
        return None, None


def _norm_addr(host):
    h = (host or "*").strip()
    if h in ("*", "0.0.0.0", "::", "[::]", "::0"):
        return "*"
    if h.startswith("::ffff:"):
        return h[7:]
    return h


def parse_lsof_listen(text):
    """Parse `lsof -nP -iTCP -sTCP:LISTEN -Fpcun` into raw listen records."""
    recs, cur = [], None

    def flush():
        if not cur:
            return
        for port, addr in cur["listens"]:
            recs.append({
                "pid": cur["pid"], "command": cur["command"], "user": cur["user"],
                "uid": cur["uid"], "port": port, "address": addr,
            })

    for line in str(text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            flush()
            try:
                cur = {"pid": int(val), "command": None, "user": None, "uid": None, "listens": []}
            except ValueError:
                cur = None
        elif cur is None:
            continue
        elif tag == "c":
            cur["command"] = val or None
        elif tag == "u":
            cur["user"] = val or None
            if val and val.isdigit():
                cur["uid"] = int(val)
        elif tag == "n":
            host, port = _split_host_port(val)
            if port:
                cur["listens"].append((port, host))
    flush()
    return recs


_SS_USERS_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
_SS_ADDR_RE = re.compile(r"(\[?[0-9A-Fa-f:\.*]+\]?:\d+)")


def parse_ss_listen(text):
    """Parse `ss -ltnpH` into the same raw listen records as parse_lsof_listen."""
    recs = []
    for line in str(text or "").splitlines():
        addrs = _SS_ADDR_RE.findall(line)
        if not addrs:
            continue
        host, port = _split_host_port(addrs[0])
        if not port:
            continue
        m = _SS_USERS_RE.search(line)
        recs.append({
            "pid": int(m.group(2)) if m else None,
            "command": (m.group(1).rstrip(".") if m else None),
            "user": None, "uid": None, "port": port, "address": host,
        })
    return recs


def _collect_listen_records():
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if lsof:
        try:
            out = subprocess.run(
                [lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcun"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False, shell=False,
            ).stdout.decode("utf-8", "replace")
            recs = parse_lsof_listen(out)
            if recs:
                return recs
        except (OSError, subprocess.SubprocessError) as e:
            LOG.debug("lsof listen listing failed: %s", e, exc_info=True)
    ss = shutil.which("ss")
    if ss:
        try:
            out = subprocess.run(
                [ss, "-ltnpH"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=False, shell=False,
            ).stdout.decode("utf-8", "replace")
            return parse_ss_listen(out)
        except (OSError, subprocess.SubprocessError) as e:
            LOG.debug("ss listen listing failed: %s", e, exc_info=True)
    LOG.warning("No lsof or ss available to list listening ports")
    return []


def _pid_uid(pid):
    if not pid:
        return None
    if psutil is not None:
        try:
            return psutil.Process(int(pid)).uids().real
        except Exception:
            LOG.debug("psutil uid failed for pid %s", pid, exc_info=True)
    try:
        return Path("/proc/%d" % int(pid)).stat().st_uid
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "uid="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3, check=False, shell=False,
        ).stdout.decode().strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _pid_user(uid, fallback=None):
    if fallback and not (fallback.isdigit() if isinstance(fallback, str) else False):
        return fallback
    if uid is None:
        return fallback
    try:
        import pwd
        return pwd.getpwuid(int(uid)).pw_name
    except Exception:
        return fallback or str(uid)


def _run_for_pid_or_port(pid, port):
    for run in list(RUNS.values()):
        if not run.alive:
            continue
        if port and run.port == port:
            return run
        if pid and run.proc and run.proc.pid == pid:
            return run
    return None


def _project_for_listen(pid, port, cwd, cfg):
    run = _run_for_pid_or_port(pid, port)
    if run:
        folder = None
        try:
            it = find_item(run.item_id)
            if it and it.get("path"):
                folder = tilde(it["path"])
        except Exception:
            LOG.debug("could not resolve project for run %s", run.item_id, exc_info=True)
        if not folder and cwd:
            folder = tilde(cwd)
        return folder, run.item_id, run.name
    if not cwd:
        return None, None, None
    cwd_abs = os.path.abspath(cwd)
    best = None
    for root in list(cfg.get("watch") or []) + list(cfg.get("pinned") or []):
        r = expand(root)
        if not os.path.isdir(r):
            continue
        r = os.path.abspath(r)
        if cwd_abs == r or cwd_abs.startswith(r + os.sep):
            if best is None or len(r) > len(best):
                best = r
    if best:
        return tilde(cwd_abs), None, None
    return None, None, None


def _addr_rank(addr):
    if addr in ("*", "0.0.0.0", "::"):
        return 0
    if addr in ("127.0.0.1", "::1"):
        return 1
    return 2


def _listen_url(port, addresses):
    addrs = addresses or ["127.0.0.1"]
    if "*" in addrs or "0.0.0.0" in addrs or "127.0.0.1" in addrs:
        host = "127.0.0.1"
    elif "::" in addrs or "::1" in addrs:
        host = "[::1]"
    else:
        a = addrs[0]
        host = ("[%s]" % a) if ":" in a else a
    return "http://%s:%d" % (host, port)


def list_listening_ports(own_port=None):
    """Every TCP LISTEN socket, enriched for the Ports view. Never uses a shell."""
    cfg = load_config()
    recs = _collect_listen_records()
    grouped = {}
    for r in recs:
        key = (r["port"], r.get("pid"))
        g = grouped.setdefault(key, {
            "port": r["port"], "pid": r.get("pid"), "command": r.get("command"),
            "user": r.get("user"), "uid": r.get("uid"), "addresses": [],
        })
        if r.get("address") and r["address"] not in g["addresses"]:
            g["addresses"].append(r["address"])
        if r.get("command") and not g["command"]:
            g["command"] = r["command"]
        if r.get("user") and not g["user"]:
            g["user"] = r["user"]
        if r.get("uid") is not None and g["uid"] is None:
            g["uid"] = r["uid"]
    pids = [g["pid"] for g in grouped.values() if g["pid"]]
    cwds = _cwds(pids) if pids else {}
    me = os.getuid()
    out = []
    PORTS_IDENTITY.clear()
    for g in grouped.values():
        pid, port = g["pid"], g["port"]
        if g["uid"] is None and pid:
            g["uid"] = _pid_uid(pid)
        g["user"] = _pid_user(g["uid"], g.get("user"))
        addrs = sorted(g["addresses"] or ["*"], key=_addr_rank)
        display = addrs[0]
        if display == "*":
            display = "0.0.0.0"
        folder, item_id, item_name = _project_for_listen(pid, port, cwds.get(pid), cfg)
        cmd = g.get("command") or ""
        base = os.path.basename(cmd).lower()
        http = bool(
            port in LIKELY_HTTP_PORTS
            or base in LIKELY_HTTP_CMDS
            or any(base.startswith(c) for c in ("python", "node", "next-"))
        )
        if item_id:
            run = RUNS.get(item_id)
            if run and (run.detected_web if hasattr(run, "detected_web") else False):
                http = True
        ident = process_identity(pid) if pid else None
        if ident:
            if not ident.get("command") and cmd:
                ident["command"] = cmd
            PORTS_IDENTITY[(port, pid)] = ident
        ours = bool(own_port and port == own_port)
        owned = bool(pid and g["uid"] is not None and g["uid"] == me)
        foreign = bool(cmd and AI_FOREIGN_GUARD_RE.search(cmd))
        can_stop = bool(owned and pid and pid != 1 and not ours and not foreign)
        out.append({
            "port": port,
            "address": display,
            "addresses": addrs,
            "pid": pid,
            "command": cmd or None,
            "user": g["user"],
            "uid": g["uid"],
            "folder": folder,
            "item_id": item_id,
            "item_name": item_name,
            "ours": ours,
            "owned": owned,
            "can_stop": can_stop,
            "http": http,
            "url": _listen_url(port, addrs) if http else None,
            "started_at": ident.get("started_at") if ident else None,
        })
    out.sort(key=lambda x: (x["port"], x["pid"] or 0, x["address"]))
    return out


def stop_listening_port(port, pid, confirm, own_port=None):
    """Stop the process listening on port, after a fresh identity check (finding 7)."""
    try:
        port = int(port)
        pid = int(pid)
    except (TypeError, ValueError):
        raise ApiError(400, "port and pid are required")
    if own_port and port == int(own_port):
        raise ApiError(409, "That's the Slothmore dashboard itself; stop it from Terminal or with brew services.")
    if pid <= 1:
        raise ApiError(403, "Won't signal PID %d." % pid)
    if not confirm:
        raise ApiError(409, "confirm_required", detail="Stop the process on port %d (PID %d)?" % (port, pid))
    uid = _pid_uid(pid)
    if uid is None or uid != os.getuid():
        raise ApiError(403, "Can only stop processes owned by you.")
    ident = fresh_port_identity(port)
    if not ident or not ident.get("pid"):
        raise ApiError(409, "Couldn't find the process listening on port %d. Refresh and try again." % port)
    if int(ident["pid"]) != pid:
        raise ApiError(409, "Port %d is no longer PID %d. Refresh and try again." % (port, pid))
    prev = PORTS_IDENTITY.get((port, pid))
    if prev and not _identities_match(prev, ident):
        raise ApiError(409, "The process on port %d has changed. Refresh and try again." % port)
    # Even without a cached identity, require the live process still to be the port owner
    live = process_identity(pid)
    if live and not _identities_match(ident, live):
        raise ApiError(409, "The process on port %d has changed. Refresh and try again." % port)
    cmd = ident.get("command") or (live or {}).get("command") or ""
    if cmd and AI_FOREIGN_GUARD_RE.search(cmd):
        raise ApiError(409, "Port %d belongs to %s; stop the container or VM there." % (port, cmd))
    run = _run_for_pid_or_port(pid, port)
    if run and run.alive:
        stop_run(run)
        PORTS_IDENTITY.pop((port, pid), None)
        LOG.info("stopped Slothmore-started process on port %d (pid %d)", port, pid)
        return {"stopped": "ours", "pid": pid, "port": port}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise ApiError(403, "Not allowed to stop PID %d (owned by another user)." % pid)
    PORTS_IDENTITY.pop((port, pid), None)
    LOG.info("sent SIGTERM to pid %d on port %d", pid, port)
    return {"stopped": "signal", "pid": pid, "port": port}


def make_server(port, token):

    """Bind 127.0.0.1:port (0 = any free port) and attach the auth token and allowed Host/Origin values."""
    global _ACTIVE_TOKEN
    if token:
        _ACTIVE_TOKEN = token
        _remember_secret(token)
    if not LOG.handlers:
        setup_logging(interactive=False)
    _refresh_secret_values()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    server.allowed_hosts = {"127.0.0.1:%d" % actual, "localhost:%d" % actual}
    server.allowed_origins = {"http://" + h for h in server.allowed_hosts}
    server.token = token
    return server


def _probe(port, token):
    """'ours' if a Slothmore with this token answers on port, 'other' if something else does, None if nothing."""
    req = urllib.request.Request("http://127.0.0.1:%d/api/settings" % port,
                                 headers={"Authorization": "Bearer " + (token or "")})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return "ours" if r.status == 200 else "other"
    except urllib.error.HTTPError:
        return "other"
    except (OSError, ValueError):
        return None


def _state_port():
    try:
        return int(json.loads(STATE_PATH.read_text()).get("port"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _open_url(url):
    try:
        import webbrowser
        return webbrowser.open(url)
    except Exception:
        LOG.warning("could not open the browser", exc_info=True)
        return False


# --------------------------------------------------------------------------- menu bar helper (macOS)

MENUBAR_LABEL = "com.slothmore.menubar"
RUMPS_HELP = ("The menu bar icon needs the Python package 'rumps', which is not installed for this Python.\n"
              "  Homebrew install:  brew reinstall markusswedpal/tap/slothmore   (0.2.1 or newer installs it)\n"
              "  From source:       %s -m pip install --user rumps\n"
              "Then run `slothmore menubar` again.")


def launch_agents_dir():
    return Path(os.environ.get("LAUNCH_AGENTS_DIR") or (Path.home() / "Library" / "LaunchAgents"))


def menubar_plist_path():
    return launch_agents_dir() / (MENUBAR_LABEL + ".plist")


def rumps_available():
    try:
        import importlib.util
        return importlib.util.find_spec("rumps") is not None
    except (ImportError, ValueError):
        return False


def webkit_available():
    """PyObjC WebKit bindings present: the menu bar icon opens the quick panel instead of the classic menu."""
    try:
        import importlib.util
        return all(importlib.util.find_spec(m) is not None for m in ("WebKit", "AppKit"))
    except (ImportError, ValueError):
        return False


def slothmore_command():
    """argv that starts this install of Slothmore. The Homebrew wrapper sets SLOTHMORE_BIN to a path that survives upgrades."""
    exe = os.environ.get("SLOTHMORE_BIN")
    if exe and os.path.isfile(exe):
        return [exe]
    return [sys.executable, str(HERE / "bin" / "slothmore")]


def menubar_plist_data():
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"}
    for k in ("SLOTHMORE_HOME", "LAUNCHER_HOME", "SLOTHMORE_BIN", "SLOTHMORE_HOTKEY", "SLOTHMORE_MENUBAR_CLASSIC"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    log = str(HOME_DIR / "menubar.log")
    return {
        "Label": MENUBAR_LABEL,
        "ProgramArguments": slothmore_command() + ["menubar"],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        # Relaunch after a crash, but not after "Quit menu bar helper" (clean exit).
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _launchctl(*args):
    """Run launchctl with an argv list (never a shell). SLOTHMORE_DRY_RUN=1 skips it."""
    if os.environ.get("SLOTHMORE_DRY_RUN") == "1":
        LOG.info("[dry run] launchctl %s", " ".join(args))
        return 0
    try:
        r = subprocess.run(["launchctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=15, check=False, shell=False)
        if r.returncode:
            LOG.debug("launchctl %s -> %s %s", " ".join(args), r.returncode, r.stderr.decode("utf-8", "replace").strip())
        return r.returncode
    except (OSError, subprocess.SubprocessError):
        LOG.warning("launchctl failed", exc_info=True)
        return 1


def menubar_panel_state():
    """'on', or 'off' when the menu bar helper's crash guard switched the quick panel off for this version."""
    try:
        st = json.loads((HOME_DIR / "menubar.state").read_text())
    except (OSError, ValueError):
        return "on"
    if not isinstance(st, dict):
        return "on"
    if st.get("state") == "panel-disabled" and st.get("version") == __version__:
        return "off"
    if st.get("state") == "starting-panel":
        return "starting"
    return "on"


def menubar_login_status():
    plist = menubar_plist_path()
    return {"supported": IS_MAC, "installed": plist.is_file(), "plist": str(plist),
            "rumps": rumps_available(), "webkit": webkit_available(), "log": str(HOME_DIR / "menubar.log"),
            "panel": menubar_panel_state()}


def reset_menubar_panel():
    """Forget the crash guard's 'quick panel off' note and restart the login item if it is installed."""
    try:
        (HOME_DIR / "menubar.state").unlink()
    except FileNotFoundError:
        pass
    if IS_MAC and menubar_plist_path().is_file():
        _launchctl("kickstart", "-k", "gui/%s/%s" % (os.getuid(), MENUBAR_LABEL))


def install_menubar_login():
    """Write the LaunchAgent and load it. Raises ApiError (400/409) with a clear message when it cannot."""
    import plistlib
    if not IS_MAC:
        raise ApiError(409, "The menu bar icon only works on macOS.")
    if not rumps_available():
        raise ApiError(409, RUMPS_HELP % sys.executable)
    d = launch_agents_dir()
    d.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    plist = menubar_plist_path()
    with open(str(plist), "wb") as f:
        plistlib.dump(menubar_plist_data(), f)
    uid = str(os.getuid())
    _launchctl("bootout", "gui/%s/%s" % (uid, MENUBAR_LABEL))  # fine if it was not loaded
    rc = _launchctl("bootstrap", "gui/%s" % uid, str(plist))
    if rc:
        raise ApiError(500, "launchctl could not load %s (exit %d). Try logging out and in again." % (plist, rc))
    LOG.info("menu bar login item installed: %s", plist)
    return menubar_login_status()


def uninstall_menubar_login():
    if IS_MAC:
        _launchctl("bootout", "gui/%s/%s" % (os.getuid(), MENUBAR_LABEL))
    try:
        menubar_plist_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOG.warning("could not remove %s", menubar_plist_path(), exc_info=True)
    LOG.info("menu bar login item removed")
    return menubar_login_status()


def cmd_menubar(argv):
    """`slothmore menubar [--install-login | --uninstall-login | --status]`"""
    ap = argparse.ArgumentParser(
        prog="slothmore menubar",
        description="Show Slothmore in the macOS menu bar (needs the 'rumps' package, installed by the Homebrew formula).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install-login", action="store_true", help="start the menu bar icon at every login")
    g.add_argument("--uninstall-login", action="store_true", help="remove the login item")
    g.add_argument("--status", action="store_true", help="show whether the login item is installed")
    g.add_argument("--reset-panel", action="store_true",
                   help="turn the quick panel back on after a crash switched it off (restarts the login item)")
    ap.add_argument("--url", default=None, help="dashboard address (default: read from ~/.slothmore/server.json)")
    ap.add_argument("--classic", action="store_true",
                    help="click shows the classic menu instead of the quick panel (or SLOTHMORE_MENUBAR_CLASSIC=1)")
    ap.add_argument("--hotkey", default=None, metavar="KEYS",
                    help="global shortcut that toggles the quick panel, e.g. alt+space (off by default; or SLOTHMORE_HOTKEY)")
    args = ap.parse_args(argv)
    if args.status:
        s = menubar_login_status()
        panel = {"on": "on", "starting": "was opening when the helper last stopped",
                 "off": "off after a crash (run: slothmore menubar --reset-panel)"}[s["panel"]]
        print("login item: %s (%s)\nrumps: %s\nquick panel (WebKit): %s\nquick panel: %s\nlog: %s" % (
            "installed" if s["installed"] else "not installed", s["plist"],
            "found" if s["rumps"] else "missing", "found" if s.get("webkit") else "missing (classic menu)",
            panel, s["log"]))
        return 0
    if args.reset_panel:
        reset_menubar_panel()
        print("Quick panel turned back on%s." % (" and the menu bar helper restarted" if menubar_plist_path().is_file() else ""))
        return 0
    if args.uninstall_login:
        uninstall_menubar_login()
        print("Menu bar login item removed.")
        return 0
    if not IS_MAC:
        sys.stderr.write("The menu bar icon only works on macOS.\n")
        return 2
    if not rumps_available():
        sys.stderr.write(RUMPS_HELP % sys.executable + "\n")
        return 2
    if args.install_login:
        # --classic / --hotkey given with --install-login are kept in the login item's environment.
        if args.classic:
            os.environ["SLOTHMORE_MENUBAR_CLASSIC"] = "1"
        if args.hotkey:
            os.environ["SLOTHMORE_HOTKEY"] = args.hotkey
        try:
            install_menubar_login()
        except ApiError as e:
            sys.stderr.write(str(e.payload.get("error")) + "\n")
            return 2
        print("Menu bar login item installed (%s). It starts at every login; log: %s" % (
            menubar_plist_path(), HOME_DIR / "menubar.log"))
        return 0
    ensure_private_dirs()
    import importlib.util
    spec = importlib.util.spec_from_file_location("slothmore_menubar", str(HERE / "macos" / "menubar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fwd = (["--url", args.url] if args.url else []) + (["--classic"] if args.classic else []) + \
        (["--hotkey", args.hotkey] if args.hotkey else [])
    return mod.main(fwd)


def cmd_open(argv, print_only=False):
    """`slothmore open` / `slothmore url`: sign in to an already running server (for example brew services)."""
    ap = argparse.ArgumentParser(prog="slothmore " + ("url" if print_only else "open"),
                                 description="Print the signed-in dashboard address." if print_only
                                 else "Open the running dashboard in your browser, signed in.")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    port = args.port or _state_port() or int(os.environ.get("SLOTHMORE_PORT") or DEFAULT_PORT)
    token = load_token(create=False)
    if not token:
        sys.exit("No token yet in %s. Start Slothmore first: `slothmore` or `brew services start slothmore`."
                 % TOKEN_PATH)
    state = _probe(port, token)
    if state is None:
        sys.exit("Nothing is answering on 127.0.0.1:%d. Start it with `slothmore` or `brew services start slothmore`."
                 % port)
    if state == "other":
        sys.exit("Port %d is used by another program (or a Slothmore with a different token). "
                 "Try `slothmore open --port <port>`." % port)
    url = dashboard_url(port, token)
    print(url)
    if not print_only:
        _open_url(url)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("open", "url"):
        return cmd_open(argv[1:], print_only=argv[0] == "url")
    if argv and argv[0] == "menubar":
        return cmd_menubar(argv[1:])
    ap = argparse.ArgumentParser(
        prog="slothmore",
        description="Slothmore: a local dashboard that starts web servers and runs scripts from folders.",
        epilog="Subcommands: `slothmore open` opens a running dashboard signed in; "
               "`slothmore url` prints its signed-in address; `slothmore menubar` shows the macOS menu bar icon.",
    )
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SLOTHMORE_PORT") or os.environ.get("LAUNCHER_PORT") or DEFAULT_PORT),
    )
    ap.add_argument("--watch", action="append", default=[], help="folder whose subfolders to list")
    ap.add_argument("--open", dest="open_browser", action="store_true", help="open the dashboard in a browser")
    ap.add_argument("--no-open", dest="open_browser", action="store_false", help="do not open a browser")
    ap.add_argument("--reset-token", action="store_true",
                    help="make a new access token (signs out browsers and installed web apps)")
    ap.add_argument("--log-level", default=None, choices=["debug", "info", "warning", "error", "critical"],
                    help="log level (or set SLOTHMORE_LOG_LEVEL). Default: info")
    ap.set_defaults(open_browser=None)
    args = ap.parse_args(argv)

    ensure_private_dirs()
    setup_logging(level=args.log_level)
    token = load_token(reset=args.reset_token)
    if args.reset_token:
        print("New access token written to %s." % TOKEN_PATH, flush=True)

    for w in args.watch:
        w = expand(w)
        if os.path.isdir(w):
            add_watch(w)
        else:
            print("Not a folder, skipping: %s" % w, file=sys.stderr)

    open_browser = args.open_browser
    if open_browser is None:
        open_browser = sys.stdout.isatty() and not os.environ.get("SLOTHMORE_SERVICE")

    try:
        server = make_server(args.port, token)
    except OSError as e:
        if _probe(args.port, token) == "ours" and not args.reset_token:
            url = dashboard_url(args.port, token)
            print("Slothmore is already running: %s" % url, flush=True)
            if open_browser:
                _open_url(url)
            return 0
        sys.exit("Could not listen on 127.0.0.1:%d (%s). Another program uses this port; "
                 "try `slothmore --port 7788`." % (args.port, e))
    port = server.server_address[1]
    try:
        private_write(STATE_PATH, json.dumps({"port": port, "pid": os.getpid(), "version": __version__}))
    except OSError:
        LOG.warning("could not write %s", STATE_PATH, exc_info=True)

    def on_term(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    recover_history()
    threading.Thread(target=_watch_loop, daemon=True).start()
    threading.Thread(target=_monitor_loop, daemon=True).start()

    url = dashboard_url(port, token)
    if open_browser:
        def _open_browser():
            time.sleep(0.4)
            _open_url(url)
        threading.Thread(target=_open_browser, daemon=True).start()

    LOG.info("Slothmore %s listening on 127.0.0.1:%d", __version__, port)
    if sys.stdout.isatty():
        print("Slothmore running at %s  (Ctrl+C to quit)" % url, flush=True)
    else:  # keep the token out of service logs
        print("Slothmore running at http://127.0.0.1:%d/  (run `slothmore open` to sign in)" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopped")
    finally:
        stop_all()
        server.server_close()
        try:
            if json.loads(STATE_PATH.read_text()).get("pid") == os.getpid():
                STATE_PATH.unlink()
        except (OSError, ValueError, AttributeError):
            LOG.debug("could not remove stale server.json", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
