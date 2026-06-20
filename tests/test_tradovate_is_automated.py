"""Tests for the `isAutomated` flag on Tradovate order payloads.

Background: trade-copier services that fan out a leader Tradovate
account to follower accounts (TradeSyncer.com, plus the equivalents
built into Tradovate's own Trader Console) typically use the
isAutomated field on each order to decide whether to broadcast it.
The default behaviour is to SKIP isAutomated=true orders — that's
how copiers avoid loops between accounts that all run algos, and
how leader operators keep their own algorithmic strategies private.

Until this fix shipped, the TradovateClient sent isAutomated=true
hardcoded on every order, so the user's leader account would accept
the order normally but TradeSyncer.com would silently drop it on
the broadcast side. Symptom: leader fills, followers don't.

The fix introduces a single config knob threaded all the way
through to every payload-emitting place in tradovate.py:

  Config.tradovate_is_automated  (defaults to False)
       ↑ read from env var TRADOVATE_IS_AUTOMATED in Config.load
       ↑ surfaced in the GUI's General tab → "Tradovate application"
         section so the user can flip it without editing files
       ↓ passed to TradovateClient.__init__ in main.py
            ↓ self._is_automated
                 ↓ payload["isAutomated"] in place_order
                 ↓ payload["isAutomated"] in place_bracket (entry)
                 ↓ child["isAutomated"]   in place_bracket (each leg)
                 ↓ payload["isAutomated"] in modify_order

This test file pins every step of that chain.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the project root importable when running the file directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradesync.brokers.tradovate import TradovateClient
from tradesync.config import Config


# ── Helpers ─────────────────────────────────────────────────────────── #

def _make_client(*, is_automated: bool) -> TradovateClient:
    """Build a TradovateClient WITHOUT shadow mode, with the
    networking stub. is_automated flows through to every payload."""
    c = TradovateClient(
        api_url="https://demo.tradovateapi.com/v1",
        username="fake-user", password="fake-pass",
        app_id="TradeSynchronizer", app_version="1.0",
        cid="fake-cid", sec="fake-sec",
        pinned_account_id=123,
        device_id="fake-device",
        is_automated=is_automated,
    )
    # Skip authentication entirely — we only care about payload shape.
    c._access_token = "fake-token"
    c._md_access_token = "fake-md-token"
    from datetime import datetime, timedelta, timezone
    c._expiration = datetime.now(timezone.utc) + timedelta(hours=1)
    c._account_id = 123
    c._shadow_mode = False
    return c


def _fake_response(status: int = 200, body: dict | None = None):
    """Build a minimal requests.Response-like object."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    resp.json.return_value = body or {"orderId": 7777}
    return resp


# ── Config layer ────────────────────────────────────────────────────── #

def _load_config_with_env(env_overrides: dict) -> Config:
    """Run Config.load() with a temporary .env.demo on disk and a
    specific env-var overlay. Patches env_file_for so we don't have
    to write to the real project root, and stubs the app-credentials
    loader so we don't read _app_credentials.py."""
    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / ".env.demo"
        # Minimum-viable .env.demo; Config.load only requires that
        # the file exists. Per-env values may be overridden via
        # os.environ below.
        env_file.write_text(
            "TRADOVATE_USERNAME=u\n"
            "TRADOVATE_PASSWORD=p\n"
            "PROXY_LISTEN_PORT=8081\n"
        )
        full_env = {
            "TRADOVATE_ENVIRONMENT": "demo",
            **env_overrides,
        }
        with patch.dict(os.environ, full_env, clear=False), \
             patch("tradesync.config.env_file_for", return_value=env_file), \
             patch("tradesync.config.load_app_credentials_or_empty",
                   return_value=("cid", "sec")), \
             patch("tradesync.config._merge_into_environ"):
            # _merge_into_environ would call load_dotenv on the real
            # path and pull state from outside our overrides; patch
            # it out so the overlay above is the only env source.
            return Config.load()


class TestConfigDefault(unittest.TestCase):

    def test_env_var_set_to_true_flips_field(self):
        """TRADOVATE_IS_AUTOMATED=true via env makes the field True."""
        cfg = _load_config_with_env({"TRADOVATE_IS_AUTOMATED": "true"})
        self.assertTrue(cfg.tradovate_is_automated)

    def test_env_var_unset_defaults_to_false(self):
        """No env var at all → False. Matters because the upgrade
        path for existing installs goes through this branch — they
        have .env files without TRADOVATE_IS_AUTOMATED and must NOT
        accidentally start sending isAutomated=true post-upgrade."""
        # Make sure no stray var from another test leaks in.
        os.environ.pop("TRADOVATE_IS_AUTOMATED", None)
        cfg = _load_config_with_env({})
        self.assertFalse(cfg.tradovate_is_automated)

    def test_env_var_accepts_canonical_truthy_spellings(self):
        """Be forgiving about how users spell 'true' in .env files.
        Matches the convention used by VERBOSE_TROUBLESHOOTING — same
        parser, same accepted values."""
        for truthy in ("true", "True", "TRUE", "1", "yes", "on"):
            cfg = _load_config_with_env(
                {"TRADOVATE_IS_AUTOMATED": truthy}
            )
            self.assertTrue(
                cfg.tradovate_is_automated,
                f"'{truthy}' should parse as truthy",
            )

    def test_env_var_garbage_value_is_treated_as_false(self):
        """Anything we don't explicitly recognise as truthy →
        False. Defensive: don't crash on a typo, just behave
        conservatively (the safer default for the trade-copier
        compatibility story)."""
        for falsy in ("false", "0", "no", "off", "definitely_not", "  "):
            cfg = _load_config_with_env(
                {"TRADOVATE_IS_AUTOMATED": falsy}
            )
            self.assertFalse(
                cfg.tradovate_is_automated,
                f"'{falsy}' should parse as falsy",
            )


# ── TradovateClient layer ───────────────────────────────────────────── #

class TestPlaceOrderPayload(unittest.TestCase):
    """Single-order /order/placeorder payloads must carry the flag
    set on the client at construction time. No silent True override
    anywhere down the stack."""

    def _run_place_and_capture(self, is_automated: bool) -> dict:
        c = _make_client(is_automated=is_automated)
        with patch.object(c._http, "post",
                          return_value=_fake_response()) as post:
            c.place_order(
                tradovate_symbol="MNQM6", contract_id=4327110,
                action="Buy", qty=1, order_type="Market",
            )
        # Captured kwargs: post(url, json=PAYLOAD, headers=..., timeout=...)
        kwargs = post.call_args.kwargs
        return kwargs["json"]

    def test_default_false_flag_is_sent_to_tradovate(self):
        payload = self._run_place_and_capture(is_automated=False)
        self.assertIn("isAutomated", payload)
        self.assertFalse(payload["isAutomated"],
                         "default client must NOT mark orders automated; "
                         "trade-copiers like TradeSyncer.com would drop them")

    def test_explicit_true_flag_is_sent_to_tradovate(self):
        payload = self._run_place_and_capture(is_automated=True)
        self.assertTrue(payload["isAutomated"],
                        "when the user opts in to is_automated=True the "
                        "client must honour it on the wire")


class TestPlaceBracketPayload(unittest.TestCase):
    """A bracket is one entry + 1-2 children, all of which must
    carry the same isAutomated value. Children especially mattered
    in the field report — the leader account accepted them all but
    TradeSyncer dropped the TP/SL because they shipped with
    isAutomated=true while the user expected mirror semantics."""

    def _run_bracket_and_capture(self, is_automated: bool) -> dict:
        c = _make_client(is_automated=is_automated)
        with patch.object(c._http, "post",
                          return_value=_fake_response(body={
                              "orderId": 1000,
                              "oso1Id": 1001, "oso2Id": 1002,
                          })) as post:
            c.place_bracket(
                tradovate_symbol="MNQM6", contract_id=4327110,
                entry_action="Buy", entry_qty=1,
                entry_order_type="Market",
                brackets=[
                    {"action": "Sell", "order_type": "Limit",
                     "limit_price": 29292.0, "tif": "GTC"},
                    {"action": "Sell", "order_type": "Stop",
                     "stop_price":  28942.0, "tif": "GTC"},
                ],
            )
        return post.call_args.kwargs["json"]

    def test_entry_carries_configured_flag_default(self):
        payload = self._run_bracket_and_capture(is_automated=False)
        self.assertFalse(payload["isAutomated"])

    def test_both_children_carry_same_flag_as_entry_default(self):
        payload = self._run_bracket_and_capture(is_automated=False)
        self.assertFalse(payload["bracket1"]["isAutomated"],
                         "bracket1 (TP) was the silent-failure path in the "
                         "field report — its isAutomated must follow the "
                         "client setting, not be True hardcoded")
        self.assertFalse(payload["bracket2"]["isAutomated"])

    def test_entry_and_children_all_true_when_opted_in(self):
        payload = self._run_bracket_and_capture(is_automated=True)
        self.assertTrue(payload["isAutomated"])
        self.assertTrue(payload["bracket1"]["isAutomated"])
        self.assertTrue(payload["bracket2"]["isAutomated"])


class TestModifyOrderPayload(unittest.TestCase):
    """Modifies (used when the user drags SL/TP on TradingView) must
    also honour the flag — otherwise a Buy@MKT bracket gets
    broadcasted by TradeSyncer at placement (isAutomated=false) but
    the subsequent stop-loss move ships with isAutomated=true and
    silently doesn't propagate, leaving followers stuck at the
    original stop."""

    def _run_modify_and_capture(self, is_automated: bool) -> dict:
        c = _make_client(is_automated=is_automated)
        with patch.object(c._http, "post",
                          return_value=_fake_response()) as post:
            c.modify_order(
                order_id=11978727757,
                order_type="Stop",
                stop_price=28870.5,
                tif="GTC",
            )
        return post.call_args.kwargs["json"]

    def test_default_false_in_modify(self):
        payload = self._run_modify_and_capture(is_automated=False)
        self.assertFalse(payload["isAutomated"])

    def test_explicit_true_in_modify(self):
        payload = self._run_modify_and_capture(is_automated=True)
        self.assertTrue(payload["isAutomated"])


# ── EnvStore round-trip (GUI persistence layer) ─────────────────────── #

class TestEnvStoreRoundTripIsAutomated(unittest.TestCase):
    """The GUI persists TRADOVATE_IS_AUTOMATED in the shared .env.
    Writing then re-loading must preserve the exact value."""

    def test_value_survives_load_write_load(self):
        from tradesync.ui.app import EnvStore
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            (project_root / ".env").write_text(
                "TRADOVATE_APP_ID=TradeSynchronizer\n"
                "TRADOVATE_APP_VERSION=1.0\n"
                "TRADOVATE_IS_AUTOMATED=true\n"
            )
            (project_root / ".env.live").write_text("TRADOVATE_USERNAME=live-u\n")
            (project_root / ".env.demo").write_text("TRADOVATE_USERNAME=demo-u\n")

            s = EnvStore(project_root)
            s.load()
            self.assertEqual(s.shared.get("TRADOVATE_IS_AUTOMATED"), "true",
                             "loader must surface the value as-stored")

            s.write()  # writes all three files

            s2 = EnvStore(project_root)
            s2.load()
            self.assertEqual(s2.shared.get("TRADOVATE_IS_AUTOMATED"), "true",
                             "value must survive write→reload unchanged")

    def test_absent_key_in_existing_env_gets_default_on_write(self):
        """Upgrading from an older install (no TRADOVATE_IS_AUTOMATED
        in the .env yet): write must emit the canonical default
        of 'false' so the next load surfaces it consistently."""
        from tradesync.ui.app import EnvStore
        with tempfile.TemporaryDirectory() as td:
            project_root = Path(td)
            shared = project_root / ".env"
            shared.write_text(
                "TRADOVATE_APP_ID=TradeSynchronizer\n"
                "TRADOVATE_APP_VERSION=1.0\n"
            )
            (project_root / ".env.live").write_text("TRADOVATE_USERNAME=u\n")
            (project_root / ".env.demo").write_text("TRADOVATE_USERNAME=u\n")
            s = EnvStore(project_root)
            s.load()
            s.write()
            written = shared.read_text()
            self.assertIn("TRADOVATE_IS_AUTOMATED=false", written,
                          "canonical default emitted on first save after "
                          "upgrade so future loads are deterministic")


if __name__ == "__main__":
    unittest.main()
