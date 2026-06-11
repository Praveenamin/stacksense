"""
Agent resilience tests -- what the push agent does when StackSense is UNREACHABLE.

These prove the property that matters for production client VMs: when the monitoring
server is down, the agent DROPS the data it can't send -- it does not buffer/spool, so
it can't fill the client VM's disk or balloon its memory; it stays time-bounded (no
tight-loop, no hang); and it recovers when the server returns.

This is a STANDALONE suite (the agent is a standalone script, not part of Django). Run:
    python agent/test_agent_resilience.py
    # or:  python -m unittest agent.test_agent_resilience
"""
import gc
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stacksense_agent as agent  # noqa: E402


# Shared, mutable server state so a test can pick the response mode and count attempts.
STATE = {"mode": "ok", "attempts": 0}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output clean

    def do_POST(self):
        STATE["attempts"] += 1
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            self.rfile.read(length)
        except Exception:
            pass
        mode = STATE["mode"]
        if mode == "ok":
            self._json(200, {"status": "ok", "stored": True})
        elif mode == "500":
            self.send_response(500)
            self.end_headers()
        elif mode == "401":
            self._json(401, {"error": "bad token"})
        elif mode == "recover_after_2":
            if STATE["attempts"] < 3:
                self.send_response(500)
                self.end_headers()
            else:
                self._json(200, {"status": "ok"})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _dead_port():
    """A port nobody is listening on -> connecting to it is refused immediately."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PushResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # Don't actually sleep between retries during tests; keep the real attempt count.
        cls._delay, agent.RETRY_DELAY = agent.RETRY_DELAY, 0
        cls._timeout, agent.HTTP_TIMEOUT = agent.HTTP_TIMEOUT, 2

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        agent.RETRY_DELAY = cls._delay
        agent.HTTP_TIMEOUT = cls._timeout

    def setUp(self):
        STATE["mode"], STATE["attempts"] = "ok", 0
        self.opener = agent.build_opener(False)

    def _cfg(self, port=None):
        return {"url": f"http://127.0.0.1:{port or self.port}", "token": "t",
                "interval": 30, "verify_tls": False}

    def _push(self, cfg):
        return agent.push(cfg, self.opener, "/api/agent/metrics/", {"cpu": 1})

    # --- happy path -----------------------------------------------------------------
    def test_server_up_returns_parsed_response(self):
        STATE["mode"] = "ok"
        result = self._push(self._cfg())
        self.assertEqual(result.get("status"), "ok")

    # --- server unreachable ---------------------------------------------------------
    def test_server_down_returns_none_and_does_not_raise(self):
        # Connection refused -> push drops the sample (returns None), never raises.
        self.assertIsNone(self._push(self._cfg(port=_dead_port())))

    def test_server_down_is_time_bounded(self):
        t0 = time.monotonic()
        self._push(self._cfg(port=_dead_port()))
        self.assertLess(time.monotonic() - t0, 10)   # bounded by retries, no hang

    def test_server_error_retries_then_gives_up(self):
        STATE["mode"] = "500"
        self.assertIsNone(self._push(self._cfg()))
        self.assertEqual(STATE["attempts"], agent.MAX_RETRIES)   # retried up to the cap

    def test_bad_token_fails_fast_without_retry(self):
        STATE["mode"] = "401"
        self.assertIsNone(self._push(self._cfg()))
        self.assertEqual(STATE["attempts"], 1)   # 4xx won't fix itself -> no retry storm

    # --- recovery -------------------------------------------------------------------
    def test_recovers_within_a_single_call(self):
        STATE["mode"] = "recover_after_2"
        self.assertEqual(self._push(self._cfg()).get("status"), "ok")

    def test_outage_then_recovery_across_calls(self):
        self.assertIsNone(self._push(self._cfg(port=_dead_port())))   # during outage
        STATE["mode"] = "ok"
        self.assertEqual(self._push(self._cfg()).get("status"), "ok")  # after recovery

    # --- the core promise: no buffering on the client VM ----------------------------
    def test_no_disk_spool_on_repeated_failures(self):
        dead = self._cfg(port=_dead_port())
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
            os.environ["HOME"] = home
            os.chdir(cwd)
            try:
                for _ in range(20):
                    self._push(dead)
                self.assertEqual(os.listdir(home), [])   # no spool/cache file appears
                self.assertEqual(os.listdir(cwd), [])
            finally:
                os.chdir(old_cwd)
                if old_home is not None:
                    os.environ["HOME"] = old_home

    def test_repeated_failures_do_not_accumulate(self):
        dead = self._cfg(port=_dead_port())
        gc.collect()
        before = len(gc.get_objects())
        for _ in range(500):
            self._push(dead)               # 500 dropped samples
        gc.collect()
        # A backlog/queue would grow O(N) (~500+). Stateless drop -> roughly flat.
        self.assertLess(len(gc.get_objects()) - before, 250)


class OnceCycleTests(unittest.TestCase):
    """A full collection cycle (--once) against a down server exits cleanly and bounded."""

    def test_once_against_dead_server_exits_clean_and_bounded(self):
        env = dict(os.environ,
                   STACKSENSE_URL=f"http://127.0.0.1:{_dead_port()}",
                   STACKSENSE_TOKEN="t", STACKSENSE_VERIFY_TLS="false",
                   STACKSENSE_INTERVAL="1")
        agent_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "stacksense_agent.py")
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, agent_path, "--once"], env=env,
                              capture_output=True, text=True, timeout=120)
        elapsed = time.monotonic() - t0
        self.assertEqual(proc.returncode, 0)   # one cycle, server down -> clean exit
        self.assertLess(elapsed, 90)           # bounded; never hangs


if __name__ == "__main__":
    unittest.main(verbosity=2)
