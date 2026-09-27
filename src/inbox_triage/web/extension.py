"""Server side of the Chrome/Brave extension: sign-in bridge, tokens, and the dashboard summary.

The extension only draws a dashboard in Gmail and nudges the server to sort new mail; it never
holds a Google token or a Jev key. It signs in with ``chrome.identity.launchWebAuthFlow`` against
this server: the connect page (``/connect``) asks a signed-in person to approve the extension,
hands it a one-time code bound to a PKCE challenge, and the extension swaps the code for a
revocable bearer token that can only read this account's dashboard and ask for a sync.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..gmail.client import write_private
from ..runner import ATTENTION, TOPICS

TOKEN_TTL = 90 * 86400
CODE_TTL = 300
SYNC_INTERVAL = 60  # seconds between syncs one account's extension may start
SUMMARY_DAYS = 7
# chrome.identity.getRedirectURL(): https://<32-letter extension id>.chromiumapp.org/<path>
REDIRECT_RE = re.compile(r"^https://([a-p]{32})\.chromiumapp\.org/[A-Za-z0-9_\-/]*$")
LABEL_KEYS = {**{name: key for key, name in ATTENTION.items()}, **{name: key for key, name in TOPICS.items()}}


def allowed_ids() -> set[str]:
    """INBOX_TRIAGE_EXTENSION_IDS: the extension IDs a hosted server hands tokens to (comma-separated)."""
    return {x.strip() for x in os.environ.get("INBOX_TRIAGE_EXTENSION_IDS", "").split(",") if x.strip()}


def redirect_allowed(uri: str, hosted: bool) -> bool:
    """Only an extension's own chromiumapp.org address, and on a hosted server only a listed extension:
    otherwise any extension could quietly ask a signed-in person's browser for a token."""
    match = REDIRECT_RE.match(uri or "")
    if not match:
        return False
    return match.group(1) in allowed_ids() if hosted else True


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier or "") <= 128:
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return hmac.compare_digest(digest, challenge or "")


class TokenRegistry:
    """Which extension tokens an account has issued; deleting an entry (or the account) revokes it."""

    def __init__(self, account_dir: Path):
        self.path = account_dir / "extension.json"

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        tokens = data.get("tokens") if isinstance(data, dict) else None
        return {str(k): int(v) for k, v in tokens.items()} if isinstance(tokens, dict) else {}

    def add(self, token_id: str, now: int) -> None:
        tokens = {k: v for k, v in self._load().items() if now - v < TOKEN_TTL}
        tokens[token_id] = now
        write_private(self.path, json.dumps({"tokens": tokens}, sort_keys=True))

    def valid(self, token_id: str) -> bool:
        return token_id in self._load()

    def remove(self, token_id: str) -> None:
        tokens = self._load()
        if tokens.pop(token_id, None) is not None:
            write_private(self.path, json.dumps({"tokens": tokens}, sort_keys=True))

    def any(self) -> bool:
        return bool(self._load())


def _zone(name: str):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def label_counts(events: list[dict], now: int, tz_name: str = "", days: int = SUMMARY_DAYS) -> dict:
    """Verified label decisions of the last ``days`` days: totals per label and one row per local day.
    Only IDs and label names are stored, so this needs no mail content."""
    tz = _zone(tz_name)
    today = datetime.fromtimestamp(now, tz).date()
    rows = {}
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        rows[day] = {"day": day, **{key: 0 for key in LABEL_KEYS.values()}}
    totals = {key: 0 for key in LABEL_KEYS.values()}
    for event in events:
        if event.get("status") != "verified":
            continue
        day = datetime.fromtimestamp(int(event.get("ts", 0)), tz).date().isoformat()
        if day not in rows:
            continue
        for name in event.get("names") or ():
            key = LABEL_KEYS.get(name)
            if key:
                rows[day][key] += 1
                totals[key] += 1
    return {"totals": totals, "days": list(rows.values())}

