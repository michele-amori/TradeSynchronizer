"""
Tests for the broker endpoint protocols (tradesync.brokers.endpoint).

These pin two things:
  1. The neutral result dataclasses (PlacedRef / PlacedBracketRef)
     have the shape and defaults the replicator will rely on.
  2. The Protocols are structural and runtime-checkable: a class with
     the right methods satisfies isinstance(), one missing a method
     does not. This is what lets concrete broker adapters conform
     without inheriting, and lets tests assert conformance cheaply.
"""

import unittest
from typing import Callable

from tradesync.brokers.endpoint import (
    FollowerEndpoint,
    PlacedBracketRef,
    PlacedRef,
    SourceEndpoint,
)
from tradesync.order_event import (
    BracketSpec,
    ModifySpec,
    OrderEvent,
    OrderSpec,
)


class TestNeutralResultTypes(unittest.TestCase):

    def test_placed_ref_defaults(self):
        ref = PlacedRef(follower_order_id="11978727757")
        self.assertEqual(ref.follower_order_id, "11978727757")
        self.assertEqual(ref.raw, {})

    def test_placed_bracket_ref_defaults(self):
        ref = PlacedBracketRef(entry_order_id="100")
        self.assertEqual(ref.entry_order_id, "100")
        self.assertEqual(ref.child_order_ids, [])
        self.assertIsNone(ref.oco_id)
        self.assertEqual(ref.raw, {})

    def test_placed_bracket_ref_with_children(self):
        ref = PlacedBracketRef(
            entry_order_id="100",
            child_order_ids=["101", "102"],
            oco_id=None,
            raw={"orderId": 100},
        )
        self.assertEqual(ref.child_order_ids, ["101", "102"])


# ── Conformance fixtures ─────────────────────────────────────────────── #

class _ConformingSource:
    """A minimal class that structurally satisfies SourceEndpoint."""
    @property
    def identity(self) -> str:
        return "fake_source"

    def start_observing(self, on_event: Callable[[OrderEvent], None]) -> None:
        pass

    def stop_observing(self) -> None:
        pass


class _ConformingFollower:
    """A minimal class that structurally satisfies FollowerEndpoint."""
    @property
    def identity(self) -> str:
        return "fake_follower"

    @property
    def native_oco(self) -> bool:
        return False

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def place_order(self, spec: OrderSpec, *, symbol: str) -> PlacedRef:
        return PlacedRef(follower_order_id="1")

    def place_bracket(self, spec: BracketSpec, *, symbol: str) -> PlacedBracketRef:
        return PlacedBracketRef(entry_order_id="1")

    def cancel_order(self, follower_order_id: str) -> None:
        pass

    def modify_order(self, follower_order_id: str, changes: ModifySpec) -> None:
        pass

    def order_status(self, follower_order_id: str) -> str:
        return "Working"


class _NotAFollower:
    """Missing most follower methods — must NOT satisfy the protocol."""
    def connect(self) -> None:
        pass


class TestProtocolConformance(unittest.TestCase):

    def test_conforming_source_passes_isinstance(self):
        self.assertIsInstance(_ConformingSource(), SourceEndpoint)

    def test_conforming_follower_passes_isinstance(self):
        self.assertIsInstance(_ConformingFollower(), FollowerEndpoint)

    def test_incomplete_follower_fails_isinstance(self):
        self.assertNotIsInstance(_NotAFollower(), FollowerEndpoint)

    def test_source_is_not_follower(self):
        # A pure source shouldn't accidentally satisfy the follower
        # contract (it has none of place/cancel/modify).
        self.assertNotIsInstance(_ConformingSource(), FollowerEndpoint)


if __name__ == "__main__":
    unittest.main()
