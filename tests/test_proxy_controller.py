"""
Tests for ProxyController — the per-environment engine-subprocess
supervisor, extracted from ui/app.py.

These exercise the parts that don't require actually spawning a real
engine: state transitions + change callbacks, the log queue's
bounded-drop behaviour, and Python-interpreter resolution. Spawning is
left to manual / integration testing since it needs a real main.py and
a port to bind.
"""

import queue
import unittest
from pathlib import Path

from tradesync.ui.proxy_controller import ProxyController


def _make(tmp_root="/tmp/tsync-test-root"):
    return ProxyController("live", Path(tmp_root), queue.Queue(maxsize=10))


class TestStateMachine(unittest.TestCase):

    def test_starts_stopped(self):
        self.assertEqual(_make().state, ProxyController.STATE_STOPPED)

    def test_state_change_fires_callback(self):
        c = _make()
        seen = []
        c.on_state_change(seen.append)
        c._set_state(ProxyController.STATE_RUNNING)
        self.assertEqual(seen, [ProxyController.STATE_RUNNING])

    def test_same_state_does_not_refire(self):
        c = _make()
        seen = []
        c.on_state_change(seen.append)
        c._set_state(ProxyController.STATE_RUNNING)
        c._set_state(ProxyController.STATE_RUNNING)  # no-op
        self.assertEqual(seen, [ProxyController.STATE_RUNNING])

    def test_callback_exception_does_not_propagate(self):
        c = _make()

        def boom(_state):
            raise RuntimeError("callback blew up")

        c.on_state_change(boom)
        # Must not raise despite the bad callback.
        c._set_state(ProxyController.STATE_RUNNING)
        self.assertEqual(c.state, ProxyController.STATE_RUNNING)

    def test_start_reports_missing_main_py(self):
        # A root with no main.py should yield a clear error, not spawn.
        c = ProxyController("live", Path("/tmp/definitely-no-main-here"),
                            queue.Queue())
        err = c.start()
        self.assertIsNotNone(err)
        self.assertIn("main.py", err)


class TestLogQueue(unittest.TestCase):

    def test_enqueue_drops_oldest_when_full(self):
        q: "queue.Queue[str]" = queue.Queue(maxsize=2)
        c = ProxyController("live", Path("/tmp"), q)
        c._enqueue("a\n")
        c._enqueue("b\n")
        c._enqueue("c\n")        # full → drops oldest ("a")
        drained = []
        try:
            while True:
                drained.append(q.get_nowait())
        except queue.Empty:
            pass
        self.assertNotIn("a\n", drained)
        self.assertIn("c\n", drained)


class TestResolvePython(unittest.TestCase):

    def test_falls_back_to_sys_executable(self, ):
        import sys
        # A root with no .venv → resolve falls back to sys.executable.
        c = ProxyController("live", Path("/tmp/no-venv-here"), queue.Queue())
        self.assertEqual(c._resolve_python(), sys.executable)


if __name__ == "__main__":
    unittest.main()
