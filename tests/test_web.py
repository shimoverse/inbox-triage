"""Web app tests: WSGI calls with fake Gmail/model; no network. Synthetic data only."""
import io
import json
from types import SimpleNamespace

import pytest

from inbox_triage import accounts, preferences
from inbox_triage.accounts import Account, latest_slot
from inbox_triage.models import ContextPack, Destination, JevSignals, MailEvidence
from inbox_triage.policy import decide
from inbox_triage.web import app as webapp
from inbox_triage.web import oauth

EMAIL = "a@example.org"


def call(app, method, path, body=None, cookie="", headers=None):
    raw = json.dumps(body).encode() if body is not None else b""
    path, _, query = path.partition("?")
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "127.0.0.1:8765",
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw), "HTTP_COOKIE": cookie,
               "HTTP_X_REQUESTED_WITH": "inbox-triage", **(headers or {})}
    out = {}
    def start(status, hdrs):
        out["status"], out["headers"] = int(status.split()[0]), dict(hdrs)
    data = b"".join(app(environ, start))
    ctype = out["headers"].get("Content-Type", "")
    return out["status"], out["headers"], (json.loads(data) if ctype == "application/json" and data else data)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runs = []
    def fake_run(account, state_root, **kw):
        runs.append((account, kw))
        return {"processed": 3, "gmail_changes": 2, "outcomes": {"for_you": 2, "unchanged": 1}}
    a = webapp.App(config_dir=tmp_path / "cfg", state_dir=tmp_path / "state", run_fn=fake_run)
    a.fake_runs = runs
    (tmp_path / "cfg" / "tokens").mkdir(parents=True)
    (tmp_path / "cfg" / "tokens" / f"{EMAIL}.json").write_text("{}")
    return a


def signed_in(app, emails=(EMAIL,)):
    return app.session_cookie(list(emails)).split(";")[0]


def test_static_page_has_strict_csp(app):
    status, headers, body = call(app, "GET", "/")
    assert status == 200 and b"Inbox Triage" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(app, "GET", "/static/../app.py")[0] == 404


def test_static_ui_stays_within_the_csp(app):
    # The CSP has no 'unsafe-inline': no inline styles or scripts, and the UI never parses HTML strings.
    csp = call(app, "GET", "/")[1]["Content-Security-Policy"]
    assert "unsafe-inline" not in csp
    for name in ("index.html", "privacy.html"):
        html = (webapp.STATIC / name).read_text()
        assert "style=" not in html and "<style" not in html and "onclick=" not in html
        assert "<script>" not in html
    js = (webapp.STATIC / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert 'setAttribute("style"' not in js


def test_security_guards(app):
    assert call(app, "GET", "/api/state", headers={"HTTP_HOST": "evil.example"})[0] == 400
    assert call(app, "POST", "/api/logout", {}, headers={"HTTP_X_REQUESTED_WITH": ""})[0] == 403
    # Another account's data needs its own Google sign-in.
    assert call(app, "GET", f"/api/accounts/{EMAIL}/preferences")[0] == 403
    forged = signed_in(app).rsplit(".", 1)[0] + ".bad"
    assert call(app, "GET", f"/api/accounts/{EMAIL}/preferences", cookie=forged)[0] == 403


def test_state_and_login_without_oauth_client(app, monkeypatch):
    monkeypatch.delenv("INBOX_TRIAGE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(oauth, "BUNDLED", app.config_dir / "missing.json")
    status, _, state = call(app, "GET", "/api/state", cookie=signed_in(app))
    assert status == 200 and state["oauth_configured"] is False
    assert state["accounts"][0]["email"] == EMAIL and state["accounts"][0]["connected"]
    assert state["jev"] == {"connected": True, "signup_url": "https://console.typesafe.ai/"}
    assert state["assistant"]["available"] is False and state["assistant"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert call(app, "POST", "/api/login", {"email": EMAIL})[0] == 409
    assert call(app, "POST", "/api/oauth-client", {"json": "{}"})[0] == 400
    good = json.dumps({"installed": {"client_id": "cid", "client_secret": "s"}})
    assert call(app, "POST", "/api/oauth-client", {"json": good})[0] == 200
    assert oauth.client_config(app.config_dir)["installed"]["token_uri"].startswith("https://oauth2.googleapis.com")
    assert call(app, "POST", "/api/oauth-client", {"json": good})[0] == 409


def test_login_builds_pkce_google_url_and_callback_rejects_unknown_state(app, monkeypatch):
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_SECRET", "not-secret")
    status, _, data = call(app, "POST", "/api/login", {"email": EMAIL})
    assert status == 200
    url = data["url"]
    assert url.startswith("https://accounts.google.com/") and "code_challenge_method=S256" in url
    assert "gmail.modify" in url and "access_type=offline" in url and "login_hint=a%40example.org" in url
    status, headers, _ = call(app, "GET", "/oauth/callback?state=forged&code=x")
    assert status == 302 and "error=" in headers["Location"]


def test_callback_saves_token_and_signs_in(app, monkeypatch):
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_SECRET", "s")
    _, _, data = call(app, "POST", "/api/login", {"email": "new@example.org"})
    state = data["url"].split("state=")[1].split("&")[0]
    creds = SimpleNamespace(granted_scopes=[oauth.SCOPE], to_json=lambda: '{"refresh_token":"r"}')
    monkeypatch.setattr(oauth, "exchange", lambda *a: creds)
    monkeypatch.setattr(oauth, "profile_email", lambda c: "New@Example.org")
    status, headers, _ = call(app, "GET", f"/oauth/callback?state={state}&code=abc")
    assert status == 302 and headers["Location"].startswith("/#account=new")
    token = app.config_dir / "tokens" / "new@example.org.json"
    assert token.exists() and token.stat().st_mode & 0o077 == 0
    cookie = headers["Set-Cookie"].split(";")[0]
    assert app.session_emails({"HTTP_COOKIE": cookie}) == ["new@example.org"]
    # The state is single-use.
    assert "error=" in call(app, "GET", f"/oauth/callback?state={state}&code=abc")[1]["Location"]


def test_settings_preferences_and_interpret(app, monkeypatch):
    cookie = signed_in(app)
    status, _, s = call(app, "PUT", f"/api/accounts/{EMAIL}/settings",
                        {"model": "jev-latest", "schedule": {"frequency": "weekly", "hour": 30, "weekday": 2}}, cookie)
    assert status == 200 and s["schedule"]["hour"] == 23 and s["model"] == "jev-latest" and "provider" not in s
    assert call(app, "PUT", f"/api/accounts/{EMAIL}/settings", {"schedule": {"frequency": "yearly"}}, cookie)[0] == 400
    prefs = {"rules": [{"kind": "domain", "value": "@School.example", "action": "important", "note": "kids"},
                       {"kind": "domain", "value": "nodot", "action": "important"},
                       {"kind": "keyword", "value": "real estate", "action": "not_important"}], "summary": "School matters."}
    _, _, saved = call(app, "PUT", f"/api/accounts/{EMAIL}/preferences", prefs, cookie)
    assert [r["value"] for r in saved["rules"]] == ["school.example", "real estate"]

    class FakeLLM:
        def complete_json(self, system, user, schema, name):
            assert "untrusted" in system and "#2" in json.loads(user)["notes"]
            return {"summary": "Kids' school is important.", "rules": [
                {"kind": "domain", "value": "school.example", "action": "important", "note": "school"}]}, {}
    monkeypatch.setattr(webapp, "Assistant", lambda key: FakeLLM())
    status, _, proposed = call(app, "POST", f"/api/accounts/{EMAIL}/interpret",
                               {"notes": "#2 is my kids' school, important", "emails": [{"from": "x", "domain": "school.example", "subject": "Trip"}]}, cookie)
    assert status == 200 and proposed["rules"][0]["value"] == "school.example"


def test_interpret_needs_the_optional_assistant_key(app):
    cookie = signed_in(app)
    status, _, data = call(app, "POST", f"/api/accounts/{EMAIL}/interpret", {"notes": "hi"}, cookie)
    assert status == 422 and "OpenRouter" in data["error"]


def test_nothing_runs_without_jev(app, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cookie = signed_in(app)
    status, _, data = call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 7}, cookie)
    assert status == 409 and "console.typesafe.ai" in data["error"]
    assert call(app, "GET", "/api/state", cookie=cookie)[2]["jev"]["connected"] is False
    Account(app.state_dir, EMAIL).update_settings({"schedule": {"frequency": "hourly"}}, now=1_000_000)
    assert app.scheduler_tick(now=1_000_000 + 7200) == [] and app.fake_runs == []


def test_saving_jev_key_verifies_it_first(app, monkeypatch):
    from inbox_triage.providers.base import ProviderError
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    class Bad:
        def verify(self):
            e = ProviderError("no"); e.status = 401; raise e
    class Good:
        def verify(self): pass
    monkeypatch.setattr(webapp, "make_provider", lambda *a, **k: Bad())
    status, _, data = call(app, "PUT", "/api/keys", {"role": "jev", "key": "wrong"})
    assert status == 422 and "didn't accept" in data["error"]
    assert not (app.config_dir / "secrets.json").exists()
    monkeypatch.setattr(webapp, "make_provider", lambda *a, **k: Good())
    assert call(app, "PUT", "/api/keys", {"role": "jev", "key": "right"})[0] == 200
    assert json.loads((app.config_dir / "secrets.json").read_text()) == {"TYPESAFE_API_KEY": "right"}
    assert (app.config_dir / "secrets.json").stat().st_mode & 0o077 == 0
    assert call(app, "PUT", "/api/keys", {"role": "openai", "key": "x"})[0] == 400


def test_run_job_history_and_single_flight(app):
    cookie = signed_in(app)
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 400}, cookie)[0] == 400
    status, _, job = call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 7, "dry_run": True}, cookie)
    assert status == 200
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        import time; time.sleep(.01)
    assert app.jobs[EMAIL].status == "ok"
    account, kw = app.fake_runs[0]
    assert account == EMAIL and kw["days"] == 7 and kw["runner_kwargs"]["dry_run"] is True
    assert kw["trigger"] == "manual"


def test_scheduler_starts_due_accounts(app):
    acct = Account(app.state_dir, EMAIL)
    acct.update_settings({"schedule": {"frequency": "hourly"}}, now=1_000_000)
    assert app.scheduler_tick(now=1_000_000 + 7200) == [EMAIL]


def test_schedule_slots():
    s = {"frequency": "daily", "hour": 7}
    import datetime as dt
    utc = dt.timezone.utc
    t = int(dt.datetime(2026, 9, 24, 8, 30, tzinfo=utc).timestamp())
    assert latest_slot(s, t, utc) == int(dt.datetime(2026, 9, 24, 7, tzinfo=utc).timestamp())
    early = int(dt.datetime(2026, 9, 24, 6, tzinfo=utc).timestamp())
    assert latest_slot(s, early, utc) == int(dt.datetime(2026, 9, 23, 7, tzinfo=utc).timestamp())
    monthly = {"frequency": "monthly", "hour": 20}
    assert latest_slot(monthly, t, utc) == int(dt.datetime(2026, 8, 31, 20, tzinfo=utc).timestamp())
    weekly = {"frequency": "weekly", "hour": 9, "weekday": 0}  # Mondays
    assert latest_slot(weekly, t, utc) == int(dt.datetime(2026, 9, 21, 9, tzinfo=utc).timestamp())
    assert latest_slot({"frequency": "off"}, t, utc) is None


def test_due_only_after_anchor_and_once_per_slot(tmp_path):
    import datetime as dt
    utc = dt.timezone.utc
    acct = Account(tmp_path, EMAIL)
    saved = int(dt.datetime(2026, 9, 24, 8, tzinfo=utc).timestamp())
    acct.update_settings({"schedule": {"frequency": "daily", "hour": 7}}, now=saved)
    assert not acct.is_due(saved + 60, utc)  # today's 07:00 slot was before the schedule existed
    tomorrow = saved + 86400
    assert acct.is_due(tomorrow, utc)
    acct.record_run({"started": tomorrow, "trigger": "schedule", "status": "ok"})
    assert not acct.is_due(tomorrow + 60, utc)
    assert acct.next_run(tomorrow + 60, utc) == int(dt.datetime(2026, 9, 26, 7, tzinfo=utc).timestamp())


def test_schedule_runs_in_the_zone_it_was_set_in(tmp_path):
    # "Every day at 07:00" picked in Los Angeles means 07:00 there, whatever the server's clock is.
    import datetime as dt
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    acct = Account(tmp_path, EMAIL)
    saved = int(dt.datetime(2026, 9, 25, 12, tzinfo=la).timestamp())
    s = acct.update_settings({"schedule": {"frequency": "daily", "hour": 7, "tz": "America/Los_Angeles"}}, now=saved)
    assert s["schedule"]["tz"] == "America/Los_Angeles"
    assert acct.next_run(saved) == int(dt.datetime(2026, 9, 26, 7, tzinfo=la).timestamp())
    assert not acct.is_due(int(dt.datetime(2026, 9, 26, 6, 59, tzinfo=la).timestamp()))
    assert acct.is_due(int(dt.datetime(2026, 9, 26, 7, 1, tzinfo=la).timestamp()))
    # An unknown zone isn't stored; the schedule falls back to the server's clock.
    assert acct.update_settings({"schedule": {"tz": "Mars/Olympus_Mons"}}, now=saved)["schedule"]["tz"] == ""


def test_daily_slot_runs_once_when_clocks_go_back(tmp_path):
    # 01:00 happens twice in New York on 2026-11-01; a daily 01:00 schedule must run once.
    import datetime as dt
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    acct = Account(tmp_path, EMAIL)
    acct.update_settings({"schedule": {"frequency": "daily", "hour": 1, "tz": "America/New_York"}},
                         now=int(dt.datetime(2026, 10, 31, 12, tzinfo=ny).timestamp()))
    first = int(dt.datetime(2026, 11, 1, 1, 30, tzinfo=ny).timestamp())            # 01:30 EDT
    assert acct.is_due(first)
    acct.record_run({"started": first, "trigger": "schedule", "status": "ok"})
    repeat = int(dt.datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ny).timestamp())   # 01:30 EST, an hour later
    assert repeat - first == 3600 and not acct.is_due(repeat)


def test_run_account_batches_and_records_history(tmp_path, monkeypatch):
    from inbox_triage import runner
    results = iter([{"processed": 100, "remaining": True, "outcomes": {"later": 100}, "mode": "label-only"},
                    {"processed": 5, "remaining": False, "outcomes": {"later": 5}, "mode": "label-only"}])
    monkeypatch.setattr(runner, "run", lambda *a, **k: next(results))
    entry = accounts.run_account(EMAIL, tmp_path, days=30, now=1_000)
    assert entry["processed"] == 105 and entry["outcomes"] == {"later": 105} and entry["status"] == "ok"
    assert Account(tmp_path, EMAIL).runs()[0]["days"] == 30


def test_preferences_match_and_policy_safety():
    prefs = preferences.Preferences.from_json({"rules": [
        {"kind": "domain", "value": "school.example", "action": "important"},
        {"kind": "keyword", "value": "real estate", "action": "not_important"},
        {"kind": "sender", "value": "agent@homes.example", "action": "important"}]})
    school = MailEvidence("m", sender="School <office@mail.school.example>", sender_domain="mail.school.example",
                          subject="Field trip", auth={"dmarc": "pass"})
    spoofed = MailEvidence("m", sender="School <office@school.example>", sender_domain="school.example",
                           subject="Field trip", auth={"dmarc": "fail"})
    listing = MailEvidence("m", sender="x@homes.example", sender_domain="homes.example", subject="New real estate listings")
    assert preferences.match(prefs, school).action == "important"
    assert preferences.match(prefs, spoofed) is None
    assert preferences.match(prefs, listing).action == "not_important"
    agent = MailEvidence("m", sender="agent@homes.example", sender_domain="homes.example",
                         subject="real estate question", auth={"dmarc": "pass"})
    assert preferences.match(prefs, agent).kind == "sender"  # most specific wins
    important = preferences.match(prefs, school)
    assert decide(school, ContextPack(), JevSignals(), preference=important).destination == Destination.FOR_YOU
    assert decide(school, ContextPack(), JevSignals(deceptive=.9), preference=important).destination == Destination.UNCHANGED
    assert decide(listing, ContextPack(), JevSignals(), preference=preferences.match(prefs, listing)).destination == Destination.LATER
    alert = MailEvidence("m", subject="Security alert: real estate portal sign-in", protected_kinds=frozenset({"security"}))
    assert decide(alert, ContextPack(), JevSignals(), preference=preferences.match(prefs, alert)).destination != Destination.LATER


def test_user_notes_reach_the_model_payload():
    from inbox_triage.providers.base import evidence_state
    state = evidence_state(MailEvidence("id"), ContextPack(user_notes="School mail matters."))
    assert state["context"]["user_preferences"] == "School mail matters."
    assert "user_preferences" not in evidence_state(MailEvidence("id"), ContextPack())["context"]


def test_bad_inputs_are_400_not_500(app):
    cookie = signed_in(app)
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": "abc"}, cookie)[0] == 400
    assert call(app, "GET", f"/api/accounts/{EMAIL}/emails?days=abc", cookie=cookie)[0] == 400
    assert call(app, "PUT", f"/api/accounts/{EMAIL}/settings", {"schedule": {"hour": "x"}}, cookie)[0] == 400


def test_each_account_can_bring_its_own_jev_key(app, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    class Good:
        def verify(self): pass
    monkeypatch.setattr(webapp, "make_provider", lambda *a, **k: Good())
    cookie = signed_in(app)
    assert call(app, "GET", "/api/state", cookie=cookie)[2]["accounts"][0]["jev_connected"] is False
    assert call(app, "PUT", f"/api/accounts/{EMAIL}/jev-key", {"key": "mine"}, cookie)[0] == 200
    assert Account(app.state_dir, EMAIL).jev_key() == "mine"
    assert call(app, "GET", "/api/state", cookie=cookie)[2]["accounts"][0]["jev_connected"] is True
    status, _, _ = call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 1}, cookie)
    assert status == 200
    import time
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        time.sleep(.01)
    assert app.fake_runs[-1][1]["runner_kwargs"]["api_key"] == "mine"
    # Hosted users can't set the server-wide key, only their own.
    app.hosted = True
    assert call(app, "PUT", "/api/keys", {"role": "jev", "key": "x"}, cookie)[0] == 403


def test_hosted_server_never_uses_its_own_jev_key_for_users(app, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "operator-key")  # set on the server by mistake
    app.hosted = True
    cookie = signed_in(app)
    state = call(app, "GET", "/api/state", cookie=cookie)[2]
    assert state["jev"]["connected"] is False and state["accounts"][0]["jev_connected"] is False
    status, _, data = call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 1}, cookie)
    assert status == 409 and "Connect Jev" in data["error"]
    Account(app.state_dir, EMAIL).update_settings({"schedule": {"frequency": "hourly"}}, now=1_000_000)
    assert app.scheduler_tick(now=1_000_000 + 7200) == [] and app.fake_runs == []
    # Once the user adds their own key, it (and only it) is used.
    Account(app.state_dir, EMAIL).save_jev_key("users-own-key")
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 1}, cookie)[0] == 200
    import time
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        time.sleep(.01)
    assert app.fake_runs[-1][1]["runner_kwargs"]["api_key"] == "users-own-key"


def test_healthz_is_open_and_leaks_nothing(app):
    status, _, data = call(app, "GET", "/healthz", headers={"HTTP_HOST": "10.0.0.5:8765"})
    assert status == 200 and data["ok"] is True and set(data) == {"ok", "version"}


def test_disconnect_deletes_all_account_data(app, monkeypatch):
    revoked = []
    monkeypatch.setattr(oauth, "revoke", lambda path: revoked.append(path))
    cookie = signed_in(app, (EMAIL, "b@example.org"))
    acct = Account(app.state_dir, EMAIL)
    acct.save_jev_key("k")
    acct.update_settings({"schedule": {"frequency": "daily"}})
    acct.record_run({"started": 1, "status": "ok"})
    (acct.dir / "events.jsonl").write_text('{"id":"m","status":"verified"}\n')
    status, headers, _ = call(app, "DELETE", f"/api/accounts/{EMAIL}", cookie=cookie)
    assert status == 200 and revoked
    assert not (app.config_dir / "tokens" / f"{EMAIL}.json").exists()
    assert not acct.dir.exists()
    assert app.session_emails({"HTTP_COOKIE": headers["Set-Cookie"].split(";")[0]}) == ["b@example.org"]


def test_privacy_policy_page_for_google_consent_screen(app, monkeypatch):
    monkeypatch.setenv("INBOX_TRIAGE_SUPPORT_EMAIL", "help@example.org")
    status, headers, body = call(app, "GET", "/privacy")
    text = body.decode()
    assert status == 200 and "help@example.org" in text and "{{" not in text
    assert "Limited Use" in text and "gmail.modify" in text and "Disconnect account" in text
    assert b"<script" not in body
    index = call(app, "GET", "/")[2].decode()
    assert 'href="/privacy"' in index and "never sends, deletes" in index


def _sign_in_via_callback(app, monkeypatch, email):
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("INBOX_TRIAGE_OAUTH_CLIENT_SECRET", "s")
    _, _, data = call(app, "POST", "/api/login", {"email": email})
    state = data["url"].split("state=")[1].split("&")[0]
    creds = SimpleNamespace(granted_scopes=[oauth.SCOPE], refresh_token="rt-" + email, token="at",
                            to_json=lambda: '{"refresh_token":"r"}')
    monkeypatch.setattr(oauth, "exchange", lambda *a: creds)
    monkeypatch.setattr(oauth, "profile_email", lambda c: email)
    return call(app, "GET", f"/oauth/callback?state={state}&code=abc")


def test_free_beta_caps_new_accounts_but_not_returning_ones(app, monkeypatch):
    revoked = []
    monkeypatch.setattr(oauth, "revoke_token", lambda t: revoked.append(t))
    monkeypatch.setenv("INBOX_TRIAGE_MAX_ACCOUNTS", "2")
    monkeypatch.setenv("INBOX_TRIAGE_BETA_ENDS", "2026-10-31")
    beta = call(app, "GET", "/api/state")[2]["beta"]
    # No counts are exposed: just the limit and the end date.
    assert beta == {"enabled": True, "max_accounts": 2, "ends": "2026-10-31", "ended": beta["ended"]}
    status, headers, _ = _sign_in_via_callback(app, monkeypatch, "second@example.org")  # fixture has 1 account
    assert headers["Location"].startswith("/#account=") and app.beta_full()
    # A third person is turned away, and the fresh Google grant is revoked.
    status, headers, _ = _sign_in_via_callback(app, monkeypatch, "third@example.org")
    assert "beta%20is%20full" in headers["Location"] and revoked == ["rt-third@example.org"]
    assert not (app.config_dir / "tokens" / "third@example.org.json").exists()
    # Existing users can always sign back in.
    status, headers, _ = _sign_in_via_callback(app, monkeypatch, EMAIL)
    assert headers["Location"].startswith("/#account=")


def test_no_beta_limits_by_default(app, monkeypatch):
    monkeypatch.delenv("INBOX_TRIAGE_MAX_ACCOUNTS", raising=False)
    monkeypatch.delenv("INBOX_TRIAGE_BETA_ENDS", raising=False)
    assert call(app, "GET", "/api/state")[2]["beta"]["enabled"] is False


def test_junk_rules_are_saved_and_the_assistant_may_propose_them(app):
    from inbox_triage import onboarding
    cookie = signed_in(app)
    rules = {"rules": [{"kind": "domain", "value": "promo-blast.example", "action": "junk", "note": ""},
                       {"kind": "keyword", "value": "flash sale", "action": "bogus"}], "summary": ""}
    _, _, saved = call(app, "PUT", f"/api/accounts/{EMAIL}/preferences", rules, cookie)
    assert saved["rules"] == [{"kind": "domain", "value": "promo-blast.example", "action": "junk", "note": ""}]
    assert "junk" in onboarding.SCHEMA["properties"]["rules"]["items"]["properties"]["action"]["enum"]


def test_paused_run_resumes_itself_after_gmails_break(app):
    acct = Account(app.state_dir, EMAIL)
    acct.record_run({"started": 1_000, "trigger": "manual", "status": "paused", "resume_at": 2_000, "days": 7,
                     "mode": "label-only", "processed": 40})
    state = call(app, "GET", "/api/state", cookie=signed_in(app))[2]
    assert state["accounts"][0]["resume_at"] == 2_000
    assert app.scheduler_tick(now=1_500) == []          # not before the time Gmail gave
    assert app.scheduler_tick(now=2_001) == [EMAIL]
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        import time; time.sleep(.01)
    account, kw = app.fake_runs[-1]
    assert kw["trigger"] == "resume" and kw["days"] == 7 and kw["runner_kwargs"]["dry_run"] is False


def test_auto_resume_gives_up_after_a_few_tries_but_clicks_dont_count(tmp_path):
    acct = Account(tmp_path, EMAIL)
    acct.record_run({"started": 0, "trigger": "manual", "status": "paused", "resume_at": 10})
    for i in range(accounts.MAX_RESUMES - 1):
        acct.record_run({"started": 1 + i, "trigger": "resume", "status": "paused", "resume_at": 10})
    for i in range(20):  # clicking Run now while paused never uses up retries...
        acct.record_run({"started": 100 + i, "trigger": "manual", "status": "paused", "resume_at": 10})
    assert acct.due_run(now=11)[0] == "resume"
    acct.record_run({"started": 200, "trigger": "resume", "status": "paused", "resume_at": 10})
    for i in range(20):  # ...and can't push earlier automatic attempts out of the count either
        acct.record_run({"started": 300 + i, "trigger": "manual", "status": "paused", "resume_at": 10})
    assert acct.pending_resume() is None and acct.due_run(now=11) is None
    # Out of automatic resumes, but Gmail's latest break still holds back a due schedule until it ends.
    acct.update_settings({"schedule": {"frequency": "hourly"}}, now=0)
    acct.record_run({"started": 500, "trigger": "resume", "status": "paused", "resume_at": 20_000})
    assert acct.pending_resume() is None and acct.is_due(10_800)
    assert acct.due_run(now=10_800) is None and acct.due_run(now=20_000) == ("schedule", None)
    acct.record_run({"started": 400, "trigger": "schedule", "status": "ok"})
    assert acct.pending_resume() is None


def test_a_paused_preview_is_not_replayed_but_still_holds_the_schedule(tmp_path):
    # A preview keeps no progress, so resuming it would repeat the same Jev calls from the start.
    acct = Account(tmp_path, EMAIL)
    acct.update_settings({"schedule": {"frequency": "hourly"}}, now=0)
    acct.record_run({"started": 100, "trigger": "manual", "status": "paused", "resume_at": 20_000, "mode": "dry-run"})
    assert acct.pending_resume() is None
    assert acct.is_due(10_800) and acct.due_run(10_800) is None      # Gmail's break still holds the schedule
    assert acct.due_run(20_000) == ("schedule", None)                  # then the schedule runs, not a replay


def test_schedule_waits_for_gmails_break_then_the_paused_run_goes_first(app):
    # A 30-day rescan paused at 1_000 until 90_000; the hourly schedule comes due meanwhile.
    acct = Account(app.state_dir, EMAIL)
    acct.update_settings({"schedule": {"frequency": "hourly"}}, now=500)
    acct.record_run({"started": 1_000, "trigger": "manual", "status": "paused", "resume_at": 90_000, "days": 30,
                     "mode": "label-only"})
    assert acct.is_due(20_000) and acct.due_run(20_000) is None
    assert app.scheduler_tick(now=20_000) == []            # Gmail isn't contacted during its break
    assert app.scheduler_tick(now=90_001) == [EMAIL]
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        import time; time.sleep(.01)
    _, kw = app.fake_runs[-1]
    assert kw["trigger"] == "resume" and kw["days"] == 30   # the rescan's window is kept


def test_cli_due_mode_honours_a_pending_pause(tmp_path, monkeypatch, capsys):
    from inbox_triage import runner
    cfg, state = tmp_path / "cfg", tmp_path / "state"
    (cfg / "tokens").mkdir(parents=True)
    (cfg / "tokens" / f"{EMAIL}.json").write_text("{}")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a, **k: calls.append(k) or {"processed": 0, "remaining": False})
    acct = Account(state, EMAIL)
    acct.update_settings({"schedule": {"frequency": "hourly"}}, now=0)
    import time
    acct.record_run({"started": 1, "trigger": "manual", "status": "paused", "resume_at": int(time.time()) + 900,
                     "days": 14, "mode": "label-only"})
    args = ["--all", "--due", "--config-dir", str(cfg), "--state-dir", str(state)]
    assert runner.main(args) == 0 and calls == []           # due by the clock, but Gmail asked for a break
    pinned = int(time.time()) - 15 * 86400
    acct.record_run({"started": 2, "trigger": "manual", "status": "paused", "resume_at": 5, "days": 14,
                     "since": pinned, "mode": "label-only"})
    assert runner.main(args) == 0
    assert calls and calls[0]["since_days"] == 14 and acct.runs(1)[0]["trigger"] == "resume"
    assert calls[0]["since"] == pinned == acct.runs(1)[0]["since"]  # the rescan's window didn't slide
    assert calls[0]["dry_run"] is False
    # --dry-run promises Gmail is never changed, even when resuming a paused label-writing run.
    acct.record_run({"started": 3, "trigger": "resume", "status": "paused", "resume_at": 5, "days": 14,
                     "mode": "label-only"})
    assert runner.main(args + ["--dry-run"]) == 0 and calls[1]["dry_run"] is True


def test_paused_job_status_and_throttled_dashboard_reads(app, monkeypatch):
    from inbox_triage.gmail.client import RateLimited
    import time
    app.run_fn = lambda account, root, **kw: {"status": "paused", "resume_at": int(time.time()) + 600,
                                              "reason": "userRateLimitExceeded", "processed": 3}
    cookie = signed_in(app)
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {}, cookie)[0] == 200
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            break
        time.sleep(.01)
    assert app.jobs[EMAIL].status == "paused"
    def throttled(email):
        raise RateLimited(403, "User-rate limit exceeded.", "userRateLimitExceeded", retry_at=time.time() + 600)
    monkeypatch.setattr(app, "client", throttled)
    status, _, body = call(app, "GET", f"/api/accounts/{EMAIL}/emails", cookie=cookie)
    assert status == 429 and "about 10 minutes" in body["error"]


def test_dashboard_reuses_recent_message_details(app, monkeypatch):
    fetches = []
    class Client:
        def get_many(self, ids, full=False):
            fetches.append(list(ids))
            return [{"id": i, "payload": {"headers": [{"name": "Subject", "value": "Hi " + i}]}} for i in ids]
    monkeypatch.setattr(app, "client", lambda email: Client())
    first, ok = app.decorate(EMAIL, [{"id": "m1"}, {"id": "m2"}])
    assert ok and [d["subject"] for d in first] == ["Hi m1", "Hi m2"]
    again, ok = app.decorate(EMAIL, [{"id": "m1"}, {"id": "m3"}])
    assert fetches == [["m1", "m2"], ["m3"]] and again[0]["subject"] == "Hi m1"
    monkeypatch.setattr(app, "client", lambda email: (_ for _ in ()).throw(RuntimeError("Gmail unreachable")))
    missing, ok = app.decorate(EMAIL, [{"id": "m9"}])
    assert not ok and "subject" not in missing[0]    # the page says "unavailable", not "deleted"


def test_run_account_records_a_pause_with_its_progress(tmp_path, monkeypatch):
    from inbox_triage import runner
    results = iter([{"processed": 100, "gmail_changes": 80, "remaining": True, "outcomes": {"later": 80}},
                    {"processed": 12, "gmail_changes": 9, "remaining": True, "outcomes": {"later": 9},
                     "paused_until": 5_000, "pause_code": "userRateLimitExceeded",
                     "pause_reason": "Gmail API HTTP 403 (userRateLimitExceeded): User-rate limit exceeded."}])
    batches = []
    monkeypatch.setattr(runner, "run", lambda *a, **k: batches.append(1) or next(results))
    entry = accounts.run_account(EMAIL, tmp_path, now=1_000)
    assert len(batches) == 2  # no more batches once Gmail asks for a break
    assert entry["status"] == "paused" and entry["resume_at"] == 5_000 and entry["reason"] == "userRateLimitExceeded"
    assert entry["processed"] == 112 and entry["gmail_changes"] == 89
    acct = Account(tmp_path, EMAIL)
    assert acct.due_run(now=4_999) is None and acct.due_run(now=5_000) == ("resume", acct.runs(1)[0])


def test_a_rescan_keeps_its_window_across_batches_and_resumes(tmp_path, monkeypatch):
    from inbox_triage import runner
    seen = []
    results = iter([{"processed": 100, "remaining": True}, {"processed": 3, "remaining": True, "paused_until": 9_000_000},
                    {"processed": 50, "remaining": False}])
    monkeypatch.setattr(runner, "run", lambda *a, **k: seen.append(k["since"]) or next(results))
    start = 5_000_000
    first = accounts.run_account(EMAIL, tmp_path, days=30, now=start)
    assert first["status"] == "paused" and first["since"] == start - 30 * 86400
    later = accounts.run_account(EMAIL, tmp_path, trigger="resume", days=30, since=first["since"], now=start + 7200)
    assert later["since"] == first["since"] and seen == [first["since"]] * 3  # the window never slides forward


def test_runner_keeps_the_pinned_window_however_long_the_pauses(tmp_path, monkeypatch):
    from inbox_triage import runner
    from inbox_triage.context import ContextSyncStats
    queries = []
    class Client:
        def __init__(self, token, **kw): pass
        def profile(self): return {"emailAddress": EMAIL, "historyId": "8"}
    monkeypatch.setattr(runner, "GmailReadOnlyClient", Client)
    monkeypatch.setattr(runner, "list_ids", lambda client, query: queries.append(query) or [])
    monkeypatch.setattr(runner, "ensure_labels", lambda client: {})
    monkeypatch.setattr(runner, "make_provider", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: ContextSyncStats())
    now = 50_000_000
    ninety = now - 90 * 86400 - 7200        # a 90-day rescan resumed two hours after it started
    long_paused = now - 90 * 86400 - 6 * 2 * 86400  # ...or after six two-day breaks
    runner.run(EMAIL, tmp_path / "t.json", tmp_path / "s", now=now, since_days=30, since=now - 31 * 86400)
    runner.run(EMAIL, tmp_path / "t.json", tmp_path / "s", now=now, since_days=90, since=ninety)
    runner.run(EMAIL, tmp_path / "t.json", tmp_path / "s", now=now, since_days=90, since=long_paused)
    runner.run(EMAIL, tmp_path / "t.json", tmp_path / "s", now=now, since_days=30)
    assert queries[0].startswith(f"after:{now - 31 * 86400} ")
    assert queries[1].startswith(f"after:{ninety} ")         # kept, not slid forward
    assert queries[2].startswith(f"after:{long_paused} ")
    assert queries[3].startswith(f"after:{now - 30 * 86400} ")  # a new rescan starts its own window


def test_resumes_and_run_now_after_a_pause_keep_the_rescan_window(app):
    acct = Account(app.state_dir, EMAIL)
    acct.record_run({"started": 1_000, "trigger": "manual", "status": "paused", "resume_at": 2_000, "days": 30,
                     "since": 777, "mode": "label-only"})
    def wait():
        import time
        for _ in range(100):
            if app.jobs[EMAIL].status != "running":
                return
            time.sleep(.01)
    assert app.scheduler_tick(now=2_001) == [EMAIL]
    wait()
    assert app.fake_runs[-1][1]["since"] == 777 and app.fake_runs[-1][1]["days"] == 30
    cookie = signed_in(app)
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 30}, cookie)[0] == 200
    wait()
    assert app.fake_runs[-1][1]["since"] == 777              # Run now with the same dates continues the rescan
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 7}, cookie)[0] == 200
    wait()
    assert app.fake_runs[-1][1]["since"] is None             # different dates: a new window
