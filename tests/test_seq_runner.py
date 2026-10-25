"""Tests for the sequential runner + ledger. Stdlib only, no network."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import Ledger  # noqa: E402
from seq_runner import SequentialRunner, classify_error, order_accounts  # noqa: E402


class FakeRunner:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = {}
        self.order = []

    def confirm_previous(self, account):
        return None

    def run_account_tasks(self, account):
        uid = account["uid"]
        self.calls[uid] = self.calls.get(uid, 0) + 1
        self.order.append(uid)
        if uid in self.failures and self.calls[uid] == 1:
            raise RuntimeError(self.failures[uid])
        return {"tasks": [{"name": "checkin", "status": "success",
                           "earned": 0, "error": ""}],
                "balance_before": 100, "balance_after": 130,
                "pending_confirmation": False}


def make_runner(tmp, **config_overrides):
    ledger = Ledger(os.path.join(tmp, "data"))
    config = {"gap_min_seconds": 0, "gap_max_seconds": 0,
              "retry_gap_min_seconds": 0, "retry_gap_max_seconds": 0}
    config.update(config_overrides)
    # Sleeper that records, but never actually sleeps.
    sleeps = []
    runner = SequentialRunner(ledger, config, sleeper=sleeps.append)
    return ledger, runner, sleeps


class TestClassify(unittest.TestCase):
    def test_11140_is_not_retryable(self):
        code, retryable = classify_error("403 code 11140 abnormal")
        self.assertEqual(code, "11140")
        self.assertFalse(retryable)

    def test_11128_is_retryable(self):
        code, retryable = classify_error("code 11128 sampling")
        self.assertEqual(code, "11128")
        self.assertTrue(retryable)

    def test_token_expired_is_not_retryable(self):
        _, retryable = classify_error("token expired 401")
        self.assertFalse(retryable)


class TestSequential(unittest.TestCase):
    def test_main_then_retry_same_morning(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runner, _ = make_runner(tmp)
            accounts = [
                {"uid": "a-cn", "realm": "cn"},
                {"uid": "b-intl", "realm": "intl"},
            ]
            fake = FakeRunner(failures={"a-cn": "network timeout"})
            records = runner.run_day(accounts, fake, date_str="2026-10-26")
            phases = [(r["phase"], r["account_uid"], r["status"])
                      for r in records]
            # Main pass: both accounts, in some order. Retry pass:
            # only the failed account, once, and it succeeds.
            self.assertEqual(len(records), 3)
            self.assertEqual(phases[-1], ("retry", "a-cn", "success"))
            self.assertEqual(fake.calls["a-cn"], 2)
            self.assertEqual(fake.calls["b-intl"], 1)

    def test_restricted_account_is_skipped_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runner, _ = make_runner(tmp)
            accounts = [{"uid": "r1", "realm": "cn",
                         "restricted_until": 9999999999.0}]
            fake = FakeRunner()
            records = runner.run_day(accounts, fake, date_str="2026-10-26")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "skipped")
            self.assertEqual(fake.calls, {})

    def test_11140_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runner, _ = make_runner(tmp)
            accounts = [{"uid": "x", "realm": "intl"}]
            fake = FakeRunner(failures={"x": "11140 restricted"})
            records = runner.run_day(accounts, fake, date_str="2026-10-26")
            self.assertEqual(len(records), 1)
            self.assertEqual(fake.calls["x"], 1)

    def test_ledger_snapshot_and_retry_do_not_overwrite_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runner, _ = make_runner(tmp)
            accounts = [{"uid": "a-cn", "realm": "cn"}]
            fake = FakeRunner(failures={"a-cn": "timeout"})
            runner.run_day(accounts, fake, date_str="2026-10-26")
            runs = ledger.recent_runs()
            self.assertEqual(len(runs), 2)
            self.assertEqual(runs[0]["attempt"], 1)
            self.assertEqual(runs[1]["attempt"], 2)
            snap = ledger.snapshot["accounts"]["a-cn"]
            self.assertEqual(snap["last_status"], "success")
            self.assertEqual(snap["last_phase"], "retry")

    def test_expiring_accounts_go_first(self):
        accounts = [
            {"uid": "late", "expiry_days": 30},
            {"uid": "urgent", "expiry_days": 3},
        ]
        ordered = order_accounts(accounts, "2026-10-26")
        self.assertEqual(ordered[0]["uid"], "urgent")


if __name__ == "__main__":
    unittest.main()
