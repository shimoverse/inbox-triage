import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inbox_triage import runner
from inbox_triage.models import ContextPack, Destination, JevSignals, MailEvidence
from inbox_triage.policy import decide
from inbox_triage.providers.lean_jev import LeanJevProvider


class FakeMessages:
    def __init__(self):
        self.labels = {"INBOX", "STARRED"}
        self.calls = []
    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda: {"labelIds": sorted(self.labels)})
    def modify(self, **kwargs):
        self.calls.append(kwargs["body"])
        def execute():
            self.labels.update(kwargs["body"]["addLabelIds"])
            self.labels.difference_update(kwargs["body"]["removeLabelIds"])
        return SimpleNamespace(execute=execute)


def test_label_only_preserves_native_labels_and_reads_back():
    messages = FakeMessages()
    client = SimpleNamespace(service=SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: messages)))
    assert runner.apply_labels(client, "sample", {"Triage/For You"}, {"Triage/For You": "custom1"})
    assert messages.labels == {"INBOX", "STARRED", "custom1"}
    assert messages.calls == [{"addLabelIds": ["custom1"], "removeLabelIds": []}]


def test_readback_mismatch_stops():
    messages = FakeMessages()
    messages.modify = lambda **kwargs: SimpleNamespace(execute=lambda: None)
    client = SimpleNamespace(service=SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: messages)))
    with pytest.raises(RuntimeError, match="readback mismatch"):
        runner.apply_labels(client, "sample", {"Triage/For You"}, {"Triage/For You": "custom1"})


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
    service = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: messages))
    class Client:
        def __init__(self, token): self.service = service
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
    monkeypatch.setattr(runner, "LeanJevProvider", Provider)
    monkeypatch.setattr(runner, "bootstrap_context", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "sync_incremental", lambda *a, **kw: None)
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
