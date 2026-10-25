"""Sequential early-morning WorkBuddy task runner.

This is the part that differs from workbuddy2api-hub's built-in
scheduler, on purpose:

* Hub splits tasks across several fixed hours (check-in 09:00/21:00,
  travel 09:00/21:00, night tasks 01:00, daily activity at random
  slots) and runs the international daily-activity batch with a
  configurable concurrency. This runner instead does ONE pass per
  account, in the early morning, with minutes -- not seconds --
  between accounts.
* There is no batch deadline: if the main queue runs long, the
  remaining accounts still run, in order. (Hub's daily-activity
  batch drops the rest of the batch at its deadline; that is the
  behaviour the user explicitly rejected for this runner.)
* A failed account does not block the queue and is not retried
  immediately. Failures are collected and re-run once, later the
  same morning, in a separate retry pass.
* Running tasks and consuming points are two lines: this module
  never inspects proxy dispatch state and never waits for an
  account to become idle. The only hard skip is an upstream
  restriction, which is a ban-protection rule, not a consumption
  lock.

The task work itself is deliberately behind a small protocol
(``run_account_tasks`` / ``confirm_previous``). In production that
protocol is implemented by ``hub_adapter.py`` on top of hub's
``wb_tasks`` / ``Account`` methods, so this module never re-implements
a WorkBuddy API call and never invents an endpoint. In tests and
in ``run.py --dry-run`` it is implemented by a fake.
"""

import random
import time

# Error codes that may be retried once, later the same morning.
RETRYABLE_CODES = {"timeout", "network", "transient", "11128", "unknown"}
# Error codes that must NOT be retried the same day.
NON_RETRYABLE_CODES = {
    "11140", "restricted", "token_expired", "already_claimed",
}


def classify_error(text):
    """Classify a failure string into a retry decision.

    Returns (code, retryable). Anything unrecognised is treated as
    ``unknown`` and retried once: one extra attempt in the morning
    is cheap, and a persistent unknown failure will simply fail
    the retry too and land in quarantine for the next day.
    """
    t = str(text or "").lower()
    if "11140" in t or "restricted" in t or "abnormal" in t:
        return "11140", False
    if "11128" in t:
        return "11128", True
    if "token" in t and ("expired" in t or "invalid" in t or "401" in t):
        return "token_expired", False
    if "already" in t and "claim" in t:
        return "already_claimed", False
    if "timeout" in t or "timed out" in t:
        return "timeout", True
    if "network" in t or "connection" in t or "dns" in t:
        return "network", True
    if not t:
        return "unknown", True
    return "unknown", True


def order_accounts(accounts, date_str, rng=None):
    """Order the main queue.

    Accounts whose points expire soonest go first (they are the
    ones where a missed day actually costs points). The rest are
    shuffled with a seed derived from the date, so the order is
    not the identical fixed sequence every morning, but is still
    reproducible for one day's ledger.
    """
    rng = rng or random.Random(date_str)
    urgent = [a for a in accounts
              if a.get("expiry_days") is not None and a["expiry_days"] <= 7]
    rest = [a for a in accounts if a not in urgent]
    urgent.sort(key=lambda a: a["expiry_days"])
    rng.shuffle(rest)
    return urgent + rest


class SequentialRunner:
    def __init__(self, ledger, config, sleeper=None, clock=None):
        self.ledger = ledger
        self.config = config
        # sleeper/clock are injectable so tests never actually sleep.
        self.sleep = sleeper or time.sleep
        self.now = clock or time.time

    def _gap(self, lo_key, hi_key):
        lo = float(self.config.get(lo_key, 900))
        hi = float(self.config.get(hi_key, 2400))
        return random.uniform(lo, hi)

    def _run_one(self, account, task_runner, date_str, phase, attempt):
        """Run one account, record it, return the record."""
        from ledger import mask_uid

        uid = account["uid"]
        display = account.get("display") or mask_uid(uid)
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        record = {
            "date": date_str,
            "phase": phase,
            "attempt": attempt,
            "account_uid": uid,
            "display": display,
            "realm": account.get("realm", ""),
            "started": started,
            "tasks": [],
            "balance_before": None,
            "balance_after": None,
            "delta": None,
            "status": "failed",
            "error_code": "",
            "pending_confirmation": False,
            "next_action": "",
        }
        # 1. Upstream restriction: skip, do not probe, do not retry.
        if account.get("restricted_until", 0) > self.now():
            record["status"] = "skipped"
            record["error_code"] = "restricted"
            record["next_action"] = "quarantine: wait out restriction"
            record["ended"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime())
            return self.ledger.append(record)
        # 2. Confirm yesterday's next-day rewards before today's tasks,
        #    using the task's own status, not the balance (see ledger.py).
        try:
            if attempt == 1 and phase == "main":
                pending = task_runner.confirm_previous(account)
                if pending is not None:
                    record["previous_pending"] = pending
        except Exception as exc:  # confirmation must not kill today's run
            record["previous_pending"] = {"ok": False, "error": str(exc)}
        # 3. The account's own task pass (hub_adapter or a fake).
        try:
            result = task_runner.run_account_tasks(account)
        except Exception as exc:
            code, retryable = classify_error(str(exc))
            record["error_code"] = code
            record["next_action"] = ("retry" if retryable
                                     else "quarantine: manual review")
            record["ended"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime())
            return self.ledger.append(record)
        record["tasks"] = result.get("tasks", [])
        record["balance_before"] = result.get("balance_before")
        record["balance_after"] = result.get("balance_after")
        if (record["balance_before"] is not None
                and record["balance_after"] is not None):
            record["delta"] = record["balance_after"] - record["balance_before"]
        record["pending_confirmation"] = bool(result.get("pending_confirmation"))
        failed = [t for t in record["tasks"] if t.get("status") == "failed"]
        if not record["tasks"]:
            record["status"] = "skipped"
            record["next_action"] = "nothing due"
        elif not failed:
            record["status"] = "success"
        else:
            record["status"] = "partial"
            first = failed[0]
            code, retryable = classify_error(first.get("error", ""))
            record["error_code"] = code
            record["next_action"] = ("retry" if retryable
                                     else "quarantine: manual review")
        if record["status"] == "failed":
            # run_account_tasks returned normally but reported failure
            # at the top level (kept for adapters that do that).
            record["next_action"] = "retry"
        record["ended"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return self.ledger.append(record)

    def run_day(self, accounts, task_runner, date_str=None):
        """Main pass, then (later the same morning) one retry pass.

        Returns the list of records, main pass first, retry pass last.
        """
        date_str = date_str or time.strftime("%Y-%m-%d", time.localtime())
        ordered = order_accounts(list(accounts), date_str)
        records = []
        failed_for_retry = []
        for index, account in enumerate(ordered):
            if index > 0:
                # Minutes between accounts, randomised. This is the
                # deliberate difference from hub's 2-5 second gap.
                self.sleep(self._gap("gap_min_seconds", "gap_max_seconds"))
            rec = self._run_one(account, task_runner, date_str, "main", 1)
            records.append(rec)
            if rec.get("next_action") == "retry":
                failed_for_retry.append(account)
        # Retry pass: same morning, after its own gap, at most once
        # per account, only for accounts the main pass marked retry.
        if failed_for_retry:
            self.sleep(self._gap("retry_gap_min_seconds",
                                 "retry_gap_max_seconds"))
            for index, account in enumerate(failed_for_retry):
                if index > 0:
                    self.sleep(self._gap("gap_min_seconds",
                                         "gap_max_seconds"))
                rec = self._run_one(account, task_runner, date_str,
                                    "retry", 2)
                records.append(rec)
        return records
