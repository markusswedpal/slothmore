"""Install and run: successful install then start, failed install stays stopped, missing tool is rejected."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import LiveServer, make_tree, server


def wait_until(pred, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class InstallRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = LiveServer()
        cls.tmp = Path(tempfile.mkdtemp(prefix="install-run-")).resolve()

    @classmethod
    def tearDownClass(cls):
        server.stop_all()
        cls.srv.close()

    def tearDown(self):
        server.stop_all()
        with server.INSTALL_RUN_LOCK:
            server.INSTALL_RUN_PENDING.clear()

    def pin_project(self, name, run_py=None):
        if run_py is None:
            run_py = (
                "import pathlib, sys, time\n"
                "pathlib.Path('started.flag').write_text(' '.join(sys.argv[1:]))\n"
                "time.sleep(60)\n"
            )
        d = make_tree(self.tmp / name, {"main.py": run_py, "requirements.txt": "demo\n"})
        server.pin_path(str(d))
        server.invalidate_scan()
        iid = server.item_id(server.expand(str(d)))
        return d, iid

    def patch_install(self, folder, script, tool=None):
        folder = Path(folder).resolve()
        original = server._python_install

        def fake(path):
            path = Path(path)
            if path.resolve() != folder:
                return original(path)
            return {
                "argv": ["/bin/sh", "-c", script],
                "display": "fake install",
                "tool": tool,
                "cwd": str(path),
            }

        return mock.patch("server._python_install", side_effect=fake)

    def item(self, iid):
        code, _, body = self.srv.request("/api/items")
        self.assertEqual(code, 200, body)
        found = [i for i in json.loads(body)["items"] if i["id"] == iid]
        self.assertTrue(found, "item %s not in listing" % iid)
        return found[0]

    def test_success_chains_to_start(self):
        d, iid = self.pin_project("okproj")
        script = "echo INSTALLED; echo ok > install.flag"
        with self.patch_install(d, script):
            listing = self.item(iid)
            self.assertTrue(listing["deps_missing"])
            self.assertEqual(listing["install_cmd"], "fake install")
            self.assertFalse(listing["install_run_pending"])
            code, _, body = self.srv.request(
                "/api/items/%s/install_run" % iid, "POST", body={"args": "--from-test"}
            )
            self.assertEqual(code, 200, body)
            data = json.loads(body)
            self.assertTrue(data.get("running") or data.get("install_run_pending"))
            self.assertTrue(
                wait_until(lambda: (d / "started.flag").exists(), timeout=10),
                "project did not start after a successful install",
            )
        self.assertEqual((d / "started.flag").read_text(), "--from-test")
        self.assertTrue((d / "install.flag").exists())
        self.assertTrue((d / "install.flag").read_text().strip() == "ok")
        run = server.RUNS.get(iid)
        self.assertTrue(run and run.alive)
        self.assertEqual(run.trigger, "install-run")
        self.assertIn("main.py", run.display_cmd or "")
        self.assertIn("--from-test", (run.display_cmd or "") + " " + (run.args_str or ""))
        text = "".join(run.chunks)
        self.assertIn("INSTALLED", text)
        self.assertIn("starting after install", text)
        self.assertNotIn(iid, server.INSTALL_RUN_PENDING)

    def test_failed_install_does_not_start(self):
        d, iid = self.pin_project("failproj")
        with self.patch_install(d, "echo FAIL; exit 7"):
            code, _, body = self.srv.request("/api/items/%s/install_run" % iid, "POST", body={})
            self.assertEqual(code, 200, body)
            self.assertTrue(
                wait_until(
                    lambda: (
                        server.RUNS.get(iid)
                        and not server.RUNS[iid].alive
                        and server.RUNS[iid].exit_code is not None
                        and iid not in server.INSTALL_RUN_PENDING
                    ),
                    timeout=10,
                ),
                "install run did not finish",
            )
        self.assertFalse((d / "started.flag").exists())
        run = server.RUNS[iid]
        self.assertEqual(run.exit_code, 7)
        self.assertFalse(run.alive)
        self.assertEqual(run.trigger, "install-run")
        self.assertIn("install failed; not starting", "".join(run.chunks))
        self.assertNotIn(iid, server.INSTALL_RUN_PENDING)

    def test_missing_tool_rejected(self):
        d, iid = self.pin_project("notool")
        tool = "definitely-not-a-slothmore-tool"
        with self.patch_install(d, "echo should-not-run; echo ran > install.flag", tool=tool):
            listing = self.item(iid)
            self.assertEqual(listing["install_missing_tool"], tool)
            code, _, body = self.srv.request("/api/items/%s/install_run" % iid, "POST", body={})
            self.assertEqual(code, 400, body)
            self.assertIn(tool, json.loads(body)["error"])
        self.assertFalse((d / "started.flag").exists())
        self.assertFalse((d / "install.flag").exists())
        self.assertNotIn(iid, server.INSTALL_RUN_PENDING)
        self.assertTrue(server.RUNS.get(iid) is None or not server.RUNS[iid].alive)

    def test_duplicate_click_rejected(self):
        d, iid = self.pin_project("twice")
        script = "echo START; sleep 1.5; echo DONE"
        results = []

        def post():
            results.append(self.srv.request("/api/items/%s/install_run" % iid, "POST", body={}))

        with self.patch_install(d, script):
            t1 = threading.Thread(target=post)
            t2 = threading.Thread(target=post)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            codes = sorted(r[0] for r in results)
            self.assertEqual(codes, [200, 409], results)
            err = json.loads(next(r[2] for r in results if r[0] == 409))
            self.assertIn("already in progress", err["error"])
            self.assertTrue(
                wait_until(lambda: (d / "started.flag").exists(), timeout=10),
                "project did not start after the one accepted install",
            )
        self.assertTrue((d / "started.flag").exists())
        run = server.RUNS.get(iid)
        self.assertTrue(run and run.alive)

    def test_ui_has_install_and_run(self):
        js = (server.STATIC / "app.js").read_text()
        self.assertIn(">Install and run</button>", js)
        self.assertIn('data-install-run="', js)
        self.assertIn('itemAction(id, "install_run"', js)
        self.assertIn('itemAction(id, "install")', js)
        compact = (server.STATIC / "compact.js").read_text()
        self.assertIn("install_run", compact)
        self.assertIn("Install and run", compact)
        self.assertIn("function startAction", compact)
        css = (server.STATIC / "app.css").read_text()
        self.assertIn(".deps-acts", css)


if __name__ == "__main__":
    unittest.main()
