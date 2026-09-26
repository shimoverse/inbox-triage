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
                status, body, *headers = responses.pop(0) if len(responses) > 1 else responses[0]
                if status >= 400:
                    raise urllib.error.HTTPError(req.full_url, status, "err", headers[0] if headers else {},
                                                 io.BytesIO(json.dumps(body).encode()))
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


def test_rate_limit_403_backs_off_and_retries(tmp_path, monkeypatch):
    # Gmail says "slow down" with a 403 and a rate-limit reason; that must be retried, not fatal.
    http = FakeHTTP(monkeypatch)
    limited = {"error": {"code": 403, "message": "User-rate limit exceeded.",
                         "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}]}}
    http.route("/labels", (403, limited), (403, limited), (200, {"labels": [{"id": "L1", "name": "Triage/Later"}]}))
    assert gc.GmailClient(token_file(tmp_path, expired=False)).labels() == [{"id": "L1", "name": "Triage/Later"}]
    assert len(http.requests) == 3


def test_other_403_keeps_googles_reason(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("/messages", (403, {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                                             "status": "PERMISSION_DENIED",
                                             "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}))
    with pytest.raises(gc.GmailError) as exc:
        gc.GmailClient(token_file(tmp_path, expired=False)).list_ids("in:inbox", 5)
    assert exc.value.reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT" and len(http.requests) == 1
    assert str(exc.value) == ("Gmail API HTTP 403 (ACCESS_TOKEN_SCOPE_INSUFFICIENT): "
                              "Request had insufficient authentication scopes.")


def test_requests_are_paced_across_threads(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    waits = []
    monkeypatch.setattr(gc.time, "sleep", waits.append)
    monkeypatch.setattr(gc.time, "monotonic", lambda: 100.0)  # a burst: every request asks at the same instant
    monkeypatch.setattr(gc, "RATE_STEP", 0)
    http.route("/messages/", (200, {"id": "m"}))
    gc.GmailClient(token_file(tmp_path, expired=False)).get_many([f"m{i}" for i in range(5)])
    assert sorted(round(w, 3) for w in waits) == [round(i / gc.START_RATE, 3) for i in range(1, 5)]


def test_clients_for_one_mailbox_share_a_throttle(tmp_path):
    # Gmail's limits are per mailbox: a run and the dashboard reading it must not add up past them.
    token = token_file(tmp_path, expired=False)
    other = tmp_path / "other"
    other.mkdir()
    assert gc.GmailClient(token).throttle is gc.GmailClient(token).throttle
    assert gc.GmailClient(token).throttle is not gc.GmailClient(token_file(other, expired=False)).throttle


LIMITED = {"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded", "domain": "usageLimits"}],
                     "message": "User-rate limit exceeded.  Retry after {when}"}}


def limited_until(seconds):
    when = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + seconds))
    return {"error": {**LIMITED["error"], "message": LIMITED["error"]["message"].format(when=when)}}


def test_long_retry_after_stops_at_once_and_holds_every_client(tmp_path, monkeypatch):
    # Gmail asks for a 15-minute break: don't hammer it with retries, report when to come back.
    http = FakeHTTP(monkeypatch)
    http.route("/messages/", (403, limited_until(900)))
    token = token_file(tmp_path, expired=False)
    with pytest.raises(gc.RateLimited) as exc:
        gc.GmailClient(token).get_many([f"m{i}" for i in range(40)])
    assert 880 < exc.value.retry_at - time.time() <= 900
    assert "userRateLimitExceeded" in str(exc.value) and "Retry after" in str(exc.value)
    sent = len(http.requests)
    assert sent <= gc.FETCH_WORKERS  # the reads still queued were dropped, not sent
    with pytest.raises(gc.RateLimited):  # another client for the same mailbox waits too, without asking Gmail
        gc.GmailClient(token).profile()
    assert len(http.requests) == sent


def test_a_request_waiting_its_turn_still_honours_a_new_break(tmp_path, monkeypatch):
    # A dashboard read queued behind a run's reads must not go out once Gmail has asked for a break.
    import threading
    http = FakeHTTP(monkeypatch)
    http.route("/profile", (200, {"emailAddress": "a@example.org", "historyId": 1}))
    client = gc.GmailClient(token_file(tmp_path, expired=False))
    for _ in range(gc.MAX_IN_FLIGHT):
        client.throttle.slots.acquire()  # every slot busy
    outcome = []
    def read():
        try:
            outcome.append(client.profile())
        except gc.RateLimited as exc:
            outcome.append(exc)
    reader = threading.Thread(target=read)
    reader.start()
    threading.Event().wait(0.1)  # the read has passed the pace check and is queued for a slot
    client.throttle.block(gc.RateLimited(403, "User-rate limit exceeded.", "userRateLimitExceeded",
                                         retry_at=time.time() + 600))
    for _ in range(gc.MAX_IN_FLIGHT):
        client.throttle.slots.release()
    reader.join(5)
    assert isinstance(outcome[0], gc.RateLimited) and http.requests == []


def test_a_request_waiting_its_turn_still_honours_a_short_pause(tmp_path, monkeypatch):
    import threading
    http = FakeHTTP(monkeypatch)
    waits = []
    monkeypatch.setattr(gc.time, "sleep", waits.append)
    http.route("/profile", (200, {"emailAddress": "a@example.org", "historyId": 1}))
    client = gc.GmailClient(token_file(tmp_path, expired=False))
    for _ in range(gc.MAX_IN_FLIGHT):
        client.throttle.slots.acquire()
    reader = threading.Thread(target=client.profile)
    reader.start()
    threading.Event().wait(0.1)  # queued for a slot
    client.throttle.pushed_back(5)  # another request was told to slow down for 5 seconds
    for _ in range(gc.MAX_IN_FLIGHT):
        client.throttle.slots.release()
    reader.join(5)
    assert len(http.requests) == 1 and max(waits) > 4.5  # it waited out the pause before going


def test_a_slow_down_is_recorded_before_the_slot_is_freed(tmp_path, monkeypatch):
    # Otherwise a request queued for the slot could take it and reach Gmail before the break is known.
    http = FakeHTTP(monkeypatch)
    http.route("/profile", (429, {"error": {"code": 429, "message": "Too many concurrent requests for user"}},
                            {"Retry-After": "3"}), (403, limited_until(900)))
    client = gc.GmailClient(token_file(tmp_path, expired=False))
    known = []
    class Slots:
        def __enter__(self): return self
        def __exit__(self, *exc):
            t = client.throttle
            known.append((t.hold_until > time.monotonic(), t.blocked_until > time.time()))
    client.throttle.slots = Slots()
    with pytest.raises(gc.RateLimited):
        client.profile()
    assert known == [(True, False), (True, True)]  # the pause, then the long break, each before release


def test_a_hold_extended_while_waiting_is_waited_out_too(monkeypatch):
    throttle, waits = gc.Throttle(), []
    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            throttle.pushed_back(10)  # meanwhile another request is told to wait longer
    monkeypatch.setattr(gc.time, "sleep", sleep)
    throttle.pushed_back(2)
    throttle.check()
    assert len(waits) == 3 and waits[0] <= 2 and waits[1] > 9  # (the third is its turn at the slower pace)


def test_requests_held_by_a_pause_leave_it_one_at_a_time(monkeypatch):
    throttle, waits = gc.Throttle(), []
    monkeypatch.setattr(gc.time, "sleep", waits.append)
    monkeypatch.setattr(gc.time, "monotonic", lambda: 100.0)
    throttle.pushed_back(2)  # the pace halves to 5 a second and everything holds until 102
    for _ in range(3):       # three requests that were already queued for a slot
        throttle.check()
    assert [round(w, 3) for w in waits[1::2]] == [2.0, 2.2, 2.4]  # then spaced at the slower pace, not a burst


def test_a_break_asked_for_while_taking_a_turn_after_a_pause_is_honoured(monkeypatch):
    throttle, waits = gc.Throttle(), []
    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:  # during its turn at the slower pace, another request is told to stay away
            throttle.block(gc.RateLimited(403, "User-rate limit exceeded.", "userRateLimitExceeded",
                                          retry_at=time.time() + 600))
    monkeypatch.setattr(gc.time, "sleep", sleep)
    monkeypatch.setattr(gc.time, "monotonic", lambda: 100.0)
    throttle.pushed_back(2)
    with pytest.raises(gc.RateLimited):
        throttle.check()


def test_short_retry_after_is_honoured_and_slows_the_pace(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    waits = []
    monkeypatch.setattr(gc.time, "sleep", waits.append)
    http.route("/profile", (429, {"error": {"code": 429, "message": "Too many concurrent requests for user"}},
                            {"Retry-After": "7"}), (200, {"emailAddress": "a@example.org", "historyId": 1}))
    client = gc.GmailClient(token_file(tmp_path, expired=False))
    assert client.profile()["emailAddress"] == "a@example.org"
    assert max(waits) > 6.9 and client.throttle.rate < gc.START_RATE and client.throttle.pushbacks == 1


def test_endless_rate_limits_become_a_pause_not_a_crash(tmp_path, monkeypatch):
    http = FakeHTTP(monkeypatch)
    http.route("/labels", (403, {"error": {"code": 403, "message": "Rate Limit Exceeded",
                                           "errors": [{"reason": "rateLimitExceeded"}]}}))
    with pytest.raises(gc.RateLimited) as exc:
        gc.GmailClient(token_file(tmp_path, expired=False)).labels()
    assert len(http.requests) == gc.RETRIES + 1
    assert abs(exc.value.retry_at - time.time() - gc.COOL_DOWN) < 5


def test_retry_after_parsing():
    class E:
        def __init__(self, headers): self.headers = headers
    assert gc._retry_after(E({"Retry-After": "12"})) == 12
    assert 25 < gc._retry_after(E({}), limited_until(30)["error"]["message"]) <= 30
    assert gc._retry_after(E({}), "Rate Limit Exceeded") == 0
    assert gc._retry_after(E(None), "") == 0
    assert gc._retry_after(E({"Retry-After": "inf"})) == 2 * 86400 and gc._retry_after(E({"Retry-After": "nan"})) == 0
    assert gc._retry_after(E({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0  # already past


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
