"""The stdlib Gmail/OAuth client, against a fake HTTP layer (no network)."""
import base64
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from inbox_triage.gmail import client as gc
from inbox_triage.gmail import oauth


class FakeHTTP:
    """Routes urlopen calls to handlers; records every request."""
    def __init__(self, monkeypatch):
        self.requests, self.routes = [], []
        monkeypatch.setattr(urllib.request, "urlopen", self)
        monkeypatch.setattr(gc.time, "sleep", lambda s: None)

    def route(self, match, *responses):
        self.routes.append([match, list(responses)])

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        for match, responses in self.routes:
            if match in req.full_url and responses:
                status, body = responses.pop(0) if len(responses) > 1 else responses[0]
                if status >= 400:
                    raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))
                return _Resp(body)
        raise AssertionError("unexpected request " + req.full_url)


class _Resp(io.BytesIO):
    def __init__(self, body): super().__init__(json.dumps(body).encode())
    def __enter__(self): return self
    def __exit__(self, *a): pass


def token_file(tmp_path, expired=True):
    path = tmp_path / "t.json"
    expiry = "2020-01-01T00:00:00.000000Z" if expired else "2999-01-01T00:00:00Z"
    # The exact shape google-auth writes, so existing tokens keep working.
    path.write_text(json.dumps({"token": "old", "refresh_token": "r", "token_uri": gc.TOKEN_URI, "client_id": "cid",
                                "client_secret": "cs", "scopes": [gc.SCOPE], "universe_domain": "googleapis.com",
                                "account": "", "expiry": expiry}))
    return path


def test_expired_token_refreshes_and_is_saved_privately(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("oauth2.googleapis.com/token", (200, {"access_token": "new", "expires_in": 3600}))
    http.route("/profile", (200, {"emailAddress": "a@example.org", "historyId": 5}))
    path = token_file(tmp_path)
    profile = gc.GmailClient(path).profile()
    assert profile == {"emailAddress": "a@example.org", "historyId": "5"}
    form = dict(urllib.parse.parse_qsl(http.requests[0].data.decode()))
    assert form == {"grant_type": "refresh_token", "refresh_token": "r", "client_id": "cid", "client_secret": "cs"}
    assert http.requests[1].get_header("Authorization") == "Bearer new"
    saved = json.loads(path.read_text())
    assert saved["token"] == "new" and saved["refresh_token"] == "r" and path.stat().st_mode & 0o077 == 0


def test_401_refreshes_once_and_429_backs_off(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("oauth2.googleapis.com/token", (200, {"access_token": "fresh", "expires_in": 3600}))
    http.route("/profile", (401, {}), (429, {}), (200, {"emailAddress": "a@example.org", "historyId": 1}))
    assert gc.GmailClient(token_file(tmp_path, expired=False)).profile()["emailAddress"] == "a@example.org"
    auths = [r.get_header("Authorization") for r in http.requests if "/profile" in r.full_url]
    assert auths == ["Bearer old", "Bearer fresh", "Bearer fresh"]


def test_non_retryable_error_raises_status(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("/labels", (403, {}))
    with pytest.raises(gc.GmailError) as exc:
        gc.GmailClient(token_file(tmp_path, expired=False)).labels()
    assert exc.value.status == 403 and len(http.requests) == 1


def test_get_many_is_parallel_safe_skips_404_and_keeps_order(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("/messages/gone", (404, {}))
    for mid in ("m1", "m2", "m3"):
        http.route(f"/messages/{mid}?", (200, {"id": mid}))
    got = gc.GmailClient(token_file(tmp_path, expired=False)).get_many(["m1", "gone", "m2", "m3"])
    assert [m["id"] for m in got] == ["m1", "m2", "m3"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(
        next(r.full_url for r in http.requests if "/messages/m1" in r.full_url)).query)
    assert query["format"] == ["metadata"] and "Authentication-Results" in query["metadataHeaders"]


def test_label_writes_are_the_only_mutations(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("/messages/x/modify", (200, {}))
    http.route("/labels", (200, {"id": "L1", "name": "Triage/Later"}))
    client = gc.GmailClient(token_file(tmp_path, expired=False))
    client.modify_labels("x", ["L1"], [])
    client.create_label("Triage/Later")
    bodies = [(r.get_method(), r.full_url.split("/users/me")[1], json.loads(r.data)) for r in http.requests]
    assert bodies[0] == ("POST", "/messages/x/modify", {"addLabelIds": ["L1"], "removeLabelIds": []})
    assert bodies[1][2]["name"] == "Triage/Later"
    # No send/delete/trash/batchDelete endpoints exist on the client at all.
    assert not any(hasattr(client, n) for n in ("send", "delete", "trash", "archive"))


def test_oauth_url_uses_pkce_and_exchange_sends_verifier(monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("oauth2.googleapis.com/token", (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                                                   "scope": gc.SCOPE}))
    client = {"installed": {"client_id": "cid", "client_secret": "cs"}}
    url, verifier = oauth.authorization_url(client, "http://127.0.0.1:8765/oauth/callback", "st8", "a@example.org")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert url.startswith(oauth.AUTH_URI) and q["code_challenge"] == expected and q["code_challenge_method"] == "S256"
    assert q["scope"] == gc.SCOPE and q["access_type"] == "offline" and q["prompt"] == "consent" and q["state"] == "st8"
    creds = oauth.exchange(client, "http://127.0.0.1:8765/oauth/callback", "the-code", verifier)
    form = dict(urllib.parse.parse_qsl(http.requests[0].data.decode()))
    assert form["code_verifier"] == verifier and form["code"] == "the-code" and form["grant_type"] == "authorization_code"
    assert creds.refresh_token == "rt" and creds.granted_scopes == [gc.SCOPE] and creds.expiry > time.time()
    assert json.loads(creds.to_json())["refresh_token"] == "rt"


def test_service_account_requires_optional_extra(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__
    def no_google(name, *a, **k):
        if name.startswith("google"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_google)
    with pytest.raises(RuntimeError, match="workspace"):
        gc.GmailClient(service_account=tmp_path / "key.json", subject="a@corp.example")
