from __future__ import annotations

import re
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Iterable

from .gmail.client import HistoryExpired
from .models import ContextPack, MailEvidence
from .store import TriageStore

DAY = 86400
RELATIONSHIP_TTL = 400 * DAY
THREAD_TTL = 180 * DAY
PURCHASE_TTL = 400 * DAY
_TOPIC_PATTERNS = {
    "shopping": re.compile(r"\b(order|receipt|shipment|delivery|return|refund|invoice|purchase|warranty)\b", re.I),
    "home": re.compile(r"\b(home|mortgage|rent|lease|property|utility|plumber|electrician|repair)\b", re.I),
    "travel": re.compile(r"\b(flight|hotel|booking|reservation|itinerary|trip)\b", re.I),
    "health": re.compile(r"\b(doctor|clinic|appointment|prescription|medical|health)\b", re.I),
    "money": re.compile(r"\b(bank|tax|loan|investment|insurance|refund|payment)\b", re.I),
    "work_projects": re.compile(r"\b(project|proposal|contract|client|meeting)\b", re.I),
}
_PURCHASE = re.compile(r"\b(order(?: confirmation)?|receipt|purchase|shipment|delivery|return|refund|invoice)\b", re.I)
# Gmail search: `{a b}` is OR; `(a b)` would require every word.
PURCHASE_QUERY = ('-in:spam -in:trash newer_than:{days}d '
                  '(subject:{{order receipt purchase shipment delivery return refund invoice}} OR "order confirmation")')
# Consumer mailbox domains are shared by millions of senders, so they never prove a purchase relationship.
FREEMAIL = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                      "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com"})

@dataclass(frozen=True)
class ContextSyncStats:
    sent_scanned: int = 0
    purchase_scanned: int = 0
    history_messages: int = 0
    history_pages: int = 0
    cursor_advanced: bool = False
    history_truncated: bool = False
    history_expired: bool = False


def _headers(message: dict) -> dict[str, str]:
    payload = message.get("payload") or {}
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", [])}


def _timestamp(message: dict, now: int) -> int:
    try:
        value = int(str(message.get("internalDate", "0"))) // 1000
        return value if value > 0 else now
    except (TypeError, ValueError):
        return now


def _topics(subject: str) -> tuple[str, ...]:
    return tuple(sorted(name for name, pattern in _TOPIC_PATTERNS.items() if pattern.search(subject)))


def learn_message(store: TriageStore, account: str, message: dict, now: int) -> None:
    """Learn only explicit Sent participation or transaction-subject evidence."""
    headers, labels = _headers(message), set(message.get("labelIds", []) or [])
    subject, thread = headers.get("subject", ""), str(message.get("threadId", ""))
    when = _timestamp(message, now)
    topics = _topics(subject)
    if "SENT" in labels:
        if thread:
            store.put_fact(account, "thread", thread, when, when + THREAD_TTL)
        recipients = {addr.casefold() for _, addr in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if "@" in addr}
        for address in recipients:
            store.put_fact(account, "person", address, when, when + RELATIONSHIP_TTL)
            for topic in topics:
                store.put_fact(account, "person", address, when, when + RELATIONSHIP_TTL, topic)
    if labels & {"SENT", "SPAM", "TRASH"}:
        return
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
    # A purchase needs transaction language from a DMARC-aligned, non-freemail sender;
    # otherwise any phisher could mint "recent purchase" protection for their domain.
    dmarc_pass = bool(re.search(r"\bdmarc=pass\b", headers.get("authentication-results", ""), re.I))
    if domain and domain not in FREEMAIL and dmarc_pass and _PURCHASE.search(subject):
        store.put_fact(account, "purchase_domain", domain, when, when + PURCHASE_TTL, "shopping")
        if thread:
            store.put_fact(account, "purchase_thread", thread, when, when + PURCHASE_TTL, "shopping")


def bootstrap_context(client, store: TriageStore, account: str, *, days: int, max_sent: int,
                      max_purchases: int, now: int | None = None) -> ContextSyncStats:
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    sent = list(client.iter_metadata(f"in:sent newer_than:{days}d", max_sent))
    purchases = list(client.iter_metadata(PURCHASE_QUERY.format(days=days), max_purchases))
    for message in sent:
        learn_message(store, account, message, now)
    for message in purchases:
        learn_message(store, account, message, now)
    store.prune_context(account, now)
    return ContextSyncStats(sent_scanned=len(sent), purchase_scanned=len(purchases))


def sync_incremental(client, store: TriageStore, account: str, *, target_history_id: str,
                     max_pages: int, page_size: int, now: int | None = None) -> ContextSyncStats:
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    cursor = store.get_cursor(account)
    if not cursor:
        store.set_cursor(account, target_history_id)
        return ContextSyncStats(cursor_advanced=True)
    pages = messages = 0
    truncated = False
    last = ""
    try:
        for page in client.iter_history(cursor, max_pages=max_pages, page_size=page_size):
            pages += 1
            for message in page.get("messages", []):
                learn_message(store, account, message, now); messages += 1
            last = page.get("last_history_id") or last
            if page.get("truncated"):
                truncated = True
    except HistoryExpired:
        # Gmail keeps history for roughly a week; after that a full resync is required.
        store.set_cursor(account, "")
        return ContextSyncStats(history_expired=True)
    advanced = not truncated
    if advanced:
        store.set_cursor(account, target_history_id)
    elif last:
        # Resume after the processed pages next run instead of rereading them forever.
        store.set_cursor(account, last)
    store.prune_context(account, now)
    return ContextSyncStats(history_messages=messages, history_pages=pages,
                            cursor_advanced=advanced, history_truncated=truncated)


def build_context(store: TriageStore, account: str, evidence: MailEvidence,
                  now: int | None = None, priorities_path: str | Path | None = None) -> ContextPack:
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    address = parseaddr(evidence.sender)[1].casefold()
    thread = evidence.thread_id
    participated = bool(thread and store.has_fact(account, "thread", thread, now))
    known = bool(address and store.has_fact(account, "person", address, now))
    relationship = "active_thread" if participated else "known_person" if known else "unknown"
    purchase = bool((thread and store.has_fact(account, "purchase_thread", thread, now)) or
                    (evidence.sender_domain and store.has_fact(account, "purchase_domain", evidence.sender_domain, now)))
    owned = store.topics_for(account, "person", address, now) if known else ()
    if purchase and "shopping" not in owned:
        owned = tuple(sorted(set(owned) | {"shopping"}))
    priorities = relevant_priorities(evidence, priorities_path, now) if priorities_path else ()
    return ContextPack(sender_relationship=relationship, thread_participation=participated,
                       recent_purchase=purchase, active_priorities=priorities,
                       priorities_relevant=bool(priorities),
                       owned_topics=owned, similar_replied=1 if (participated or known) else 0)

def relevant_priorities(evidence: MailEvidence, path: str | Path, now: int) -> tuple[str, ...]:
    """Only send explicitly matching, nonexpired, private topic names to Jev."""
    file = Path(path)
    if not file.exists(): return ()
    try:
        data = json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        raise ValueError(f"Could not parse priorities file {file}") from None
    if not isinstance(data, dict): return ()
    haystack = " ".join((evidence.subject, evidence.excerpt, evidence.sender_domain)).casefold()
    matches = []
    for item in data.get("priorities", []):
        topic, expiry = item.get("topic", ""), item.get("expires", "")
        if not isinstance(topic, str) or not isinstance(expiry, str) or not topic or len(topic) > 80: continue
        try: expiration = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + DAY
        except ValueError: continue
        if expiration <= now: continue
        terms = item.get("terms", [])
        if not isinstance(terms, list): continue
        if any(isinstance(term, str) and len(term) >= 3 and re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", haystack) for term in terms[:20]):
            matches.append(topic)
    return tuple(matches[:5])
