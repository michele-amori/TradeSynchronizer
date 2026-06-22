"""Tests for ibc_gateway_orchestrator — multi-Gateway auto-launch."""

import json
import tempfile
import unittest
from pathlib import Path

from tradesync.ibc_gateway_orchestrator import (
    GatewaySpec,
    PortStartResult,
    ensure_ports_listening,
    load_gateway_map,
    required_ports_for,
)


class _FakeGateway:
    """Container for the dependency fakes a test wires in."""
    def __init__(self, open_ports=()):
        self.open_ports = set(open_ports)
        self.launched = []          # ports we were asked to launch, in order
        self.sleeps = 0

    def is_open(self, host, port):
        return port in self.open_ports

    def launch(self, spec):
        self.launched.append(spec.port)
        # By default a launched Gateway comes up immediately.
        self.open_ports.add(spec.port)

    def sleep(self, _seconds):
        self.sleeps += 1


def _spec(port, login=""):
    return GatewaySpec(port=port, command=[f"/run-{port}.sh"], login=login)


class TestEnsurePortsListening(unittest.TestCase):
    def test_already_listening_left_untouched(self):
        fake = _FakeGateway(open_ports={4002})
        out = ensure_ports_listening(
            [4002], {4002: _spec(4002)},
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(out[0].result, PortStartResult.ALREADY_UP)
        self.assertEqual(fake.launched, [])  # never launched

    def test_launches_missing_port_and_waits(self):
        fake = _FakeGateway(open_ports=set())
        out = ensure_ports_listening(
            [4002], {4002: _spec(4002, "userA")},
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(out[0].result, PortStartResult.LAUNCHED_UP)
        self.assertEqual(fake.launched, [4002])

    def test_launches_multiple_one_at_a_time_in_order(self):
        fake = _FakeGateway(open_ports=set())
        out = ensure_ports_listening(
            [4002, 4003], {4002: _spec(4002), 4003: _spec(4003)},
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(fake.launched, [4002, 4003])  # ordered
        self.assertTrue(all(o.result == PortStartResult.LAUNCHED_UP
                            for o in out))

    def test_mixed_already_up_and_needs_launch(self):
        fake = _FakeGateway(open_ports={4002})  # 4002 up, 4003 not
        out = ensure_ports_listening(
            [4002, 4003], {4002: _spec(4002), 4003: _spec(4003)},
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(fake.launched, [4003])  # only the missing one
        results = {o.port: o.result for o in out}
        self.assertEqual(results[4002], PortStartResult.ALREADY_UP)
        self.assertEqual(results[4003], PortStartResult.LAUNCHED_UP)

    def test_no_mapping_for_required_port(self):
        fake = _FakeGateway(open_ports=set())
        out = ensure_ports_listening(
            [4099], {},  # nothing mapped
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(out[0].result, PortStartResult.NO_MAPPING)
        self.assertEqual(fake.launched, [])

    def test_launch_timeout_when_port_never_opens(self):
        fake = _FakeGateway(open_ports=set())

        def launch_but_stay_down(spec):
            fake.launched.append(spec.port)  # launched, but DON'T open it

        out = ensure_ports_listening(
            [4002], {4002: _spec(4002)},
            port_open_check=fake.is_open, launcher=launch_but_stay_down,
            sleeper=fake.sleep, wait_timeout=4.0, poll_interval=1.0)
        self.assertEqual(out[0].result, PortStartResult.LAUNCHED_TIMEOUT)

    def test_launch_failure_is_reported_not_raised(self):
        fake = _FakeGateway(open_ports=set())

        def boom(_spec):
            raise OSError("script not found")

        out = ensure_ports_listening(
            [4002], {4002: _spec(4002)},
            port_open_check=fake.is_open, launcher=boom, sleeper=fake.sleep)
        self.assertEqual(out[0].result, PortStartResult.LAUNCH_FAILED)

    def test_duplicate_ports_collapsed(self):
        # Two followers sharing a port → launch once.
        fake = _FakeGateway(open_ports=set())
        out = ensure_ports_listening(
            [4002, 4002], {4002: _spec(4002)},
            port_open_check=fake.is_open, launcher=fake.launch,
            sleeper=fake.sleep)
        self.assertEqual(len(out), 1)
        self.assertEqual(fake.launched, [4002])


class TestLoadGatewayMap(unittest.TestCase):
    def _write(self, payload):
        d = Path(tempfile.mkdtemp())
        p = d / "ibc_gateways.json"
        p.write_text(json.dumps(payload))
        return p

    def test_missing_file_yields_empty_map(self):
        m = load_gateway_map(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertEqual(m, {})

    def test_script_arg_form(self):
        p = self._write({"gateways": {
            "4002": {"script": "~/ibc-demo/start.sh", "arg": "A",
                     "login": "u"}}})
        m = load_gateway_map(p)
        self.assertIn(4002, m)
        self.assertTrue(m[4002].command[0].endswith("ibc-demo/start.sh"))
        self.assertEqual(m[4002].command[1], "A")
        self.assertEqual(m[4002].login, "u")

    def test_explicit_command_form(self):
        p = self._write({"gateways": {
            "4003": {"command": ["/x/start.sh", "B"]}}})
        m = load_gateway_map(p)
        self.assertEqual(m[4003].command, ["/x/start.sh", "B"])

    def test_bare_object_without_gateways_key(self):
        p = self._write({"4002": {"script": "/s.sh", "arg": "A"}})
        m = load_gateway_map(p)
        self.assertIn(4002, m)

    def test_bad_port_raises(self):
        p = self._write({"gateways": {"notaport": {"script": "/s.sh"}}})
        with self.assertRaises(ValueError):
            load_gateway_map(p)

    def test_spec_without_command_or_script_raises(self):
        p = self._write({"gateways": {"4002": {"login": "u"}}})
        with self.assertRaises(ValueError):
            load_gateway_map(p)


class TestRequiredPortsFor(unittest.TestCase):
    def test_distinct_ports_from_enabled_ibkr_followers(self):
        from tradesync.replication_config import (
            ReplicationConfig, ReplicationPair, EndpointRef,
            IbkrGatewayConfig,
        )
        default_gw = IbkrGatewayConfig(host="127.0.0.1", port=4001)

        def pair(name, port, enabled=True):
            return ReplicationPair(
                name=name,
                source=EndpointRef(broker="tradovate", env="demo",
                                   account_id="D1"),
                follower=EndpointRef(broker="ibkr", env="demo",
                                     account_id=f"DU{port}"),
                enabled=enabled,
                ibkr_gateway=IbkrGatewayConfig(host="127.0.0.1", port=port))

        cfg = ReplicationConfig(pairs=[
            pair("a", 4002),
            pair("b", 4003),
            pair("c", 4002),          # dup port → collapses
            pair("d", 4099, enabled=False),  # disabled → ignored
        ])
        self.assertEqual(required_ports_for(cfg, default_gw), [4002, 4003])


if __name__ == "__main__":
    unittest.main()
