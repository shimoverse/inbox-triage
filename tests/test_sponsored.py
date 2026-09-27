"""Jev through OpenRouter, a sponsored beta's daily allowance, and label colours. Synthetic data only; no network."""
import json
import urllib.error
import urllib.request

import pytest

from inbox_triage import accounts, config, runner
from inbox_triage.context import ContextSyncStats
from inbox_triage.models import ContextPack, JevSignals, MailEvidence
from inbox_triage.providers import make_provider
from inbox_triage.providers.base import questions
from inbox_triage.web import app as webapp

from test_web import EMAIL, call, signed_in, app  # noqa: F401 - the web fixture


class _Resp:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return json.dumps(self.data).encode()


def _answers():
    answers = {k: {"type": "noul", "noul": .05} for k, q in questions().items() if q["type"] == "noul"}
    answers["category"] = {"type": "choice", "choice": "update", "probabilities": {"update": .8}, "confidence": .8}
    return answers


def test_an_openrouter_key_reaches_jev_through_openrouter(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_BASE", raising=False)
    sent = []
    def urlopen(req, timeout):
        sent.append(req)
        return _Resp({"answers": _answers(), "usage": {"input_tokens": 1500, "output_tokens": 0, "cost": 0.000063},
                      "id": "gen-1", "provider": "TypeSafe"})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    signals, usage = make_provider("jev", api_key="sk-or-v1-test").classify_with_usage(
        MailEvidence("secret-id", subject="Statement"), ContextPack())
    assert sent[0].full_url == "https://openrouter.ai/api/v1/systemone"
    assert sent[0].get_header("Authorization") == "Bearer sk-or-v1-test"
    assert json.loads(sent[0].data)["model"] == "jev-latest" and "secret-id" not in sent[0].data.decode()
    assert signals.category == "update" and usage == {"input_tokens": 1500, "output_tokens": 0, "cost": 0.000063}
    # A TypeSafe key still goes to TypeSafe, and an explicit base URL always wins.
    make_provider("jev", api_key="ts-key").classify_with_usage(MailEvidence("m"), ContextPack())
    assert sent[1].full_url == "https://api.typesafe.ai/v1/systemone" and "sk-or" not in str(sent[1].headers)
    monkeypatch.setenv("TYPESAFE_API_BASE", "https://jev.example")
    make_provider("jev", api_key="sk-or-v1-test").classify_with_usage(MailEvidence("m"), ContextPack())
    assert sent[2].full_url == "https://jev.example/v1/systemone"


def test_one_openrouter_key_serves_jev_when_there_is_no_typesafe_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-local")
    monkeypatch.delenv("INBOX_TRIAGE_SPONSORED_JEV_DAILY", raising=False)
    assert config.api_key_for("jev", tmp_path) == "sk-or-v1-local"
    assert config.jev_access(config_dir=tmp_path) == config.JevAccess("sk-or-v1-local")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    assert config.api_key_for("jev", tmp_path) == "ts-key"  # a TypeSafe key, when there is one, comes first


def test_hosted_server_pays_only_in_a_sponsored_beta_and_only_up_to_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-operator")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("INBOX_TRIAGE_SPONSORED_JEV_DAILY", raising=False)
    with pytest.raises(config.JevRequired):
        config.jev_access(config_dir=tmp_path, hosted=True)
    monkeypatch.setenv("INBOX_TRIAGE_SPONSORED_JEV_DAILY", "not-a-number")
    with pytest.raises(config.JevRequired):
        config.jev_access(config_dir=tmp_path, hosted=True)
    monkeypatch.setenv("INBOX_TRIAGE_SPONSORED_JEV_DAILY", "500")
    assert config.jev_access(config_dir=tmp_path, hosted=True) == config.JevAccess("sk-or-v1-operator", None, 500)
    # A user's own key always wins, with no limit.
    assert config.jev_access(config_dir=tmp_path, account_key="mine", hosted=True) == config.JevAccess("mine")


def _wait(app):  # noqa: F811
    import time
    for _ in range(100):
        if app.jobs[EMAIL].status != "running":
            return
        time.sleep(.01)


def test_sponsored_beta_needs_no_key_step(app, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-operator")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("INBOX_TRIAGE_SPONSORED_JEV_DAILY", "300")
    app.hosted = True
    cookie = signed_in(app)
    state = call(app, "GET", "/api/state", cookie=cookie)[2]
    assert state["jev"]["sponsored"] is True and state["accounts"][0]["jev_connected"] is True
    assert state["accounts"][0]["own_jev_key"] is False
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 7}, cookie)[0] == 200
    _wait(app)
    kw = app.fake_runs[-1][1]
    assert kw["runner_kwargs"]["api_key"] == "sk-or-v1-operator" and kw["jev_daily_limit"] == 300
    # Bringing your own key is still possible, and then nothing is capped or sponsored.
    webapp.Account(app.state_dir, EMAIL).save_jev_key("users-own-key")
    assert call(app, "POST", f"/api/accounts/{EMAIL}/run", {"days": 1}, cookie)[0] == 200
    _wait(app)
    kw = app.fake_runs[-1][1]
    assert kw["runner_kwargs"]["api_key"] == "users-own-key" and kw["jev_daily_limit"] is None


def test_operator_key_check_logs_the_outcome_without_the_key(monkeypatch, capsys):
    from inbox_triage.providers.base import ProviderError
    class Bad:
        def verify(self):
            e = ProviderError("no"); e.status = 401; raise e
    monkeypatch.setattr(webapp, "make_provider", lambda *a, **k: Bad())
    assert webapp.check_operator_key("sk-or-v1-secret") is False
    err = capsys.readouterr().err
    assert "check failed status=401" in err and "sk-or" not in err


def _fake_mailbox(monkeypatch, ids, jev):
    labeled = {}
    class Client:
        def __init__(self, token, **kw): pass
        def profile(self): return {"emailAddress": "a@example.org", "historyId": "8"}
        def _get(self, mid, *, full): return {"id": mid, "payload": {"mimeType": "text/plain", "headers": []}}
        def attachment_data(self, mid, aid): return ""
        def message_labels(self, mid): return set(labeled.get(mid, ()))
        def modify_labels(self, mid, add, remove): labeled.setdefault(mid, set()).update(add)
    monkeypatch.setattr(runner, "GmailReadOnlyClient", Client)
    monkeypatch.setattr(runner, "list_ids", lambda client, query: list(ids))
    monkeypatch.setattr(runner, "ensure_labels", lambda client, *extra, **kw: {n: n for n in runner.LABELS})
    monkeypatch.setattr(runner, "make_provider", lambda *a, **kw: jev)
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: ContextSyncStats())
    monkeypatch.setattr(runner, "build_context", lambda *a, **kw: ContextPack())
    return labeled


class _Jev:
    def __init__(self): self.asked = []
    def classify_with_usage(self, evidence, context):
        self.asked.append(evidence.message_id)
        return JevSignals(personal_relevance=.99), {"input_tokens": 1500, "cost": 0.0001}


def test_daily_allowance_pauses_the_run_until_midnight_utc_and_carries_on(tmp_path, monkeypatch):
    jev = _Jev()
    _fake_mailbox(monkeypatch, ["m1", "m2", "m3", "m4", "m5"], jev)
    day = 1_790_000_000  # 2026-09-21T14:13:20Z
    first = accounts.run_account("a@example.org", tmp_path, jev_daily_limit=3, now=day,
                                 runner_kwargs={"token": tmp_path / "t.json", "api_key": "k"})
    assert first["status"] == "paused" and first["reason"] == "sponsored_limit"
    assert first["resume_at"] == accounts.next_utc_midnight(day) == 1_790_035_200
    assert first["jev_calls"] == 3 and first["processed"] == 3 and first["jev_cost"] == 0.0003
    acct = accounts.Account(tmp_path, "a@example.org")
    assert acct.sponsored_calls(day) == 3
    # Nothing is failed or skipped, so nothing is lost; the break holds back the schedule too.
    assert acct.pending_resume()["resume_at"] == 1_790_035_200 and acct.due_run(day + 60) is None
    # Later the same day there is still no allowance: it pauses again without asking Jev.
    again = accounts.run_account("a@example.org", tmp_path, jev_daily_limit=3, now=day + 600,
                                 runner_kwargs={"token": tmp_path / "t.json", "api_key": "k"})
    assert again["status"] == "paused" and again["jev_calls"] == 0 and jev.asked == ["m1", "m2", "m3"]
    # After midnight UTC the allowance refills and the rest is sorted, each email asked once.
    after = accounts.run_account("a@example.org", tmp_path, jev_daily_limit=3, now=1_790_035_200 + 60,
                                 runner_kwargs={"token": tmp_path / "t.json", "api_key": "k"}, trigger="resume")
    assert after["status"] == "ok" and after["jev_calls"] == 2 and jev.asked == ["m1", "m2", "m3", "m4", "m5"]
    assert acct.sponsored_calls(1_790_035_200 + 60) == 2


def test_out_of_credits_pauses_instead_of_failing_emails(tmp_path, monkeypatch):
    from inbox_triage.providers.jev import JevError
    class Broke:
        def classify_with_usage(self, evidence, context):
            error = JevError("Jev HTTP 402"); error.status = 402; raise error
    _fake_mailbox(monkeypatch, ["m1", "m2"], Broke())
    result = runner.run("a@example.org", tmp_path / "t.json", tmp_path / "state", now=5_000_000)
    assert result["paused_until"] == 5_000_000 + runner.CREDITS_RETRY and result["pause_code"] == "jev_credits"
    assert result["processed"] == 0 and result["provider_failures"] == 0 and not result["cursor_advanced"]
    journal = runner.scoped_directory(tmp_path / "state", "a@example.org") / "events.jsonl"
    assert not journal.exists() or not journal.read_text()  # no email was marked failed


def test_labels_are_created_with_colours_and_old_ones_coloured_once():
    made, colored = [], []
    existing = [{"id": "L1", "name": "Triage/Later"},
                {"id": "L2", "name": "Triage/For You", "color": {"backgroundColor": "#000000", "textColor": "#ffffff"}}]
    class Client:
        def labels(self): return existing + [{"id": n, "name": n, "color": c} for n, c in made]
        def create_label(self, name, color=None): made.append((name, color))
        def color_label(self, label_id, color): colored.append((label_id, color))
    ids = runner.ensure_labels(Client())
    assert ("Triage/Needs You", {"backgroundColor": "#ffc8af", "textColor": "#7a2e0b"}) in made
    assert all(color for _, color in made) and not colored  # recolouring is opt-in
    runner.ensure_labels(Client(), recolor=True)
    # The old uncoloured label gets its colour; one someone already coloured is left alone.
    assert colored == [("L1", {"backgroundColor": "#e7e7e7", "textColor": "#464646"})]
    assert ids["Triage/Later"] == "L1"


def test_a_colour_gmail_rejects_never_blocks_labeling():
    from inbox_triage.gmail.client import GmailError, RateLimited
    made = []
    class Client:
        def labels(self): return [{"id": n, "name": n} for n in made]
        def create_label(self, name, color=None):
            if color:
                raise GmailError(400, "Invalid label color")
            made.append(name)
        def color_label(self, label_id, color): raise GmailError(400, "Invalid label color")
    ids = runner.ensure_labels(Client(), recolor=True)
    assert ids["Triage/Needs You"] == "Triage/Needs You"  # created without a colour, and the run goes on
    class Throttled(Client):
        def color_label(self, label_id, color): raise RateLimited(429, "slow down", retry_at=1.0)
    with pytest.raises(RateLimited):  # a Gmail break still pauses the run
        runner.ensure_labels(Throttled(), recolor=True)


def test_every_label_colour_is_in_gmails_palette():
    palette = {"#000000", "#434343", "#666666", "#999999", "#cccccc", "#efefef", "#f3f3f3", "#ffffff", "#fb4c2f",
               "#ffad47", "#fad165", "#16a766", "#43d692", "#4a86e8", "#a479e2", "#f691b3", "#f6c5be", "#ffe6c7",
               "#fef1d1", "#b9e4d0", "#c6f3de", "#c9daf8", "#e4d7f5", "#fcdee8", "#efa093", "#ffd6a2", "#fce8b3",
               "#89d3b2", "#a0eac9", "#a4c2f4", "#d0bcf1", "#fbc8d9", "#e66550", "#ffbc6b", "#fcda83", "#44b984",
               "#68dfa9", "#6d9eeb", "#b694e8", "#f7a7c0", "#cc3a21", "#eaa041", "#f2c960", "#149e60", "#3dc789",
               "#3c78d8", "#8e63ce", "#e07798", "#ac2b16", "#cf8933", "#d5ae49", "#0b804b", "#2a9c68", "#285bac",
               "#653e9b", "#b65775", "#822111", "#a46a21", "#aa8831", "#076239", "#1a764d", "#1c4587", "#41236d",
               "#83334c", "#464646", "#e7e7e7", "#0d3472", "#b6cff5", "#0d3b44", "#98d7e4", "#3d188e", "#e3d7ff",
               "#711a36", "#fbd3e0", "#8a1c0a", "#f2b2a8", "#7a2e0b", "#ffc8af", "#7a4706", "#ffdeb5", "#594c05",
               "#fbe983", "#684e07", "#fdedc1", "#0b4f30", "#b3efd3", "#04502e", "#a2dcc1", "#c2c2c2", "#4986e7",
               "#2da2bb", "#b99aff", "#994a64", "#f691b2", "#ff7537", "#ffad46", "#662e37", "#ebdbde", "#cca6ac",
               "#094228", "#42d692", "#16a765"}
    assert set(runner.LABEL_COLORS) == set(runner.LABELS)
    assert all(bg in palette and text in palette for bg, text in runner.LABEL_COLORS.values())


def test_colouring_a_label_sends_only_its_colour(monkeypatch):
    from inbox_triage.gmail import client as gc
    sent = []
    client = gc.GmailClient.__new__(gc.GmailClient)
    monkeypatch.setattr(client, "_request", lambda method, path, params=None, body=None: sent.append((method, path, body)) or {},
                        raising=False)
    client.color_label("Label_7", {"backgroundColor": "#e7e7e7", "textColor": "#464646"})
    client.create_label("Triage/Later", color={"backgroundColor": "#e7e7e7", "textColor": "#464646"})
    assert sent[0] == ("PATCH", "/labels/Label_7", {"color": {"backgroundColor": "#e7e7e7", "textColor": "#464646"}})
    assert sent[1][2]["color"] == {"backgroundColor": "#e7e7e7", "textColor": "#464646"}
    assert sent[1][2]["name"] == "Triage/Later"
