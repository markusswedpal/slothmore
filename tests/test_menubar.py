"""`slothmore menubar`: missing rumps message, login item plist and launchctl calls (mocked), API, and the helper's pure parts."""
import importlib.util
import io
import json
import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import LiveServer, server


def load_menubar_module():
    spec = importlib.util.spec_from_file_location("menubar_under_test", str(server.HERE / "macos" / "menubar.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MenubarCliTests(unittest.TestCase):
    def setUp(self):
        self.agents = Path(tempfile.mkdtemp(prefix="agents-")).resolve()
        env = mock.patch.dict(os.environ, {"LAUNCH_AGENTS_DIR": str(self.agents)})
        env.start()
        self.addCleanup(env.stop)
        self.mac = server.IS_MAC
        self.addCleanup(lambda: setattr(server, "IS_MAC", self.mac))
        server.IS_MAC = True
        self.ctl = mock.patch("server.subprocess.run").start()
        self.ctl.return_value = mock.Mock(returncode=0, stderr=b"", stdout=b"")
        self.addCleanup(mock.patch.stopall)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = server.cmd_menubar(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_missing_rumps_prints_fix_not_traceback(self):
        with mock.patch("server.rumps_available", return_value=False):
            rc, out, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("rumps", err)
        self.assertIn("brew reinstall", err)
        self.assertNotIn("Traceback", err)

    def test_install_login_missing_rumps_writes_nothing(self):
        with mock.patch("server.rumps_available", return_value=False):
            rc, out, err = self.run_cli("--install-login")
        self.assertEqual(rc, 2)
        self.assertFalse((self.agents / "com.slothmore.menubar.plist").exists())
        self.assertFalse(self.ctl.called)

    def test_not_macos(self):
        server.IS_MAC = False
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("macOS", err)

    def test_install_login_writes_plist_and_calls_launchctl(self):
        with mock.patch("server.rumps_available", return_value=True), \
             mock.patch.dict(os.environ, {"SLOTHMORE_BIN": ""}):
            rc, out, err = self.run_cli("--install-login")
        self.assertEqual(rc, 0, err)
        plist = self.agents / "com.slothmore.menubar.plist"
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], "com.slothmore.menubar")
        self.assertEqual(data["ProgramArguments"][-1], "menubar")
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(data["StandardOutPath"].endswith("menubar.log"))
        self.assertEqual(data["StandardOutPath"], data["StandardErrorPath"])
        calls = [c.args[0] for c in self.ctl.call_args_list]
        uid = str(os.getuid())
        self.assertEqual(calls[0], ["launchctl", "bootout", "gui/%s/com.slothmore.menubar" % uid])
        self.assertEqual(calls[1], ["launchctl", "bootstrap", "gui/%s" % uid, str(plist)])
        for c in self.ctl.call_args_list:
            self.assertIsInstance(c.args[0], list)
            self.assertFalse(c.kwargs.get("shell", False))

    def test_login_item_uses_upgrade_proof_wrapper_path(self):
        wrapper = self.agents / "slothmore"
        wrapper.write_text("#!/bin/sh\n")
        with mock.patch.dict(os.environ, {"SLOTHMORE_BIN": str(wrapper)}):
            data = server.menubar_plist_data()
        self.assertEqual(data["ProgramArguments"], [str(wrapper), "menubar"])
        self.assertEqual(data["EnvironmentVariables"]["SLOTHMORE_BIN"], str(wrapper))

    def test_uninstall_login_removes_plist_and_unloads(self):
        plist = self.agents / "com.slothmore.menubar.plist"
        plist.write_bytes(b"x")
        rc, out, err = self.run_cli("--uninstall-login")
        self.assertEqual(rc, 0)
        self.assertFalse(plist.exists())
        self.assertEqual(self.ctl.call_args_list[0].args[0][:2], ["launchctl", "bootout"])
        # a second uninstall is fine
        rc, _o, _e = self.run_cli("--uninstall-login")
        self.assertEqual(rc, 0)

    def test_launchctl_failure_is_reported(self):
        self.ctl.return_value = mock.Mock(returncode=5, stderr=b"nope", stdout=b"")
        with mock.patch("server.rumps_available", return_value=True):
            rc, out, err = self.run_cli("--install-login")
        self.assertEqual(rc, 2)
        self.assertIn("launchctl", err)

    def test_status(self):
        with mock.patch("server.rumps_available", return_value=False):
            rc, out, err = self.run_cli("--status")
        self.assertEqual(rc, 0)
        self.assertIn("login item: not installed", out)
        self.assertIn("rumps: missing", out)

    def test_main_dispatches_menubar(self):
        with mock.patch("server.cmd_menubar", return_value=7) as m:
            self.assertEqual(server.main(["menubar", "--status"]), 7)
        m.assert_called_once_with(["--status"])


class MenubarApiTests(unittest.TestCase):
    def setUp(self):
        self.agents = Path(tempfile.mkdtemp(prefix="agents-")).resolve()
        env = mock.patch.dict(os.environ, {"LAUNCH_AGENTS_DIR": str(self.agents), "SLOTHMORE_DRY_RUN": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.mac = server.IS_MAC
        self.addCleanup(lambda: setattr(server, "IS_MAC", self.mac))
        self.srv = LiveServer()
        self.addCleanup(self.srv.close)

    def test_requires_auth(self):
        self.assertEqual(self.srv.request("/api/menubar", auth=None)[0], 401)
        self.assertEqual(self.srv.request("/api/menubar", method="POST", body={"enabled": True}, auth=None)[0], 401)

    def test_status_and_toggle_on_mac(self):
        server.IS_MAC = True
        with mock.patch("server.rumps_available", return_value=True):
            st, _h, body = self.srv.request("/api/menubar")
            self.assertEqual(json.loads(body)["installed"], False)
            self.assertTrue(json.loads(body)["supported"])
            st, _h, body = self.srv.request("/api/menubar", method="POST", body={"enabled": True})
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(body)["installed"])
            self.assertTrue((self.agents / "com.slothmore.menubar.plist").exists())
            st, _h, body = self.srv.request("/api/menubar", method="POST", body={"enabled": False})
            self.assertFalse(json.loads(body)["installed"])

    def test_not_supported_off_mac(self):
        server.IS_MAC = False
        st, _h, body = self.srv.request("/api/menubar")
        self.assertFalse(json.loads(body)["supported"])
        st, _h, body = self.srv.request("/api/menubar", method="POST", body={"enabled": True})
        self.assertEqual(st, 409)

    def test_enabled_must_be_boolean(self):
        st, _h, _b = self.srv.request("/api/menubar", method="POST", body={"enabled": "yes"})
        self.assertEqual(st, 400)

    def test_missing_rumps_is_a_409_with_the_fix(self):
        server.IS_MAC = True
        with mock.patch("server.rumps_available", return_value=False):
            st, _h, body = self.srv.request("/api/menubar", method="POST", body={"enabled": True})
        self.assertEqual(st, 409)
        self.assertIn("rumps", json.loads(body)["error"])


class MenubarHelperTests(unittest.TestCase):
    """macos/menubar.py without rumps: port discovery, offline model, start command."""

    def setUp(self):
        self.mod = load_menubar_module()
        self.home = Path(tempfile.mkdtemp(prefix="mbhome-")).resolve()
        env = mock.patch.dict(os.environ, {"SLOTHMORE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_port_comes_from_server_json(self):
        (self.home / "server.json").write_text(json.dumps({"port": 7790, "pid": 1}))
        self.assertEqual(self.mod.read_port(), 7790)
        self.assertEqual(self.mod.Client().base, "http://127.0.0.1:7790")
        self.assertEqual(self.mod.Client("http://127.0.0.1:9999/").base, "http://127.0.0.1:9999")

    def test_default_when_no_server_json(self):
        self.assertIsNone(self.mod.read_port())
        self.assertTrue(self.mod.Client().base.startswith("http://127.0.0.1:"))

    def test_token_file_is_used_for_auth(self):
        (self.home / "token").write_text("abc" * 12 + "\n")
        seen = {}

        def fake_urlopen(req, timeout=0):
            seen["auth"] = req.get_header("Authorization")
            return io.BytesIO(b'{"items": []}')

        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=lambda r, timeout=0: _Ctx(fake_urlopen(r))):
            self.mod.Client("http://127.0.0.1:1").listing()
        self.assertEqual(seen["auth"], "Bearer " + "abc" * 12)

    def test_offline_model_and_start_command(self):
        m = self.mod.build_model(None)
        self.assertFalse(m["online"])
        with mock.patch.dict(os.environ, {"SLOTHMORE_BIN": ""}):
            cmd = self.mod.server_command()
        self.assertEqual(cmd[-1], "--no-open")
        self.assertTrue(cmd[-2].endswith(os.path.join("bin", "slothmore")))

    def test_start_server_uses_argv_without_shell(self):
        with mock.patch.object(self.mod.subprocess, "Popen") as popen:
            self.mod.start_server()
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs.get("start_new_session"))

    def test_main_without_rumps_prints_fix(self):
        err = io.StringIO()
        with mock.patch.dict("sys.modules", {"rumps": None}), redirect_stderr(err):
            rc = self.mod.main([])
        self.assertEqual(rc, 2)
        self.assertIn("rumps", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class _Ctx:
    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
