"""Google OAuth 2.0 authorization-code flow with PKCE, standard library only."""
from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse

from .client import SCOPE, TOKEN_URI, Credentials, GmailClient, post_form

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


def _client_fields(client: dict) -> dict:
    inner = client.get("web") or client.get("installed") or {}
    return {"client_id": inner["client_id"], "client_secret": inner.get("client_secret", ""),
            "auth_uri": inner.get("auth_uri") or AUTH_URI, "token_uri": inner.get("token_uri") or TOKEN_URI}


def authorization_url(client: dict, redirect_uri: str, state: str, login_hint: str | None = None) -> tuple[str, str]:
    """Returns (url, code_verifier). prompt=consent guarantees a refresh token."""
    fields = _client_fields(client)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": fields["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
              "scope": SCOPE, "state": state, "access_type": "offline", "prompt": "consent",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if login_hint:
        params["login_hint"] = login_hint
    return fields["auth_uri"] + "?" + urllib.parse.urlencode(params), verifier


def exchange(client: dict, redirect_uri: str, code: str, verifier: str) -> Credentials:
    fields = _client_fields(client)
    data = post_form(fields["token_uri"], {"grant_type": "authorization_code", "code": code,
                                           "redirect_uri": redirect_uri, "code_verifier": verifier,
                                           "client_id": fields["client_id"], "client_secret": fields["client_secret"]})
    return Credentials.from_token_response(data, fields["client_id"], fields["client_secret"], fields["token_uri"])


def profile_email(credentials: Credentials) -> str:
    return GmailClient(credentials=credentials).profile()["emailAddress"]
