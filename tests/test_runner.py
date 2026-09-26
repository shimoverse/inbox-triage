import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inbox_triage import runner
from inbox_triage.context import ContextSyncStats
from inbox_triage.models import ContextPack, Destination, JevSignals, MailEvidence
from inbox_triage.policy import decide
from inbox_triage.providers.lean_jev import LeanJevProvider


class FakeMessages:
    """Stands in for the Gmail client's label methods."""
    def __init__(self):
        self.labels = {"INBOX", "STARRED"}
        self.calls = []
    def message_labels(self, mid):
        return set(self.labels)
    def modify_labels(self, mid, add, remove):
        self.calls.append({"addLabelIds": add, "removeLabelIds": remove})
        self.labels.update(add)
        self.labels.difference_update(remove)


def test_label_only_preserves_native_labels_and_reads_back():
    messages = FakeMessages()
    assert runner.apply_labels(messages, "sample", {"Triage/For You"}, {"Triage/For You": "custom1"})
    assert messages.labels == {"INBOX", "STARRED", "custom1"}
    assert messages.calls == [{"addLabelIds": ["custom1"], "removeLabelIds": []}]


def test_readback_mismatch_stops():
    messages = FakeMessages()
    messages.modify_labels = lambda mid, add, remove: None  # the write silently didn't stick
    with pytest.raises(RuntimeError, match="readback mismatch"):
        runner.apply_labels(messages, "sample", {"Triage/For You"}, {"Triage/For You": "custom1"})


def test_scoped_state_and_complete_scan():
    assert runner.scoped_directory(Path("state"), "a@example.org") != runner.scoped_directory(Path("state"), "b@example.org")
    assert runner.scan_query(2000000, 1000000).startswith("after:1827200 ")
    assert runner.scan_query(1000000, 1000000).startswith("after:1000000 ")


def test_journal_resume_and_atomic_state(tmp_path):
    journal = tmp_path / "events.jsonl"
    runner.append_event(journal, {"id": "example", "status": "classified", "names": ["Triage/Later"]})
    runner.append_event(journal, {"id": "example", "status": "verified", "names": ["Triage/Later"]})
    assert runner.load_events(journal)["example"]["status"] == "verified"
    assert journal.stat().st_mode & 0o077 == 0
    state = tmp_path / "state.json"
    runner.save_state(state, {"account": "a@example.org", "cursor_epoch": 100})
    assert json.loads(state.read_text())["cursor_epoch"] == 100
    assert not state.with_suffix(".tmp").exists()
    assert state.stat().st_mode & 0o077 == 0


def test_conservative_policy_never_routes_uncertain_mail_to_later():
    decision = decide(MailEvidence("example"), ContextPack(), JevSignals())
    assert decision.destination == Destination.UNCHANGED
    assert runner.desired_names(decision) == set()


def test_lean_payload_excludes_ids_and_family_context(monkeypatch):
    p = LeanJevProvider(api_key="test-only")
    payload = p.payload(MailEvidence("secret-message-id", subject="S"*400, excerpt="body "*400), ContextPack())
    text = json.dumps(payload)
    assert "secret-message-id" not in text
    assert "topic_shopping" in payload["questions"]
    assert len(payload["state"]["excerpt"]) <= 1000
    assert len(payload["state"]["subject"]) <= 200


def test_live_runner_verifies_account_and_resumes_without_new_model_call(tmp_path, monkeypatch):
    messages = FakeMessages()
    class Client:
        def __init__(self, token, **kw): pass
        def message_labels(self, mid): return messages.message_labels(mid)
        def modify_labels(self, mid, add, remove): messages.modify_labels(mid, add, remove)
        def profile(self): return {"emailAddress": "a@example.org", "historyId": "8"}
        def _get(self, mid, *, full): return {"id": mid, "payload": {"mimeType": "text/plain", "headers": []}}
        def attachment_data(self, mid, aid): return ""
    calls = []
    class Provider:
        def classify_with_usage(self, evidence, context):
            calls.append(evidence.message_id)
            return JevSignals(personal_relevance=.99), {"input_tokens": 10, "output_tokens": 2}
    monkeypatch.setattr(runner, "GmailReadOnlyClient", Client)
    monkeypatch.setattr(runner, "list_ids", lambda client, query: ["synthetic-id"])
    monkeypatch.setattr(runner, "ensure_labels", lambda client: {name: "custom1" if name == "Triage/For You" else name for name in runner.LABELS})
    monkeypatch.setattr(runner, "make_provider", lambda *a, **kw: Provider())
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: ContextSyncStats())
    monkeypatch.setattr(runner, "build_context", lambda *a, **kw: ContextPack())
    with pytest.raises(RuntimeError, match="account mismatch"):
        runner.run("b@example.org", tmp_path / "token.json", tmp_path / "state", now=2_000_000)
    assert calls == []
    result = runner.run("a@example.org", tmp_path / "token.json", tmp_path / "state", now=2_000_000)
    assert result["frontier_calls"] == 0 and result["jev_calls"] == 1
    assert result["gmail_changes"] == 1 and not result["remaining"]
    assert messages.labels == {"INBOX", "STARRED", "custom1"}
    repeat = runner.run("a@example.org", tmp_path / "token.json", tmp_path / "state", now=2_000_001)
    assert repeat["jev_calls"] == repeat["gmail_changes"] == 0
    assert calls == ["synthetic-id"]


def test_junk_rule_labels_without_asking_jev_but_never_security_alerts(tmp_path, monkeypatch):
    from inbox_triage import preferences
    state = tmp_path / "state"
    directory = runner.scoped_directory(state, "a@example.org")
    directory.mkdir(parents=True)
    rules = preferences.Preferences.from_json({"rules": [{"kind": "keyword", "value": "flash sale", "action": "junk"}]})
    (directory / "preferences.json").write_text(json.dumps(rules.to_json()))
    subjects = {"deal": "Flash sale: 70% off everything", "alert": "Security alert: new sign-in (flash sale account)"}
    applied, created = {}, []
    class Client:
        def __init__(self, token, **kw): pass
        def profile(self): return {"emailAddress": "a@example.org", "historyId": "8"}
        def _get(self, mid, *, full):
            return {"id": mid, "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": subjects[mid]}]}}
        def attachment_data(self, mid, aid): return ""
        def labels(self): return [{"id": n, "name": n} for n in created]
        def create_label(self, name): created.append(name)
        def message_labels(self, mid): return set(applied.get(mid, ()))
        def modify_labels(self, mid, add, remove): applied.setdefault(mid, set()).update(add)
    asked = []
    class Provider:
        def classify_with_usage(self, evidence, context):
            asked.append(evidence.subject)
            return JevSignals(), {}
    monkeypatch.setattr(runner, "GmailReadOnlyClient", Client)
    monkeypatch.setattr(runner, "list_ids", lambda client, query: ["deal", "alert"])
    monkeypatch.setattr(runner, "make_provider", lambda *a, **kw: Provider())
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: ContextSyncStats())
    monkeypatch.setattr(runner, "build_context", lambda *a, **kw: ContextPack())
    result = runner.run("a@example.org", tmp_path / "token.json", state, now=2_000_000)
    assert "Triage/Junk" in created                      # created because a Junk rule exists
    assert applied["deal"] == {"Triage/Junk"}            # junk is labelled...
    assert asked == [subjects["alert"]]                  # ...without ever going to Jev
    assert "Triage/Junk" not in applied.get("alert", set())  # security alerts are never junked
    assert result["outcomes"]["junk"] == 1 and result["jev_calls"] == 1


def test_junk_label_is_only_created_when_someone_uses_it():
    created = []
    client = SimpleNamespace(labels=lambda: [{"id": n, "name": n} for n in created], create_label=created.append)
    labels = runner.ensure_labels(client)
    assert "Triage/Junk" not in created and "Triage/Junk" not in labels
    assert "Triage/Junk" in runner.ensure_labels(client, "Triage/Junk")
