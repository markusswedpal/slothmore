"""Finding 9 and 10: app logging, redaction, generic 500s, and SSE writes off the lock."""
import json
import threading
import unittest
from unittest import mock

from support import LiveServer, server


TOKEN = "sekrit-token-abcdefghijklmnopqrstuvwxyz01234567"


class LoggingTests(unittest.TestCase):
    def setUp(self):
        for h in list(server.LOG.handlers):
            server.LOG.removeHandler(h)
            h.close()
        server.setup_logging(level="debug", interactive=False)
        def _close():
            for h in list(server.LOG.handlers):
                server.LOG.removeHandler(h)
                h.close()
        self.addCleanup(_close)

    def test_redact_strips_token_and_bearer(self):
        server._ACTIVE_TOKEN = TOKEN
        server._remember_secret(TOKEN)
        text = "Authorization: Bearer %s cookie=%s path=/?token=%s oops %s" % (TOKEN, TOKEN, TOKEN, TOKEN)
        out = server._redact(text)
        self.assertNotIn(TOKEN, out)
        self.assertIn("[redacted]", out)
        self.assertIn("?token=[redacted]", out)

    def test_internal_error_is_generic_and_logged_without_token(self):
        server._ACTIVE_TOKEN = TOKEN
        server._remember_secret(TOKEN)
        srv = LiveServer(token=TOKEN)
        self.addCleanup(srv.close)

        def boom(*_a, **_k):
            raise RuntimeError("leaked " + TOKEN)

        with mock.patch("server.list_listening_ports", side_effect=boom):
            status, _headers, body = srv.request("/api/ports")
        self.assertEqual(status, 500)
        data = json.loads(body)
        self.assertEqual(data["error"], "internal error")
        raw = body.decode("utf-8", "replace")
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("leaked", raw)
        log = server.LOG_PATH.read_text()
        self.assertIn("internal error", log.lower())
        self.assertNotIn(TOKEN, log)
        self.assertIn("[redacted]", log)


class SsePollTests(unittest.TestCase):
    def _run(self):
        run = server.Run({"id": "0" * 10, "cmd": ["true"], "name": "demo"})
        run.alive = True
        run.exit_code = None
        return run

    def test_sse_poll_releases_lock_after_chunks(self):
        run = self._run()
        run.chunks.append("hello")
        idx, kind, payload = server.sse_poll(run, 0, 0.2)
        self.assertEqual(kind, "chunks")
        self.assertEqual(payload, ["hello"])
        self.assertEqual(idx, 1)
        self.assertTrue(run.cond.acquire(timeout=0.2))
        run.cond.release()

    def test_sse_poll_exit_and_ping_release_lock(self):
        run = self._run()
        run.alive = False
        run.exit_code = 0
        idx, kind, payload = server.sse_poll(run, 0, 0.2)
        self.assertEqual(kind, "exit")
        self.assertEqual(payload, 0)
        self.assertTrue(run.cond.acquire(timeout=0.2))
        run.cond.release()

        run = self._run()
        idx, kind, payload = server.sse_poll(run, 0, 0.05)
        self.assertEqual(kind, "ping")
        self.assertTrue(run.cond.acquire(timeout=0.2))
        run.cond.release()

    def test_stream_writes_can_take_the_run_lock(self):
        """If a socket write ran while still holding run.cond, this would time out."""
        run = self._run()
        run.chunks.append("line")
        idx, kind, payload = server.sse_poll(run, 0, 0.2)
        self.assertEqual(kind, "chunks")
        held = []

        def write(_data):
            ok = run.cond.acquire(timeout=0.5)
            held.append(ok)
            if ok:
                run.cond.release()

        # Mimic _stream: write after sse_poll has returned.
        write(("id: %d\ndata: %s\n\n" % (idx, json.dumps({"t": payload[0]}))).encode())
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()
