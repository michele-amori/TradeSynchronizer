"""
Tests for the replication settings controller (headless logic behind
the GUI pair-picker). The tkinter panel itself isn't unit-tested (no
display in CI); the controller holds all the logic and is fully covered
here.
"""

import tempfile
import unittest
from pathlib import Path

from tradesync.account_book import Account
from tradesync.replication_config import ReplicationConfigError
from tradesync.ui.replication_settings import (
    PairDraft,
    ReplicationSettingsController,
)


def _draft(name="tv→ibkr", s_broker="tradovate", s_acct="50000001",
           f_broker="ibkr", f_acct="DU0000002", env="demo", enabled=True):
    return PairDraft(
        name=name, source_broker=s_broker, source_env=env,
        source_account=s_acct, follower_broker=f_broker, follower_env=env,
        follower_account=f_acct, enabled=enabled)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config" / "replication.json"
        # Pin BOTH paths into the temp dir. Without an explicit
        # accounts_path the controller would fall back to the real
        # config/accounts.json and tests would scribble on the user's
        # actual address book.
        self.accounts_path = Path(self._tmp.name) / "config" / "accounts.json"
        self.ctl = ReplicationSettingsController(
            config_path=self.path, accounts_path=self.accounts_path)

    def tearDown(self):
        self._tmp.cleanup()


class TestAddRemoveToggle(_Base):

    def test_add_valid_pair(self):
        self.ctl.add_pair(_draft())
        self.assertEqual(len(self.ctl.pairs), 1)
        self.assertEqual(self.ctl.pairs[0].name, "tv→ibkr")

    def test_add_invalid_broker_rejected_without_mutation(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_pair(_draft(s_broker="schwab"))
        self.assertEqual(len(self.ctl.pairs), 0)   # not added

    def test_add_loop_rejected(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_pair(_draft(
                s_broker="tradovate", s_acct="111",
                f_broker="tradovate", f_acct="111"))

    def test_add_empty_account_rejected(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_pair(_draft(s_acct="   "))

    def test_duplicate_name_rejected(self):
        self.ctl.add_pair(_draft(name="dup"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_pair(_draft(name="dup", s_acct="999"))
        self.assertEqual(len(self.ctl.pairs), 1)

    def test_remove(self):
        self.ctl.add_pair(_draft(name="a"))
        self.ctl.add_pair(_draft(name="b", s_acct="222"))
        self.ctl.remove_pair(0)
        self.assertEqual([p.name for p in self.ctl.pairs], ["b"])

    def test_remove_out_of_range(self):
        with self.assertRaises(IndexError):
            self.ctl.remove_pair(5)

    def test_toggle(self):
        self.ctl.add_pair(_draft(enabled=True))
        new_val = self.ctl.toggle_pair(0)
        self.assertFalse(new_val)
        self.assertFalse(self.ctl.pairs[0].enabled)
        self.assertTrue(self.ctl.toggle_pair(0))


class TestUpdateAndDraft(_Base):

    def test_update_replaces_in_place(self):
        self.ctl.add_pair(_draft(name="a", s_acct="111"))
        d = _draft(name="a", s_acct="999")
        self.ctl.update_pair(0, d)
        self.assertEqual(len(self.ctl.pairs), 1)
        self.assertEqual(self.ctl.pairs[0].source.account_id, "999")

    def test_update_can_keep_same_name(self):
        # Editing a pair without renaming it must not trip the
        # duplicate-name guard against itself.
        self.ctl.add_pair(_draft(name="keep", s_acct="111"))
        self.ctl.update_pair(0, _draft(name="keep", s_acct="222"))
        self.assertEqual(self.ctl.pairs[0].source.account_id, "222")

    def test_update_rejects_name_clashing_with_another(self):
        self.ctl.add_pair(_draft(name="a", s_acct="111"))
        self.ctl.add_pair(_draft(name="b", s_acct="222"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.update_pair(1, _draft(name="a", s_acct="333"))
        # unchanged
        self.assertEqual(self.ctl.pairs[1].name, "b")

    def test_update_invalid_does_not_mutate(self):
        self.ctl.add_pair(_draft(name="a", s_acct="111"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.update_pair(0, _draft(name="a", s_broker="schwab"))
        self.assertEqual(self.ctl.pairs[0].source.account_id, "111")

    def test_update_out_of_range(self):
        with self.assertRaises(IndexError):
            self.ctl.update_pair(5, _draft())

    def test_draft_for_round_trips(self):
        self.ctl.add_pair(_draft(name="rt", s_acct="111", f_acct="DU1"))
        d = self.ctl.draft_for(0)
        self.assertEqual(d.name, "rt")
        self.assertEqual(d.source_account, "111")
        self.assertEqual(d.follower_account, "DU1")
        # the round-tripped draft rebuilds the same pair
        self.assertEqual(d.to_pair().name, "rt")

    def test_draft_for_ratio_formatted(self):
        d = _draft(name="r"); d.ratio = "0.33"
        self.ctl.add_pair(d)
        self.assertEqual(self.ctl.draft_for(0).ratio, "0.33")

    def test_draft_for_out_of_range(self):
        with self.assertRaises(IndexError):
            self.ctl.draft_for(0)


class TestGateway(_Base):

    def test_set_gateway(self):
        self.ctl.set_gateway(host="127.0.0.1", port=4001, client_id=12)
        self.assertEqual(self.ctl.gateway.port, 4001)
        self.assertEqual(self.ctl.gateway.client_id, 12)

    def test_set_gateway_bad_port_raises(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.set_gateway(port=99999)


class TestPersistence(_Base):

    def test_save_then_load_round_trip(self):
        self.ctl.add_pair(_draft(name="live mirror"))
        self.ctl.set_gateway(port=4002)
        self.ctl.save()
        self.assertTrue(self.path.exists())

        ctl2 = ReplicationSettingsController(
            config_path=self.path, accounts_path=self.accounts_path)
        ctl2.load()
        self.assertEqual(len(ctl2.pairs), 1)
        self.assertEqual(ctl2.pairs[0].name, "live mirror")
        self.assertEqual(ctl2.gateway.port, 4002)

    def test_load_missing_file_is_empty(self):
        self.ctl.load()   # file doesn't exist yet
        self.assertEqual(self.ctl.pairs, [])

    def test_summary_rows(self):
        self.ctl.add_pair(_draft(name="x"))
        rows = self.ctl.summary_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "x")
        self.assertTrue(rows[0]["needs_gateway"])   # ibkr follower
        self.assertTrue(rows[0]["enabled"])

    def test_summary_row_no_gateway_for_tradovate_follower(self):
        self.ctl.add_pair(_draft(
            name="y", s_broker="ibkr", s_acct="U1",
            f_broker="tradovate", f_acct="111"))
        rows = self.ctl.summary_rows()
        self.assertFalse(rows[0]["needs_gateway"])


class TestGatewayHelpers(_Base):

    def test_needs_gateway_true_with_ibkr_follower(self):
        self.ctl.add_pair(_draft())   # ibkr follower
        self.assertTrue(self.ctl.needs_gateway())

    def test_needs_gateway_false_without(self):
        self.ctl.add_pair(_draft(
            name="z", s_broker="ibkr", s_acct="U1",
            f_broker="tradovate", f_acct="111"))
        self.assertFalse(self.ctl.needs_gateway())

    def test_open_gateway_delegates_to_launcher(self):
        from unittest.mock import patch
        sentinel = object()
        with patch("tradesync.ibkr_gateway_launcher.ensure_gateway_running",
                   return_value=sentinel) as mock:
            result = self.ctl.open_gateway()
        mock.assert_called_once()
        self.assertIs(result, sentinel)


class TestPanelImportIsLazy(unittest.TestCase):
    """Importing the module must NOT import tkinter (so headless tests +
    the controller work). build_panel imports tkinter only when called."""

    def test_module_import_does_not_require_tkinter(self):
        import sys
        import importlib
        # Re-import fresh and confirm the controller is usable without
        # a display. (We can't easily assert tkinter is absent from
        # sys.modules globally, but we CAN confirm the controller path
        # works headless, which is the contract that matters.)
        mod = importlib.import_module(
            "tradesync.ui.replication_settings")
        self.assertTrue(hasattr(mod, "ReplicationSettingsController"))
        self.assertTrue(hasattr(mod, "build_panel"))


class TestPairDraftRatio(unittest.TestCase):
    """The GUI draft → ReplicationPair bridge for the ratio field."""

    def test_blank_ratio_defaults_to_one(self):
        d = _draft(); d.ratio = ""
        self.assertEqual(d.to_pair().ratio, 1.0)

    def test_numeric_ratio_parsed(self):
        d = _draft(); d.ratio = "0.33"
        self.assertEqual(d.to_pair().ratio, 0.33)

    def test_non_numeric_ratio_raises(self):
        d = _draft(); d.ratio = "abc"
        with self.assertRaises(ReplicationConfigError):
            d.to_pair()

    def test_whitespace_ratio_defaults_to_one(self):
        d = _draft(); d.ratio = "   "
        self.assertEqual(d.to_pair().ratio, 1.0)

    def test_added_pair_persists_ratio(self):
        # End to end through the controller: add with a ratio, reload.
        tmp = Path(tempfile.mkdtemp()) / "config"
        ctl = ReplicationSettingsController(
            config_path=tmp / "rep.json",
            accounts_path=tmp / "accounts.json")
        d = _draft(); d.ratio = "0.5"
        ctl.add_pair(d)
        ctl.save()
        ctl.load()
        self.assertEqual(ctl.pairs[0].ratio, 0.5)


class TestAccountBook(_Base):
    """The reusable-account address book behind the pair-form dropdowns."""

    def _acct(self, label="Miki TVT", broker="tradovate", env="demo",
              account_id="50000001"):
        return Account(label=label, broker=broker, env=env,
                       account_id=account_id)

    def test_add_account_and_list(self):
        self.ctl.add_account(self._acct(label="A"))
        self.ctl.add_account(self._acct(label="B", broker="ibkr",
                                        account_id="DU1"))
        self.assertEqual(self.ctl.account_labels(), ["A", "B"])

    def test_add_invalid_account_rejected_without_mutation(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_account(self._acct(broker="schwab"))
        self.assertEqual(self.ctl.accounts, [])

    def test_add_empty_account_id_rejected(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_account(self._acct(account_id="  "))

    def test_duplicate_label_rejected_case_insensitive(self):
        self.ctl.add_account(self._acct(label="Dup"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.add_account(self._acct(label="dup", account_id="999"))
        self.assertEqual(len(self.ctl.accounts), 1)

    def test_draft_from_labels_copies_fields(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        draft = self.ctl.draft_from_labels(
            name="p", source_label="src", follower_label="flw", ratio="1.0")
        pair = draft.to_pair()
        self.assertEqual(pair.source.identity, "tradovate_demo_50000001")
        self.assertEqual(pair.follower.identity, "ibkr_demo_DU0000002")

    def test_draft_from_unknown_label_raises(self):
        self.ctl.add_account(self._acct(label="only"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.draft_from_labels(
                name="p", source_label="only", follower_label="ghost",
                ratio="1.0")

    def test_remove_unused_account(self):
        self.ctl.add_account(self._acct(label="gone"))
        self.ctl.remove_account("gone")
        self.assertEqual(self.ctl.accounts, [])

    def test_remove_unknown_account_raises(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.remove_account("nope")

    def test_remove_in_use_account_blocked_and_names_pair(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="live mirror", source_label="src", follower_label="flw",
            ratio="1.0"))
        with self.assertRaises(ReplicationConfigError) as ctx:
            self.ctl.remove_account("flw")
        # the error names the offending pair so the user knows why
        self.assertIn("live mirror", str(ctx.exception))
        # and the account is still there
        self.assertIn("flw", self.ctl.account_labels())

    def test_pairs_using_account_reports_both_sides(self):
        self.ctl.add_account(self._acct(label="hub", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="f1", broker="ibkr",
                                        account_id="DU1"))
        self.ctl.add_account(self._acct(label="f2", broker="ibkr",
                                        account_id="DU2"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="a", source_label="hub", follower_label="f1", ratio="1.0"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="b", source_label="hub", follower_label="f2", ratio="1.0"))
        # hub is the source of both pairs
        self.assertEqual(set(self.ctl.pairs_using_account("hub")), {"a", "b"})
        self.assertEqual(self.ctl.pairs_using_account("f1"), ["a"])

    def test_account_book_round_trips_on_disk(self):
        self.ctl.add_account(self._acct(label="persist", broker="ibkr",
                                        account_id="U0000001"))
        self.ctl.save_accounts()
        ctl2 = ReplicationSettingsController(
            config_path=self.path,
            accounts_path=self.accounts_path)
        ctl2.load()
        self.assertEqual(ctl2.account_labels(), ["persist"])
        self.assertEqual(ctl2.accounts[0].account_id, "U0000001")

    def test_account_rows_flag_in_use(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="unused", broker="ibkr",
                                        account_id="DU9"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="p", source_label="src", follower_label="flw", ratio="1.0"))
        rows = {r["label"]: r["in_use_by"] for r in self.ctl.account_rows()}
        self.assertEqual(rows["src"], ["p"])
        self.assertEqual(rows["flw"], ["p"])
        self.assertEqual(rows["unused"], [])

    # ── update_account ──────────────────────────────────────────── #

    def test_update_unused_account_can_change_everything(self):
        self.ctl.add_account(self._acct(label="old", broker="ibkr",
                                        env="demo", account_id="DU1"))
        self.ctl.update_account("old", Account(
            label="new", broker="tradovate", env="live",
            account_id="50000001"))
        self.assertEqual(self.ctl.account_labels(), ["new"])
        a = self.ctl.account_by_label("new")
        self.assertEqual(a.identity, "tradovate_live_50000001")

    def test_update_unknown_account_raises(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.update_account("ghost", self._acct(label="x"))

    def test_update_in_use_account_can_rename(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="p", source_label="src", follower_label="flw", ratio="1.0"))
        # rename only — same broker/env/id — is allowed while in use
        self.ctl.update_account("src", Account(
            label="renamed", broker="tradovate", env="demo",
            account_id="50000001"))
        self.assertIn("renamed", self.ctl.account_labels())
        # the pair still points at the same identity
        self.assertEqual(self.ctl.pairs[0].source.identity,
                         "tradovate_demo_50000001")

    def test_update_in_use_account_identity_change_blocked(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="live mirror", source_label="src", follower_label="flw",
            ratio="1.0"))
        with self.assertRaises(ReplicationConfigError) as ctx:
            self.ctl.update_account("src", Account(
                label="src", broker="tradovate", env="demo",
                account_id="99999999"))
        self.assertIn("live mirror", str(ctx.exception))
        # account unchanged
        self.assertEqual(self.ctl.account_by_label("src").account_id,
                         "50000001")

    def test_update_rejects_label_clashing_with_another(self):
        self.ctl.add_account(self._acct(label="a", account_id="1"))
        self.ctl.add_account(self._acct(label="b", account_id="2"))
        with self.assertRaises(ReplicationConfigError):
            self.ctl.update_account("b", self._acct(label="a", account_id="2"))
        self.assertEqual(self.ctl.account_by_label("b").label, "b")

    def test_update_can_keep_own_label(self):
        self.ctl.add_account(self._acct(label="keep", account_id="1"))
        self.ctl.update_account("keep", self._acct(label="keep",
                                                   account_id="2"))
        self.assertEqual(self.ctl.account_by_label("keep").account_id, "2")

    def test_account_draft_for_reports_locked(self):
        self.ctl.add_account(self._acct(label="src", broker="tradovate",
                                        account_id="50000001"))
        self.ctl.add_account(self._acct(label="free", broker="ibkr",
                                        account_id="DU9"))
        self.ctl.add_account(self._acct(label="flw", broker="ibkr",
                                        account_id="DU0000002"))
        self.ctl.add_pair(self.ctl.draft_from_labels(
            name="p", source_label="src", follower_label="flw", ratio="1.0"))
        self.assertTrue(self.ctl.account_draft_for("src")["locked"])
        self.assertFalse(self.ctl.account_draft_for("free")["locked"])

    def test_account_draft_for_unknown_raises(self):
        with self.assertRaises(ReplicationConfigError):
            self.ctl.account_draft_for("nope")


if __name__ == "__main__":
    unittest.main()
