"""The extension's server side: connect page sign-in, one-time codes with PKCE, bearer tokens, the
dashboard summary and syncs. WSGI calls only; synthetic data; no network."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

from inbox_triage.accounts import Account
from inbox_triage.web import app as webapp
from inbox_triage.web import extension as ext
from inbox_triage.web import oauth

from test_web import EMAIL, _finish_login, call, signed_in, app  # noqa: F401 - the web fixture

EXT_ID = "abcdefghijklmnopabcdefghijklmnop"
REDIRECT = f"https://{EXT_ID}.chromiumapp.org/cb"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
ORIGIN = {"HTTP_ORIGIN": f"chrome-extension://{EXT_ID}"}


def authorize(app, cookie, **overrides):  # noqa: F811
    body = {"redirect_uri": REDIRECT, "state": "state-1234", "code_challenge": CHALLENGE, "email": EMAIL, **overrides}
    return call(app, "POST", "/api/ext/authorize", body, cookie)


def connect(app):  # noqa: F811
    """Sign the extension in the way it does: approve on the connect page, then swap the code."""
    status, _, data = authorize(app, signed_in(app))
    assert status == 200, data
    query = parse_qs(urlparse(data["redirect"]).query)
    status, headers, token = call(app, "POST", "/api/ext/token", {"code": query["code"][0], "code_verifier": VERIFIER,
                                                                  "redirect_uri": REDIRECT}, headers=ORIGIN)
    assert status == 200, token
    return token


def bearer(token):
    return {"HTTP_AUTHORIZATION": "Bearer " + token["token"], **ORIGIN}


def test_the_extension_signs_in_through_the_connect_page_with_pkce(app):  # noqa: F811
    status, _, data = authorize(app, signed_in(app))
    assert status == 200 and data["redirect"].startswith(REDIRECT + "?code=")
    query = parse_qs(urlparse(data["redirect"]).query)
    assert query["state"] == ["state-1234"]
    code = query["code"][0]
    # The code is useless without the verifier behind the challenge, and works only once.
    swap = {"code": code, "code_verifier": "w" * 64, "redirect_uri": REDIRECT}
    assert call(app, "POST", "/api/ext/token", swap, headers=ORIGIN)[0] == 400
    status, _, token = call(app, "POST", "/api/ext/token", {**swap, "code_verifier": VERIFIER}, headers=ORIGIN)
    assert status == 400  # the failed attempt used the code up
    token = connect(app)
    assert token["email"] == EMAIL and token["expires_at"] > 0
    status, headers, summary = call(app, "GET", "/api/ext/summary?tz=UTC", headers=bearer(token))
    assert status == 200 and summary["email"] == EMAIL
    assert headers["Access-Control-Allow-Origin"] == ORIGIN["HTTP_ORIGIN"]
    assert "Access-Control-Allow-Credentials" not in headers  # tokens, never cookies


def test_codes_are_bound_to_the_extension_that_asked(app):  # noqa: F811
    _, _, data = authorize(app, signed_in(app))
    code = parse_qs(urlparse(data["redirect"]).query)["code"][0]
    other = "https://ponmlkjihgfedcbaponmlkjihgfedcba.chromiumapp.org/cb"
    assert call(app, "POST", "/api/ext/token", {"code": code, "code_verifier": VERIFIER, "redirect_uri": other},
                headers=ORIGIN)[0] == 400


def test_only_a_signed_in_person_can_approve_and_only_for_an_extension(app):  # noqa: F811
    assert authorize(app, "")[0] == 403                                      # no session
    assert authorize(app, signed_in(app), email="b@example.org")[0] == 403   # not this browser's account
    assert authorize(app, signed_in(app), redirect_uri="https://evil.example/cb")[0] == 403
    assert authorize(app, signed_in(app), code_challenge="short")[0] == 400
    # The connect page itself can't be read cross-origin: no CORS on the cookie-based endpoint.
    headers = call(app, "POST", "/api/ext/authorize", {}, signed_in(app), headers=ORIGIN)[1]
    assert "Access-Control-Allow-Origin" not in headers


def test_a_hosted_server_hands_tokens_only_to_listed_extensions(app, monkeypatch):  # noqa: F811
    app.hosted = True
    monkeypatch.delenv("INBOX_TRIAGE_EXTENSION_IDS", raising=False)
    assert authorize(app, signed_in(app))[0] == 403
    monkeypatch.setenv("INBOX_TRIAGE_EXTENSION_IDS", "ponmlkjihgfedcbaponmlkjihgfedcba, " + EXT_ID)
    assert authorize(app, signed_in(app))[0] == 200


def test_unpacked_and_store_ids_can_coexist_during_migration(app, monkeypatch):  # noqa: F811
    app.hosted = True
    unpacked = "leagpjpjajkpjaiegjjenjnlegffkofj"
    store = "ponmlkjihgfedcbaponmlkjihgfedcba"  # illustrative second ID, not an assigned store item
    monkeypatch.setenv("INBOX_TRIAGE_EXTENSION_IDS", f"{unpacked}, {store}")
    cookie = signed_in(app)
    for extension_id in (unpacked, store):
        redirect = f"https://{extension_id}.chromiumapp.org/cb"
        assert authorize(app, cookie, redirect_uri=redirect)[0] == 200
        assert ext.redirect_allowed(redirect, hosted=True)
    assert not ext.redirect_allowed(REDIRECT, hosted=True)


def test_tokens_are_revocable_and_die_with_the_account(app, monkeypatch):  # noqa: F811
    assert call(app, "GET", "/api/ext/summary", headers=ORIGIN)[0] == 401
    assert call(app, "GET", "/api/ext/summary", headers={"HTTP_AUTHORIZATION": "Bearer forged.abc"})[0] == 401
    # A session cookie is not an extension token.
    cookie_value = signed_in(app).split("=", 1)[1]
    assert call(app, "GET", "/api/ext/summary", headers={"HTTP_AUTHORIZATION": "Bearer " + cookie_value})[0] == 401
    token = connect(app)
    assert call(app, "DELETE", "/api/ext/token", headers=bearer(token))[0] == 200
    assert call(app, "GET", "/api/ext/summary", headers=bearer(token))[0] == 401
    token = connect(app)
    monkeypatch.setattr(oauth, "revoke", lambda path: None)
    assert call(app, "DELETE", f"/api/accounts/{EMAIL}", cookie=signed_in(app))[0] == 200
    assert call(app, "GET", "/api/ext/summary", headers=bearer(token))[0] == 401


def test_cors_preflight_for_the_extension(app):  # noqa: F811
    status, headers, _ = call(app, "OPTIONS", "/api/ext/summary", headers=ORIGIN)
    assert status == 204 and "Authorization" in headers["Access-Control-Allow-Headers"]
    assert "Access-Control-Allow-Origin" not in call(app, "OPTIONS", "/api/ext/summary",
                                                     headers={"HTTP_ORIGIN": "https://evil.example"})[1]


def test_summary_counts_labels_per_local_day_without_mail_content(app, monkeypatch):  # noqa: F811
    token = connect(app)
    acct = Account(app.state_dir, EMAIL)
    now = 1_790_000_000  # 2026-09-21T14:13:20Z
    events = [{"id": "a", "status": "verified", "destination": "needs_you", "names": ["Triage/Needs You"], "ts": now},
              {"id": "b", "status": "verified", "destination": "later", "names": ["Triage/Later", "Topics/Shopping"],
               "ts": now - 86400},
              {"id": "c", "status": "verified", "destination": "unchanged", "names": [], "ts": now},
              {"id": "d", "status": "classified", "destination": "for_you", "names": ["Triage/For You"], "ts": now},
              {"id": "e", "status": "verified", "destination": "later", "names": ["Triage/Later"], "ts": now - 30 * 86400}]
    (acct.dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    monkeypatch.setattr(webapp.time, "time", lambda: float(now))
    monkeypatch.setattr(app, "decorate", lambda email, items: ([{**d, "from": "Dana <d@example.org>",
                                                                   "subject": "Lease"} for d in items], True))
    data = call(app, "GET", "/api/ext/summary?tz=America/Los_Angeles", headers=bearer(token))[2]
    assert data["totals"] == {"needs_you": 1, "updates": 0, "for_you": 0, "later": 1, "junk": 0, "shopping": 1}
    assert [d["day"] for d in data["days"]][-1] == "2026-09-21" and len(data["days"]) == 7
    assert data["days"][-1]["needs_you"] == 1 and data["days"][-2]["later"] == 1
    # Only labeled mail is listed, newest first, with the sender and subject fetched live.
    assert [r["id"] for r in data["recent"]] == ["a", "b", "e"] and data["recent"][0]["subject"] == "Lease"
    assert data["labels"]["Triage/Needs You"] == "needs_you" and data["app_url"].startswith("http")


def test_summary_and_sync_remain_usable_during_long_run(app, monkeypatch):
    from inbox_triage import accounts, runner
    token = connect(app)
    monkeypatch.setattr(oauth, "revoke", lambda path: None)
    def batch(*args, **kwargs):
        status, _, summary = call(app, "GET", "/api/ext/summary", headers=bearer(token))
        assert status == 200 and summary["email"] == EMAIL
        status, _, sync = call(app, "POST", "/api/ext/sync", {}, headers=bearer(token))
        assert status == 200 and sync == {"started": False, "reason": "running"}
        assert call(app, "DELETE", f"/api/accounts/{EMAIL}", cookie=signed_in(app))[0] == 409
        return {"processed": 0, "remaining": False}
    monkeypatch.setattr(runner, "run", batch)
    assert accounts.run_account(EMAIL, app.state_dir)["status"] == "ok"


def test_sync_starts_a_run_but_never_more_than_once_a_minute(app, monkeypatch):  # noqa: F811
    token = connect(app)
    started = []
    monkeypatch.setattr(app, "start_job", lambda email, days, dry_run, trigger, since=None, **kw: started.append(trigger))
    assert call(app, "POST", "/api/ext/sync", {}, headers=bearer(token))[2] == {"started": True}
    assert call(app, "POST", "/api/ext/sync", {}, headers=bearer(token))[2]["reason"] == "recent"
    assert started == ["extension"]
    app.ext_syncs.clear()
    Account(app.state_dir, EMAIL).record_run({"started": 1, "status": "paused", "resume_at": 4_000_000_000})
    assert call(app, "POST", "/api/ext/sync", {}, headers=bearer(token))[2]["reason"] == "paused"
    assert started == ["extension"]


def test_connecting_the_extension_keeps_new_mail_sorted_between_visits(app):  # noqa: F811
    acct = Account(app.state_dir, EMAIL)
    assert acct.settings()["schedule"]["frequency"] == "off"
    connect(app)
    assert acct.settings()["schedule"]["frequency"] == "hourly"
    # Someone who already picked a schedule on the website keeps it.
    acct.update_settings({"onboarded": True, "schedule": {"frequency": "daily"}})
    connect(app)
    assert acct.settings()["schedule"]["frequency"] == "daily"


def test_google_sign_in_can_return_to_the_connect_page(app, monkeypatch):  # noqa: F811
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_SECRET", "s")
    back = f"/connect?redirect_uri={REDIRECT}&state=state-1234&code_challenge={CHALLENGE}&login_hint={EMAIL}"
    _, _, data = call(app, "POST", "/api/login", {"email": EMAIL, "next": back})
    state = data["url"].split("state=")[1].split("&")[0]
    status, headers, _ = _finish_login(app, monkeypatch, state, EMAIL)
    assert status == 302 and headers["Location"] == back
    # Anywhere else, or anything that could split the header, is ignored: no open redirects.
    for bad in ("https://evil.example/", "//evil.example/connect?", "/connect?x=1\r\nSet-Cookie: a=b"):
        _, _, data = call(app, "POST", "/api/login", {"email": EMAIL, "next": bad})
        state = data["url"].split("state=")[1].split("&")[0]
        assert _finish_login(app, monkeypatch, state, EMAIL)[1]["Location"].startswith("/#account=")


def test_connect_page_is_served_and_stays_within_the_csp(app):  # noqa: F811
    status, headers, body = call(app, "GET", "/connect?redirect_uri=x")
    assert status == 200 and b"/static/connect.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
    assert b"<script>" not in body and b"style=" not in body
    js = (webapp.STATIC / "connect.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js


def test_redirect_and_pkce_rules():
    assert ext.redirect_allowed(REDIRECT, hosted=False)
    assert not ext.redirect_allowed("https://abc.chromiumapp.org/cb", hosted=False)            # not an extension id
    assert not ext.redirect_allowed(REDIRECT + "?x=https://evil.example", hosted=False)
    assert not ext.redirect_allowed(f"https://{EXT_ID}.chromiumapp.org.evil.example/", hosted=False)
    assert ext.pkce_matches(VERIFIER, CHALLENGE) and not ext.pkce_matches("short", CHALLENGE)
