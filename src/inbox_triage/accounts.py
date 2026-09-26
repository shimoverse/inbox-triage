"""Per-account settings, schedule, and run history shared by the CLI and web app."""
from __future__ import annotations

import calendar
import json
import os
import time
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import preferences as prefs_mod
from .gmail.client import write_private

FREQUENCIES = ("off", "hourly", "daily", "weekly", "monthly")
DEFAULT_SETTINGS = {"model": "", "dry_run": False, "onboarded": False,
                    "schedule": {"frequency": "off", "hour": 7, "weekday": 0, "anchor": 0, "tz": ""}}
MAX_BATCHES = 20  # 20 x 100 messages per run keeps a 30-day backfill bounded
MAX_RESUMES = 6  # automatic pick-ups of a run Gmail paused, before waiting for the schedule or a click


def account_dir(root: Path, account: str) -> Path:
    from .runner import scoped_directory
    directory = scoped_directory(root.expanduser(), account)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


class Account:
    def __init__(self, state_root: Path, email: str):
        self.email = email.casefold()
        self.dir = account_dir(state_root, self.email)

    # -- settings ---------------------------------------------------------
    def settings(self) -> dict:
        try:
            saved = json.loads((self.dir / "settings.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        merged = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
        merged["schedule"] = {**DEFAULT_SETTINGS["schedule"], **(saved.get("schedule") or {})}
        return merged

    def update_settings(self, changes: dict, now: int | None = None) -> dict:
        current = self.settings()
        for key in ("model",):
            if key in changes:
                current[key] = str(changes[key] or "")[:120]
        for key in ("dry_run", "onboarded"):
            if key in changes:
                current[key] = bool(changes[key])
        if "schedule" in changes:
            sched = {**current["schedule"], **(changes["schedule"] or {})}
            if sched.get("frequency") not in FREQUENCIES:
                raise ValueError("Unknown schedule frequency")
            sched["hour"] = min(23, max(0, int(sched.get("hour", 7))))
            sched["weekday"] = min(6, max(0, int(sched.get("weekday", 0))))
            # The IANA zone the person picked the hour in (from their browser); unknown zones fall back to server time.
            name = str(sched.get("tz") or "")[:64]
            sched["tz"] = name if schedule_zone({"tz": name}) else ""
            # Only slots after this moment count, so saving a schedule never fires a missed past slot.
            sched["anchor"] = int(now if now is not None else time.time())
            current["schedule"] = sched
        write_private(self.dir / "settings.json", json.dumps(current, sort_keys=True))
        return current

    # -- Jev key (per account, so a hosted server never pays for users' Jev) --
    def jev_key(self) -> str:
        try:
            return str(json.loads((self.dir / "secrets.json").read_text(encoding="utf-8")).get("TYPESAFE_API_KEY", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""

    def save_jev_key(self, key: str) -> None:
        write_private(self.dir / "secrets.json", json.dumps({"TYPESAFE_API_KEY": key} if key else {}))

    # -- preferences ------------------------------------------------------
    def preferences(self) -> prefs_mod.Preferences:
        return prefs_mod.load(self.dir / "preferences.json")

    def save_preferences(self, prefs: prefs_mod.Preferences) -> None:
        write_private(self.dir / "preferences.json", json.dumps(prefs.to_json(), sort_keys=True))

    # -- run history ------------------------------------------------------
    def record_run(self, entry: dict) -> None:
        path = self.dir / "runs.jsonl"
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as file:
            file.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")

    def runs(self, limit: int | None = 50) -> list[dict]:
        """Newest first; ``limit=None`` returns them all."""
        path = self.dir / "runs.jsonl"
        if not path.exists():
            return []
        out = []
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[-limit:] if limit else lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))

    def last_scheduled_run(self) -> int:
        return max((int(r.get("started", 0)) for r in self.runs(500) if r.get("trigger") == "schedule"), default=0)

    def decisions(self, limit: int = 50) -> list[dict]:
        """Latest verified label decisions (IDs and labels only; no message content is stored)."""
        from .runner import load_events
        events = [e for e in load_events(self.dir / "events.jsonl").values() if e.get("status") == "verified"]
        events.sort(key=lambda e: int(e.get("ts", 0)), reverse=True)
        return events[:limit]

    def pending_resume(self) -> dict | None:
        """The latest run, if Gmail paused it and it will be picked up again automatically.
        A preview isn't: it keeps no progress, so resuming would only repeat the same Jev calls."""
        runs = self.runs(None)
        if not runs or runs[0].get("status") != "paused" or not runs[0].get("resume_at") \
                or runs[0].get("mode") == "dry-run":
            return None
        resumes = 0
        for run in runs:  # every pause since the last finished run; clicking Run now doesn't use up retries
            if run.get("status") != "paused":
                break
            resumes += run.get("trigger") == "resume"
        return runs[0] if resumes < MAX_RESUMES else None

    def due_run(self, now: int | None = None, tz: tzinfo | None = None) -> tuple[str, dict | None] | None:
        """What the scheduler should start now: ("resume", paused run) once the break Gmail asked
        for is over, ("schedule", None) when the schedule is due, or None. Nothing starts while a
        break is pending, not even the schedule: the mailbox gets the rest Gmail asked for, and
        the paused run (with its own window) carries on first."""
        now = int(now if now is not None else time.time())
        latest = self.runs(1)
        if latest and latest[0].get("status") == "paused" and int(latest[0].get("resume_at") or 0) > now:
            return None  # even once automatic resumes are used up, the break still holds back the schedule
        paused = self.pending_resume()
        if paused:
            return ("resume", paused)
        return ("schedule", None) if self.is_due(now, tz) else None

    def is_due(self, now: int | None = None, tz: tzinfo | None = None) -> bool:
        now = int(now if now is not None else time.time())
        sched = self.settings()["schedule"]
        slot = latest_slot(sched, now, tz or schedule_zone(sched))
        return slot is not None and slot > max(int(sched.get("anchor", 0)), self.last_scheduled_run())

    def next_run(self, now: int | None = None, tz: tzinfo | None = None) -> int | None:
        now = int(now if now is not None else time.time())
        sched = self.settings()["schedule"]
        if sched["frequency"] == "off":
            return None
        tz = tz or schedule_zone(sched)
        # Walk forward local hour by local hour (at most ~32 days) to the next slot.
        probe = _local(now, tz).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for _ in range(24 * 32):
            ts = int(probe.timestamp())
            if latest_slot(sched, ts, tz) == ts:
                return ts
            probe += timedelta(hours=1)
        return None


def schedule_zone(sched: dict) -> tzinfo | None:
    """The zone a schedule's hours are in: the one it was set in, else None (the server's local time)."""
    name = str(sched.get("tz") or "")
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local(ts: int, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts).astimezone()


def latest_slot(sched: dict, now: int, tz: tzinfo | None = None) -> int | None:
    """The most recent scheduled moment at or before ``now`` (epoch seconds)."""
    freq = sched.get("frequency", "off")
    if freq == "off":
        return None
    local = _local(now, tz)
    hour = int(sched.get("hour", 7))
    if freq == "hourly":
        slot = local.replace(minute=0, second=0, microsecond=0)
    elif freq == "daily":
        slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > local:
            slot -= timedelta(days=1)
    elif freq == "weekly":
        slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        slot -= timedelta(days=(slot.weekday() - int(sched.get("weekday", 0))) % 7)
        if slot > local:
            slot -= timedelta(days=7)
    elif freq == "monthly":  # last day of the month
        def last_day(year: int, month: int) -> datetime:
            return local.replace(year=year, month=month, day=calendar.monthrange(year, month)[1],
                                 hour=hour, minute=0, second=0, microsecond=0)
        slot = last_day(local.year, local.month)
        if slot > local:
            year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
            slot = last_day(year, month)
    else:
        return None
    if freq != "hourly":
        # A daily, weekly or monthly hour that happens twice when clocks go back is one slot: its first occurrence.
        slot = slot.replace(fold=0)
    return int(slot.timestamp())


def run_account(account: str, state_root: Path, *, trigger: str = "manual", days: int | None = None,
                since: int | None = None, runner_kwargs: dict | None = None, now: int | None = None) -> dict:
    """Run triage in batches until the window is done (bounded), and record history.
    A run Gmail throttles is recorded as paused (with when to resume), not as failed.
    A rescan of ``days`` records where its window starts (``since``) so a resume covers the same mail."""
    from . import runner
    started = int(now if now is not None else time.time())
    since = (int(since) if since else started - days * 86400) if days else None
    acct = Account(state_root, account)
    totals: dict = {"processed": 0, "gmail_changes": 0, "jev_calls": 0, "jev_ms": 0, "provider_failures": 0,
                    "gmail_slowdowns": 0, "outcomes": {}}
    entry = {"started": started, "trigger": trigger, "days": days, **({"since": since} if since else {})}

    def add(result: dict) -> None:
        for key in ("processed", "gmail_changes", "jev_calls", "jev_ms", "provider_failures", "gmail_slowdowns"):
            totals[key] += int(result.get(key) or 0)
        for key, value in (result.get("outcomes") or {}).items():
            totals["outcomes"][key] = totals["outcomes"].get(key, 0) + value
        totals.update({k: result[k] for k in ("mode", "remaining") if k in result})

    try:
        for _ in range(MAX_BATCHES):
            result = runner.run(account, root=state_root, since_days=days, since=since, now=now, **(runner_kwargs or {}))
            add(result)
            if result.get("paused_until"):
                # Gmail asked for a break: keep what's done and pick the rest up when it says.
                entry.update(resume_at=int(result["paused_until"]), reason=str(result.get("pause_code") or ""),
                             message=str(result.get("pause_reason") or "")[:300])
                break
            if not result.get("remaining") or result.get("mode") == "dry-run" or not result.get("processed"):
                break
        entry.update(status="paused" if "resume_at" in entry else "ok", **totals)
    except Exception as exc:
        add(getattr(exc, "partial", None) or {})  # what the interrupted batch did still counts
        entry.update(status="error", error=type(exc).__name__, reason=str(getattr(exc, "reason", "") or ""),
                     message=str(exc)[:300], **totals)
        raise
    finally:
        entry["finished"] = int(time.time()) if now is None else started
        acct.record_run(entry)
    return entry
