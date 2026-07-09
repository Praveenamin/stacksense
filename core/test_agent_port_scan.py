"""2a: agent non-root listening-port discovery via /proc/net/tcp{,6}.

These exercise the standalone agent script (agent/stacksense_agent.py) directly, since the
fallback runs on the monitored box, not the server. The parser is pure; the collect_services
test simulates psutil being denied (as it is for an unprivileged agent) and asserts the /proc
fallback still surfaces the listening port so Response/SLO can light up.
"""
import importlib.util
import os
import unittest

_AGENT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent", "stacksense_agent.py",
)


def _load_agent():
    spec = importlib.util.spec_from_file_location("stacksense_agent_under_test", _AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(os.path.exists(_AGENT_PATH), "agent script not present")
class ProcNetTcpParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = _load_agent()

    def test_ipv4_listen_sockets_parsed(self):
        sample = (
            "  sl  local_address rem_address   st tx rx tr tm retr uid to inode\n"
            "   0: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1 1\n"
            "   1: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 1 1\n"
        )
        got = self.agent._parse_proc_net_tcp_listeners(sample, is_v6=False)
        self.assertIn((80, "0.0.0.0"), got)          # 0x0050 on wildcard
        self.assertIn((8080, "127.0.0.1"), got)      # 0x1F90 on loopback (LE-decoded)

    def test_non_listen_sockets_ignored(self):
        # State 01 == ESTABLISHED must never be reported as a listening service.
        sample = (
            "header\n"
            "   0: 00000000:0050 0A0A0A0A:C000 01 00000000:00000000 00:00000000 00000000 0 0 1 1\n"
        )
        self.assertEqual(self.agent._parse_proc_net_tcp_listeners(sample, is_v6=False), [])

    def test_ipv6_wildcard_and_loopback(self):
        sample = (
            "header\n"
            "   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A x x x 0 0 1 1\n"
            "   1: 00000000000000000000000001000000:0050 00000000000000000000000000000000:0000 0A x x x 0 0 1 1\n"
        )
        got = dict(self.agent._parse_proc_net_tcp_listeners(sample, is_v6=True))
        self.assertEqual(got.get(22), "::")           # wildcard v6
        self.assertEqual(got.get(80), "::1")          # loopback v6 (4 LE words)

    def test_decode_addr(self):
        self.assertEqual(self.agent._decode_proc_addr("00000000", is_v6=False), "0.0.0.0")
        self.assertEqual(self.agent._decode_proc_addr("0100007F", is_v6=False), "127.0.0.1")


@unittest.skipUnless(os.path.exists(_AGENT_PATH), "agent script not present")
class CollectServicesFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = _load_agent()

    def test_proc_fallback_recovers_port_when_psutil_denied(self):
        a = self.agent
        if not a._IS_LINUX:
            self.skipTest("fallback is Linux-only")

        def _denied(kind="inet"):
            raise a.psutil.AccessDenied()

        saved = (a.psutil.net_connections, a._listening_ports_from_proc,
                 a._identify_port, a._attach_service_latency)
        try:
            a.psutil.net_connections = _denied                       # unprivileged: denied
            a._listening_ports_from_proc = lambda: [(9999, "0.0.0.0")]
            a._identify_port = lambda port: None                     # no banner grab in tests
            a._attach_service_latency = lambda services: None        # no real connects in tests
            svcs = a.collect_services()
        finally:
            (a.psutil.net_connections, a._listening_ports_from_proc,
             a._identify_port, a._attach_service_latency) = saved

        ports = [s for s in svcs if s.get("service_type") == "port" and s.get("port") == 9999]
        self.assertEqual(len(ports), 1, "fallback should surface the listening port")
        self.assertEqual(ports[0]["name"], "port-9999")
        self.assertEqual(ports[0]["process_id"], "")                 # no PID/owner from /proc

    def test_no_duplicate_when_psutil_already_saw_the_port(self):
        a = self.agent
        if not a._IS_LINUX:
            self.skipTest("fallback is Linux-only")

        class _Addr:
            ip = "0.0.0.0"; port = 8080

        class _Conn:
            status = "LISTEN"; laddr = _Addr(); pid = None

        def _one_listener(kind="inet"):
            return [_Conn()]

        saved = (a.psutil.net_connections, a._listening_ports_from_proc,
                 a._identify_port, a._attach_service_latency)
        try:
            a.psutil.net_connections = _one_listener
            a._listening_ports_from_proc = lambda: [(8080, "0.0.0.0")]  # same port as psutil
            a._identify_port = lambda port: None
            a._attach_service_latency = lambda services: None
            svcs = a.collect_services()
        finally:
            (a.psutil.net_connections, a._listening_ports_from_proc,
             a._identify_port, a._attach_service_latency) = saved

        p8080 = [s for s in svcs if s.get("service_type") == "port" and s.get("port") == 8080]
        self.assertEqual(len(p8080), 1, "psutil + fallback must not double-count a port")
