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
import threading
import time
import traceback
from dataclasses import dataclass, field
from email.utils import parseaddr
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import config, onboarding
from ..accounts import FREQUENCIES, Account, run_account
from ..gmail.client import SCOPE, GmailClient, write_private
from ..preferences import Preferences
from ..assistant import DEFAULT_MODEL as ASSIST_DEFAULT_MODEL, Assistant
from ..providers import KEY_ENV, ProviderError, make_provider
from ..runner import MAX_WINDOW_DAYS, default_token, discover_accounts
from . import oauth

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "inbox_triage_session"
SESSION_TTL = 30 * 86400
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Job:
    account: str
    started: int
    status: str = "running"  # running | ok | error
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

    def __call__(self, environ, start_response):
        try:
            status, headers, body = self.handle(environ)
        except HTTPError as exc:
            status, headers, body = exc.status, [], json.dumps({"error": exc.message}).encode()
            headers.append(("Content-Type", "application/json"))
        except Exception:  # never leak internals to the browser
            traceback.print_exc()
            status, headers, body = 500, [("Content-Type", "application/json")], b'{"error":"Internal error"}'
        headers += [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                    ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store")]
        start_response(f"{status} {_REASONS.get(status, 'OK')}", headers)
        return [body]

    # ------------------------------------------------------------------ routing
    def handle(self, environ):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/") or "/"
        host = environ.get("HTTP_HOST", "")
        if host and host not in self.allowed_hosts:
            raise HTTPError(400, "Unexpected Host header")  # DNS-rebinding guard
        if method in {"POST", "PUT", "DELETE"} and environ.get("HTTP_X_REQUESTED_WITH") != "inbox-triage":
            raise HTTPError(403, "Missing CSRF header")
        if path == "/" or path == "/index.html":
            return self.static("index.html")
        if path.startswith("/static/"):
            return self.static(path.removeprefix("/static/"))
        if path == "/oauth/callback":
            return self.oauth_callback(environ)
        if not path.startswith("/api/"):
            raise HTTPError(404, "Not found")
        body = self.read_json(environ) if method in {"POST", "PUT"} else {}
        query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        parts = [p for p in path.removeprefix("/api/").split("/") if p]
        emails = self.session_emails(environ)
        route = (method, parts[0] if parts else "")
        if route == ("GET", "state"):
            return self.json(self.state(emails))
        if route == ("POST", "login"):
            return self.json(self.login(body))
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
            return self.json(self.account_api(method, account, action, body, query))
        raise HTTPError(404, "Not found")

    @staticmethod
    def read_json(environ) -> dict:
        try:
            length = min(int(environ.get("CONTENT_LENGTH") or 0), 1_000_000)
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

    def machine_jev_key(self) -> str:
        """This computer's Jev key, used only when self-hosting. Hosted servers ignore it:
        every user must connect their own Jev, so the operator never pays for others."""
        return "" if self.hosted else config.api_key_for("jev", self.config_dir)

    # ------------------------------------------------------------------ state
    def state(self, emails: list[str]) -> dict:
        connected = set(discover_accounts(self.config_dir))
        accounts = []
        for email in emails:
            acct = Account(self.state_dir, email)
            settings = acct.settings()
            runs = acct.runs(1)
            job = self.jobs.get(email)
            accounts.append({"email": email, "connected": email in connected, "settings": settings,
                             "jev_connected": bool(acct.jev_key() or self.machine_jev_key()),
                             "last_run": runs[0] if runs else None, "next_run": acct.next_run(),
                             "job": job.__dict__ if job else None,
                             "rules": len(acct.preferences().rules)})
        return {"accounts": accounts, "hosted": self.hosted,
                "oauth_configured": oauth.client_config(self.config_dir) is not None,
                "jev": {"connected": bool(self.machine_jev_key()), "signup_url": config.signup_url()},
                "assistant": {"available": bool(config.api_key_for("assistant", self.config_dir)),
                              "model": os.environ.get("INBOX_TRIAGE_ASSIST_MODEL") or ASSIST_DEFAULT_MODEL},
                "frequencies": list(FREQUENCIES), "max_days": MAX_WINDOW_DAYS}

    # ------------------------------------------------------------------ OAuth
    def login(self, body: dict) -> dict:
        email = str(body.get("email", "")).strip().casefold()
        if email and not EMAIL_RE.match(email):
            raise HTTPError(400, "Enter a valid email address")
        client = oauth.client_config(self.config_dir)
        if client is None:
            raise HTTPError(409, "Google sign-in isn't configured on this server yet")
        state = secrets.token_urlsafe(24)
        url, verifier = oauth.authorization_url(client, self.redirect_uri, state, email or None)
        with self.lock:
            now = time.time()
            self.pending = {k: v for k, v in self.pending.items() if now - v["created"] < 900}
            self.pending[state] = {"verifier": verifier, "created": now, "hint": email}
        return {"url": url}

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/callback"

    def oauth_callback(self, environ):
        query = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        with self.lock:
            pending = self.pending.pop(query.get("state", ""), None)
        if not pending:
            return self.redirect("/#error=" + quote("Sign-in expired, please try again"))
        if "error" in query or "code" not in query:
            return self.redirect("/#error=" + quote("Google sign-in was cancelled"))
        client = oauth.client_config(self.config_dir)
        try:
            credentials = oauth.exchange(client, self.redirect_uri, query["code"], pending["verifier"])
        except Exception:
            return self.redirect("/#error=" + quote("Google sign-in failed, please try again"))
        granted = getattr(credentials, "granted_scopes", None) or [SCOPE]
        if SCOPE not in granted:
            return self.redirect("/#error=" + quote("Please tick the Gmail permission so labels can be added"))
        email = oauth.profile_email(credentials).casefold()
        write_private(default_token(email, self.config_dir), credentials.to_json())
        emails = sorted(set(self.session_emails(environ)) | {email})
        return self.redirect(f"/#account={quote(email)}", [("Set-Cookie", self.session_cookie(emails))])

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
        acct = Account(self.state_dir, email)
        if (method, action) == ("GET", "emails"):
            return {"emails": self.recent_emails(email, _int(query.get("days", 7)))}
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
            except (TypeError, ValueError) as exc:
                raise HTTPError(400, str(exc)) from None
        if (method, action) == ("POST", "run"):
            return self.start_job(email, body.get("days"), bool(body.get("dry_run")), "manual")
        if (method, action) == ("GET", "job"):
            job = self.jobs.get(email)
            return job.__dict__ if job else {}
        if (method, action) == ("GET", "history"):
            return {"runs": acct.runs(50), "decisions": self.decorate(email, acct.decisions(30))}
        if (method, action) == ("DELETE", ""):
            token = default_token(email, self.config_dir)
            if token.exists():
                oauth.revoke(token)
                token.unlink()
            return {"ok": True}
        raise HTTPError(404, "Not found")

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
        client = self.client(email)
        ids = client.list_ids(f"in:inbox newer_than:{days}d -in:sent", 40)
        return [_summarize(m) for m in client.get_many(ids, full=False)]

    def decorate(self, email: str, decisions: list[dict]) -> list[dict]:
        """Fetch subject/from live so no message content is ever stored locally."""
        if not decisions:
            return []
        try:
            fetched = {m.get("id"): _summarize(m) for m in self.client(email).get_many([d["id"] for d in decisions])}
        except Exception:
            fetched = {}
        return [{**d, **fetched.get(d["id"], {})} for d in decisions]

    def interpret(self, acct: Account, body: dict) -> dict:
        emails = body.get("emails") if isinstance(body.get("emails"), list) else []
        try:
            assistant = Assistant(config.api_key_for("assistant", self.config_dir))
            proposed = onboarding.interpret(assistant, str(body.get("notes", "")),
                                            [e for e in emails if isinstance(e, dict)][:40])
        except ProviderError as exc:
            raise HTTPError(422, str(exc)) from None
        return proposed.to_json()

    # ------------------------------------------------------------------ jobs
    def start_job(self, email: str, days, dry_run: bool, trigger: str) -> dict:
        if days not in (None, ""):
            days = _int(days)
            if not 1 <= days <= MAX_WINDOW_DAYS:
                raise HTTPError(400, f"Choose between 1 and {MAX_WINDOW_DAYS} days")
        else:
            days = None
        try:
            config.jev_settings(None, None, self.config_dir, Account(self.state_dir, email).jev_key(),
                                allow_machine_key=not self.hosted)
        except config.JevRequired as exc:
            raise HTTPError(409, str(exc)) from None
        with self.lock:
            current = self.jobs.get(email)
            if current and current.status == "running":
                raise HTTPError(409, "A run is already in progress")
            job = self.jobs[email] = Job(email, int(time.time()))
        threading.Thread(target=self._run_job, args=(job, days, dry_run, trigger), daemon=True).start()
        return job.__dict__

    def _run_job(self, job: Job, days, dry_run: bool, trigger: str) -> None:
        acct = Account(self.state_dir, job.account)
        settings = acct.settings()
        try:
            model, key = config.jev_settings(settings, None, self.config_dir, acct.jev_key(),
                                             allow_machine_key=not self.hosted)
            job.result = self.run_fn(job.account, self.state_dir, trigger=trigger, days=days, runner_kwargs={
                "token": default_token(job.account, self.config_dir), "model": model,
                "api_key": key, "dry_run": dry_run or bool(settings.get("dry_run"))})
            job.status = "ok"
        except Exception as exc:
            job.status, job.result = "error", {"error": type(exc).__name__, "message": str(exc)[:300]}

    def scheduler_tick(self, now: int | None = None) -> list[str]:
        started = []
        for email in discover_accounts(self.config_dir):
            try:
                if Account(self.state_dir, email).is_due(now):
                    self.start_job(email, None, False, "schedule")
                    started.append(email)
            except HTTPError:
                continue  # already running, or Jev isn't connected
        return started


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPError(400, "Expected a number") from None


def _summarize(message: dict) -> dict:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in (message.get("payload") or {}).get("headers", [])}
    sender = headers.get("from", "")
    address = parseaddr(sender)[1].casefold()
    return {"id": message.get("id", ""), "from": sender[:160], "address": address,
            "domain": address.rsplit("@", 1)[-1] if "@" in address else "",
            "subject": headers.get("subject", "")[:200], "date": headers.get("date", "")[:60],
            "snippet": str(message.get("snippet", ""))[:200], "labels": message.get("labelIds", [])}


_REASONS = {200: "OK", 302: "Found", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 422: "Unprocessable Entity", 500: "Internal Server Error"}


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
    base = args.public_url or f"http://127.0.0.1:{args.port}"
    config.load_dotenv(Path(".env"), args.config_dir / ".env")
    if not args.public_url:
        # oauthlib insists on HTTPS except for loopback redirects like this one.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    app = App(config_dir=args.config_dir, state_dir=args.state_dir, base_url=base, hosted=bool(args.public_url))
    if app.hosted and config.api_key_for("jev", args.config_dir):
        print("Note: hosted mode ignores the server's TYPESAFE_API_KEY; every user connects their own Jev key.")

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
