"""Regression tests for the security suite's temporary probe listener and the public-listener check (no root needed).
VM failure 2026-11-07: no_unexpected_public_listeners flagged the suite's own probe ["0.0.0.0:18080", "0.0.0.0:22", "[::]:22"].
Run: python3 -m unittest tests.test_listeners"""
import os, socket, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "acceptance"))
import listeners as L  # noqa: E402

ME = 4242
VM_SS = f'''LISTEN 0 5       0.0.0.0:18080      0.0.0.0:*    users:(("python3",pid={ME},fd=5))
LISTEN 0 4096    0.0.0.0:22         0.0.0.0:*    users:(("sshd",pid=811,fd=3))
LISTEN 0 4096       [::]:22            [::]:*    users:(("sshd",pid=811,fd=4))
LISTEN 0 128  10.233.0.1:8080       0.0.0.0:*    users:(("python3",pid=900,fd=3))
LISTEN 0 5     127.0.0.1:8090       0.0.0.0:*    users:(("python3",pid=901,fd=3))
LISTEN 0 4096 127.0.0.53%lo:53      0.0.0.0:*    users:(("systemd-resolve",pid=500,fd=14))
'''

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class TestClassify(unittest.TestCase):
    def test_vm_failure_case_is_clean_and_reports_the_excluded_probe(self):
        unexpected, own, ok = L.classify_public(L.parse_ss(VM_SS), own_probe=(18080, ME))
        self.assertEqual(unexpected, []); self.assertEqual(own, ["0.0.0.0:18080"]); self.assertEqual(ok, ["0.0.0.0:22", "[::]:22"])
    def test_without_the_exclusion_the_probe_is_still_flagged(self):   # the original false failure, reproduced
        self.assertEqual(L.classify_public(L.parse_ss(VM_SS))[0], ["0.0.0.0:18080"])
    def test_real_unexpected_listeners_are_still_caught(self):
        cases = {"another process on the probe port": 'LISTEN 0 5 0.0.0.0:18080 0.0.0.0:* users:(("nc",pid=77,fd=3))',
                 "probe port shared with another pid": f'LISTEN 0 5 0.0.0.0:18080 0.0.0.0:* users:(("python3",pid={ME},fd=5),("nc",pid=77,fd=3))',
                 "this process on another port": f'LISTEN 0 5 0.0.0.0:9999 0.0.0.0:* users:(("python3",pid={ME},fd=6))',
                 "web bound publicly": 'LISTEN 0 5 0.0.0.0:8090 0.0.0.0:* users:(("python3",pid=901,fd=3))',
                 "gateway bound publicly": 'LISTEN 0 5 [::]:8080 [::]:* users:(("python3",pid=900,fd=3))',
                 "docker api": 'LISTEN 0 5 *:2375 *:* users:(("dockerd",pid=5,fd=3))'}
        for why, line in cases.items():
            self.assertNotEqual(L.classify_public(L.parse_ss(line), own_probe=(18080, ME))[0], [], why)
    def test_allowed_and_loopback(self):
        ss = 'LISTEN 0 5 0.0.0.0:443 0.0.0.0:* users:(("caddy",pid=1,fd=3))\nLISTEN 0 5 [::]:80 [::]:* users:(("caddy",pid=1,fd=4))\nLISTEN 0 5 [::1]:631 [::]:*'
        self.assertEqual(L.classify_public(L.parse_ss(ss)), ([], [], ["0.0.0.0:443", "[::]:80"]))

class TestProbeLifecycle(unittest.TestCase):
    def test_guard_before_bind_cleanup_after_and_on_error(self):
        events = []
        port = free_port()
        def connect():
            try: socket.create_connection(("127.0.0.1", port), timeout=1).close(); return True
            except OSError: return False
        install = lambda p: events.append(("guard-up", p, connect()))      # the guard must be up while nothing listens yet
        remove = lambda p: events.append(("guard-down", p, connect()))     # and come down only after the socket is closed
        with L.probe_listener(port, install, remove):
            self.assertTrue(connect())
        self.assertFalse(connect())
        self.assertEqual(events, [("guard-up", port, False), ("guard-down", port, False)])
        events.clear()
        with self.assertRaises(RuntimeError):
            with L.probe_listener(port, install, remove):
                raise RuntimeError("suite crashed mid-run")
        self.assertFalse(connect()); self.assertEqual([e[0] for e in events], ["guard-up", "guard-down"])
    def test_no_bind_if_guard_cannot_be_installed(self):
        port = free_port(); removed = []
        def bad(p): raise RuntimeError("iptables unavailable")
        with self.assertRaises(RuntimeError):
            with L.probe_listener(port, bad, lambda p: removed.append(p)): self.fail("must not run unguarded")
        with self.assertRaises(OSError): socket.create_connection(("127.0.0.1", port), timeout=1)
        self.assertEqual(removed, [port])

class TestSuiteWiring(unittest.TestCase):
    def test_suite_uses_guarded_probe_exclusion_and_cleanup_check(self):
        src = open(os.path.join(os.path.dirname(L.__file__), "rootless_suite.py")).read()
        self.assertIn("L.probe_listener(PROBE_PORT)", src); self.assertNotIn("def listener(", src)
        self.assertIn("own_probe=(PROBE_PORT, os.getpid())", src)
        for name in ("probe_listener_guarded_from_external_interfaces", "no_unexpected_public_listeners", "probe_listener_cleaned_up"): self.assertIn(f'"{name}"', src)
        self.assertLess(src.index("STACK.close()   # shut down"), src.index('(ACC / "latest.json").write_text'))
        v = open(os.path.join(os.path.dirname(L.__file__), "..", "..", "deploy", "vm", "verify.sh")).read()
        self.assertIn('ss -ltnH "sport = :$PROBE_PORT"', v); self.assertIn("drop_guard", v)

if __name__ == "__main__":
    unittest.main()
