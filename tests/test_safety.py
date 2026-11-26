"""Security follow-ups: README Open never launches a binary, and AI server stop re-checks the process."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_tree, server


def item_for(folder):
    return {"path": str(folder)}


class ReadmeOpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="readme-open-")).resolve()
        self.mac = server.IS_MAC
        self.popen = mock.patch("server.subprocess.Popen").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(lambda: setattr(server, "IS_MAC", self.mac))

    def _popen_argv(self):
        self.assertTrue(self.popen.called)
        args, kwargs = self.popen.call_args
        argv = args[0]
        self.assertIsInstance(argv, list)
        self.assertFalse(kwargs.get("shell", False))
        return argv

    def test_macos_open_uses_dash_t(self):
        server.IS_MAC = True
        d = make_tree(self.tmp / "md", {"README.md": "# hi\n"})
        with mock.patch("server.shutil.which", return_value="/usr/bin/open"):
            out = server.open_readme(item_for(d), "open")
        self.assertEqual(self._popen_argv(), ["/usr/bin/open", "-t", str(d / "README.md")])
        self.assertEqual(out["mode"], "open")

    def test_macos_open_uses_dash_t_for_extensionless_readme(self):
        server.IS_MAC = True
        d = make_tree(self.tmp / "bare", {"README": "#!/bin/sh\necho pwned\n"})
        os.chmod(d / "README", 0o755)
        with mock.patch("server.shutil.which", return_value="/usr/bin/open"):
            server.open_readme(item_for(d), "open")
        argv = self._popen_argv()
        self.assertEqual(argv[1], "-t")
        self.assertNotEqual(argv, ["/usr/bin/open", str(d / "README")])

    def test_macos_reveal_uses_dash_r(self):
        server.IS_MAC = True
        d = make_tree(self.tmp / "rev", {"README.md": "# hi\n"})
        with mock.patch("server.shutil.which", return_value="/usr/bin/open"):
            server.open_readme(item_for(d), "reveal")
        self.assertEqual(self._popen_argv(), ["/usr/bin/open", "-R", str(d / "README.md")])

    def test_linux_open_md_uses_xdg_open(self):
        server.IS_MAC = False
        d = make_tree(self.tmp / "linux-md", {"README.md": "# hi\n"})
        with mock.patch("server.shutil.which", return_value="/usr/bin/xdg-open"):
            server.open_readme(item_for(d), "open")
        self.assertEqual(self._popen_argv(), ["/usr/bin/xdg-open", str(d / "README.md")])

    def test_linux_refuses_extensionless_executable_readme(self):
        server.IS_MAC = False
        d = make_tree(self.tmp / "linux-bare", {"README": "#!/bin/sh\necho pwned\n"})
        os.chmod(d / "README", 0o755)
        with mock.patch("server.shutil.which", return_value="/usr/bin/xdg-open"):
            with self.assertRaises(server.ApiError) as ctx:
                server.open_readme(item_for(d), "open")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("text extension", ctx.exception.payload["error"])
        self.assertFalse(self.popen.called)

    def test_linux_reveal_opens_folder(self):
        server.IS_MAC = False
        d = make_tree(self.tmp / "linux-rev", {"README": "hello\n"})
        with mock.patch("server.shutil.which", return_value="/usr/bin/xdg-open"):
            server.open_readme(item_for(d), "reveal")
        self.assertEqual(self._popen_argv(), ["/usr/bin/xdg-open", str(d)])

    def test_linux_without_xdg_open(self):
        server.IS_MAC = False
        d = make_tree(self.tmp / "no-xdg", {"README.md": "# hi\n"})
        with mock.patch("server.shutil.which", return_value=None):
            with self.assertRaises(server.ApiError) as ctx:
                server.open_readme(item_for(d), "open")
        self.assertEqual(ctx.exception.status, 501)
        self.assertFalse(self.popen.called)

    def test_can_open_flags(self):
        server.IS_MAC = False
        with mock.patch("server.shutil.which", return_value="/usr/bin/xdg-open"):
            self.assertTrue(server._can_open_readme("README.md"))
            self.assertFalse(server._can_open_readme("README"))
            self.assertTrue(server._can_reveal_readme())
        server.IS_MAC = True
        with mock.patch("server.shutil.which", return_value="/usr/bin/open"):
            self.assertTrue(server._can_open_readme("README"))


PREV = {"pid": 4242, "command": "ollama", "started_at": 1_700_000_000.0}


class AiServerStopTests(unittest.TestCase):
    def setUp(self):
        self.port = 11434
        self.key = "ollama"
        server.AI_PORT_STATE[self.port] = {
            "open": True,
            "result": {"key": "ollama"},
            "at": 0,
            "identity": dict(PREV),
        }
        self.addCleanup(server.AI_PORT_STATE.pop, self.port, None)
        self.addCleanup(server.AI_OWNER_CACHE.pop, self.port, None)

    def _stop(self, ident=PREV, probe={"key": "ollama"}, confirm=True):
        with mock.patch("server._probe_port", return_value=probe) as p, \
             mock.patch("server.fresh_port_identity", return_value=ident) as f, \
             mock.patch("server.os.kill") as kill, \
             mock.patch("server._ai_cli", return_value=None):
            try:
                result = server.ai_server_stop(self.key, self.port, confirm)
                return result, kill, p, f
            except server.ApiError as e:
                return e, kill, p, f

    def test_same_process_is_signalled(self):
        result, kill, probe, _ = self._stop()
        self.assertEqual(result["stopped"], "signal")
        self.assertEqual(result["pid"], PREV["pid"])
        kill.assert_called_once_with(PREV["pid"], server.signal.SIGTERM)
        probe.assert_called()  # live re-probe, not the cached AI_PORT_STATE result alone

    def test_refuses_without_confirm(self):
        err, kill, *_ = self._stop(confirm=False)
        self.assertEqual(err.status, 409)
        self.assertEqual(err.payload["error"], "confirm_required")
        self.assertFalse(kill.called)

    def test_pid_changed(self):
        err, kill, *_ = self._stop(ident={"pid": 9999, "command": "ollama", "started_at": PREV["started_at"]})
        self.assertEqual(err.status, 409)
        self.assertIn("has changed", err.payload["error"])
        self.assertFalse(kill.called)

    def test_start_time_changed_pid_reuse(self):
        err, kill, *_ = self._stop(ident={"pid": 4242, "command": "ollama", "started_at": PREV["started_at"] + 30})
        self.assertEqual(err.status, 409)
        self.assertIn("has changed", err.payload["error"])
        self.assertFalse(kill.called)

    def test_command_changed(self):
        err, kill, *_ = self._stop(ident={"pid": 4242, "command": "python3", "started_at": PREV["started_at"]})
        self.assertEqual(err.status, 409)
        self.assertFalse(kill.called)

    def test_probe_no_longer_matches(self):
        err, kill, *_ = self._stop(probe=None)
        self.assertEqual(err.status, 409)
        self.assertIn("no longer running", err.payload["error"])
        self.assertFalse(kill.called)

    def test_probe_different_server(self):
        err, kill, *_ = self._stop(probe={"key": "vllm"})
        self.assertEqual(err.status, 409)
        self.assertFalse(kill.called)

    def test_foreign_container_not_signalled(self):
        err, kill, *_ = self._stop(ident={"pid": 4242, "command": "com.docker.backend", "started_at": PREV["started_at"]})
        # command change already refuses; also cover same-pid docker with no prev command mismatch
        self.assertEqual(err.status, 409)
        self.assertFalse(kill.called)

    def test_foreign_guard_when_no_previous_identity(self):
        server.AI_PORT_STATE[self.port]["identity"] = None
        err, kill, *_ = self._stop(ident={"pid": 77, "command": "docker-proxy", "started_at": 1.0})
        self.assertEqual(err.status, 409)
        self.assertIn("docker", err.payload["error"].lower())
        self.assertFalse(kill.called)

    def test_missing_listener(self):
        err, kill, *_ = self._stop(ident=None)
        self.assertEqual(err.status, 409)
        self.assertIn("Couldn't find the process", err.payload["error"])
        self.assertFalse(kill.called)

    def test_identities_match_helper(self):
        self.assertTrue(server._identities_match(PREV, dict(PREV)))
        self.assertTrue(server._identities_match(PREV, {"pid": 4242, "command": "/usr/bin/ollama",
                                                        "started_at": PREV["started_at"] + 0.2}))
        self.assertFalse(server._identities_match(PREV, {"pid": 1, "command": "ollama",
                                                         "started_at": PREV["started_at"]}))
        self.assertFalse(server._identities_match(None, PREV))

    def test_cli_stop_also_reprobes(self):
        server.AI_PORT_STATE[1234] = {"identity": {"pid": 8, "command": "LM Studio", "started_at": 10.0}}
        ident = {"pid": 8, "command": "LM Studio", "started_at": 10.0}
        with mock.patch("server._probe_port", return_value={"key": "lmstudio"}) as p, \
             mock.patch("server.fresh_port_identity", return_value=ident), \
             mock.patch("server._ai_cli", return_value="/usr/bin/lms"), \
             mock.patch("server.os.kill") as kill, \
             mock.patch("server.subprocess.run", return_value=mock.Mock(stdout=b"ok")) as run:
            result = server.ai_server_stop("lmstudio", 1234, True)
        self.assertEqual(result["stopped"], "cli")
        self.assertFalse(kill.called)
        p.assert_called()
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["/usr/bin/lms", "server"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        server.AI_PORT_STATE.pop(1234, None)


if __name__ == "__main__":
    unittest.main()
