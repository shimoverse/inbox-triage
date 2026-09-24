"""Where the app keeps its local configuration, and how a provider is chosen."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .gmail.client import write_private
from .providers import KEY_ENV, PROVIDERS

CONFIG_DIR = Path.home() / ".config/inbox-triage"
STATE_DIR = Path.home() / ".local/share/inbox-triage"


def load_secrets(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    try:
        data = json.loads((config_dir.expanduser() / "secrets.json").read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def save_secret(name: str, value: str, config_dir: Path = CONFIG_DIR) -> None:
    if name not in {v for v in KEY_ENV.values() if v}:
        raise ValueError("Unknown API key name")
    secrets = load_secrets(config_dir)
    if value:
        secrets[name] = value.strip()
    else:
        secrets.pop(name, None)
    write_private(config_dir.expanduser() / "secrets.json", json.dumps(secrets, sort_keys=True))


def api_key_for(provider: str, config_dir: Path = CONFIG_DIR) -> str:
    env = KEY_ENV.get(provider, "")
    if not env:
        return ""
    return os.environ.get(env) or load_secrets(config_dir).get(env, "")


def available_providers(config_dir: Path = CONFIG_DIR) -> dict[str, bool]:
    """Which providers are usable right now (key present, or none needed)."""
    return {name: (not KEY_ENV[name]) or bool(api_key_for(name, config_dir)) for name in PROVIDERS}


def resolve_provider(settings: dict | None, override: str | None = None, model_override: str | None = None,
                     config_dir: Path = CONFIG_DIR) -> tuple[str, str | None, str]:
    """CLI flag > account setting > INBOX_TRIAGE_PROVIDER > jev."""
    settings = settings or {}
    name = override or settings.get("provider") or os.environ.get("INBOX_TRIAGE_PROVIDER") or "jev"
    if name not in PROVIDERS:
        raise ValueError(f"Unknown provider {name!r}")
    model = model_override or settings.get("model") or None
    return name, model, api_key_for(name, config_dir)
