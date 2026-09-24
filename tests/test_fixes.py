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
from inbox_triage.providers.base import parse_answers, questions, yes_probability
from inbox_triage.providers.llm import OpenAICompatibleProvider, answer_schema
from inbox_triage.providers.rules import RulesProvider
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


def test_yes_probability_and_answer_validation():
    assert yes_probability("yes", .9) == .9
    assert yes_probability("no", .9) == pytest.approx(.1)
    assert yes_probability("yes", .4) == 0.0
    good = {k: {"choice": "no", "confidence": .9} for k in questions()}
    good["category"] = {"choice": "update", "confidence": .8}
    assert parse_answers(good).category == "update"
    with pytest.raises(ProviderError):
        parse_answers({**good, "deceptive": {"choice": "maybe", "confidence": .5}})
    with pytest.raises(ProviderError):
        parse_answers({**good, "deceptive": {"choice": "no", "confidence": True}})


def test_answer_schema_requires_every_question():
    qs = questions()
    schema = answer_schema(qs)
    assert set(schema["required"]) == set(qs) and schema["additionalProperties"] is False
    assert schema["properties"]["category"]["properties"]["choice"]["enum"] == sorted(qs["category"]["criteria"])


def test_openai_compatible_provider_parses_structured_reply(monkeypatch):
    answers = {k: {"choice": "no", "confidence": .9} for k in questions()}
    answers["category"] = {"choice": "low_priority", "confidence": .9}
    answers["unsolicited_bulk"] = {"choice": "yes", "confidence": .95}
    p = OpenAICompatibleProvider(model="local-test", base_url="http://localhost:1/v1", api_key="")
    sent = {}
    def post(body):
        sent.update(body)
        return {"choices": [{"message": {"content": json.dumps(answers)}}], "usage": {"prompt_tokens": 7}}
    monkeypatch.setattr(p, "_post", post)
    signals, usage = p.classify_with_usage(MailEvidence("secret-id", subject="Sale"), ContextPack())
    assert signals.unsolicited_bulk == .95 and usage["input_tokens"] == 7
    assert "secret-id" not in json.dumps(sent)


def test_provider_factory_validation(monkeypatch):
    monkeypatch.delenv("INBOX_TRIAGE_MODEL", raising=False)
    with pytest.raises(ProviderError):
        make_provider("openai")
    with pytest.raises(ProviderError):
        make_provider("nope")
    assert isinstance(make_provider("rules"), RulesProvider)


def test_rules_provider_only_flags_categorized_bulk():
    promo = MailEvidence("m", labels=frozenset({"CATEGORY_PROMOTIONS"}), bulk=True, list_unsubscribe=True)
    s, _ = RulesProvider().classify_with_usage(promo, ContextPack())
    assert decide(promo, ContextPack(), s).destination == Destination.LATER
    personal = MailEvidence("m", subject="lunch?")
    s, _ = RulesProvider().classify_with_usage(personal, ContextPack())
    assert decide(personal, ContextPack(), s).destination == Destination.UNCHANGED


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
    assert runner.main(["--all", "--config-dir", str(tmp_path / "cfg"), "--state-dir", str(tmp_path / "s")]) == 0
    assert [a for a, _ in seen] == ["a@example.org", "b@example.org"]
    assert seen[0][1] == tokens / "a@example.org.json"
    assert runner.main(["--account", "missing@example.org", "--config-dir", str(tmp_path / "cfg"), "--verbose"]) == 1
    assert "No token" in capsys.readouterr().err


def test_anthropic_provider_request_shape():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from inbox_triage.providers.llm import AnthropicProvider
    answers = {k: {"choice": "no", "confidence": .9} for k in questions()}
    answers["category"] = {"choice": "update", "confidence": .8}
    seen = {}
    def handler(request):
        seen["beta"] = request.headers.get("anthropic-beta")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": json.dumps(answers)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 3}})
    provider = AnthropicProvider(api_key="test-only")
    provider.client = anthropic.Anthropic(api_key="test-only", http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(handler)))
    signals, usage = provider.classify_with_usage(MailEvidence("secret-id", subject="Statement"), ContextPack())
    assert signals.category == "update" and usage == {"input_tokens": 12, "output_tokens": 3}
    assert seen["beta"] == "server-side-fallback-2026-07-01" and seen["body"]["fallbacks"] == "default"
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert "secret-id" not in json.dumps(seen["body"])
