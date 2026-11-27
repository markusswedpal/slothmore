"""Docker Compose projects: compose file parsing, detection and precedence, Docker missing / not running,
and start / stop / restart against a fake `docker` CLI (a shell script on PATH, so no Docker is needed)."""
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import LiveServer, make_tree, server

FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info)
    if [ -f "$FAKE_DOCKER_STATE/down" ]; then
      echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2
      exit 1
    fi
    echo 27.3.1; exit 0 ;;
  compose)
    shift
    case "$1" in
      version)
        [ -f "$FAKE_DOCKER_STATE/no-plugin" ] && { echo "docker: 'compose' is not a docker command." >&2; exit 1; }
        echo "Docker Compose version v2.29.7"; exit 0 ;;
      up)
        trap 'echo "Gracefully stopping... (press Ctrl+C again to force)"; echo stopped > up-stopped.flag; exit 130' INT
        echo "env COMPOSE_MENU=$COMPOSE_MENU"
        echo "Attaching to web-1"
        echo "web-1  | nginx listening on port 80"
        while :; do sleep 0.1; done ;;
      down)
        echo " Container demo-web-1  Removed"
        echo " Network demo_default  Removed"
        echo down > down.flag
        exit 0 ;;
    esac ;;
esac
exit 0
"""


def wait_until(pred, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class ComposeParsing(unittest.TestCase):
    def test_port_specs(self):
        cases = {
            "8080:80": (8080, 80, "tcp"),
            '"127.0.0.1:8081:80"': (8081, 80, "tcp"),
            "'[::1]:8082:80'": (8082, 80, "tcp"),
            "8000-8005:8000-8005": (8000, 8000, "tcp"),
            "5353:53/udp": (5353, 53, "udp"),
            "8443:443/tcp": (8443, 443, "tcp"),
            "80": None,                     # random host port
            "127.0.0.1::80": None,
            "nonsense": None,
        }
        for spec, want in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(server.parse_port_spec(spec), want)

    def test_port_spec_variables(self):
        self.assertEqual(server.parse_port_spec("${WEB_PORT:-3000}:3000", {}), (3000, 3000, "tcp"))
        self.assertEqual(server.parse_port_spec("${WEB_PORT:-3000}:3000", {"WEB_PORT": "4000"}), (4000, 3000, "tcp"))
        self.assertEqual(server.parse_port_spec("${WEB_PORT:-3000}:3000", {"WEB_PORT": ""}), (3000, 3000, "tcp"))
        self.assertEqual(server.parse_port_spec("$P:80", {"P": "9001"}), (9001, 80, "tcp"))
        self.assertIsNone(server.parse_port_spec("${UNSET_PORT}:80", {}))

    def test_compose_file(self):
        text = """
# comment
name: demo
x-common: &common
  restart: unless-stopped
services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"   # database
    environment:
      POSTGRES_PASSWORD: example
  web:
    <<: *common
    build: .
    ports:
    - "${WEB_PORT:-8080}:80"
    - 9229            # random host port: skipped
    - "53:53/udp"     # udp: skipped
  api:
    image: example/api
    ports: ["3001:3000", '127.0.0.1:3002:3000']
  admin:
    image: example/admin
    ports:
      - target: 80
        published: "8090"
        protocol: tcp
      - target: 81
        host_ip: 127.0.0.1
        published: 8091
  worker:
    image: example/worker
    command: ["python", "-m", "worker"]
volumes:
  data:
    ports_are_not_here: true
"""
        parsed = server.parse_compose(text, {"WEB_PORT": "8088"})
        self.assertEqual(parsed["services"], ["db", "web", "api", "admin", "worker"])
        self.assertEqual([(p["service"], p["port"]) for p in parsed["ports"]],
                         [("db", 5432), ("web", 8088), ("api", 3001), ("api", 3002), ("admin", 8090), ("admin", 8091)])
        self.assertEqual(server._primary_compose_port(parsed["ports"]), 8088)

    def test_primary_port_skips_databases(self):
        ports = [{"service": "db", "port": 5432, "target": 5432}, {"service": "cache", "port": 6379, "target": 6379}]
        self.assertIsNone(server._primary_compose_port(ports))
        ports.append({"service": "worker", "port": 9100, "target": 9100})
        self.assertEqual(server._primary_compose_port(ports), 9100)
        ports.append({"service": "frontend", "port": 5173, "target": 5173})
        self.assertEqual(server._primary_compose_port(ports), 5173)

    def test_garbage_does_not_raise(self):
        for text in ("", "services:", "services: {}", "\tservices:\n\t\tweb:\n", "- a\n- b", "services:\n  web:\n    ports:\n"):
            with self.subTest(text=text):
                parsed = server.parse_compose(text)
                self.assertEqual(parsed["ports"], [])


class ComposeDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="compose-detect-")).resolve()
        self.docker = mock.patch.object(server, "docker_exe", return_value="/usr/local/bin/docker")
        self.docker.start()

    def tearDown(self):
        self.docker.stop()

    def test_file_names(self):
        for name in server.COMPOSE_FILES:
            with self.subTest(name=name):
                d = make_tree(self.tmp / name.replace(".", "_"),
                              {name: "services:\n  web:\n    image: nginx\n    ports:\n      - 8080:80\n"})
                info = server.detect(d)
                self.assertEqual(info["kind"], "Compose")
                self.assertEqual(info["argv"], ["docker", "compose", "up", "--remove-orphans"])
                self.assertEqual(info["port"], 8080)
                self.assertTrue(info["web"])
                self.assertEqual(info["stop_signal"], "INT")
                self.assertIsNone(info["install"])
                self.assertFalse(info["deps_missing"])
                self.assertEqual(info["compose"]["file"], name)
                self.assertIsNone(info["missing_tool"])

    def test_compose_order_matches_docker(self):
        d = make_tree(self.tmp / "both", {"docker-compose.yml": "services:\n  a:\n    image: x\n",
                                          "compose.yaml": "services:\n  b:\n    image: x\n"})
        self.assertEqual(server.detect(d)["compose"]["file"], "compose.yaml")

    def test_db_only_has_no_open_button(self):
        d = make_tree(self.tmp / "db", {"compose.yml": "services:\n  db:\n    image: postgres\n    ports:\n      - 5432:5432\n"})
        info = server.detect(d)
        self.assertEqual(info["kind"], "Compose")
        self.assertFalse(info["web"])
        self.assertIsNone(info["port"])
        self.assertEqual(info["compose"]["ports"][0]["port"], 5432)

    def test_dotenv_port(self):
        d = make_tree(self.tmp / "dotenv", {"compose.yaml": "services:\n  web:\n    ports:\n      - ${APP_PORT:-8000}:8000\n",
                                            ".env": "# x\nAPP_PORT=8123\n"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_PORT", None)
            self.assertEqual(server.detect(d)["port"], 8123)

    def test_compose_beats_fallbacks(self):
        for extra in ({"run.sh": "echo hi\n"}, {"index.js": "console.log(1)\n"}, {"index.html": "<h1>x</h1>"},
                      {"a.py": "print(1)\n", "b.py": "print(2)\n"},
                      {"package.json": json.dumps({"scripts": {"lint": "eslint ."}})}):
            with self.subTest(extra=list(extra)):
                files = dict(extra)
                files["compose.yaml"] = "services:\n  web:\n    image: nginx\n"
                d = make_tree(self.tmp / ("fb-" + "-".join(extra).replace(".", "_")), files)
                info = server.detect(d)
                self.assertEqual(info["kind"], "Compose")
                self.assertIsNone(info["choices"])

    def test_dev_command_wins_and_compose_is_offered(self):
        files = {"package.json": json.dumps({"scripts": {"dev": "vite", "build": "vite build"}}),
                 "docker-compose.yml": "services:\n  db:\n    image: postgres\n    ports: ['5432:5432']\n  app:\n    build: .\n    ports: ['3000:3000']\n"}
        d = make_tree(self.tmp / "node-compose", files)
        nm = mock.patch.object(server, "_node_modules_present", return_value=False)
        nm.start()
        self.addCleanup(nm.stop)
        info = server.detect(d)
        self.assertEqual(info["kind"], "Node")
        self.assertEqual(info["choice"], "dev")
        self.assertTrue(info["deps_missing"])
        keys = [c["key"] for c in info["choices"]]
        self.assertEqual(keys[-1], server.COMPOSE_CHOICE)
        self.assertIn("dev", keys)

        info = server.detect(d, {"choice": server.COMPOSE_CHOICE})
        self.assertEqual(info["kind"], "Compose")
        self.assertEqual(info["choice"], server.COMPOSE_CHOICE)
        self.assertEqual(info["display_cmd"], "docker compose up")
        self.assertEqual(info["port"], 3000)
        self.assertFalse(info["deps_missing"])     # Install and run doesn't apply to compose
        self.assertIsNone(info["install"])
        self.assertIn("dev", [c["key"] for c in info["choices"]])    # can switch back

    def test_single_command_gets_a_picker(self):
        d = make_tree(self.tmp / "go-compose", {"go.mod": "module x\n", "compose.yaml": "services:\n  db:\n    image: redis\n"})
        info = server.detect(d)
        self.assertEqual(info["kind"], "Go")
        self.assertEqual(info["choice"], server.DEFAULT_CHOICE)
        self.assertEqual([c["key"] for c in info["choices"]], [server.DEFAULT_CHOICE, server.COMPOSE_CHOICE])
        self.assertEqual(info["choice_label"], "Run")
        back = server.detect(d, {"choice": server.DEFAULT_CHOICE})
        self.assertEqual(back["kind"], "Go")

    def test_override_wins_without_compose_choice(self):
        d = make_tree(self.tmp / "override", {"slothmore.json": json.dumps({"cmd": "echo hi"}),
                                              "compose.yaml": "services:\n  web:\n    image: nginx\n"})
        info = server.detect(d, {"choice": server.COMPOSE_CHOICE})
        self.assertEqual(info["cmd"], "echo hi")
        self.assertIsNone(info["choices"])

    def test_docker_missing(self):
        d = make_tree(self.tmp / "nodocker", {"compose.yaml": "services:\n  web:\n    image: nginx\n"})
        with mock.patch.object(server, "docker_exe", return_value=None), \
                mock.patch.object(server, "which", return_value=None):
            info = server.detect(d)
        self.assertEqual(info["kind"], "Compose")
        self.assertEqual(info["missing_tool"], "docker")
        self.assertIn("Docker Desktop, OrbStack or Colima", info["error"])
        self.assertFalse(server.public(dict(info, id="0123456789", source="pinned", root=None, path=str(d)))["startable"])

    def test_docker_exe_fallback_dirs(self):
        home = self.tmp / "home"
        b = make_tree(home / ".orbstack" / "bin", {"docker": "#!/bin/sh\n"})
        (b / "docker").chmod(0o755)
        self.docker.stop()
        try:
            with mock.patch.object(server, "which", return_value=None), \
                    mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(server.docker_exe(), str(b / "docker"))
        finally:
            self.docker.start()


class DockerCheck(unittest.TestCase):
    def setUp(self):
        server.DOCKER_STATUS.update(at=0.0, ok=None, prefix=None, dir=None, exe=None)

    def tearDown(self):
        server.DOCKER_STATUS.update(at=0.0, ok=None, prefix=None, dir=None, exe=None)

    def run_result(self, code, err=b""):
        return subprocess.CompletedProcess([], code, stdout=b"", stderr=err)

    def test_not_installed(self):
        with mock.patch.object(server, "docker_exe", return_value=None), mock.patch.object(server, "which", return_value=None):
            with self.assertRaises(server.ApiError) as cm:
                server.docker_check()
        self.assertEqual(cm.exception.payload["missing_tool"], "docker")
        self.assertIn("isn't installed", cm.exception.payload["error"])

    def test_daemon_down(self):
        err = b"Cannot connect to the Docker daemon at unix:///Users/m/.docker/run/docker.sock. Is the docker daemon running?"
        with mock.patch.object(server, "docker_exe", return_value="/usr/local/bin/docker"), \
                mock.patch.object(server, "which", return_value=None), \
                mock.patch.object(server.subprocess, "run", return_value=self.run_result(1, err)) as run:
            with self.assertRaises(server.ApiError) as cm:
                server.docker_check()
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(cm.exception.payload["error"], server.DOCKER_DOWN_MSG)
        self.assertTrue(cm.exception.payload["docker_down"])
        self.assertEqual(run.call_args[0][0][:2], ["/usr/local/bin/docker", "info"])
        self.assertFalse(server.DOCKER_STATUS["ok"])   # a failure is never cached

    def test_daemon_timeout(self):
        with mock.patch.object(server, "docker_exe", return_value="/usr/local/bin/docker"), \
                mock.patch.object(server, "which", return_value=None), \
                mock.patch.object(server.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            with self.assertRaises(server.ApiError) as cm:
                server.docker_check()
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("isn't responding", cm.exception.payload["error"])

    def test_ok_and_cached(self):
        with mock.patch.object(server, "docker_exe", return_value="/opt/homebrew/bin/docker"), \
                mock.patch.object(server, "which", return_value=None), \
                mock.patch.object(server.subprocess, "run", return_value=self.run_result(0)) as run:
            prefix, ddir = server.docker_check()
            self.assertEqual(prefix, ["/opt/homebrew/bin/docker", "compose"])
            self.assertEqual(ddir, "/opt/homebrew/bin")
            self.assertEqual(run.call_count, 2)          # docker info + docker compose version
            server.docker_check()
            self.assertEqual(run.call_count, 3)          # the daemon is asked again, the plugin lookup is cached
            self.assertEqual(run.call_args[0][0][1], "info")
        # a cached plugin lookup doesn't hide a daemon that stopped since
        with mock.patch.object(server, "docker_exe", return_value="/opt/homebrew/bin/docker"), \
                mock.patch.object(server, "which", return_value=None), \
                mock.patch.object(server.subprocess, "run", return_value=self.run_result(1, b"Cannot connect")):
            with self.assertRaises(server.ApiError) as cm:
                server.docker_check()
        self.assertEqual(cm.exception.payload["error"], server.DOCKER_DOWN_MSG)

    def test_plugin_missing_uses_docker_compose(self):
        def fake_run(argv, **kw):
            return self.run_result(1 if argv[1:2] == ["compose"] else 0)

        def fake_which(tool):
            return "/opt/homebrew/bin/docker-compose" if tool == "docker-compose" else None

        with mock.patch.object(server, "docker_exe", return_value="/opt/homebrew/bin/docker"), \
                mock.patch.object(server, "which", side_effect=fake_which), \
                mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            prefix, _ = server.docker_check()
        self.assertEqual(prefix, ["/opt/homebrew/bin/docker-compose"])
        self.assertEqual(server.compose_argv({"argv": ["docker", "compose", "up", "--remove-orphans"]}, prefix),
                         ["/opt/homebrew/bin/docker-compose", "up", "--remove-orphans"])
        self.assertEqual(server.compose_argv({}, prefix, "down"),
                         ["/opt/homebrew/bin/docker-compose", "down", "--remove-orphans"])

    def test_plugin_missing_no_fallback(self):
        def fake_run(argv, **kw):
            return self.run_result(1 if argv[1:2] == ["compose"] else 0)

        with mock.patch.object(server, "docker_exe", return_value="/usr/local/bin/docker"), \
                mock.patch.object(server, "which", return_value=None), \
                mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(server.ApiError) as cm:
                server.docker_check()
        self.assertIn("compose plugin is missing", cm.exception.payload["error"])


class ComposeRuns(unittest.TestCase):
    """Start / Stop / Restart through the real process code with a fake docker CLI first on PATH."""

    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()
        cls.tmp = Path(tempfile.mkdtemp(prefix="compose-run-")).resolve()
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        (cls.bin / "docker").write_text(FAKE_DOCKER)
        (cls.bin / "docker").chmod(0o755 | stat.S_IXUSR)
        cls.state = cls.tmp / "state"
        cls.state.mkdir()
        cls.log = cls.tmp / "docker-calls.log"
        cls.env = mock.patch.dict(os.environ, {"PATH": str(cls.bin) + os.pathsep + os.environ.get("PATH", ""),
                                               "FAKE_DOCKER_LOG": str(cls.log), "FAKE_DOCKER_STATE": str(cls.state)})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        server.stop_all()
        cls.env.stop()
        cls.srv.close()

    def setUp(self):
        server.DOCKER_STATUS.update(at=0.0, ok=None, prefix=None, dir=None, exe=None)
        for f in self.state.iterdir():
            f.unlink()
        if self.log.exists():
            self.log.unlink()

    def tearDown(self):
        server.stop_all()

    def pin(self, name, port, extra=None):
        files = {"compose.yaml": "services:\n  web:\n    image: nginx\n    ports:\n      - \"%d:80\"\n" % port}
        files.update(extra or {})
        d = make_tree(self.tmp / name, files)
        server.pin_path(str(d))
        server.invalidate_scan()
        iid = server.item_id(server.expand(str(d)))
        return d, iid

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def text(self, run):
        with run.cond:
            return "".join(run.chunks)

    def test_start_and_stop_runs_down(self):
        d, iid = self.pin("app", 18765)
        it = server.find_item(iid)
        self.assertEqual(it["kind"], "Compose")
        run = server.start_run(it)
        self.assertTrue(wait_until(lambda: "Attaching to web-1" in self.text(run)))
        self.assertIn(str(self.bin / "docker") + " compose up --remove-orphans", run.cmd)
        self.assertIn("COMPOSE_MENU=false", self.text(run))
        pub = server.public(server.find_item(iid))
        self.assertTrue(pub["running"])
        self.assertEqual(pub["port"], 18765)                    # from the compose file, not the log's ":80"
        self.assertEqual(pub["url"], "http://localhost:18765")
        self.assertEqual(pub["compose"]["ports"][0]["port"], 18765)

        server.stop_run(run)
        self.assertTrue(wait_until(lambda: not run.alive, timeout=15))
        self.assertTrue((d / "up-stopped.flag").exists(), "compose up did not get Ctrl+C")
        self.assertTrue((d / "down.flag").exists(), "docker compose down did not run")
        out = self.text(run)
        self.assertIn("[stopping containers: docker compose down --remove-orphans]", out)
        self.assertIn("Network demo_default  Removed", out)
        self.assertLess(out.index("Removed"), out.index("[stopped]"))
        self.assertIn("compose down --remove-orphans", self.calls()[-1])

    def test_restart_does_not_run_down(self):
        d, iid = self.pin("restart", 18766)
        run = server.start_run(server.find_item(iid))
        self.assertTrue(wait_until(lambda: "Attaching" in self.text(run)))
        new = server.restart_run(server.find_item(iid))
        self.assertIsNot(new, run)
        self.assertFalse(run.alive)
        self.assertTrue((d / "up-stopped.flag").exists())
        self.assertFalse((d / "down.flag").exists())
        self.assertTrue(new.alive)

    def test_process_exit_without_stop_skips_down(self):
        d, iid = self.pin("crash", 18767)
        run = server.start_run(server.find_item(iid))
        self.assertTrue(wait_until(lambda: "Attaching" in self.text(run)))
        os.killpg(run.proc.pid, 9)
        self.assertTrue(wait_until(lambda: not run.alive, timeout=10))
        self.assertFalse((d / "down.flag").exists())

    def test_docker_not_running_api_message(self):
        (self.state / "down").write_text("")
        d, iid = self.pin("down", 18768)
        code, _, body = self.srv.request("/api/items/%s/start" % iid, "POST", body={})
        self.assertEqual(code, 503)
        data = json.loads(body)
        self.assertEqual(data["error"], server.DOCKER_DOWN_MSG)
        self.assertTrue(data.get("docker_down"))
        self.assertFalse(server.RUNS.get(iid) and server.RUNS[iid].alive)
        self.assertFalse(any("compose up" in c for c in self.calls()))

    def test_daemon_gone_mid_run_hint(self):
        d, iid = self.pin("midrun", 18771)
        run = server.start_run(server.find_item(iid))
        self.assertTrue(wait_until(lambda: "Attaching" in self.text(run)))
        run.append("Error response from daemon: Cannot connect to the Docker daemon at unix:///x.sock\n")
        os.killpg(run.proc.pid, 9)
        self.assertTrue(wait_until(lambda: not run.alive, timeout=10))
        self.assertIn(server.DOCKER_DOWN_MSG, self.text(run))

    def test_install_run_not_applicable(self):
        _, iid = self.pin("noinstall", 18769, {"package.json": json.dumps({"scripts": {"lint": "x"}, "dependencies": {"a": "1"}})})
        it = server.find_item(iid)
        self.assertEqual(it["kind"], "Compose")
        self.assertFalse(server.public(it)["deps_missing"])
        code, _, body = self.srv.request("/api/items/%s/install_run" % iid, "POST", body={})
        self.assertEqual(code, 400)
        self.assertIn("Nothing to install", json.loads(body)["error"])

    def test_choose_compose_through_prefs(self):
        _, iid = self.pin("choose", 18770, {"go.mod": "module x\n"})
        self.assertEqual(server.find_item(iid)["kind"], "Go")
        code, _, body = self.srv.request("/api/items/%s/prefs" % iid, "POST", body={"choice": server.COMPOSE_CHOICE})
        self.assertEqual(code, 200, body)
        server.invalidate_scan()
        it = server.find_item(iid)
        self.assertEqual(it["kind"], "Compose")
        self.assertEqual(it["port"], 18770)

    def test_port_taken_hint(self):
        import socket
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            _, iid = self.pin("taken", port)
            with self.assertRaises(server.ApiError) as cm:
                server.start_run(server.find_item(iid))
            self.assertEqual(cm.exception.payload["error"], "port_in_use")
            self.assertEqual(cm.exception.payload["port"], port)
            self.assertIn("docker compose down", cm.exception.payload["hint"])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
