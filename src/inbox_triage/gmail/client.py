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
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Iterator

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Authentication-Results"]
RETRIES = 5
RETRYABLE = {500, 502, 503, 504}
# Gmail says "slow down" with a 429, or a 403 with one of these reasons; Google says to back off and retry.
RATE_LIMITED = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"}
FETCH_WORKERS = 4  # parallel message reads
# Gmail limits each mailbox, whichever app is asking: requests in flight at once, and 250 quota units a
# second (a message read costs 5). Start well under both, halve the pace whenever Gmail pushes back, and
# creep back up while requests succeed.
MAX_IN_FLIGHT = 4
START_RATE, MIN_RATE, MAX_RATE, RATE_STEP = 10.0, 1.0, 20.0, 0.2  # requests a second per mailbox
MAX_WAIT = 60  # Gmail asks for a longer break than this: stop and come back later instead of holding on
COOL_DOWN = 15 * 60  # the break to take when Gmail keeps refusing without saying for how long
TIMEOUT = 60
_RETRY_AT = re.compile(r"retry after (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))", re.I)


class GmailError(RuntimeError):
    def __init__(self, status: int, message: str = "", reason: str = ""):
        super().__init__(f"Gmail API HTTP {status}" + (f" ({reason})" if reason else "") + (f": {message}" if message else ""))
        self.status, self.reason, self.message = status, reason, message


class RateLimited(GmailError):
    """Gmail wants this mailbox left alone for a while; ``retry_at`` (epoch seconds) is when to come back."""

    def __init__(self, status: int, message: str = "", reason: str = "", retry_at: float = 0.0):
        super().__init__(status, message, reason)
        self.retry_at = retry_at


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


def _retry_after(exc: urllib.error.HTTPError, message: str = "") -> float:
    """Seconds Gmail asked us to wait: a Retry-After header, or "Retry after <time>" in its message."""
    now, asked = time.time(), 0.0
    try:
        header = str(exc.headers.get("Retry-After") or "").strip()
    except AttributeError:
        header = ""
    found = _RETRY_AT.search(message or "")
    try:
        if header:
            try:
                asked = float(header)
            except ValueError:
                asked = parsedate_to_datetime(header).timestamp() - now
        elif found:
            asked = datetime.fromisoformat(found.group(1).replace("Z", "+00:00")).timestamp() - now
    except (TypeError, ValueError, IndexError):
        asked = 0.0
    return min(max(0.0, asked), 2 * 86400) if asked == asked else 0.0  # never negative, NaN, or absurdly long


class Throttle:
    """Request pacing for one mailbox, shared by every client in this process that reads it,
    so a run and the dashboard reading the same mailbox never add up to more than Gmail allows."""

    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self.rate = START_RATE
        self.next_at = 0.0        # monotonic time the next request may start
        self.hold_until = 0.0     # monotonic time a short pause Gmail asked for ends
        self.blocked_until = 0.0  # wall-clock time Gmail asked us to stay away until
        self.blocked_by = (429, "", "")
        self.pushbacks = 0

    def _check(self) -> None:
        if self.blocked_until > time.time():
            # Gmail already asked for a break: don't knock again until it's over.
            raise RateLimited(*self.blocked_by, retry_at=self.blocked_until)

    def check(self) -> None:
        """Right before sending: honour a break, or a short pause, Gmail asked for while this request
        waited its turn."""
        waited_for = 0.0
        while True:
            with self.lock:
                self._check()
                until = self.hold_until
            hold = until - time.monotonic()
            if hold <= 0 or until == waited_for:
                return
            time.sleep(hold)
            waited_for = until  # look again: another request may have been told to wait longer meanwhile

    def wait(self) -> None:
        with self.lock:
            self._check()
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + 1 / self.rate
        if start > now:
            time.sleep(start - now)

    def succeeded(self) -> None:
        with self.lock:
            self.rate = min(MAX_RATE, self.rate + RATE_STEP)

    def pushed_back(self, pause: float) -> None:
        """Gmail said slow down: halve the pace and hold every request to this mailbox for ``pause``."""
        with self.lock:
            self.pushbacks += 1
            self.rate = max(MIN_RATE, self.rate / 2)
            self.hold_until = max(self.hold_until, time.monotonic() + pause)
            self.next_at = max(self.next_at, self.hold_until)

    def block(self, error: RateLimited) -> None:
        with self.lock:
            self.pushbacks += 1
            self.rate = min(self.rate, START_RATE / 2)  # come back gently
            if error.retry_at > self.blocked_until:
                self.blocked_until, self.blocked_by = error.retry_at, (error.status, error.message, error.reason)


_THROTTLES: dict[str, Throttle] = {}
_THROTTLES_LOCK = threading.Lock()


def throttle_for(key: str) -> Throttle:
    with _THROTTLES_LOCK:
        return _THROTTLES.setdefault(key, Throttle())


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
        # Gmail's limits are per mailbox, so every client for the same mailbox shares one throttle.
        path, subject = getattr(self.credentials, "path", None), getattr(self.credentials, "subject", "")
        key = f"token:{Path(path).expanduser().resolve()}" if path else f"subject:{subject.casefold()}" if subject else ""
        self.throttle = throttle_for(key) if key else Throttle()

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False
        for attempt in range(RETRIES + 1):
            self.throttle.wait()
            headers = {"Authorization": "Bearer " + self.credentials.access_token(), "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self.throttle.slots:
                    self.throttle.check()
                    try:
                        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                            raw = response.read()
                    except urllib.error.HTTPError as exc:
                        # Decided (and any slow-down recorded) before this slot frees up, so a request
                        # queued for it can't slip out ahead of the break Gmail just asked for.
                        wait = self._after_error(exc, attempt, refreshed)
                    else:
                        self.throttle.succeeded()
                        return json.loads(raw) if raw else {}
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt >= RETRIES:
                    raise
                wait = min(32, 2 ** attempt) + random.random()
            if wait is None:  # the access token was rejected: refresh it once and try again
                self.credentials.access_token(force_refresh=True)
                refreshed = True
                continue
            time.sleep(wait)
        raise GmailError(503)

    def _after_error(self, exc: urllib.error.HTTPError, attempt: int, refreshed: bool) -> float | None:
        """What an HTTP error means: seconds to wait before retrying, None to refresh the token and
        retry, or an exception. Gmail's slow-downs are recorded on the mailbox's throttle."""
        status = exc.code
        reason, message = _error_details(exc)
        if status == 401 and not refreshed:
            return None
        limited = status == 429 or (status == 403 and reason in RATE_LIMITED)
        if not (limited or status in RETRYABLE):
            raise GmailError(status, message, reason) from None
        asked = _retry_after(exc, message)
        if limited and (asked > MAX_WAIT or attempt >= RETRIES):
            # Gmail wants a longer break: stop now and come back when it says (or after a cool-down).
            error = RateLimited(status, message, reason, retry_at=time.time() + (asked if asked > MAX_WAIT else COOL_DOWN))
            self.throttle.block(error)
            raise error from None
        if attempt >= RETRIES:
            raise GmailError(status, message, reason) from None
        # Exponential backoff with jitter, as Google recommends for rate limits and 5xx.
        wait = max(min(asked, MAX_WAIT), min(32, 2 ** attempt) + random.random())
        if limited:
            self.throttle.pushed_back(wait)  # every request to this mailbox waits, this one included
            return 0.0
        return wait

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
            pool = ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(ids)))
            try:
                results = list(pool.map(fetch, ids))
            finally:
                # When Gmail stops us, drop the reads still queued instead of sending them anyway.
                pool.shutdown(wait=True, cancel_futures=True)
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

    def modify_labels(self, message_id: str, add: list[str], remove: list[str]) -> dict:
        """Gmail answers with the message as it is after the change, labels included."""
        return self._request("POST", f"/messages/{urllib.parse.quote(message_id)}/modify",
                      body={"addLabelIds": add, "removeLabelIds": remove})


# Backwards-compatible name; the client was never strictly read-only.
GmailReadOnlyClient = GmailClient
