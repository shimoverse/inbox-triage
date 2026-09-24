"""Local configuration: where files live, and the Jev / assistant keys."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .gmail.client import write_private
from .providers import KEY_ENV, SIGNUP_URL

CONFIG_DIR = Path.home() / ".config/inbox-triage"
STATE_DIR = Path.home() / ".local/share/inbox-triage"
KNOWN_ENV = {"TYPESAFE_API_KEY", "TYPESAFE_API_BASE", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
             "INBOX_TRIAGE_ASSIST_MODEL", "INBOX_TRIAGE_JEV_MODEL", "INBOX_TRIAGE_JEV_SIGNUP_URL",
             "INBOX_TRIAGE_OAUTH_CLIENT_ID", "INBOX_TRIAGE_OAUTH_CLIENT_SECRET"}


class JevRequired(RuntimeError):
    """Inbox Triage doesn't run without a Jev key."""


def signup_url() -> str:
    return os.environ.get("INBOX_TRIAGE_JEV_SIGNUP_URL") or SIGNUP_URL


def load_dotenv(*paths: Path) -> None:
    """Read KEY=value lines from .env files for known settings; real env vars win."""
    for path in paths:
        try:
            lines = path.expanduser().read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            key = key.removeprefix("export ").strip()
            if sep and key in KNOWN_ENV and key not in os.environ:
                os.environ[key] = value.strip().strip("'\"")


def load_secrets(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    try:
        data = json.loads((config_dir.expanduser() / "secrets.json").read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save_secret(name: str, value: str, config_dir: Path = CONFIG_DIR) -> None:
    if name not in KEY_ENV.values():
        raise ValueError("Unknown API key name")
    secrets = load_secrets(config_dir)
    if value:
        secrets[name] = value.strip()
    else:
        secrets.pop(name, None)
    write_private(config_dir.expanduser() / "secrets.json", json.dumps(secrets, sort_keys=True))


def api_key_for(role: str, config_dir: Path = CONFIG_DIR) -> str:
    """``role`` is "jev" or "assistant"."""
    env = KEY_ENV[role]
    return os.environ.get(env) or load_secrets(config_dir).get(env, "")


def jev_settings(settings: dict | None = None, model_override: str | None = None,
                 config_dir: Path = CONFIG_DIR, account_key: str = "",
                 allow_machine_key: bool = True) -> tuple[str | None, str]:
    """(model, key) for Jev: the account's own key, else, when self-hosting only,
    this machine's key. A hosted server passes ``allow_machine_key=False`` so an
    operator's key can never pay for other users. Raises JevRequired otherwise."""
    key = account_key or (api_key_for("jev", config_dir) if allow_machine_key else "")
    if not key:
        raise JevRequired(f"Connect Jev first: add a TYPESAFE_API_KEY (get one at {signup_url()})")
    model = model_override or (settings or {}).get("model") or os.environ.get("INBOX_TRIAGE_JEV_MODEL") or None
    return model, key
