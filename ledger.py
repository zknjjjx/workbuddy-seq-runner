"""Per-account ledger for the sequential WorkBuddy task runner.

Design notes (from the discussion that shaped this project):

* One account at a time, in one early-morning pass, then a separate
  retry pass later the same morning. Running tasks and consuming
  points are two separate lines: this runner never takes the proxy
  dispatch lock and never pauses because an account is being used
  for API calls. The only skip that is *not* a consumption lock is
  an upstream restriction (``restricted_until`` / 11140): retrying a
  restricted account only risks the rest of the pool.
* Some rewards arrive the next day (for example the international
  daily-activity reward). A run therefore has two phases:
  ``submitted`` at run time and ``confirmed`` / ``unconfirmed``
  when the next run re-checks. Balance delta alone cannot confirm a
  next-day reward, because daytime consumption moves the same
  balance, so confirmation must come from the task's own status
  (see ``hub_adapter.confirm_previous``), never from balance alone.
* The ledger is append-only (``ledger.jsonl``) plus a snapshot
  (``snapshot.json``) that the hub panel can render. A retry never
  overwrites the main run: it appends a second record for the same
  account and day with ``attempt=2``.
"""

import json
import os
import threading


def mask_uid(uid):
    """Show only enough of an id for a human to tell accounts apart."""
    uid = str(uid or "")
    if len(uid) <= 8:
        return uid
    return uid[:4] + "..." + uid[-4:]


class Ledger:
    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.jsonl_path = os.path.join(directory, "ledger.jsonl")
        self.snapshot_path = os.path.join(directory, "snapshot.json")
        self._lock = threading.Lock()
        self.snapshot = self._load_snapshot()

    def _load_snapshot(self):
        try:
            with open(self.snapshot_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"accounts": {}}

    def _save_snapshot(self):
        tmp = self.snapshot_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.snapshot, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.snapshot_path)

    def append(self, record):
        """Append one account run and update that account's snapshot."""
        with self._lock:
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            uid = record["account_uid"]
            snap = self.snapshot["accounts"].setdefault(uid, {
                "account_uid": uid,
                "realm": record.get("realm", ""),
                "display": record.get("display", mask_uid(uid)),
                "last_date": "",
                "last_status": "",
                "last_phase": "",
                "last_attempt": 0,
                "consecutive_failures": 0,
                "last_balance": None,
                "last_delta": None,
                "pending_confirmation": False,
                "next_action": "",
            })
            snap["realm"] = record.get("realm", snap["realm"])
            snap["display"] = record.get("display", snap["display"])
            snap["last_date"] = record.get("date", snap["last_date"])
            snap["last_status"] = record.get("status", snap["last_status"])
            snap["last_phase"] = record.get("phase", snap["last_phase"])
            snap["last_attempt"] = record.get("attempt", snap["last_attempt"])
            snap["last_balance"] = record.get("balance_after", snap["last_balance"])
            snap["last_delta"] = record.get("delta", snap["last_delta"])
            snap["pending_confirmation"] = bool(record.get("pending_confirmation"))
            snap["next_action"] = record.get("next_action", "")
            if record.get("status") == "failed":
                snap["consecutive_failures"] = snap.get("consecutive_failures", 0) + 1
            elif record.get("status") in ("success", "partial"):
                snap["consecutive_failures"] = 0
            self._save_snapshot()
        return record

    def recent_runs(self, limit=50):
        try:
            with open(self.jsonl_path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return []
        runs = []
        for line in lines[-limit:]:
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
        return runs

    def panel_json(self):
        """Shape the panel fragment in dashboard_fragment.html reads."""
        accounts = sorted(
            self.snapshot["accounts"].values(),
            key=lambda a: (a.get("last_date", ""), a.get("display", "")),
        )
        return {
            "accounts": accounts,
            "recent": self.recent_runs(limit=50),
        }
