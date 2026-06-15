"""Tests for the agent's privilege-free service identity probes.

Three layers:
  1. Pure parsers (_identify_server_header / _identify_banner / _name_for_port) -- the
     closed-set banner->product mapping and the naming precedence.
  2. Dispatch (_identify_port) -- the port->probe table, with probes monkeypatched.
  3. Loopback integration -- a real 127.0.0.1 server proves _probe_http / _probe_line /
     _probe_mysql read banners, and that a silent port is time-bounded (~1s, never hangs).

Standalone (the agent is a standalone script). Run:
    python agent/test_service_probes.py
    # or, where psutil is available:  python -m unittest agent.test_service_probes
"""
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stacksense_agent as agent  # noqa: E402


class IdentifyServerHeaderTests(unittest.TestCase):
    def test_known_web_servers(self):
        cases = {
            "nginx/1.24.0": "nginx",
            "nginx": "nginx",
            "Apache/2.4.57 (cPanel)": "Apache",
            "LiteSpeed": "LiteSpeed",
            "openlitespeed/1.7": "LiteSpeed",
            "cpsrvd/11.116": "cpsrvd",
            "openresty/1.21": "OpenResty",
            "Microsoft-IIS/10.0": "IIS",
        }
        for header, product in cases.items():
            self.assertEqual(agent._identify_server_header(header), product, header)

    def test_unknown_header_returns_first_token(self):
        self.assertEqual(agent._identify_server_header("gunicorn/20.1.0"), "gunicorn")

    def test_empty_or_none(self):
        self.assertIsNone(agent._identify_server_header(""))
        self.assertIsNone(agent._identify_server_header(None))


class IdentifyBannerTests(unittest.TestCase):
    def test_known_banners(self):
        cases = {
            "SSH-2.0-OpenSSH_9.6p1 Ubuntu": "OpenSSH",
            "SSH-2.0-dropbear": "SSH",
            "220 mail.example.com ESMTP Exim 4.96": "Exim",
            "220 mail ESMTP Postfix": "Postfix",
            "* OK [CAPABILITY IMAP4rev1] Dovecot (Ubuntu) ready.": "Dovecot",
            "+OK Dovecot ready.": "Dovecot",
            "220 ProFTPD Server ready": "ProFTPD",
            "220---------- Welcome to Pure-FTPd": "Pure-FTPd",
            "220 (vsFTPd 3.0.5)": "vsftpd",
        }
        for banner, product in cases.items():
            self.assertEqual(agent._identify_banner(banner), product, banner)

    def test_garbage_and_empty(self):
        for bad in ("", None, "random noise", "\x00\x01\x02", "200 something"):
            self.assertIsNone(agent._identify_banner(bad))


class NameForPortTests(unittest.TestCase):
    def test_precedence(self):
        # product (banner) wins
        self.assertEqual(agent._name_for_port(80, "nginx", "httpd"), ("nginx (:80)", "port-banner"))
        # then the real process name
        self.assertEqual(agent._name_for_port(80, None, "httpd"), ("httpd (:80)", "port-process"))
        # then the well-known-port role
        self.assertEqual(agent._name_for_port(80, None, ""), ("HTTP (:80)", "port-map"))
        self.assertEqual(agent._name_for_port(2083, None, ""), ("cPanel (SSL) (:2083)", "port-map"))
        # then the raw port
        self.assertEqual(agent._name_for_port(52227, None, ""), ("port-52227", "port-unknown"))


class IdentifyPortDispatchTests(unittest.TestCase):
    """The port->probe table, with the network probes monkeypatched to canned banners."""

    def setUp(self):
        self._orig = (agent._probe_http, agent._probe_line, agent._probe_mysql)

    def tearDown(self):
        agent._probe_http, agent._probe_line, agent._probe_mysql = self._orig

    def test_dispatch(self):
        agent._probe_http = lambda port, tls=False: "nginx/1.24"
        agent._probe_line = lambda port, tls=False: {
            22: "SSH-2.0-OpenSSH_9.6", 25: "220 ESMTP Exim", 465: "220 ESMTP Exim",
            143: "* OK Dovecot ready", 993: "* OK Dovecot ready", 21: "220 ProFTPD",
        }[port]
        agent._probe_mysql = lambda port: "10.6.12-MariaDB"

        self.assertEqual(agent._identify_port(80), "nginx")
        self.assertEqual(agent._identify_port(443), "nginx")   # tls path, same monkeypatch
        self.assertEqual(agent._identify_port(2083), "nginx")  # cPanel SSL -> https probe
        self.assertEqual(agent._identify_port(22), "OpenSSH")
        self.assertEqual(agent._identify_port(25), "Exim")
        self.assertEqual(agent._identify_port(465), "Exim")
        self.assertEqual(agent._identify_port(143), "Dovecot")
        self.assertEqual(agent._identify_port(993), "Dovecot")
        self.assertEqual(agent._identify_port(21), "ProFTPD")
        self.assertEqual(agent._identify_port(3306), "MariaDB")

    def test_unknown_port_not_probed(self):
        agent._probe_http = lambda *a, **k: self.fail("should not probe unknown port")
        agent._probe_line = lambda *a, **k: self.fail("should not probe unknown port")
        self.assertIsNone(agent._identify_port(52227))


# --- loopback integration -------------------------------------------------

class _NginxHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def version_string(self):
        return "nginx/1.24.0"   # send_response emits this as the Server header

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _BannerServer:
    """Tiny TCP server: on connect, send `payload` then close. If hold=True, accept
    and stay silent (to exercise the recv-timeout / time-bound path)."""

    def __init__(self, payload=b"", hold=False):
        self.payload, self.hold, self.running = payload, hold, True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self.hold:
                    time.sleep(agent.PROBE_TIMEOUT + 0.5)
                elif self.payload:
                    conn.sendall(self.payload)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


class LoopbackProbeTests(unittest.TestCase):
    def test_probe_http_reads_server_header(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _NginxHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            self.assertEqual(agent._probe_http(port), "nginx/1.24.0")
            self.assertEqual(agent._identify_server_header(agent._probe_http(port)), "nginx")
        finally:
            srv.shutdown()

    def test_probe_line_reads_banner(self):
        srv = _BannerServer(payload=b"220 mail.example.com ESMTP Exim 4.96\r\n")
        try:
            line = agent._probe_line(srv.port)
            self.assertEqual(line, "220 mail.example.com ESMTP Exim 4.96")
            self.assertEqual(agent._identify_banner(line), "Exim")
        finally:
            srv.stop()

    def test_silent_port_is_time_bounded(self):
        srv = _BannerServer(hold=True)
        try:
            start = time.monotonic()
            self.assertIsNone(agent._probe_line(srv.port))
            self.assertLess(time.monotonic() - start, 3.0)
        finally:
            srv.stop()

    def test_closed_port_returns_none(self):
        # Bind then close to get a port nobody is listening on.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertIsNone(agent._probe_http(port))

    def test_probe_mysql_parses_handshake(self):
        for version, expect_product in (("8.0.36", "MySQL"), ("5.5.5-10.6.12-MariaDB", "MariaDB")):
            payload = b"\x0a" + version.encode() + b"\x00" + b"\x00" * 16
            hdr = bytes([len(payload) & 0xFF, (len(payload) >> 8) & 0xFF, (len(payload) >> 16) & 0xFF, 0])
            srv = _BannerServer(payload=hdr + payload)
            try:
                ver = agent._probe_mysql(srv.port)
                self.assertEqual(ver, version)
                product = "MariaDB" if "mariadb" in ver.lower() else "MySQL"
                self.assertEqual(product, expect_product)
            finally:
                srv.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
