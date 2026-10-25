"""CLI for the sequential runner.

Modes:
  --dry-run   No WorkBuddy, no hub, no credentials. Runs the fake
              accounts in tests/fake_accounts.py through one main
              pass + retry pass and prints the ledger. This is the
              only mode that is safe (and meaningful) to run from
              this repository alone.
  --hub       Placeholder for the future deployment step: it refuses
              to run unless a hub checkout directory is given and
              its AccountPool can be loaded. It never touches a
              router and never installs anything. The user has
              explicitly deferred installation, so this mode is a
              guarded stub, not a silent half-deployment.

Usage:
  python3 run.py --dry-run [--config config.example.json]
  python3 run.py --hub --hub-dir /path/to/workbuddy2api-hub
"""

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger import Ledger  # noqa: E402
from seq_runner import SequentialRunner  # noqa: E402


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def wait_for_window(config, rng=None):
    """Sleep until a random start inside window_start..window_end.

    Returns the chosen start time as HH:MM. If the window has
    already passed today, it returns immediately (the caller, e.g.
    cron, is responsible for the day); it never silently rolls
    the run to tomorrow.
    """
    rng = rng or random
    start_h, start_m = [int(x) for x in config["window_start"].split(":")]
    end_h, end_m = [int(x) for x in config["window_end"].split(":")]
    start_min = start_h * 60 + start_m
    end_min = end_h * 60 + end_m
    chosen = rng.randint(start_min, end_min)
    now = time.localtime()
    now_min = now.tm_hour * 60 + now.tm_min
    if chosen > now_min:
        time.sleep((chosen - now_min) * 60)
    return "%02d:%02d" % (chosen // 60, chosen % 60)


class FakeTaskRunner:
    """Dry-run task runner: deterministic, no network, no credentials."""

    def __init__(self, failures=None):
        # failures: {uid: error string raised on the MAIN pass only}
        self.failures = failures or {}
        self.calls = {}

    def confirm_previous(self, account):
        return None

    def run_account_tasks(self, account):
        uid = account["uid"]
        self.calls[uid] = self.calls.get(uid, 0) + 1
        if uid in self.failures and self.calls[uid] == 1:
            raise RuntimeError(self.failures[uid])
        return {
            "tasks": [{"name": "checkin", "status": "success",
                       "earned": 0, "error": ""}],
            "balance_before": 1000,
            "balance_after": 1030,
            "pending_confirmation": False,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hub", action="store_true")
    parser.add_argument("--hub-dir", default="")
    parser.add_argument("--wait-for-window", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.hub:
        print("Refusing: --hub mode is a guarded stub in this repository.")
        print("It requires a hub checkout, a real AccountPool, and an")
        print("explicit deployment decision. Installation on the")
        print("router is deferred by the user and is not done here.")
        print("Use --dry-run to exercise the scheduler and ledger.")
        return 2

    if not args.dry_run:
        print("Nothing to do: pass --dry-run (safe) or --hub (stub).")
        return 2

    ledger = Ledger(config.get("ledger_dir", "./data"))
    if args.wait_for_window:
        print("Window start:", wait_for_window(config))
    # Dry-run must never actually sleep through the production gaps
    # (15-40 min between accounts, 1-1.5 h to the retry pass): show
    # the gap that *would* be slept instead.
    runner = SequentialRunner(
        ledger, config,
        sleeper=lambda s: print(f"[dry-run] gap skipped: {s:.0f}s"))
    accounts = [
        {"uid": "demo-cn-1", "display": "demo-cn-1", "realm": "cn",
         "expiry_days": 5},
        {"uid": "demo-intl-1", "display": "demo-intl-1", "realm": "intl",
         "expiry_days": 30},
        {"uid": "demo-cn-2", "display": "demo-cn-2", "realm": "cn",
         "expiry_days": 20},
    ]
    fake = FakeTaskRunner(failures={"demo-cn-2": "network timeout"})
    records = runner.run_day(accounts, fake)
    for rec in records:
        print(rec["phase"], rec["attempt"], rec["display"],
              rec["status"], rec.get("error_code", ""),
              "delta=", rec.get("delta"))
    print("Ledger:", ledger.jsonl_path)
    print("Snapshot:", ledger.snapshot_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
