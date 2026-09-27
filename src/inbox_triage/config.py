"""Local configuration: where files live, and the Jev / assistant keys."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .gmail.client import write_private
from .providers import KEY_ENV, SIGNUP_URL

CONFIG_DIR = Path.home() / ".config/inbox-triage"
STATE_DIR = Path.home() / ".local/share/inbox-triage"
KNOWN_ENV = {"TYPESAFE_API_KEY", "TYPESAFE_API_BASE", "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
             "INBOX_TRIAGE_ASSIST_MODEL", "INBOX_TRIAGE_JEV_MODEL", "INBOX_TRIAGE_JEV_SIGNUP_URL",
             "INBOX_TRIAGE_OAUTH_CLIENT_ID", "INBOX_TRIAGE_OAUTH_CLIENT_SECRET",
             "INBOX_TRIAGE_SUPPORT_EMAIL", "INBOX_TRIAGE_OPERATOR", "INBOX_TRIAGE_MAX_ACCOUNTS",
             "INBOX_TRIAGE_BETA_ENDS", "INBOX_TRIAGE_SPONSORED_JEV_DAILY"}


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
    """``role`` is "jev" or "assistant". Jev also runs through OpenRouter, so without a
    TypeSafe key the OpenRouter key serves both."""
    env = KEY_ENV[role]
    key = os.environ.get(env) or load_secrets(config_dir).get(env, "")
    return key or (api_key_for("assistant", config_dir) if role == "jev" else "")


def sponsored_daily_limit() -> int:
    """INBOX_TRIAGE_SPONSORED_JEV_DAILY: how many Jev calls a day, per account, the operator's
    key pays for on a hosted server (a sponsored beta). 0 or unset: every user brings a key."""
    try:
        return max(0, int(os.environ.get("INBOX_TRIAGE_SPONSORED_JEV_DAILY") or 0))
    except ValueError:
        return 0


@dataclass(frozen=True)
class JevAccess:
    key: str
    model: str | None = None
    daily_limit: int | None = None  # set when the operator's key pays: Jev calls per account per day


def jev_access(settings: dict | None = None, model_override: str | None = None,
               config_dir: Path = CONFIG_DIR, account_key: str = "", *, hosted: bool = False) -> JevAccess:
    """Which key pays for an account's Jev calls. The account's own key always wins.
    Otherwise this machine's key, except on a hosted server: there the operator's key
    pays for other people only when a sponsored daily limit is set, and then only up
    to that limit. Raises JevRequired when no key may be used."""
    model = model_override or (settings or {}).get("model") or os.environ.get("INBOX_TRIAGE_JEV_MODEL") or None
    if account_key:
        return JevAccess(account_key, model)
    limit = sponsored_daily_limit()
    key = api_key_for("jev", config_dir) if limit or not hosted else ""
    if not key:
        raise JevRequired(f"Connect Jev first: add a TYPESAFE_API_KEY (get one at {signup_url()}) "
                          "or an OPENROUTER_API_KEY")
    return JevAccess(key, model, limit or None)
