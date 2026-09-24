"""Regression tests for the open-source hardening pass. Synthetic data only."""
import json
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from inbox_triage import context, runner
from inbox_triage.gmail.client import GmailClient, HistoryExpired
from inbox_triage.models import ContextPack, Destination, JevSignals, MailEvidence
from inbox_triage.policy import decide
from inbox_triage.providers import ProviderError, make_provider
from inbox_triage.providers.base import parse_answers, questions
from inbox_triage.store import TriageStore

NOW = 2_000_000_000


def http_error(status):
    return HttpError(Response({"status": str(status)}), b"{}")


def msg(labels=(), subject="", sender="Shop <orders@shop.example>", auth="dmarc=pass", thread="t1", to=""):
    headers = [{"name": "Subject", "value": subject}, {"name": "From", "value": sender},
               {"name": "To", "value": to}, {"name": "Authentication-Results", "value": auth}]
    return {"labelIds": list(labels), "threadId": thread, "internalDate": str(NOW * 1000),
            "payload": {"headers": headers}}


def test_purchase_query_uses_or_braces():
    q = context.PURCHASE_QUERY.format(days=30)
    assert "subject:{order receipt" in q and "subject:(" not in q
    assert "-in:spam" in q and "-in:trash" in q


@pytest.mark.parametrize("message", [
    msg(labels=["SENT"], subject="Re: my order", sender="Me <me@gmail.com>", to="friend@gmail.com"),
    msg(labels=["SPAM"], subject="Your order #1"),
    msg(subject="Your order #1", auth="dmarc=fail"),
    msg(subject="Your order #1", sender="Scam <x@gmail.com>"),
])
def test_untrusted_mail_never_creates_purchase_protection(tmp_path, message):
    with TriageStore(tmp_path / "c.db") as store:
        context.learn_message(store, "a@example.org", message, NOW)
        assert not store.has_fact("a@example.org", "purchase_domain", "gmail.com", NOW)
        assert not store.has_fact("a@example.org", "purchase_domain", "shop.example", NOW)


def test_authenticated_purchase_is_learned_and_sent_mail_learns_people(tmp_path):
    with TriageStore(tmp_path / "c.db") as store:
        context.learn_message(store, "a@example.org", msg(subject="Your order #1 shipped"), NOW)
        context.learn_message(store, "a@example.org", msg(labels=["SENT"], subject="hi", to="Bo <bo@x.example>"), NOW)
        assert store.has_fact("a@example.org", "purchase_domain", "shop.example", NOW)
        assert store.has_fact("a@example.org", "person", "bo@x.example", NOW)
        assert (tmp_path / "c.db").stat().st_mode & 0o077 == 0


class FakeHistoryClient:
    def __init__(self, pages=None, expired=False):
        self.pages, self.expired = pages or [], expired
    def iter_history(self, cursor, **kw):
        if self.expired:
            raise HistoryExpired()
        yield from self.pages


def test_expired_history_resets_cursor(tmp_path):
    with TriageStore(tmp_path / "c.db") as store:
        store.set_cursor("a@example.org", "5")
        stats = context.sync_incremental(FakeHistoryClient(expired=True), store, "a@example.org",
                                         target_history_id="99", max_pages=1, page_size=1, now=NOW)
        assert stats.history_expired and not store.get_cursor("a@example.org")


def test_truncated_history_advances_to_last_processed_record(tmp_path):
    with TriageStore(tmp_path / "c.db") as store:
        store.set_cursor("a@example.org", "5")
        page = {"messages": [], "last_history_id": "42", "truncated": True}
        stats = context.sync_incremental(FakeHistoryClient([page]), store, "a@example.org",
                                         target_history_id="99", max_pages=1, page_size=1, now=NOW)
        assert stats.history_truncated and store.get_cursor("a@example.org") == "42"


def test_history_404_raises_history_expired():
    client = GmailClient.__new__(GmailClient)
    def fail(**kw):
        raise http_error(404)
    client.service = SimpleNamespace(users=lambda: SimpleNamespace(
        history=lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=fail))))
    with pytest.raises(HistoryExpired):
        list(client.iter_history("1", max_pages=1, page_size=10))


def test_deceptive_action_request_is_never_needs_you():
    e = MailEvidence("m", subject="Action required: verify your account")
    d = decide(e, ContextPack(), JevSignals(requires_action=.95, deceptive=.9))
    assert d.destination == Destination.UNCHANGED
    assert decide(e, ContextPack(), JevSignals(requires_action=.95)).destination == Destination.NEEDS_YOU


def test_partial_journal_line_is_ignored_and_compaction_drops_old(tmp_path):
    journal = tmp_path / "events.jsonl"
    journal.write_text('{"id":"old","status":"verified","ts":1}\n{"id":"new","status":"verified","ts":%d}\n{"id":"cut' % NOW)
    events = runner.load_events(journal)
    assert set(events) == {"old", "new"}
    kept = runner.compact_events(journal, events, NOW)
    assert set(kept) == {"new"} and set(runner.load_events(journal)) == {"new"}
    assert journal.stat().st_mode & 0o077 == 0


class FakeMessages:
    def __init__(self):
        self.labels = {"INBOX"}
    def get(self, **kw):
        return SimpleNamespace(execute=lambda **k: {"labelIds": sorted(self.labels)})
    def modify(self, **kw):
        def execute(**k):
            self.labels.update(kw["body"]["addLabelIds"])
            self.labels.difference_update(kw["body"]["removeLabelIds"])
        return SimpleNamespace(execute=execute)


def live_setup(monkeypatch, ids, provider, fetch=None):
    messages = FakeMessages()
    service = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: messages))
    class Client:
        def __init__(self, token, **kw): self.service = service
        def profile(self): return {"emailAddress": "a@example.org", "historyId": "8"}
        def _get(self, mid, *, full):
            if fetch: return fetch(mid)
            return {"id": mid, "payload": {"mimeType": "text/plain", "headers": []}}
        def attachment_data(self, mid, aid): return ""
    monkeypatch.setattr(runner, "GmailReadOnlyClient", Client)
    monkeypatch.setattr(runner, "list_ids", lambda client, query: list(ids))
    monkeypatch.setattr(runner, "ensure_labels", lambda client: {n: n for n in runner.LABELS})
    monkeypatch.setattr(runner, "make_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: context.ContextSyncStats())
    monkeypatch.setattr(runner, "build_context", lambda *a, **kw: ContextPack())


def test_poison_message_is_retried_then_skipped_without_blocking_others(tmp_path, monkeypatch):
    class Provider:
        def classify_with_usage(self, e, c):
            if e.message_id == "bad":
                raise ProviderError("invalid")
            return JevSignals(personal_relevance=.99), {}
    live_setup(monkeypatch, ["bad", "good"], Provider())
    first = runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW)
    assert first["outcomes"] == {"error": 1, "for_you": 1} and first["remaining"]
    runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW + 1)
    third = runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW + 2)
    assert third["provider_failures"] == 1 and not third["remaining"]
    fourth = runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW + 3)
    assert fourth["processed"] == 0 and fourth["cursor_advanced"]


def test_provider_outage_stops_the_run(tmp_path, monkeypatch):
    class Down:
        def classify_with_usage(self, e, c):
            raise ProviderError("down")
    live_setup(monkeypatch, [f"m{i}" for i in range(10)], Down())
    with pytest.raises(ProviderError):
        runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW)


def test_message_deleted_after_listing_is_skipped(tmp_path, monkeypatch):
    class Provider:
        def classify_with_usage(self, e, c): return JevSignals(), {}
    def fetch(mid):
        if mid == "gone": raise http_error(404)
        return {"id": mid, "payload": {"mimeType": "text/plain", "headers": []}}
    live_setup(monkeypatch, ["gone", "here"], Provider(), fetch)
    result = runner.run("a@example.org", tmp_path / "t.json", tmp_path / "s", now=NOW)
    assert result["outcomes"] == {"deleted": 1, "unchanged": 1} and not result["remaining"]


def test_cli_multi_account_discovery(tmp_path, monkeypatch, capsys):
    tokens = tmp_path / "cfg" / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "a@example.org.json").write_text("{}")
    (tokens / "b@example.org.json").write_text("{}")
    assert runner.discover_accounts(tmp_path / "cfg") == ["a@example.org", "b@example.org"]
    seen = []
    monkeypatch.setattr(runner, "run", lambda account, token, root, **kw: seen.append((account, token)) or {"account": account})
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert runner.main(["--all", "--config-dir", str(tmp_path / "cfg"), "--state-dir", str(tmp_path / "s")]) == 1
    assert "console.typesafe.ai" in capsys.readouterr().err and not seen  # no Jev, no run
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    assert runner.main(["--all", "--config-dir", str(tmp_path / "cfg"), "--state-dir", str(tmp_path / "s")]) == 0
    assert [a for a, _ in seen] == ["a@example.org", "b@example.org"]
    assert seen[0][1] == tokens / "a@example.org.json"
    assert runner.main(["--account", "missing@example.org", "--config-dir", str(tmp_path / "cfg"), "--verbose"]) == 1
    assert "No token" in capsys.readouterr().err


def jev_answers(**overrides):
    answers = {k: {"type": "noul", "noul": .05} for k, q in questions().items() if q["type"] == "noul"}
    answers["category"] = {"type": "choice", "choice": "update", "probabilities": {"update": .8}, "confidence": .8}
    answers.update(overrides)
    return answers


def test_jev_questions_use_native_noul_and_parse_probabilities():
    qs = questions()
    assert qs["requires_action"]["type"] == "noul" and set(qs["requires_action"]["criteria"]) == {"true", "false"}
    assert qs["topic_shopping"]["type"] == "noul" and qs["category"]["type"] == "choice"
    signals = parse_answers(jev_answers(deceptive={"type": "noul", "noul": .93}))
    assert signals.deceptive == .93 and signals.requires_action == .05 and signals.category == "update"
    # Confidence may be omitted: fall back to the chosen option's probability.
    signals = parse_answers(jev_answers(category={"type": "choice", "choice": "spam", "probabilities": {"spam": .7}}))
    assert signals.category_confidence == .7
    for bad in ({"type": "noul", "noul": 1.5}, {"type": "noul", "noul": True}, {"type": "choice", "choice": "yes"}):
        with pytest.raises(ProviderError):
            parse_answers(jev_answers(deceptive=bad))


def test_jev_is_the_only_classifier_and_needs_a_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="console.typesafe.ai"):
        make_provider("jev")
    with pytest.raises(ProviderError, match="Jev only"):
        make_provider("openai", api_key="k")


class _Resp:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return json.dumps(self.data).encode()


def test_jev_retries_overloaded_529_then_succeeds(monkeypatch):
    import urllib.error
    import urllib.request
    import inbox_triage.providers.jev as jev_mod
    monkeypatch.setattr(jev_mod.time, "sleep", lambda s: None)
    calls = []
    def urlopen(req, timeout):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, None)
        return _Resp({"answers": jev_answers(), "usage": {"input_tokens": 90}})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    p = make_provider("jev", api_key="test-only")
    signals, usage = p.classify_with_usage(MailEvidence("secret-id", subject="Statement"), ContextPack())
    assert len(calls) == 2 and signals.category == "update" and usage["input_tokens"] == 90
    assert calls[0]["model"] == "jev-latest" and "secret-id" not in json.dumps(calls[0])


def test_jev_verify_reports_bad_key(monkeypatch):
    import urllib.error
    import urllib.request
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(ProviderError) as exc:
        make_provider("jev", api_key="wrong").verify()
    assert exc.value.status == 401


def test_assistant_uses_deepseek_via_openrouter_with_json_mode_fallback(monkeypatch):
    import urllib.error
    import urllib.request
    from inbox_triage import onboarding
    from inbox_triage.assistant import Assistant
    monkeypatch.delenv("INBOX_TRIAGE_ASSIST_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    with pytest.raises(ProviderError, match="OpenRouter"):
        Assistant("")
    bodies, headers = [], []
    def urlopen(req, timeout):
        body = json.loads(req.data)
        bodies.append(body); headers.append(dict(req.header_items()))
        if body["response_format"]["type"] == "json_schema":
            raise urllib.error.HTTPError(req.full_url, 404, "no endpoint", {}, None)
        return _Resp({"choices": [{"message": {"content": json.dumps({"summary": "School matters.", "rules": [
            {"kind": "domain", "value": "school.example", "action": "important", "note": "kids"}]})}}]})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    prefs = onboarding.interpret(Assistant("test-only"), "#1 is my kids' school",
                                 [{"from": "Office <o@school.example>", "domain": "school.example", "subject": "Trip"}])
    assert prefs.rules[0].value == "school.example" and prefs.summary == "School matters."
    assert bodies[0]["model"] == "deepseek/deepseek-v4.1-flash" and bodies[0]["provider"] == {"require_parameters": True}
    assert bodies[1]["response_format"] == {"type": "json_object"} and "provider" not in bodies[1]
    assert headers[0]["Authorization"] == "Bearer test-only"
    assert "untrusted" in bodies[0]["messages"][0]["content"]


def test_dotenv_loads_known_keys_without_overriding(tmp_path, monkeypatch):
    from inbox_triage import config
    env = tmp_path / ".env"
    env.write_text("export TYPESAFE_API_KEY='from-file'\nOPENROUTER_API_KEY=keep\nPATH=/evil\n# comment\n")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "already-set")
    config.load_dotenv(env)
    import os
    assert os.environ["TYPESAFE_API_KEY"] == "from-file" and os.environ["OPENROUTER_API_KEY"] == "already-set"
    assert os.environ["PATH"] != "/evil"
