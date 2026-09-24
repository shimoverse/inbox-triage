"""Google sign-in for the web app (authorization code flow with PKCE).

Where the OAuth client comes from, in order:
1. ``INBOX_TRIAGE_OAUTH_CLIENT_ID`` / ``INBOX_TRIAGE_OAUTH_CLIENT_SECRET`` (operator or packager),
2. ``<config-dir>/client_secret.json`` (self-hosters; can be pasted in the web app once),
3. ``inbox_triage/web/oauth_client.json`` shipped inside a distributed build.

End users never create a Google Cloud project: whoever runs or distributes the
app configures one client once. Google treats installed-app client secrets as
non-confidential, so shipping one in a desktop build is expected.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from ..gmail.client import SCOPE, write_private

BUNDLED = Path(__file__).parent / "oauth_client.json"
GOOGLE = {"auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}


def _valid(config) -> dict | None:
    if not isinstance(config, dict):
        return None
    kind = "web" if "web" in config else "installed" if "installed" in config else None
    inner = config.get(kind) if kind else None
    if not isinstance(inner, dict) or not inner.get("client_id") or not inner.get("client_secret"):
        return None
    return {kind: {**GOOGLE, **inner}}


def client_config(config_dir: Path) -> dict | None:
    client_id = os.environ.get("INBOX_TRIAGE_OAUTH_CLIENT_ID")
    if client_id:
        return _valid({"installed": {"client_id": client_id,
                                     "client_secret": os.environ.get("INBOX_TRIAGE_OAUTH_CLIENT_SECRET", "")}})
    for path in (config_dir.expanduser() / "client_secret.json", BUNDLED):
        try:
            found = _valid(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if found:
            return found
    return None


def save_client_json(text: str, config_dir: Path) -> None:
    try:
        config = _valid(json.loads(text))
    except json.JSONDecodeError:
        config = None
    if not config:
        raise ValueError("That isn't a Google OAuth client JSON (expected an 'installed' or 'web' client)")
    write_private(config_dir.expanduser() / "client_secret.json", json.dumps(config))


def _flow(client: dict, redirect_uri: str, verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_config(client, scopes=[SCOPE], redirect_uri=redirect_uri,
                                   code_verifier=verifier, autogenerate_code_verifier=verifier is None)


def authorization_url(client: dict, redirect_uri: str, state: str, login_hint: str | None) -> tuple[str, str]:
    flow = _flow(client, redirect_uri)
    extra = {"login_hint": login_hint} if login_hint else {}
    # prompt=consent guarantees a refresh token so scheduled runs keep working.
    url, _ = flow.authorization_url(state=state, access_type="offline", prompt="consent",
                                    **extra)
    return url, flow.code_verifier


def exchange(client: dict, redirect_uri: str, code: str, verifier: str):
    flow = _flow(client, redirect_uri, verifier)
    flow.fetch_token(code=code)
    return flow.credentials


def profile_email(credentials) -> str:
    from googleapiclient.discovery import build
    profile = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().getProfile(userId="me").execute()
    return str(profile["emailAddress"])


def revoke(token_path: Path) -> None:
    """Best effort: tell Google to drop the grant when an account is disconnected."""
    try:
        token = json.loads(token_path.read_text()).get("refresh_token") or json.loads(token_path.read_text()).get("token")
        if token:
            data = urllib.parse.urlencode({"token": token}).encode()
            urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/revoke", data=data), timeout=10)
    except Exception:
        pass
