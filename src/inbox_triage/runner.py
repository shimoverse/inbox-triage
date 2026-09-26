"""Account-isolated, label-only Gmail triage without a frontier orchestrator."""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from . import preferences
from .context import bootstrap_context, build_context, sync_incremental
from .gmail.client import GmailClient, GmailError, RateLimited
from .gmail.extract import extract_gmail_message
from .policy import decide, junk_decision
from .providers import ProviderError, make_provider
from .store import TriageStore

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

JUNK_LABEL = "Triage/Junk"
ATTENTION = {"needs_you": "Triage/Needs You", "updates": "Triage/Updates",
             "for_you": "Triage/For You", "later": "Triage/Later", "junk": JUNK_LABEL}
TOPICS = {"shopping": "Topics/Shopping"}
LABELS = tuple(ATTENTION.values()) + tuple(TOPICS.values())
EXCLUDED = {"SENT", "DRAFT", "SPAM", "TRASH"}
MAX_PER_RUN = 100
MAX_ATTEMPTS = 3            # a message that fails this often is left unchanged for good
MAX_CONSECUTIVE_FAILURES = 5  # this many in a row means the provider is down: stop the run
MAX_WINDOW_DAYS = 90
JOURNAL_RETENTION = (MAX_WINDOW_DAYS + 5) * 86400
from .config import CONFIG_DIR, STATE_DIR  # noqa: E402
# Kept as a module attribute so tests can substitute a fake client.
GmailReadOnlyClient = GmailClient


def default_token(account: str, config_dir: Path = CONFIG_DIR) -> Path:
    return config_dir.expanduser() / "tokens" / f"{account.casefold()}.json"


def discover_accounts(config_dir: Path = CONFIG_DIR) -> list[str]:
    tokens = config_dir.expanduser() / "tokens"
    return sorted(p.stem for p in tokens.glob("*@*.json")) if tokens.is_dir() else []


def scoped_directory(root: Path, account: str) -> Path:
    if not account or "@" not in account:
        raise ValueError("Provide a valid account email")
    return root / hashlib.sha256(account.casefold().encode()).hexdigest()[:24]


def scan_query(cursor: int, launch: int, since: int | None = None) -> str:
    start = since if since is not None else max(launch, cursor - 2 * 86400)
    return f"after:{max(0, start)} -in:sent -in:drafts -in:spam -in:trash"


def list_ids(client: GmailClient, query: str) -> list[str]:
    return client.list_ids(query)


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, separators=(",", ":")) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(path, 0o600)


def load_events(path: Path) -> dict[str, dict]:
    events = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                events[str(event["id"])] = event
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # a crash can leave one partial line; never let it block the account
    return events


def compact_events(path: Path, events: dict[str, dict], now: int, retention: int = JOURNAL_RETENTION) -> dict[str, dict]:
    """Keep one line per message and forget messages older than the scan window can reach."""
    keep = {mid: e for mid, e in events.items() if now - int(e.get("ts", now)) < retention}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        for event in keep.values():
            file.write(json.dumps(event, separators=(",", ":")) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    return keep


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(state, file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def account_lock(path: Path):
    with path.open("a+") as lock:
        try:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            raise RuntimeError("Account triage is already running") from None
        yield


def ensure_labels(client: GmailClient, *extra: str) -> dict[str, str]:
    """Create the standard labels (and ``extra`` ones, e.g. Junk once someone has a Junk rule);
    return every Inbox Triage label that exists, so stale ones can still be removed."""
    def current() -> dict[str, str]:
        # Gmail label names are case-insensitive; match an existing "triage/later" too.
        return {x["name"].casefold(): x["id"] for x in client.labels()}
    required = [name for name in LABELS if name != JUNK_LABEL] + list(extra)
    before = current()
    for name in required:
        if name.casefold() not in before:
            client.create_label(name)
    after = current()
    if not all(name.casefold() in after for name in required):
        raise RuntimeError("Gmail label creation readback failed")
    return {name: after[name.casefold()] for name in LABELS if name.casefold() in after}


def apply_labels(client: GmailClient, mid: str, desired: set[str], labels: dict[str, str]) -> bool:
    old = client.message_labels(mid)
    owned = set(labels.values())
    target = {labels[name] for name in desired}
    add, remove = sorted(target - old), sorted((old & owned) - target)
    if not add and not remove:
        return False  # already right: nothing to write or read back
    response = client.modify_labels(mid, add, remove)
    # Gmail answers a change with the labels as they now are; read them again only if it didn't say.
    after = response.get("labelIds") if isinstance(response, dict) else None
    actual = set(after) if after is not None else client.message_labels(mid)
    if actual & owned != target or (old - owned) - actual:
        raise RuntimeError("Gmail label readback mismatch")
    return True


def desired_names(decision) -> set[str]:
    # A spam prediction is deliberately NOT a Gmail Spam operation.
    names = set()
    if decision.destination.value in ATTENTION:
        names.add(ATTENTION[decision.destination.value])
    names.update(TOPICS[t] for t in decision.topics.topics if t in TOPICS)
    return names


def run(account: str, token: Path | None = None, root: Path = STATE_DIR, *, max_messages: int = MAX_PER_RUN,
        dry_run: bool = False, now: int | None = None, provider: str = "jev", model: str | None = None,
        service_account: Path | None = None, since_days: int | None = None, api_key: str | None = None,
        since: int | None = None) -> dict:
    """Triage one batch. ``since_days`` rescans that many past days (skipping verified mail)
    instead of continuing from the checkpoint; ``since`` pins where that window starts (epoch
    seconds), so every batch and resume of one rescan covers the same mail."""
    if not 1 <= max_messages <= MAX_PER_RUN:
        raise ValueError("max_messages must be between 1 and 100")
    if since_days is not None and not 1 <= since_days <= MAX_WINDOW_DAYS:
        raise ValueError(f"since_days must be between 1 and {MAX_WINDOW_DAYS}")
    now = int(now if now is not None else time.time())
    root = root.expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    directory = scoped_directory(root, account)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with account_lock(directory / ".lock"):
        return _run_locked(account, token, directory, max_messages, dry_run, now, provider, model, service_account,
                           since_days, api_key, since)


def _client(token: Path | None, account: str, service_account: Path | None):
    if service_account:
        return GmailReadOnlyClient(None, service_account=service_account, subject=account)
    return GmailReadOnlyClient(token)


def _run_locked(account: str, token: Path | None, directory: Path, max_messages: int,
                dry_run: bool, now: int, provider_name: str = "jev", model: str | None = None,
                service_account: Path | None = None, since_days: int | None = None,
                api_key: str | None = None, since: int | None = None) -> dict:
    client = _client(token, account, service_account)
    throttle = getattr(client, "throttle", None)
    pushbacks = getattr(throttle, "pushbacks", 0)
    outcomes, seen, calls, changed, failures = Counter(), 0, 0, 0, 0
    tokens = Counter()
    consecutive = 0
    jev_seconds = 0.0
    complete = True
    paused: RateLimited | None = None
    events: dict[str, dict] = {}
    journal = directory / "events.jsonl"

    def record(event: dict) -> None:
        event = {**event, "ts": now}
        if not dry_run:
            append_event(journal, event)
            events[event["id"]] = event

    def summary() -> dict:
        return {"account": account, "mode": "dry-run" if dry_run else "label-only", "provider": provider_name,
                "processed": seen, "outcomes": dict(sorted(outcomes.items())), "model_calls": calls,
                "jev_calls": calls, "jev_ms": round(jev_seconds * 1000), "provider_failures": failures,
                "tokens": dict(tokens), "frontier_calls": 0, "gmail_changes": changed,
                "gmail_slowdowns": getattr(throttle, "pushbacks", 0) - pushbacks,
                "remaining": not complete, "cursor_advanced": complete and not dry_run}

    try:
        profile = client.profile()
        if profile["emailAddress"].casefold() != account.casefold():
            raise RuntimeError("Authenticated Gmail account mismatch")
        state_path = directory / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "account": account, "launch_epoch": now - 7 * 86400, "cursor_epoch": now - 7 * 86400}
        if state["account"].casefold() != account.casefold():
            raise RuntimeError("State belongs to a different account")
        events.update(load_events(journal))
        # A rescan's window starts where it first did (even a 90-day one resumed later), but never further
        # back than the journal remembers what was already done.
        since = (max(since, now - JOURNAL_RETENTION) if since else now - since_days * 86400) if since_days else None
        ids = list_ids(client, scan_query(int(state["cursor_epoch"]), int(state["launch_epoch"]), since))
        prefs = preferences.load(directory / "preferences.json")
        junk = (JUNK_LABEL,) if any(rule.action == "junk" for rule in prefs.rules) else ()
        labels = None if dry_run else ensure_labels(client, *junk)
        provider = make_provider(provider_name, model, api_key=api_key)

        with TriageStore(directory / "context.db") as store:
            if not store.get_cursor(account):
                bootstrap_context(client, store, account, days=365, max_sent=250, max_purchases=250, now=now)
            sync = sync_incremental(client, store, account, target_history_id=profile["historyId"],
                                    max_pages=10, page_size=500, now=now)
            if sync.history_expired:
                bootstrap_context(client, store, account, days=365, max_sent=250, max_purchases=250, now=now)
                store.set_cursor(account, profile["historyId"])
            for mid in ids:
                previous = events.get(mid)
                if previous and previous["status"] in {"verified", "skipped"}:
                    continue
                if seen >= max_messages:
                    complete = False
                    break
                seen += 1
                if not previous or previous["status"] != "classified":
                    try:
                        raw = client._get(mid, full=True)
                    except GmailError as exc:
                        if exc.status != 404:
                            raise
                        outcomes["deleted"] += 1  # deleted since it was listed
                        continue
                    evidence = extract_gmail_message(raw, lambda aid, m=mid: client.attachment_data(m, aid))
                    rule = preferences.match(prefs, evidence)
                    junked = junk_decision(evidence, rule)
                    if EXCLUDED.intersection(evidence.labels):
                        proposed = {"id": mid, "status": "classified", "destination": "excluded", "names": []}
                    elif junked:
                        # The person marked this as junk: label it without sending anything to Jev.
                        proposed = {"id": mid, "status": "classified", "destination": junked.destination.value,
                                    "names": sorted(desired_names(junked))}
                    else:
                        context = build_context(store, account, evidence, now=now,
                                                priorities_path=directory / "priorities.json")
                        if prefs.summary:
                            context = dataclasses.replace(context, user_notes=prefs.summary)
                        try:
                            started = time.perf_counter()
                            signals, usage = provider.classify_with_usage(evidence, context)
                            jev_seconds += time.perf_counter() - started
                        except ProviderError:
                            # One malformed answer must not wedge the account: retry on later
                            # runs, then give up and leave the message unchanged.
                            failures += 1
                            consecutive += 1
                            attempts = int((previous or {}).get("attempts", 0)) + 1
                            status = "skipped" if attempts >= MAX_ATTEMPTS else "failed"
                            record({"id": mid, "status": status, "attempts": attempts, "destination": "error", "names": []})
                            outcomes["error"] += 1
                            if status == "failed":
                                complete = False
                            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                                raise
                            continue
                        consecutive = 0
                        calls += 1
                        tokens.update(usage)
                        decision = decide(evidence, context, signals, preference=rule)
                        proposed = {"id": mid, "status": "classified",
                                    "destination": decision.destination.value,
                                    "names": sorted(desired_names(decision))}
                    record(proposed)
                    previous = proposed
                if not dry_run:
                    assert labels is not None
                    if JUNK_LABEL in previous["names"] and JUNK_LABEL not in labels:
                        labels = ensure_labels(client, JUNK_LABEL)  # a junk decision from an earlier run
                    changed += int(apply_labels(client, mid, set(previous["names"]), labels))
                    record({**previous, "status": "verified"})
                outcomes[previous["destination"]] += 1
    except RateLimited as exc:
        # Gmail asked for a break. Everything done so far is saved (the journal skips it next
        # time), so stop here and let the caller come back when Gmail says.
        complete, paused = False, exc
    except Exception as exc:
        exc.partial = summary()  # the run history still counts what this batch did before it stopped
        raise
    if complete and not dry_run:
        state["cursor_epoch"] = max(now, int(state["cursor_epoch"])) if since_days else now
        save_state(state_path, state)
        compact_events(journal, events, now)
    result = summary()
    if paused:
        result.update(paused_until=int(paused.retry_at), pause_code=paused.reason or str(paused.status),
                      pause_reason=str(paused)[:300])
    return result


def main(argv=None) -> int:
    from .accounts import Account, run_account
    from .config import CONFIG_DIR as _cfg, JevRequired, jev_settings, load_dotenv
    load_dotenv(Path(".env"), _cfg / ".env")
    parser = argparse.ArgumentParser(description="Private Gmail triage powered by Jev; label-only unless --dry-run")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--account", action="append", help="Gmail address to triage (repeatable)")
    who.add_argument("--all", action="store_true", help="Triage every connected account")
    parser.add_argument("--token", type=Path, help="OAuth token JSON (default: <config-dir>/tokens/<account>.json)")
    parser.add_argument("--service-account", type=Path,
                        help="Workspace service-account key with domain-wide delegation (instead of --token)")
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--model", help="Jev model override (default jev-latest)")
    parser.add_argument("--max", type=int, default=MAX_PER_RUN, help="Messages per batch (1-100)")
    parser.add_argument("--days", type=int, help=f"Rescan the last N days (1-{MAX_WINDOW_DAYS}) instead of continuing")
    parser.add_argument("--due", action="store_true",
                        help="Only run accounts whose schedule (set in the web app) is due; for an hourly cron job")
    parser.add_argument("--dry-run", action="store_true", help="Classify only; never change Gmail")
    parser.add_argument("--verbose", action="store_true", help="Include error messages (may contain Gmail IDs)")
    args = parser.parse_args(argv)
    accounts = discover_accounts(args.config_dir) if args.all else args.account
    if not accounts:
        parser.error(f"No connected accounts in {args.config_dir.expanduser() / 'tokens'}; "
                     "run inbox-triage-web or inbox-triage-auth first")
    if args.token and len(accounts) > 1:
        parser.error("--token applies to one account; omit it to use per-account default tokens")
    status = 0
    for account in accounts:
        token = None if args.service_account else (args.token or default_token(account, args.config_dir))
        try:
            acct = Account(args.state_dir, account)
            settings = acct.settings()
            trigger, days, since, dry_run = "cli", args.days, None, args.dry_run
            if args.due:
                due = acct.due_run()
                if not due:
                    continue  # not due, or Gmail asked for a break that isn't over yet
                trigger, paused = due
                if paused:  # carry on where the paused run stopped, with its window; --dry-run always wins
                    days, since, dry_run = paused.get("days"), paused.get("since"), args.dry_run or paused.get("mode") == "dry-run"
            if token is not None and not token.expanduser().exists():
                raise FileNotFoundError(f"No token at {token}; run inbox-triage-auth")
            model, api_key = jev_settings(settings, args.model, args.config_dir, acct.jev_key())
            result = run_account(account, args.state_dir, trigger=trigger, days=days, since=since,
                                 runner_kwargs={"token": token, "max_messages": args.max,
                                                "dry_run": dry_run or bool(settings.get("dry_run")),
                                                "model": model, "api_key": api_key,
                                                "service_account": args.service_account})
            print(json.dumps(result, sort_keys=True))
        except Exception as exc:
            # By default never print provider responses, message metadata, IDs, or credentials.
            error = {"account": account, "status": "error", "type": type(exc).__name__}
            if args.verbose or isinstance(exc, JevRequired):
                error["message"] = str(exc)
            print(json.dumps(error), file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
