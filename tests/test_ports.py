"""Ports view: parse lsof/ss listings, auth, and the identity-checked stop."""
import json
import os
import signal
import unittest
from unittest import mock

from support import LiveServer, server


LSOF = """\
p4242
cnode
umarkus
n127.0.0.1:5173
n[::1]:5173
p99
cpython3
umarkus
n*:7777
p7
csshd
uroot
n*:22
"""

SS = """\
LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=4242,fd=23))
LISTEN 0 128 *:7777 *:* users:(("python3",pid=99,fd=3))
LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=7,fd=5))
LISTEN 0 32 [::1]:5900 [::]:* users:(("x11vnc",pid=1454,fd=9))
"""


class ParseListenTests(unittest.TestCase):
    def test_parse_lsof(self):
        recs = server.parse_lsof_listen(LSOF)
        by = {(r["port"], r["pid"], r["address"]) for r in recs}
        self.assertIn((5173, 4242, "127.0.0.1"), by)
        self.assertIn((5173, 4242, "::1"), by)
        self.assertIn((7777, 99, "*"), by)
        self.assertIn((22, 7, "*"), by)
        node = next(r for r in recs if r["pid"] == 4242 and r["address"] == "127.0.0.1")
        self.assertEqual(node["command"], "node")
        self.assertEqual(node["user"], "markus")

    def test_parse_ss(self):
        recs = server.parse_ss_listen(SS)
        by = {(r["port"], r["pid"], r["address"]) for r in recs}
        self.assertIn((5173, 4242, "127.0.0.1"), by)
        self.assertIn((7777, 99, "*"), by)
        self.assertIn((22, 7, "*"), by)
        self.assertIn((5900, 1454, "::1"), by)
        node = next(r for r in recs if r["pid"] == 4242)
        self.assertEqual(node["command"], "node")

    def test_ss_normalizes_wildcard(self):
        recs = server.parse_ss_listen("LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=7,fd=5))")
        self.assertEqual(recs[0]["address"], "*")


class ListPortsTests(unittest.TestCase):
    def test_list_uses_mocked_lsof_and_marks_own_port(self):
        recs = server.parse_lsof_listen(LSOF)
        ident = {"pid": 4242, "command": "node", "started_at": 100.0}
        with mock.patch("server._collect_listen_records", return_value=recs), \
             mock.patch("server._cwds", return_value={4242: "/tmp/web"}), \
             mock.patch("server._pid_uid", side_effect=lambda pid: os.getuid() if pid != 7 else 0), \
             mock.patch("server._pid_user", side_effect=lambda uid, fallback=None: fallback or "me"), \
             mock.patch("server.process_identity", return_value=ident), \
             mock.patch("server.load_config", return_value={"watch": ["/tmp"], "pinned": []}):
            out = server.list_listening_ports(own_port=7777)
        by_port = {r["port"]: r for r in out if r["pid"] != 7 or r["port"] == 22}
        node = next(r for r in out if r["port"] == 5173)
        self.assertEqual(node["command"], "node")
        self.assertEqual(node["pid"], 4242)
        self.assertTrue(node["http"])
        self.assertEqual(node["url"], "http://127.0.0.1:5173")
        self.assertTrue(node["can_stop"])
        self.assertFalse(node["ours"])
        self.assertTrue(node["folder"].endswith("web"))
        ours = next(r for r in out if r["port"] == 7777)
        self.assertTrue(ours["ours"])
        self.assertFalse(ours["can_stop"])
        ssh = next(r for r in out if r["port"] == 22)
        self.assertFalse(ssh["can_stop"])
        self.assertFalse(ssh["owned"])

    def test_collect_never_uses_a_shell(self):
        runs = []

        def fake_run(argv, **kwargs):
            runs.append((argv, kwargs.get("shell")))
            class R:
                stdout = LSOF.encode()
            return R()

        with mock.patch("server.shutil.which", side_effect=lambda n: "/usr/bin/lsof" if n == "lsof" else None), \
             mock.patch("os.path.exists", return_value=False), \
             mock.patch("server.subprocess.run", side_effect=fake_run):
            recs = server._collect_listen_records()
        self.assertTrue(recs)
        self.assertEqual(runs[0][0][:4], ["/usr/bin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
        self.assertFalse(runs[0][1])


class StopPortTests(unittest.TestCase):
    def ident(self, pid=4242, cmd="node", started=100.0):
        return {"pid": pid, "command": cmd, "started_at": started}

    def test_refuses_without_confirm(self):
        with self.assertRaises(server.ApiError) as ctx:
            server.stop_listening_port(5173, 4242, False, own_port=7777)
        self.assertEqual(ctx.exception.status, 409)

    def test_refuses_own_dashboard_port(self):
        with self.assertRaises(server.ApiError) as ctx:
            server.stop_listening_port(7777, os.getpid(), True, own_port=7777)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("dashboard", ctx.exception.payload["error"].lower())

    def test_refuses_other_users_process(self):
        with mock.patch("server._pid_uid", return_value=os.getuid() + 1):
            with self.assertRaises(server.ApiError) as ctx:
                server.stop_listening_port(22, 7, True, own_port=7777)
        self.assertEqual(ctx.exception.status, 403)

    def test_refuses_when_identity_changed(self):
        server.PORTS_IDENTITY[(5173, 4242)] = self.ident(started=100.0)
        with mock.patch("server._pid_uid", return_value=os.getuid()), \
             mock.patch("server.fresh_port_identity", return_value=self.ident(started=999.0)):
            with self.assertRaises(server.ApiError) as ctx:
                server.stop_listening_port(5173, 4242, True, own_port=7777)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("changed", ctx.exception.payload["error"].lower())

    def test_refuses_when_pid_no_longer_owns_the_port(self):
        with mock.patch("server._pid_uid", return_value=os.getuid()), \
             mock.patch("server.fresh_port_identity", return_value=self.ident(pid=9999)):
            with self.assertRaises(server.ApiError) as ctx:
                server.stop_listening_port(5173, 4242, True, own_port=7777)
        self.assertEqual(ctx.exception.status, 409)

    def test_signals_after_confirm_when_identity_matches(self):
        ident = self.ident()
        server.PORTS_IDENTITY[(5173, 4242)] = ident
        with mock.patch("server._pid_uid", return_value=os.getuid()), \
             mock.patch("server.fresh_port_identity", return_value=ident), \
             mock.patch("server.process_identity", return_value=ident), \
             mock.patch("server._run_for_pid_or_port", return_value=None), \
             mock.patch("server.os.kill") as kill:
            out = server.stop_listening_port(5173, 4242, True, own_port=7777)
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertEqual(out["stopped"], "signal")
        self.assertEqual(out["pid"], 4242)


class PortsApiTests(unittest.TestCase):
    def test_ports_requires_auth(self):
        srv = LiveServer()
        self.addCleanup(srv.close)
        status, _h, _b = srv.request("/api/ports", auth=None)
        self.assertEqual(status, 401)

    def test_ports_lists_mocked_records(self):
        srv = LiveServer()
        self.addCleanup(srv.close)
        recs = server.parse_lsof_listen(LSOF)
        ident = {"pid": 4242, "command": "node", "started_at": 1.0}
        with mock.patch("server._collect_listen_records", return_value=recs), \
             mock.patch("server._cwds", return_value={}), \
             mock.patch("server._pid_uid", return_value=os.getuid()), \
             mock.patch("server.process_identity", return_value=ident):
            status, _h, body = srv.request("/api/ports")
        self.assertEqual(status, 200)
        data = json.loads(body)
        ports = {p["port"] for p in data["ports"]}
        self.assertIn(5173, ports)
        self.assertIn(7777, ports)
        marked = [p for p in data["ports"] if p["ours"]]
        self.assertTrue(all(p["port"] == srv.port for p in marked))
        if srv.port not in ports:
            self.assertEqual(marked, [])

    def test_stop_requires_auth_and_confirm(self):
        srv = LiveServer()
        self.addCleanup(srv.close)
        status, _h, _b = srv.request("/api/ports/stop", method="POST", body={"port": 5173, "pid": 4242, "confirm": True}, auth=None)
        self.assertEqual(status, 401)
        status, _h, body = srv.request("/api/ports/stop", method="POST", body={"port": 5173, "pid": 4242})
        self.assertEqual(status, 409)
        self.assertIn("confirm", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
