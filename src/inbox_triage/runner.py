"""Account-isolated, label-only Gmail triage without a frontier orchestrator."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

from .context import bootstrap_context, build_context, sync_incremental
from .gmail.client import GmailReadOnlyClient
from .gmail.extract import extract_gmail_message
from .models import Destination
from .policy import decide
from .providers.lean_jev import LeanJevProvider
from .store import TriageStore

ATTENTION = {"needs_you": "Triage/Needs You", "updates": "Triage/Updates",
             "for_you": "Triage/For You", "later": "Triage/Later"}
TOPICS = {"shopping": "Topics/Shopping"}
LABELS = tuple(ATTENTION.values()) + tuple(TOPICS.values())
EXCLUDED = {"SENT", "DRAFT", "SPAM", "TRASH"}
MAX_PER_RUN = 100


def scoped_directory(root: Path, account: str) -> Path:
    if not account or "@" not in account:
        raise ValueError("Provide a valid account email")
    return root / hashlib.sha256(account.casefold().encode()).hexdigest()[:24]


def scan_query(cursor: int, launch: int) -> str:
    return f"after:{max(0, launch, cursor - 2 * 86400)} -in:sent -in:drafts -in:spam -in:trash"


def list_ids(client: GmailReadOnlyClient, query: str) -> list[str]:
    ids, token = [], None
    while True:
        page = client.service.users().messages().list(
            userId="me", q=query, maxResults=500, pageToken=token).execute()
        ids.extend(str(item["id"]) for item in page.get("messages", ()))
        token = page.get("nextPageToken")
        if not token:
            return list(dict.fromkeys(ids))


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, separators=(",", ":")) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(path, 0o600)


def load_events(path: Path) -> dict[str, dict]:
    events = {}
    if path.exists():
        for line in path.read_text().splitlines():
            event = json.loads(line)
            events[event["id"]] = event
    return events


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        json.dump(state, file, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ensure_labels(client: GmailReadOnlyClient) -> dict[str, str]:
    labels = client.service.users().labels()
    before = {x["name"]: x["id"] for x in labels.list(userId="me").execute().get("labels", ())}
    for name in LABELS:
        if name not in before:
            labels.create(userId="me", body={"name": name,
                "labelListVisibility": "labelShow", "messageListVisibility": "show"}).execute()
    after = {x["name"]: x["id"] for x in labels.list(userId="me").execute().get("labels", ())}
    if not set(LABELS).issubset(after):
        raise RuntimeError("Gmail label creation readback failed")
    return {name: after[name] for name in LABELS}


def apply_labels(client: GmailReadOnlyClient, mid: str, desired: set[str], labels: dict[str, str]) -> bool:
    service = client.service.users().messages()
    old = set(service.get(userId="me", id=mid, format="minimal").execute().get("labelIds", ()))
    owned = set(labels.values())
    target = {labels[name] for name in desired}
    add, remove = sorted(target - old), sorted((old & owned) - target)
    if add or remove:
        service.modify(userId="me", id=mid, body={"addLabelIds": add, "removeLabelIds": remove}).execute()
    actual = set(service.get(userId="me", id=mid, format="minimal").execute().get("labelIds", ()))
    if actual & owned != target or (old - owned) - actual:
        raise RuntimeError("Gmail label readback mismatch")
    return bool(add or remove)


def desired_names(decision) -> set[str]:
    # A spam prediction is deliberately NOT a Gmail Spam operation.
    names = set()
    if decision.destination.value in ATTENTION:
        names.add(ATTENTION[decision.destination.value])
    names.update(TOPICS[t] for t in decision.topics.topics if t in TOPICS)
    return names


def run(account: str, token: Path, root: Path, *, max_messages: int = MAX_PER_RUN,
        dry_run: bool = False, now: int | None = None) -> dict:
    if not 1 <= max_messages <= MAX_PER_RUN:
        raise ValueError("max_messages must be between 1 and 100")
    now = int(now if now is not None else time.time())
    root = root.expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    directory = scoped_directory(root, account)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with (directory / ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Account triage is already running") from None
        return _run_locked(account, token, directory, max_messages, dry_run, now)


def _run_locked(account: str, token: Path, directory: Path, max_messages: int,
                dry_run: bool, now: int) -> dict:
    client = GmailReadOnlyClient(token)
    profile = client.profile()
    if profile["emailAddress"].casefold() != account.casefold():
        raise RuntimeError("Authenticated Gmail account mismatch")
    state_path, journal = directory / "state.json", directory / "events.jsonl"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "account": account, "launch_epoch": now - 7 * 86400, "cursor_epoch": now - 7 * 86400}
    if state["account"].casefold() != account.casefold():
        raise RuntimeError("State belongs to a different account")
    events = load_events(journal)
    ids = list_ids(client, scan_query(int(state["cursor_epoch"]), int(state["launch_epoch"])))
    labels = None if dry_run else ensure_labels(client)
    provider = LeanJevProvider()
    outcomes, seen, calls, changed = Counter(), 0, 0, 0
    complete = True
    with TriageStore(directory / "context.db") as store:
        if not store.get_cursor(account):
            bootstrap_context(client, store, account, days=365, max_sent=250, max_purchases=250)
        sync_incremental(client, store, account, target_history_id=profile["historyId"],
                         max_pages=10, page_size=500)
        for mid in ids:
            previous = events.get(mid)
            if previous and previous["status"] == "verified":
                continue
            if seen >= max_messages:
                complete = False
                break
            seen += 1
            if not previous or previous["status"] != "classified":
                raw = client._get(mid, full=True)
                evidence = extract_gmail_message(raw, lambda aid, m=mid: client.attachment_data(m, aid))
                if EXCLUDED.intersection(evidence.labels):
                    proposed = {"id": mid, "status": "classified", "destination": "excluded", "names": []}
                else:
                    context = build_context(store, account, evidence,
                                            priorities_path=directory / "priorities.json")
                    signals, _usage = provider.classify_with_usage(evidence, context)
                    calls += 1
                    decision = decide(evidence, context, signals)
                    proposed = {"id": mid, "status": "classified",
                                "destination": decision.destination.value,
                                "names": sorted(desired_names(decision))}
                if not dry_run:
                    append_event(journal, proposed)
                    events[mid] = proposed
                previous = proposed
            if not dry_run:
                assert labels is not None
                changed += int(apply_labels(client, mid, set(previous["names"]), labels))
                done = {**previous, "status": "verified"}
                append_event(journal, done)
                events[mid] = done
            outcomes[previous["destination"]] += 1
    if complete and not dry_run:
        state["cursor_epoch"] = now
        save_state(state_path, state)
    return {"mode": "dry-run" if dry_run else "label-only", "processed": seen,
            "outcomes": dict(sorted(outcomes.items())), "jev_calls": calls,
            "frontier_calls": 0, "gmail_changes": changed,
            "remaining": not complete, "cursor_advanced": complete and not dry_run}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private Gmail triage; label-only unless --dry-run")
    parser.add_argument("--account", required=True, help="Gmail address to verify")
    parser.add_argument("--token", type=Path, required=True, help="OAuth token JSON for this account")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/share/inbox-triage")
    parser.add_argument("--max", type=int, default=MAX_PER_RUN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args.account, args.token, args.state_dir,
                             max_messages=args.max, dry_run=args.dry_run), sort_keys=True))
        return 0
    except Exception as exc:
        # Never print provider responses, message metadata, IDs, or credentials.
        print(json.dumps({"status": "error", "type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
