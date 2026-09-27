"""CLI OAuth reconnect must respect the same account lock and deletion marker as runs."""
import json
from types import SimpleNamespace

import pytest

from inbox_triage import auth, runner


EMAIL = "alice@example.org"


def _consent(monkeypatch, email=EMAIL):
    credentials = SimpleNamespace(to_json=lambda: '{"refresh_token":"new"}')
    monkeypatch.setattr(auth, "loopback_consent", lambda *args, **kwargs: credentials)
    monkeypatch.setattr(auth.google_oauth, "profile_email", lambda value: email)
    return credentials


def _args(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "client_secret.json").write_text(json.dumps({"installed": {"client_id": "test"}}))
    state = tmp_path / "state"
    return config, state, ["--credentials", str(config / "client_secret.json"),
                           "--config-dir", str(config), "--state-dir", str(state)]


def test_cli_reauthorization_clears_tombstone_after_token_is_saved(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch, "ALICE@EXAMPLE.ORG")
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    assert auth.main(args) == 0
    assert json.loads(runner.default_token(EMAIL, config).read_text()) == {"refresh_token": "new"}
    assert not marker.exists()


def test_cli_reauthorization_with_force_replaces_old_token_and_clears_tombstone(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    token = runner.default_token(EMAIL, config)
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh_token":"old"}')
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    assert auth.main([*args, "--force"]) == 0
    assert json.loads(token.read_text()) == {"refresh_token": "new"}
    assert not marker.exists()


def test_cli_reauthorization_without_force_preserves_old_token_and_tombstone(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    token = runner.default_token(EMAIL, config)
    token.parent.mkdir(parents=True)
    token.write_text('{"refresh_token":"old"}')
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    with pytest.raises(SystemExit) as exc:
        auth.main(args)
    assert exc.value.code == 2
    assert json.loads(token.read_text()) == {"refresh_token": "old"}
    assert marker.read_text() == "1"


def test_cli_reauthorization_keeps_tombstone_when_token_write_fails(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    def fail_write(*args):
        raise OSError("disk full")
    monkeypatch.setattr(auth, "write_private", fail_write)
    with pytest.raises(OSError, match="disk full"):
        auth.main(args)
    assert marker.read_text() == "1"


def test_cli_reauthorization_does_not_write_or_clear_while_account_is_locked(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    with runner.account_lock(runner.account_lock_path(state, EMAIL)):
        with pytest.raises(RuntimeError, match="already running"):
            auth.main(args)
    assert marker.read_text() == "1"
    assert not runner.default_token(EMAIL, config).exists()


def test_cli_reauthorization_does_not_clear_other_accounts_tombstone(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    other = runner.account_lock_path(state, "bob@example.org").with_suffix(".deleted")
    other.parent.mkdir(parents=True)
    other.write_text("1")
    assert auth.main(args) == 0
    assert other.read_text() == "1"


def test_cli_consent_started_before_disconnect_cannot_reconnect(tmp_path, monkeypatch):
    from inbox_triage.web import oauth
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    consent = auth.loopback_consent
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    fence = config / "disconnect-fences" / (runner.scoped_directory(state, EMAIL).name + ".json")
    revoked = []
    monkeypatch.setattr(oauth, "revoke_token", lambda token: revoked.append(token))

    def delete_during_consent(*a, **kw):
        fence.parent.mkdir(parents=True)
        fence.write_text("new-generation")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1")
        return consent(*a, **kw)

    monkeypatch.setattr(auth, "loopback_consent", delete_during_consent)
    with pytest.raises(SystemExit, match="disconnected during sign-in"):
        auth.main(args)
    assert marker.read_text() == "1"
    assert not runner.default_token(EMAIL, config).exists()
    assert revoked == [""]  # synthetic credentials have no real Google token


def test_cli_reconnect_after_disconnect_generation_is_allowed(tmp_path, monkeypatch):
    config, state, args = _args(tmp_path)
    _consent(monkeypatch)
    marker = runner.account_lock_path(state, EMAIL).with_suffix(".deleted")
    fence = config / "disconnect-fences" / (runner.scoped_directory(state, EMAIL).name + ".json")
    fence.parent.mkdir(parents=True)
    fence.write_text("previous-deletion")
    marker.parent.mkdir(parents=True)
    marker.write_text("1")
    assert auth.main(args) == 0
    assert not marker.exists()
    assert fence.read_text() == "previous-deletion"
