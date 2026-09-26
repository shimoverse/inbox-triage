"""A small Gmail REST client using only the standard library.

Inbox Triage calls about ten Gmail endpoints, so it talks to the REST API
directly instead of loading Google's 100+ MB client library: less disk, less
memory, faster start. OAuth tokens use the same JSON format google-auth writes,
so existing token files keep working.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Authentication-Results"]
RETRIES = 5
RETRYABLE = {429, 500, 502, 503, 504}
# Gmail answers "slow down" with 403 plus one of these reasons; Google says to back off and retry.
RATE_LIMITED = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
FETCH_WORKERS = 8  # parallel message reads
# Gmail allows 250 quota units per user per second and a message read costs 5, so stay
# well under 50 requests a second however many threads are reading.
MAX_REQUESTS_PER_SECOND = 25
TIMEOUT = 60


class GmailError(RuntimeError):
    def __init__(self, status: int, message: str = "", reason: str = ""):
        super().__init__(f"Gmail API HTTP {status}" + (f" ({reason})" if reason else "") + (f": {message}" if message else ""))
        self.status, self.reason = status, reason


def _error_details(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Google's reason code and message from an API error body, when it has one."""
    try:
        error = json.loads(exc.read().decode() or "{}").get("error") or {}
    except Exception:
        return "", ""
    if not isinstance(error, dict):
        return "", str(error)[:160]
    reasons = [e.get("reason", "") for e in error.get("errors") or [] if isinstance(e, dict)]
    reasons += [d.get("reason", "") for d in error.get("details") or [] if isinstance(d, dict)]
    reason = next((r for r in reasons if r), "") or str(error.get("status") or "")
    return reason[:60], str(error.get("message") or "")[:160]


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        return min(60.0, float(exc.headers.get("Retry-After") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


class HistoryExpired(RuntimeError):
    """The stored historyId is too old; the caller must do a full resync."""


def write_private(path: Path, text: str) -> None:
    """Atomically write a 0600 file so a crash never leaves a truncated token."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def post_form(url: str, fields: dict, timeout: float = TIMEOUT) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("error", "")
        except Exception:
            detail = ""
        raise GmailError(exc.code, str(detail)[:80]) from None


def _parse_expiry(value) -> float:
    if not value:
        return 0.0
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


class Credentials:
    """An OAuth "authorized user": access token + refresh token (google-auth JSON format)."""

    def __init__(self, info: dict, path: Path | None = None):
        self.token = info.get("token") or info.get("access_token") or ""
        self.refresh_token = info.get("refresh_token", "")
        self.client_id = info.get("client_id", "")
        self.client_secret = info.get("client_secret", "")
        self.token_uri = info.get("token_uri") or TOKEN_URI
        scopes = info.get("scopes") or info.get("scope") or [SCOPE]
        self.scopes = scopes.split() if isinstance(scopes, str) else list(scopes)
        self.expiry = _parse_expiry(info.get("expiry"))
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def from_file(cls, path: str | Path) -> "Credentials":
        path = Path(path).expanduser()
        return cls(json.loads(path.read_text(encoding="utf-8")), path)

    @classmethod
    def from_token_response(cls, data: dict, client_id: str, client_secret: str,
                            token_uri: str = TOKEN_URI) -> "Credentials":
        creds = cls({"token": data.get("access_token", ""), "refresh_token": data.get("refresh_token", ""),
                     "client_id": client_id, "client_secret": client_secret, "token_uri": token_uri,
                     "scopes": data.get("scope") or [SCOPE]})
        creds.expiry = time.time() + int(data.get("expires_in", 3600))
        return creds

    @property
    def granted_scopes(self) -> list[str]:
        return self.scopes

    def valid(self) -> bool:
        return bool(self.token) and (not self.expiry or self.expiry - 60 > time.time())

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self.valid():
                self.refresh()
            return self.token

    def refresh(self) -> None:
        if not self.refresh_token:
            raise GmailError(401, "no refresh token; sign in again")
        data = post_form(self.token_uri, {"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                                          "client_id": self.client_id, "client_secret": self.client_secret})
        self.token = data["access_token"]
        self.expiry = time.time() + int(data.get("expires_in", 3600))
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        if self.path:
            # Refresh only updates the local token file, never Gmail state.
            write_private(self.path, self.to_json())

    def to_json(self) -> str:
        expiry = datetime.fromtimestamp(self.expiry, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if self.expiry else None
        return json.dumps({"token": self.token, "refresh_token": self.refresh_token, "token_uri": self.token_uri,
                           "client_id": self.client_id, "client_secret": self.client_secret,
                           "scopes": self.scopes, "expiry": expiry}, indent=2)


class ServiceAccountCredentials:
    """Google Workspace domain-wide delegation. Needs the optional extra:
    ``pip install 'inbox-triage[workspace]'`` (for RS256 signing)."""

    def __init__(self, key_path: str | Path, subject: str):
        try:
            from google.auth import crypt, jwt
        except ImportError:
            raise RuntimeError("Service accounts need the workspace extra: pip install 'inbox-triage[workspace]'") from None
        self._info = json.loads(Path(key_path).expanduser().read_text(encoding="utf-8"))
        self._signer, self._jwt = crypt.RSASigner.from_service_account_info(self._info), jwt
        self.subject, self.token, self.expiry = subject, "", 0.0
        self._lock = threading.Lock()

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self.token or self.expiry - 60 < time.time():
                now = int(time.time())
                claims = {"iss": self._info["client_email"], "sub": self.subject, "scope": SCOPE,
                          "aud": self._info.get("token_uri", TOKEN_URI), "iat": now, "exp": now + 3600}
                assertion = self._jwt.encode(self._signer, claims)
                data = post_form(self._info.get("token_uri", TOKEN_URI), {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
                self.token, self.expiry = data["access_token"], time.time() + int(data.get("expires_in", 3600))
            return self.token


class GmailClient:
    """Gmail access for triage. It never sends, deletes, archives, or marks read;
    the only writes are creating its labels and adding/removing them."""

    def __init__(self, token_path: str | Path | None = None, *, service_account: str | Path | None = None,
                 subject: str | None = None, credentials=None):
        if credentials is not None:
            self.credentials = credentials
        elif service_account:
            self.credentials = ServiceAccountCredentials(service_account, subject or "")
        elif token_path is not None:
            self.credentials = Credentials.from_file(token_path)
        else:
            raise ValueError("A token path, service account, or credentials object is required")
        self._pace_lock = threading.Lock()
        self._next_request_at = 0.0

    # ------------------------------------------------------------------ transport
    def _pace(self) -> None:
        """Space request starts evenly across all threads so bursts stay under Gmail's per-user rate."""
        with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next_request_at)
            self._next_request_at = start + 1 / MAX_REQUESTS_PER_SECOND
        if start > now:
            time.sleep(start - now)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        for attempt in range(RETRIES + 1):
            self._pace()
            wait = 0.0
            headers = {"Authorization": "Bearer " + self.credentials.access_token(), "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                status = exc.code
                reason, message = _error_details(exc)
                if status == 401 and not refreshed:
                    self.credentials.access_token(force_refresh=True)
                    refreshed = True
                    continue
                retryable = status in RETRYABLE or (status == 403 and reason in RATE_LIMITED)
                if not retryable or attempt >= RETRIES:
                    raise GmailError(status, message, reason) from None
                wait = _retry_after(exc)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= RETRIES:
                    raise
            # Exponential backoff with jitter, as Google recommends for rate limits and 5xx.
            time.sleep(max(wait, min(32, 2 ** attempt) + random.random()))
        raise GmailError(503)

    # ------------------------------------------------------------------ reads
    def profile(self) -> dict[str, str]:
        raw = self._request("GET", "/profile")
        return {"emailAddress": str(raw.get("emailAddress", "unknown")), "historyId": str(raw.get("historyId", ""))}

    def account_id(self) -> str:
        return self.profile()["emailAddress"]

    def _get(self, message_id: str, *, full: bool) -> dict:
        params = {"format": "full"} if full else {"format": "metadata", "metadataHeaders": _METADATA_HEADERS}
        return self._request("GET", f"/messages/{urllib.parse.quote(message_id)}", params)

    def get_many(self, ids: Iterable[str], *, full: bool = False) -> list[dict]:
        """Fetch messages in parallel, skipping ones deleted since they were listed."""
        ids = list(ids)
        def fetch(mid: str):
            try:
                return self._get(mid, full=full)
            except GmailError as exc:
                if exc.status == 404:
                    return None
                raise
        if len(ids) <= 1:
            results = [fetch(mid) for mid in ids]
        else:
            with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(ids))) as pool:
                results = list(pool.map(fetch, ids))
        return [m for m in results if m is not None]

    def list_ids(self, query: str, max_results: int | None = None) -> list[str]:
        ids, token = [], None
        while max_results is None or len(ids) < max_results:
            size = 500 if max_results is None else min(500, max_results - len(ids))
            params = {"q": query, "maxResults": size, **({"pageToken": token} if token else {})}
            page = self._request("GET", "/messages", params)
            ids.extend(str(item["id"]) for item in page.get("messages", ()))
            token = page.get("nextPageToken")
            if not token:
                break
        return list(dict.fromkeys(ids))

    def iter_messages(self, query: str, max_results: int) -> Iterator[dict]:
        yield from self.get_many(self.list_ids(query, max_results), full=True)

    def iter_metadata(self, query: str, max_results: int) -> Iterator[dict]:
        yield from self.get_many(self.list_ids(query, max_results), full=False)

    def iter_history(self, start_history_id: str, *, max_pages: int, page_size: int) -> Iterator[dict]:
        """Yield pages of newly added messages plus the last history id they cover."""
        page_token = None
        for page_no in range(max_pages):
            params = {"startHistoryId": start_history_id, "historyTypes": ["messageAdded"],
                      "maxResults": min(500, page_size), **({"pageToken": page_token} if page_token else {})}
            try:
                response = self._request("GET", "/history", params)
            except GmailError as exc:
                if exc.status == 404:
                    raise HistoryExpired("Gmail history cursor expired") from None
                raise
            records = response.get("history", []) or []
            ids, seen = [], set()
            for record in records:
                for added in record.get("messagesAdded", []) or []:
                    mid = str((added.get("message") or {}).get("id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
            next_token = response.get("nextPageToken")
            last = str(records[-1].get("id", "")) if records else ""
            yield {"messages": self.get_many(ids, full=False), "last_history_id": last,
                   "truncated": bool(next_token and page_no + 1 >= max_pages)}
            if not next_token:
                break
            page_token = next_token

    def attachment_data(self, message_id: str, attachment_id: str) -> str:
        path = f"/messages/{urllib.parse.quote(message_id)}/attachments/{urllib.parse.quote(attachment_id)}"
        return str(self._request("GET", path).get("data", ""))

    # ------------------------------------------------------------------ labels (the only writes)
    def labels(self) -> list[dict]:
        return list(self._request("GET", "/labels").get("labels", ()))

    def create_label(self, name: str) -> dict:
        return self._request("POST", "/labels", body={"name": name, "labelListVisibility": "labelShow",
                                                     "messageListVisibility": "show"})

    def message_labels(self, message_id: str) -> set[str]:
        raw = self._request("GET", f"/messages/{urllib.parse.quote(message_id)}", {"format": "minimal"})
        return set(raw.get("labelIds", ()))

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> None:
        self._request("POST", f"/messages/{urllib.parse.quote(message_id)}/modify",
                      body={"addLabelIds": add, "removeLabelIds": remove})


# Backwards-compatible name; the client was never strictly read-only.
GmailReadOnlyClient = GmailClient
