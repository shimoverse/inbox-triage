"""Inbox Triage web app: sign in with Google, onboard, schedule, and review runs.

Standard library only (WSGI). Runs locally by default (http://127.0.0.1:8765);
an operator can host it for others with --public-url behind HTTPS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from email.utils import parseaddr
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import __version__, config, onboarding
from ..accounts import DEFAULT_SETTINGS, FREQUENCIES, Account, run_account
from ..gmail.client import SCOPE, GmailClient, GmailError, RateLimited, write_private
from ..preferences import Preferences
from ..assistant import DEFAULT_MODEL as ASSIST_DEFAULT_MODEL, Assistant
from ..providers import KEY_ENV, ProviderError, make_provider
from ..runner import MAX_WINDOW_DAYS, account_lock, account_lock_path, default_token, discover_accounts, scoped_directory
from . import extension as ext, oauth

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "inbox_triage_session"
SESSION_TTL = 30 * 86400
OAUTH_COOKIE = "inbox_triage_oauth"  # ties a Google sign-in to the browser that started it
OAUTH_TTL = 900  # seconds a started sign-in stays valid, same as the pending table
SUMMARY_TTL = 600  # seconds the dashboard reuses a message's sender/subject (memory only, never on disk)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EXT_CORS_PATHS = {"/api/ext/token", "/api/ext/summary", "/api/ext/sync"}
# Only the extension's connect page, and only characters a URL query may hold (no spaces, CR/LF or #).
NEXT_RE = re.compile(r"/connect\?[A-Za-z0-9_\-.~%&=+:/@!$'()*,;]*")
MAX_PENDING_LOGINS = 1000  # Google sign-ins started but not finished (15-minute lifetime)
BETA_FULL = ("Free hosted access is full. Inbox Triage is open source, so you can run it yourself: "
             "github.com/shimoverse/inbox-triage")


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Job:
    account: str
    started: int
    status: str = "running"  # running | ok | paused | error
    result: dict = field(default_factory=dict)


class App:
    def __init__(self, *, config_dir: Path = config.CONFIG_DIR, state_dir: Path = config.STATE_DIR,
                 base_url: str = "http://127.0.0.1:8765", hosted: bool = False, run_fn=run_account):
        self.config_dir, self.state_dir = config_dir.expanduser(), state_dir.expanduser()
        self.base_url, self.hosted, self.run_fn = base_url.rstrip("/"), hosted, run_fn
        self.secure = self.base_url.startswith("https://")
        self.secret = self._load_secret()
        self.pending: dict[str, dict] = {}  # OAuth state -> {verifier, created}
        self.jobs: dict[str, Job] = {}
        self.summaries: dict[tuple[str, str], tuple[float, dict]] = {}  # (account, message id) -> (when, summary)
        self.ext_codes: dict[str, dict] = {}  # one-time extension sign-in code -> {email, challenge, redirect_uri, created}
        self.ext_syncs: dict[str, float] = {}  # account -> when its extension last started a sync
        self.lock = threading.Lock()
        allowed = urlparse(self.base_url)
        self.allowed_hosts = {allowed.netloc}
        if allowed.hostname in {"127.0.0.1", "localhost"}:
            port = f":{allowed.port}" if allowed.port else ""
            self.allowed_hosts |= {f"127.0.0.1{port}", f"localhost{port}"}

    # ------------------------------------------------------------------ plumbing
    def _load_secret(self) -> bytes:
        path = self.config_dir / "web_secret"
        if not path.exists():
            write_private(path, secrets.token_hex(32))
        return path.read_text().strip().encode()

    def _sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        mac = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def _unsign(self, value: str) -> dict | None:
        body, _, mac = value.rpartition(".")
        if not body or not hmac.compare_digest(mac, hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(body.encode()))
        except (ValueError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and int(data.get("exp", 0)) > time.time() else None

    def session_emails(self, environ) -> list[str]:
        cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        if SESSION_COOKIE not in cookie:
            return []
        data = self._unsign(cookie[SESSION_COOKIE].value) or {}
        return [e for e in data.get("emails", []) if isinstance(e, str)]

    def session_cookie(self, emails: list[str]) -> str:
        value = self._sign({"emails": sorted(set(emails)), "exp": int(time.time()) + SESSION_TTL})
        flags = f"; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}" + ("; Secure" if self.secure else "")
        return f"{SESSION_COOKIE}={value}{flags}"

    def oauth_cookie(self, state: str | None) -> str:
        """The OAuth ``state`` of a sign-in this browser started, or (``None``) nothing. The callback only
        completes a sign-in whose state this cookie carries, so a link to someone else's callback URL
        can't add their account to this browser's session (login CSRF). Lax is enough: Google returns the
        person with a top-level GET, which sends Lax cookies, and the cookie is scoped to the callback."""
        value, age = (state, OAUTH_TTL) if state else ("", 0)
        flags = f"; Path=/oauth/callback; HttpOnly; SameSite=Lax; Max-Age={age}" + ("; Secure" if self.secure else "")
        return f"{OAUTH_COOKIE}={value}{flags}"

    def oauth_state_from(self, environ) -> str:
        cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        return cookie[OAUTH_COOKIE].value if OAUTH_COOKIE in cookie else ""

    def __call__(self, environ, start_response):
        try:
            status, headers, body = self.handle(environ)
        except HTTPError as exc:
            status, headers, body = exc.status, [], json.dumps({"error": exc.message}).encode()
            headers.append(("Content-Type", "application/json"))
        except RateLimited as exc:
            minutes = max(1, round((exc.retry_at - time.time()) / 60))
            message = f"Gmail asked Inbox Triage to slow down. Try again in about {minutes} minute{'s' * (minutes != 1)}."
            status, headers, body = 429, [("Content-Type", "application/json")], json.dumps({"error": message}).encode()
        except Exception:  # never leak internals to the browser
            traceback.print_exc()
            status, headers, body = 500, [("Content-Type", "application/json")], b'{"error":"Internal error"}'
        headers += [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                    ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"), *self.cors_headers(environ)]
        start_response(f"{status} {_REASONS.get(status, 'OK')}", headers)
        return [body]

    # ------------------------------------------------------------------ routing
    def handle(self, environ):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/") or "/"
        if path == "/healthz" and method == "GET":
            return self.json({"ok": True, "version": __version__})  # for systemd/proxy checks; no data
        host = environ.get("HTTP_HOST", "")
        if host and host not in self.allowed_hosts:
            raise HTTPError(400, "Unexpected Host header")  # DNS-rebinding guard
        if method in {"POST", "PUT", "DELETE"} and environ.get("HTTP_X_REQUESTED_WITH") != "inbox-triage":
            raise HTTPError(403, "Missing CSRF header")
        if method == "OPTIONS" and path in EXT_CORS_PATHS:
            return 204, [], b""  # CORS preflight from the extension; cors_headers() adds the rest
        if path == "/" or path == "/index.html":
            return self.static("index.html")
        if path == "/connect":
            return self.static("connect.html")
        if path in {"/privacy", "/privacy.html"}:
            return self.privacy()
        if path.startswith("/static/") and not path.endswith("privacy.html"):
            return self.static(path.removeprefix("/static/"))
        if path == "/oauth/callback":
            return self.oauth_callback(environ)
        if not path.startswith("/api/"):
            raise HTTPError(404, "Not found")
        body = self.read_json(environ) if method in {"POST", "PUT"} else {}
        query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        parts = [p for p in path.removeprefix("/api/").split("/") if p]
        emails = self.session_emails(environ)
        if parts[:1] == ["ext"]:
            return self.json(self.ext_api(method, parts[1] if len(parts) > 1 else "", body, query, environ, emails))
        route = (method, parts[0] if parts else "")
        if route == ("GET", "state"):
            return self.json(self.state(emails))
        if route == ("POST", "login"):
            url, state = self.login(body)
            return self.json({"url": url}, [("Set-Cookie", self.oauth_cookie(state))])
        if route == ("POST", "logout"):
            return self.json({"ok": True}, [("Set-Cookie", self.session_cookie([]))])
        if route == ("POST", "oauth-client"):
            return self.json(self.save_oauth_client(body))
        if route == ("PUT", "keys"):
            return self.json(self.save_key(body))
        if parts and parts[0] == "accounts" and len(parts) >= 2:
            account = parts[1].casefold()
            if account not in emails:
                raise HTTPError(403, "Sign in with this Google account first")
            action = parts[2] if len(parts) > 2 else ""
            result = self.account_api(method, account, action, body, query)
            if method == "DELETE" and not action:
                # Also sign this account out of the browser session.
                remaining = [e for e in emails if e != account]
                return self.json(result, [("Set-Cookie", self.session_cookie(remaining))])
            return self.json(result)
        raise HTTPError(404, "Not found")

    @staticmethod
    def read_json(environ) -> dict:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise HTTPError(400, "Invalid request") from None
        if not 0 <= length <= 1_000_000:
            raise HTTPError(400, "Request too large")
        try:
            data = json.loads(environ["wsgi.input"].read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise HTTPError(400, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "Expected a JSON object")
        return data

    @staticmethod
    def json(data, headers=None):
        return 200, [("Content-Type", "application/json"), *(headers or [])], json.dumps(data).encode()

    def static(self, name: str):
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            raise HTTPError(404, "Not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        csp = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com")
        return 200, [("Content-Type", ctype), ("Content-Security-Policy", csp)], path.read_bytes()

    def beta(self) -> dict:
        """Optional free-beta limits for a hosted server, all from the environment:
        INBOX_TRIAGE_MAX_ACCOUNTS (e.g. 100) and INBOX_TRIAGE_BETA_ENDS (YYYY-MM-DD).
        The end date is informational; the operator decides what happens after it."""
        try:
            max_accounts = int(os.environ.get("INBOX_TRIAGE_MAX_ACCOUNTS") or 0)
        except ValueError:
            max_accounts = 0
        ends = os.environ.get("INBOX_TRIAGE_BETA_ENDS", "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ends):
            ends = ""
        return {"enabled": bool(max_accounts or ends), "max_accounts": max_accounts or None, "ends": ends or None,
                "ended": bool(ends) and time.strftime("%Y-%m-%d") > ends}

    def beta_full(self) -> bool:
        limit = self.beta()["max_accounts"]
        return bool(limit) and len(discover_accounts(self.config_dir)) >= limit

    def machine_jev_key(self) -> str:
        """This computer's Jev key, for accounts without their own. A hosted server uses it
        only in a sponsored beta (INBOX_TRIAGE_SPONSORED_JEV_DAILY), up to that daily limit;
        otherwise every user connects their own Jev, so the operator never pays by accident."""
        if self.hosted and not config.sponsored_daily_limit():
            return ""
        return config.api_key_for("jev", self.config_dir)

    @property
    def sponsored(self) -> bool:
        """A hosted server whose operator pays for users' Jev calls (the free beta)."""
        return self.hosted and bool(config.sponsored_daily_limit()) and bool(self.machine_jev_key())

    def jev_access(self, acct: Account, settings: dict | None = None) -> config.JevAccess:
        return config.jev_access(settings, None, self.config_dir, acct.jev_key(), hosted=self.hosted)

    def privacy(self):
        """Privacy policy for Google's consent screen; the operator's contact comes from the environment."""
        import html as _html
        address = os.environ.get("INBOX_TRIAGE_SUPPORT_EMAIL", "").strip()
        email = _html.escape(address or "the operator")
        contact = f'<a href="mailto:{email}">{email}</a>' if EMAIL_RE.match(address) else email  # no mailto: to nobody
        operator = _html.escape(os.environ.get("INBOX_TRIAGE_OPERATOR", "the operator of this Inbox Triage server"))
        status, headers, body = self.static("privacy.html")
        text = body.decode().replace("{{SUPPORT_CONTACT}}", contact).replace("{{SUPPORT_EMAIL}}", email).replace("{{OPERATOR}}", operator)
        return status, headers, text.encode()

    # ------------------------------------------------------------------ state
    def state(self, emails: list[str]) -> dict:
        connected = set(discover_accounts(self.config_dir))
        accounts = []
        for email in emails:
            if email not in connected:
                # An older signed session must not recreate a deleted state directory.
                accounts.append({"email": email, "connected": False, "settings": DEFAULT_SETTINGS,
                                 "jev_connected": False, "own_jev_key": False, "last_run": None,
                                 "next_run": None, "resume_at": None, "job": None, "rules": 0})
                continue
            try:
                with account_lock(account_lock_path(self.state_dir, email).with_suffix(".read.lock"), blocking=True):
                    if not self.connected(email):
                        accounts.append({"email": email, "connected": False, "settings": DEFAULT_SETTINGS,
                                         "jev_connected": False, "own_jev_key": False, "last_run": None,
                                         "next_run": None, "resume_at": None, "job": None, "rules": 0})
                        continue
                    accounts.append(self._account_state(email))
            except RuntimeError:
                raise HTTPError(409, "Account is busy; try again") from None
        return {"accounts": accounts, "hosted": self.hosted,
                "oauth_configured": oauth.client_config(self.config_dir) is not None,
                "jev": {"connected": bool(self.machine_jev_key()), "signup_url": config.signup_url(),
                        "sponsored": self.sponsored},
                "assistant": {"available": bool(config.api_key_for("assistant", self.config_dir)),
                              "model": os.environ.get("INBOX_TRIAGE_ASSIST_MODEL") or ASSIST_DEFAULT_MODEL},
                "beta": self.beta(), "frequencies": list(FREQUENCIES), "max_days": MAX_WINDOW_DAYS,
                "extension": {"store_url": extension_store_url()}}

    def _account_state(self, email: str) -> dict:
        acct = Account(self.state_dir, email)
        settings = acct.settings()
        runs = acct.runs(1)
        job = self.jobs.get(email)
        paused = acct.pending_resume()
        return {"email": email, "connected": True, "settings": settings,
                "jev_connected": bool(acct.jev_key() or self.machine_jev_key()),
                "own_jev_key": bool(acct.jev_key()),
                "last_run": runs[0] if runs else None, "next_run": acct.next_run(),
                "resume_at": paused["resume_at"] if paused else None,
                "job": job.__dict__ if job else None,
                "rules": len(acct.preferences().rules)}

    # ------------------------------------------------------------------ OAuth
    def login(self, body: dict) -> tuple[str, str]:
        """Google's authorization URL for this sign-in, and its ``state`` (also set as this browser's cookie)."""
        email = str(body.get("email", "")).strip().casefold()
        if email and not EMAIL_RE.match(email):
            raise HTTPError(400, "Enter a valid email address")
        if self.beta_full() and not email:
            # Checked before Google's consent screen: every person who approves an unverified app
            # uses up one of Google's 100 lifetime sign-ins, even if we turn them away afterwards.
            # A typed address always goes on to Google (members sign back in that way; the callback
            # turns anyone else away), so this answer never reveals who has an account here.
            raise HTTPError(409, BETA_FULL + " Already a member? Choose “Use a specific account” and enter your address.")
        client = oauth.client_config(self.config_dir)
        if client is None:
            raise HTTPError(409, "Google sign-in isn't configured on this server yet")
        # Where to come back to after Google: the extension's connect page, or the app.
        after = str(body.get("next", ""))
        after = after if len(after) < 2000 and NEXT_RE.fullmatch(after) else ""
        state = secrets.token_urlsafe(24)
        url, verifier = oauth.authorization_url(client, self.redirect_uri, state, email or None)
        with self.lock:
            now = time.time()
            self.pending = {k: v for k, v in self.pending.items() if now - v["created"] < 900}
            if len(self.pending) >= MAX_PENDING_LOGINS:  # unauthenticated: never let it grow without bound
                for stale in sorted(self.pending, key=lambda k: self.pending[k]["created"])[:len(self.pending) // 2]:
                    del self.pending[stale]
            # Capture every disconnect generation: a hintless Google login only reveals
            # its account after exchanging the code, so a per-hint snapshot is insufficient.
            fences = self.config_dir / "disconnect-fences"
            self.pending[state] = {"verifier": verifier, "created": now, "hint": email, "next": after,
                                   "fences": {p.name: p.read_text() for p in fences.glob("*.json")}}
        return url, state

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/callback"

    def oauth_callback(self, environ):
        query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        state = query.get("state", "")
        with self.lock:
            pending = self.pending.get(state)
        if not pending:
            return self.redirect("/#error=" + quote("Sign-in expired, please try again"))
        # Bytes, not str: compare_digest rejects non-ASCII str, and the cookie value is whatever the client sent.
        if not hmac.compare_digest(self.oauth_state_from(environ).encode("utf-8", "replace"), state.encode("utf-8", "replace")):
            # Not the browser that started this sign-in: a link to someone else's callback, or a cookie
            # that expired. Nothing is consumed, so the browser that did start it can still finish.
            return self.redirect((pending.get("next") or "/") + "#error="
                                 + quote("This sign-in was started in another browser or has expired; please try again"))
        # For a hinted account, reject a busy run before spending Google's
        # single-use authorization code. Keep state so retrying the callback works.
        if pending.get("hint") and "code" in query:
            try:
                with account_lock(account_lock_path(self.state_dir, pending["hint"])):
                    pass
            except RuntimeError:
                return self.redirect((pending.get("next") or "/") + "#error=" + quote("Account is running; try sign-in again when it finishes"))
        with self.lock:
            pending = self.pending.pop(state, None)
        if not pending:
            return self.redirect("/#error=" + quote("Sign-in expired, please try again"))
        back = pending.get("next") or "/"
        if "error" in query or "code" not in query:
            return self.redirect(back + "#error=" + quote("Google sign-in was cancelled"))
        client = oauth.client_config(self.config_dir)
        try:
            credentials = oauth.exchange(client, self.redirect_uri, query["code"], pending["verifier"])
        except Exception:
            return self.redirect(back + "#error=" + quote("Google sign-in failed, please try again"))
        granted = getattr(credentials, "granted_scopes", None) or [SCOPE]
        if SCOPE not in granted:
            return self.redirect(back + "#error=" + quote("Please tick the Gmail permission so labels can be added"))
        email = oauth.profile_email(credentials).casefold()
        try:
            with account_lock(account_lock_path(self.state_dir, email), blocking=True):
                fence = self.disconnect_fence(email)
                if pending["fences"].get(fence.name) != (fence.read_text() if fence.exists() else None):
                    oauth.revoke_token(getattr(credentials, "refresh_token", "") or getattr(credentials, "token", ""))
                    return self.redirect(back + "#error=" + quote("Account was disconnected; sign in again"))
                if self.beta_full() and email not in discover_accounts(self.config_dir):
                    oauth.revoke_token(getattr(credentials, "refresh_token", "") or getattr(credentials, "token", ""))
                    return self.redirect(back + "#error=" + quote(BETA_FULL))
                write_private(default_token(email, self.config_dir), credentials.to_json())
                account_lock_path(self.state_dir, email).with_suffix(".deleted").unlink(missing_ok=True)
        except RuntimeError:
            return self.redirect(back + "#error=" + quote("Account is busy; sign in again"))
        emails = sorted(set(self.session_emails(environ)) | {email})
        target = pending["next"] if pending.get("next") else f"/#account={quote(email)}"
        return self.redirect(target, [("Set-Cookie", self.oauth_cookie(None)), ("Set-Cookie", self.session_cookie(emails))])

    @staticmethod
    def redirect(location: str, headers=None):
        return 302, [("Location", location), *(headers or [])], b""

    def save_oauth_client(self, body: dict) -> dict:
        if self.hosted:
            raise HTTPError(403, "The operator configures Google sign-in on hosted servers")
        if oauth.client_config(self.config_dir) is not None and not body.get("replace"):
            raise HTTPError(409, "Google sign-in is already configured")
        try:
            oauth.save_client_json(str(body.get("json", "")), self.config_dir)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return {"ok": True}

    def save_key(self, body: dict) -> dict:
        """Local mode only: this computer's Jev key (verified first) or the optional assistant key."""
        if self.hosted:
            raise HTTPError(403, "API keys are managed by the operator on hosted servers")
        role, key = str(body.get("role", "")), str(body.get("key", "")).strip()[:400]
        if role not in KEY_ENV:
            raise HTTPError(400, "Unknown key")
        if role == "jev" and key:
            self.verify_jev(key)
        config.save_secret(KEY_ENV[role], key, self.config_dir)
        return {"ok": True}

    @staticmethod
    def verify_jev(key: str) -> None:
        try:
            make_provider("jev", api_key=key).verify()
        except ProviderError as exc:
            status = getattr(exc, "status", None)
            message = ("Jev didn't accept that key" if status in {401, 403}
                       else "Couldn't reach Jev to check the key; try again")
            raise HTTPError(422, message) from None

    # ------------------------------------------------------------------ account API
    def account_api(self, method: str, email: str, action: str, body: dict, query: dict):
        if (method, action) == ("DELETE", ""):
            self.forget_account(email)
            return {"ok": True}
        if (method, action) == ("GET", "job"):
            job = self.jobs.get(email)  # in memory only: answer even while the run holds the lock
            return job.__dict__ if job else {}
        if (method, action) != ("POST", "run"):
            # Reading history or rules, or saving rules, a key or settings, doesn't need the run lock: a run
            # reads those once when it starts, and the writes are atomic. Only the short read gate (held for
            # long by nothing but deleting the account) guards them, so the dashboard works while a run goes.
            with account_lock(account_lock_path(self.state_dir, email).with_suffix(".read.lock"), blocking=True):
                if not self.connected(email):
                    raise HTTPError(409, "Reconnect this account with Google")
                result = self._account_api_locked(method, email, action, body, query)
            if (method, action) == ("GET", "emails"):  # Gmail is slow sometimes: never under the gate
                return {"emails": self.recent_emails(email, _int(query.get("days", 7)))}
            if (method, action) == ("GET", "history"):
                decisions, details = self.decorate(email, result.pop("decisions"))
                return {**result, "decisions": decisions, "details": details}
            return result
        with self.lock:
            current = self.jobs.get(email)
        if current and current.status == "running":
            raise HTTPError(409, "A run is already in progress")  # say so, rather than "busy", from a stale tab
        try:
            with account_lock(account_lock_path(self.state_dir, email)):
                return self._account_api_locked(method, email, action, body, query)
        except RuntimeError as exc:
            if str(exc) != "Account triage is already running":
                raise
            raise HTTPError(409, "Account is busy; try again") from exc

    def _account_api_locked(self, method: str, email: str, action: str, body: dict, query: dict):
        if (method, action) != ("DELETE", "") and not self.connected(email):
            raise HTTPError(409, "Reconnect this account with Google")
        acct = Account(self.state_dir, email)
        if (method, action) == ("GET", "emails"):
            return {}  # fetched by the caller, outside the gate
        if (method, action) == ("GET", "preferences"):
            return acct.preferences().to_json()
        if (method, action) == ("PUT", "preferences"):
            acct.save_preferences(Preferences.from_json(body))
            return acct.preferences().to_json()
        if (method, action) == ("PUT", "jev-key"):
            key = str(body.get("key", "")).strip()[:400]
            if key:
                self.verify_jev(key)
            acct.save_jev_key(key)
            return {"ok": True}
        if (method, action) == ("POST", "interpret"):
            return self.interpret(acct, body)
        if (method, action) == ("PUT", "settings"):
            changes = {k: body[k] for k in ("model", "dry_run", "onboarded", "schedule") if k in body}
            try:
                return acct.update_settings(changes)
            except (TypeError, ValueError, OverflowError) as exc:
                raise HTTPError(400, str(exc) or "Invalid settings") from None
        if (method, action) == ("POST", "run"):
            last = acct.runs(1)
            paused = last[0] if last and last[0].get("status") == "paused" else {}
            # Running the same dates again right after a pause continues that rescan from where its window began.
            since = paused.get("since") if paused.get("days") and str(paused["days"]) == str(body.get("days")) else None
            return self.start_job(email, body.get("days"), bool(body.get("dry_run")), "manual", since=since,
                                  _account_locked=True)
        if (method, action) == ("GET", "history"):
            return {"runs": acct.runs(50), "decisions": acct.decisions(30)}  # decorated by the caller, outside the gate
        raise HTTPError(404, "Not found")

    def disconnect_fence(self, email: str) -> Path:
        return self.config_dir / "disconnect-fences" / (scoped_directory(self.state_dir, email).name + ".json")

    def connected(self, email: str) -> bool:
        return (default_token(email, self.config_dir).exists()
                and not account_lock_path(self.state_dir, email).with_suffix(".deleted").exists())

    def forget_account(self, email: str) -> None:
        """Disconnect = delete: revoke Google's grant and remove everything stored for
        this account (token, Jev key, rules, settings, history, context, journal)."""
        try:
            with account_lock(account_lock_path(self.state_dir, email)):
                # Non-blocking on purpose: a read or write in flight answers "try again" rather than being
                # waited for, and a disconnect issued from inside such a write can never wait on itself.
                with account_lock(account_lock_path(self.state_dir, email).with_suffix(".read.lock")):
                    self._forget_account_locked(email)
        except RuntimeError as exc:
            if str(exc) != "Account triage is already running":
                raise
            raise HTTPError(409, "Wait for the current run to finish, then disconnect") from exc
        _log(f"account deleted account={_tag(email)}")

    def _forget_account_locked(self, email: str) -> None:
        with self.lock:
            job = self.jobs.get(email)
            if job and job.status == "running":
                raise HTTPError(409, "Wait for the current run to finish, then disconnect")
        directory = scoped_directory(self.state_dir, email)
        # Both the runner's exclusive lock and the short read gate are held.
        token = default_token(email, self.config_dir)
        if token.exists():
            oauth.revoke(token)
        # Persist both fences before touching state: stale OAuth callbacks must not
        # authorize, and a failed removal must not leave a token with empty state.
        write_private(self.disconnect_fence(email), secrets.token_hex(16))
        write_private(account_lock_path(self.state_dir, email).with_suffix(".deleted"), "1")
        if directory.exists():
            shutil.rmtree(directory)
        if token.exists():
            token.unlink()
        with self.lock:
            self.jobs.pop(email, None)
            self.summaries = {k: v for k, v in self.summaries.items() if k[0] != email}
            self.ext_codes = {k: v for k, v in self.ext_codes.items() if v["email"] != email}
            self.ext_syncs.pop(email, None)

    def client(self, email: str) -> GmailClient:
        token = default_token(email, self.config_dir)
        if not token.exists():
            raise HTTPError(409, "Reconnect this account with Google")
        client = GmailClient(token)
        if client.profile()["emailAddress"].casefold() != email:
            raise HTTPError(409, "Token belongs to a different account")
        return client

    def recent_emails(self, email: str, days: int) -> list[dict]:
        days = min(max(days, 1), MAX_WINDOW_DAYS)
        try:
            client = self.client(email)
            ids = client.list_ids(f"in:inbox newer_than:{days}d -in:sent", 40)
            return [_summarize(m) for m in client.get_many(ids, full=False)]
        except RateLimited:
            raise
        except GmailError as exc:  # Google's status and reason, which the UI explains; never a bare "Internal error"
            raise HTTPError(502, str(exc)) from None

    def decorate(self, email: str, decisions: list[dict]) -> tuple[list[dict], bool]:
        """Fetch subject/from live so no message content is ever stored on disk. Recent answers are
        reused from memory for a few minutes, so reopening the dashboard doesn't cost Gmail quota a
        run needs. Also says whether the details could be fetched."""
        if not decisions:
            return [], True
        now = time.time()
        with self.lock:
            known = {d["id"]: hit[1] for d in decisions
                     if (hit := self.summaries.get((email, d["id"]))) and now - hit[0] < SUMMARY_TTL}
        missing = [d["id"] for d in decisions if d["id"] not in known]
        ok = True
        if missing:
            try:
                fetched = {m.get("id"): _summarize(m) for m in self.client(email).get_many(missing)}
            except Exception:
                fetched, ok = {}, False
            with self.lock:
                if len(self.summaries) > 5000:
                    self.summaries.clear()
                self.summaries.update({(email, mid): (now, summary) for mid, summary in fetched.items()})
            known.update(fetched)
        return [{**d, **known.get(d["id"], {})} for d in decisions], ok

    def interpret(self, acct: Account, body: dict) -> dict:
        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
        try:
            assistant = Assistant(config.api_key_for("assistant", self.config_dir))
            proposed = onboarding.interpret(assistant, str(body.get("notes", "")),
                                            [e for e in emails if isinstance(e, dict)][:40])
        except ProviderError as exc:
            raise HTTPError(422, str(exc)) from None
        return proposed.to_json()

    # ------------------------------------------------------------------ extension
    @staticmethod
    def cors_headers(environ) -> list[tuple[str, str]]:
        """The extension calls these endpoints from its own origin with a bearer token (never cookies),
        so any extension origin may read the answers; tokens are only ever issued to listed extensions."""
        origin = environ.get("HTTP_ORIGIN", "")
        if environ.get("PATH_INFO") not in EXT_CORS_PATHS or not origin.startswith("chrome-extension://"):
            return []
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin"), ("Access-Control-Max-Age", "600"),
                ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Requested-With"),
                ("Access-Control-Allow-Methods", "GET, POST, DELETE")]

    def ext_api(self, method: str, action: str, body: dict, query: dict, environ, emails: list[str]):
        if (method, action) == ("POST", "authorize"):
            return self.ext_authorize(body, emails)
        if (method, action) == ("POST", "token"):
            return self.ext_token(body)
        data = self.ext_identity(environ)
        email = data["email"]
        gate = account_lock_path(self.state_dir, email).with_suffix(".read.lock")
        if (method, action) in {("GET", "summary"), ("POST", "sync"), ("DELETE", "token")}:
            try:
                with account_lock(gate, blocking=True):  # a short gate; only deleting the account holds it long
                    email, token_id = self.ext_account(environ)
                    if (method, action) == ("DELETE", "token"):
                        ext.TokenRegistry(Account(self.state_dir, email).dir).remove(token_id)
                        return {"ok": True}
                    if (method, action) == ("GET", "summary"):
                        summary, labeled = self.ext_summary(email, query)
                # Sender/subject details come from Gmail live: never under the gate, so nothing else waits.
                if (method, action) == ("GET", "summary"):
                    return self.ext_decorated(email, summary, labeled)
                with account_lock(gate, blocking=True):
                    # No run lock is needed to answer a running/paused request.
                    result = self.ext_sync(email, read_only=True)
                    if result is not None:
                        return result
                try:
                    with account_lock(account_lock_path(self.state_dir, email)):
                        if not self.connected(email):
                            raise HTTPError(401, "Connect the extension again")
                        return self.ext_sync(email, _account_locked=True)
                except RuntimeError as exc:
                    if str(exc) != "Account triage is already running":
                        raise
                    return {"started": False, "reason": "running"}
            except RuntimeError as exc:
                if str(exc) != "Account triage is already running":
                    raise
                raise HTTPError(409, "Account is busy; try again") from exc
        raise HTTPError(404, "Not found")

    def ext_authorize(self, body: dict, emails: list[str]) -> dict:
        """The connect page, after the person clicks Connect: a one-time code for the extension."""
        redirect_uri, state = str(body.get("redirect_uri", "")), str(body.get("state", ""))
        challenge, email = str(body.get("code_challenge", "")), str(body.get("email", "")).strip().casefold()
        if not ext.redirect_allowed(redirect_uri, self.hosted):
            raise HTTPError(403, "This server doesn't accept this copy of the extension yet. If you run the server, "
                                 "add the extension's ID to INBOX_TRIAGE_EXTENSION_IDS; otherwise install Inbox Triage "
                                 "from the Chrome Web Store.")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{43}", challenge) or not 8 <= len(state) <= 200:
            raise HTTPError(400, "The extension's sign-in request is incomplete; try again from Gmail")
        if email not in emails:
            raise HTTPError(403, "Sign in with this Google account first")
        try:
            with account_lock(account_lock_path(self.state_dir, email)):
                if not self.connected(email):
                    raise HTTPError(409, "Reconnect this account with Google")
                acct = Account(self.state_dir, email)
                settings = acct.settings()
                if not settings["onboarded"] and settings["schedule"]["frequency"] == "off":
                    acct.update_settings({"schedule": {"frequency": "hourly"}})
                code = secrets.token_urlsafe(32)
                with self.lock:
                    now = time.time()
                    self.ext_codes = {k: v for k, v in self.ext_codes.items() if now - v["created"] < ext.CODE_TTL}
                    fence = self.disconnect_fence(email)
                    self.ext_codes[code] = {"email": email, "challenge": challenge, "redirect_uri": redirect_uri,
                                            "created": now, "fence": fence.read_text() if fence.exists() else None}
        except RuntimeError as exc:
            if str(exc) != "Account triage is already running":
                raise
            raise HTTPError(409, "Account is busy; try again") from exc
        _log(f"extension connected account={_tag(email)}")
        sep = "&" if "?" in redirect_uri else "?"
        return {"redirect": f"{redirect_uri}{sep}code={quote(code)}&state={quote(state)}"}

    def ext_token(self, body: dict) -> dict:
        """The extension swaps its one-time code (and PKCE verifier) for a revocable token."""
        with self.lock:
            entry = self.ext_codes.pop(str(body.get("code", "")), None)
        if not entry or time.time() - entry["created"] > ext.CODE_TTL:
            raise HTTPError(400, "Sign-in expired, please try again")
        if str(body.get("redirect_uri", "")) != entry["redirect_uri"] \
                or not ext.pkce_matches(str(body.get("code_verifier", "")), entry["challenge"]):
            raise HTTPError(400, "Sign-in couldn't be verified, please try again")
        now, token_id = int(time.time()), secrets.token_urlsafe(16)
        try:
            with account_lock(account_lock_path(self.state_dir, entry["email"])):
                fence = self.disconnect_fence(entry["email"])
                if entry.get("fence") != (fence.read_text() if fence.exists() else None):
                    raise HTTPError(409, "Reconnect this account with Google")
                if not self.connected(entry["email"]):
                    raise HTTPError(409, "Reconnect this account with Google")
                ext.TokenRegistry(Account(self.state_dir, entry["email"]).dir).add(token_id, now)
        except RuntimeError as exc:
            if str(exc) != "Account triage is already running":
                raise
            raise HTTPError(409, "Account is busy; try again") from exc
        token = self._sign({"kind": "ext", "email": entry["email"], "tid": token_id, "exp": now + ext.TOKEN_TTL})
        return {"token": token, "email": entry["email"], "expires_at": now + ext.TOKEN_TTL}

    def ext_account(self, environ) -> tuple[str, str]:
        data = self.ext_identity(environ)
        email, token_id = data["email"], data["tid"]
        # Disconnecting the account, or the extension, revokes its tokens.
        if not self.connected(email) \
                or not ext.TokenRegistry(Account(self.state_dir, email).dir).valid(token_id):
            raise HTTPError(401, "Connect the extension again")
        return email, token_id

    def ext_identity(self, environ) -> dict:
        auth = environ.get("HTTP_AUTHORIZATION", "")
        data = self._unsign(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not data or data.get("kind") != "ext":
            raise HTTPError(401, "Connect the extension again")
        if not isinstance(data.get("email"), str) or not EMAIL_RE.fullmatch(data["email"]):
            raise HTTPError(401, "Connect the extension again")
        return data

    def ext_summary(self, email: str, query: dict) -> tuple[dict, list[dict]]:
        """What the Gmail dashboard bar draws: counts per label per day and the run status, plus the
        recent decisions still to be decorated (see ``ext_decorated``). Reads local files only."""
        from ..runner import load_events
        acct = Account(self.state_dir, email)
        events = list(load_events(acct.dir / "events.jsonl").values())  # IDs and label names only
        counts = ext.label_counts(events, int(time.time()), str(query.get("tz", ""))[:64])
        labeled = sorted((e for e in events if e.get("status") == "verified" and e.get("names")),
                         key=lambda e: int(e.get("ts", 0)), reverse=True)[:8]
        runs, job, paused = acct.runs(1), self.jobs.get(email), acct.pending_resume()
        settings = acct.settings()
        jev = bool(acct.jev_key() or self.machine_jev_key())
        return {"email": email, "app_url": self.base_url, "jev_connected": jev, "sponsored": self.sponsored,
                "running": bool(job and job.status == "running"), "last_run": runs[0] if runs else None,
                "next_run": acct.next_run(), "resume_at": paused["resume_at"] if paused else None,
                "preview": bool(settings.get("dry_run")), "schedule": settings["schedule"]["frequency"],
                "labels": ext.LABEL_KEYS, **counts}, labeled

    def ext_decorated(self, email: str, summary: dict, labeled: list[dict]) -> dict:
        """The summary plus the sender and subject of the recently labeled mail, fetched from Gmail live."""
        recent, details = self.decorate(email, labeled)
        return {**summary, "details": details,
                "recent": [{"id": d["id"], "names": d.get("names", []), "from": d.get("from", ""),
                            "subject": d.get("subject", ""), "ts": d.get("ts", 0)} for d in recent]}

    def ext_sync(self, email: str, *, read_only: bool = False, _account_locked: bool = False) -> dict | None:
        """Check running/paused before taking the run lock; commit a new start under that lock."""
        if not self.connected(email):
            raise HTTPError(401, "Connect the extension again")
        acct = Account(self.state_dir, email)
        now = time.time()
        job = self.jobs.get(email)
        if job and job.status == "running":
            return {"started": False, "reason": "running"}
        last = acct.runs(1)
        if last and last[0].get("status") == "paused" and int(last[0].get("resume_at") or 0) > now:
            return {"started": False, "reason": "paused", "resume_at": int(last[0]["resume_at"])}
        if read_only:
            return None
        with self.lock:
            if now - self.ext_syncs.get(email, 0) < ext.SYNC_INTERVAL:
                return {"started": False, "reason": "recent"}
        try:
            self.start_job(email, None, False, "extension", _account_locked=_account_locked)
        except HTTPError as exc:
            running = exc.status == 409 and exc.message in {"Account is busy; try again", "A run is already in progress"}
            return {"started": False, "reason": "running" if running else "unavailable",
                    **({} if running else {"message": exc.message})}
        with self.lock:
            self.ext_syncs[email] = now
        return {"started": True}

    # ------------------------------------------------------------------ jobs
    def start_job(self, email: str, days, dry_run: bool, trigger: str, since: int | None = None,
                  _account_locked: bool = False) -> dict:
        if not _account_locked:
            with self.lock:
                current = self.jobs.get(email)
            if current and current.status == "running":
                raise HTTPError(409, "A run is already in progress")
            try:
                with account_lock(account_lock_path(self.state_dir, email)):
                    return self.start_job(email, days, dry_run, trigger, since, _account_locked=True)
            except RuntimeError as exc:
                if str(exc) != "Account triage is already running":
                    raise
                raise HTTPError(409, "Account is busy; try again") from exc
        if not self.connected(email):
            raise HTTPError(409, "Reconnect this account with Google")
        if days not in (None, ""):
            days = _int(days)
            if not 1 <= days <= MAX_WINDOW_DAYS:
                raise HTTPError(400, f"Choose between 1 and {MAX_WINDOW_DAYS} days")
        else:
            days = None
        try:
            self.jev_access(Account(self.state_dir, email))
        except config.JevRequired as exc:
            raise HTTPError(409, str(exc)) from None
        with self.lock:
            current = self.jobs.get(email)
            if current and current.status == "running":
                raise HTTPError(409, "A run is already in progress")
            job = self.jobs[email] = Job(email, int(time.time()))
        threading.Thread(target=self._run_job, args=(job, days, dry_run, trigger, since), daemon=True).start()
        return job.__dict__

    def _run_job(self, job: Job, days, dry_run: bool, trigger: str, since: int | None = None) -> None:
        try:
            # The thread may start after another process disconnects the account.
            # Fence before Account() (which creates its directory).
            # The lock is held for the whole run: letting it go between the check and the run would let a
            # request handler take it, and the run would fail as "already running" without being recorded.
            with account_lock(account_lock_path(self.state_dir, job.account), blocking=True):
                if not self.connected(job.account):
                    raise FileNotFoundError("Account disconnected before run started")
                acct = Account(self.state_dir, job.account)
                settings = acct.settings()
                access = self.jev_access(acct, settings)
                job.result = self.run_fn(job.account, self.state_dir, trigger=trigger, days=days, since=since,
                                         jev_daily_limit=access.daily_limit, _account_locked=True, runner_kwargs={
                    "token": default_token(job.account, self.config_dir), "model": access.model,
                    "api_key": access.key, "dry_run": dry_run or bool(settings.get("dry_run"))})
            job.status = "paused" if job.result.get("status") == "paused" else "ok"
            result = job.result
            detail = (f" resume_in={max(0, int(result.get('resume_at', 0) - time.time()))}s reason={result.get('reason', '')}"
                      if job.status == "paused" else "")
            _log(f"run {job.status} account={_tag(job.account)} trigger={trigger} processed={result.get('processed', 0)} "
                 f"labeled={result.get('gmail_changes', 0)} jev_calls={result.get('jev_calls', 0)} "
                 f"jev_cost={result.get('jev_cost', 0)} slowdowns={result.get('gmail_slowdowns', 0)}{detail}")
        except Exception as exc:
            shown = ("Reconnect this account with Google" if isinstance(exc, FileNotFoundError)  # never a server path
                     else str(exc)[:300])
            job.status, job.result = "error", {"error": type(exc).__name__, "message": shown}
            # Google's status and reason code say what went wrong without any mailbox content.
            _log(f"run error account={_tag(job.account)} trigger={trigger} type={type(exc).__name__} "
                 f"status={getattr(exc, 'status', '')} reason={getattr(exc, 'reason', '')}")

    def scheduler_tick(self, now: int | None = None) -> list[str]:
        started = []
        for email in discover_accounts(self.config_dir):
            try:
                with account_lock(account_lock_path(self.state_dir, email)):
                    if not self.connected(email):
                        continue
                    due = Account(self.state_dir, email).due_run(now)
                    if not due:
                        continue
                    trigger, paused = due
                    if paused:
                        self.start_job(email, paused.get("days"), paused.get("mode") == "dry-run", trigger,
                                       since=paused.get("since"), _account_locked=True)
                    else:
                        self.start_job(email, None, False, trigger, _account_locked=True)
                started.append(email)
            except (HTTPError, RuntimeError):
                continue  # already running, or Jev isn't connected
            except Exception:  # one account's broken state must not stop the others from being scheduled
                traceback.print_exc()
                continue
        return started


def extension_store_url() -> str:
    """INBOX_TRIAGE_EXTENSION_STORE_URL: the Chrome Web Store listing, once it is public. Until an operator
    sets it, the landing page doesn't promise a download."""
    url = os.environ.get("INBOX_TRIAGE_EXTENSION_STORE_URL", "").strip()
    return url if url.startswith("https://") and len(url) < 500 else ""


def check_operator_key(key: str) -> bool:
    """One tiny Jev call at startup, so a wrong or empty sponsored key shows up in the logs
    before any user's run fails on it."""
    try:
        make_provider("jev", api_key=key).verify()
    except ProviderError as exc:
        _log(f"sponsored Jev key check failed status={getattr(exc, 'status', '') or 'network'}")
        return False
    _log("sponsored Jev key check ok")
    return True


def _tag(email: str) -> str:
    """Short opaque account tag for logs (no addresses in server logs)."""
    return hashlib.sha256(email.encode()).hexdigest()[:10]


def _log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise HTTPError(400, "Expected a number") from None


def _summarize(message: dict) -> dict:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in (message.get("payload") or {}).get("headers", [])}
    sender = headers.get("from", "")
    address = parseaddr(sender)[1].casefold()
    return {"id": message.get("id", ""), "from": sender[:160], "address": address,
            "domain": address.rsplit("@", 1)[-1] if "@" in address else "",
            "subject": headers.get("subject", "")[:200], "date": headers.get("date", "")[:60],
            "snippet": str(message.get("snippet", ""))[:200], "labels": message.get("labelIds", [])}


_REASONS = {200: "OK", 204: "No Content", 302: "Found", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error"}


def serve(argv=None) -> int:
    import argparse
    import webbrowser
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    parser = argparse.ArgumentParser(description="Inbox Triage web app")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: this computer only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-url", help="Hosted mode: the HTTPS URL users visit (put a TLS proxy in front)")
    parser.add_argument("--config-dir", type=Path, default=config.CONFIG_DIR)
    parser.add_argument("--state-dir", type=Path, default=config.STATE_DIR)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-scheduler", action="store_true", help="Don't run scheduled triage from this process")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.public_url:
        parser.error("Binding beyond this computer requires --public-url (served over HTTPS)")
    if args.public_url and not args.public_url.startswith("https://"):
        parser.error("--public-url must be https://")
    local = "[::1]" if args.host == "::1" else args.host
    base = args.public_url or f"http://{local}:{args.port}"
    config.load_dotenv(Path(".env"), args.config_dir / ".env")
    app = App(config_dir=args.config_dir, state_dir=args.state_dir, base_url=base, hosted=bool(args.public_url))
    if app.sponsored:
        limit = config.sponsored_daily_limit()
        print(f"Sponsored access: accounts without their own Jev key use this server's key, up to {limit} Jev calls "
              "per account per day. Cap the key's spending with its provider too.")
        threading.Thread(target=check_operator_key, args=(app.machine_jev_key(),), daemon=True).start()
    elif app.hosted and config.api_key_for("jev", args.config_dir):
        print("Note: hosted mode ignores the server's Jev key; every user connects their own. "
              "Set INBOX_TRIAGE_SPONSORED_JEV_DAILY to pay for users' Jev calls instead.")

    class Server(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):  # don't log URLs: they can carry OAuth codes
            pass

    httpd = make_server(args.host, args.port, app, server_class=Server, handler_class=Quiet)
    if not args.no_scheduler:
        def loop():
            while True:
                time.sleep(60)
                try:
                    app.scheduler_tick()
                except Exception:
                    traceback.print_exc()
        threading.Thread(target=loop, daemon=True).start()
    print(f"Inbox Triage is running at {base}  (Ctrl+C to stop)")
    if not args.no_browser and not args.public_url:
        webbrowser.open(base)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
