"""Jev is the only classifier: every per-email decision is a typed Jev answer."""
from __future__ import annotations

from .base import Provider, ProviderError
from .jev import SIGNUP_URL, JevError

PROVIDERS = ("jev",)
KEY_ENV = {"jev": "TYPESAFE_API_KEY", "assistant": "OPENROUTER_API_KEY"}


def make_provider(name: str = "jev", model: str | None = None, api_key: str | None = None) -> Provider:
    if name != "jev":
        raise ProviderError("Inbox Triage classifies mail with Jev only")
    from .lean_jev import LeanJevProvider
    return LeanJevProvider(model=model or None, api_key=api_key or None)
