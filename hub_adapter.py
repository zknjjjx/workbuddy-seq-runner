"""Adapter: run one hub account's tasks through hub's own code.

This module is intentionally thin. It does NOT re-implement any
WorkBuddy endpoint. It calls the same functions hub's scheduler
calls, in the same order hub calls them, but for ONE account in
this runner's early-morning pass instead of across hub's split
hours:

* cn: checkin -> activity report -> growth tasks
  (``wb_tasks.run_growth_tasks``) -> cat travel -> streak bonus
  -> night growth (only counts 23:00-08:00, which the early-morning
  window satisfies; ``wb_tasks`` itself enforces the window).
* intl: daily activity (``Account.daily_chat``), the same call
  hub's ``_run_one_daily_chat`` makes.

Requirements to use it (NOT satisfied by this repository alone,
and deliberately not installed anywhere by this project):

* It must be placed next to a workbuddy2api-hub checkout, so
  ``wb_tasks`` / ``wb_accounts`` are importable, and it must be
  given real ``Account`` objects from hub's ``AccountPool``.
  Those objects carry credentials; this adapter never writes,
  copies or prints them.
* ``Account.fetch_credits`` / ``Account.credits`` are used only
  for the before/after balance on the ledger. If an adapter cannot
  read a balance, it records ``None`` instead of inventing one.

Deploy-time decision, not made here: if this runner takes over
the early-morning pass, hub's own overlapping scheduler hours
(check-in/travel/cat/daily-activity) should be disabled or the
two schedulers will both attempt the same tasks. Hub's ``can_*``
guards make a double attempt mostly a no-op, but "mostly" is not
a deployment plan -- decide it when (and if) this is ever
installed, which the user has explicitly deferred.
"""

import time


def _task_entry(name, result):
    if isinstance(result, dict):
        ok = bool(result.get("ok", True))
        return {
            "name": name,
            "status": "success" if ok else "failed",
            "earned": result.get("earned_credit", 0),
            "error": "" if ok else str(result.get("msg") or result.get("error") or ""),
        }
    return {"name": name, "status": "success", "earned": 0, "error": ""}


class HubTaskRunner:
    """Implements the protocol seq_runner expects, on hub objects."""

    def __init__(self, wb_tasks):
        self.wb_tasks = wb_tasks

    def _balance(self, account):
        try:
            account.fetch_credits()
            credits = getattr(account, "credits", None) or {}
            remain = credits.get("remain")
            return int(remain) if remain is not None else None
        except Exception:
            return None

    def confirm_previous(self, account):
        """Re-check a next-day reward via task status, not balance.

        Returns None when there is nothing pending to confirm
        (the caller treats that as "no pending item"). Hub's
        ``can_daily_chat`` / ``can_checkin`` returning False for
        today is the signal that yesterday's item was recorded;
        a balance comparison is NOT used here, because daytime
        consumption moves the same balance (see ledger.py).
        """
        try:
            if getattr(account, "realm", "") == "intl":
                return {"task": "daily_chat",
                        "done": not account.can_daily_chat()}
            return {"task": "checkin",
                    "done": not account.can_checkin()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_account_tasks(self, account):
        tasks = []
        balance_before = self._balance(account)
        if getattr(account, "realm", "") == "cn":
            if account.can_checkin():
                tasks.append(_task_entry(
                    "checkin", account.checkin(trigger="seq_runner")))
                time.sleep(1.0)
            if account.can_report_activity():
                tasks.append(_task_entry(
                    "report_activity", account.report_activity()))
                time.sleep(1.0)
            growth = self.wb_tasks.run_growth_tasks(account)
            tasks.append(_task_entry("growth_tasks", growth))
            travel = self.wb_tasks.do_cat_travel(account)
            tasks.append(_task_entry("cat_travel", travel))
            bonus = self.wb_tasks.run_streak_bonus(account)
            if isinstance(bonus, dict) and bonus.get("logs"):
                tasks.append({"name": "streak_bonus", "status": "success",
                              "earned": 0, "error": ""})
            night = self.wb_tasks.run_night_growth(account)
            if isinstance(night, dict) and night.get("logs"):
                tasks.append({"name": "night_growth", "status": "success",
                              "earned": 0, "error": ""})
        else:
            if account.can_daily_chat():
                tasks.append(_task_entry(
                    "daily_chat", account.daily_chat(trigger="seq_runner")))
        balance_after = self._balance(account)
        pending = False
        if getattr(account, "realm", "") == "intl":
            # The international daily-activity reward is credited
            # the next day, so a successful run is "submitted",
            # not "confirmed", until the next run's confirm step.
            pending = any(t["name"] == "daily_chat"
                          and t["status"] == "success" for t in tasks)
        return {
            "tasks": tasks,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "pending_confirmation": pending,
        }
