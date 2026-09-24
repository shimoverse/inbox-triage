"""Classification providers. All return the same signals; policy.py decides labels."""
from __future__ import annotations

from .base import Provider, ProviderError

PROVIDERS = ("jev", "anthropic", "openai", "rules")


def make_provider(name: str = "jev", model: str | None = None) -> Provider:
    if name == "jev":
        from .lean_jev import LeanJevProvider
        return LeanJevProvider(model=model)
    if name == "anthropic":
        from .llm import AnthropicProvider
        return AnthropicProvider(model=model)
    if name == "openai":
        from .llm import OpenAICompatibleProvider
        return OpenAICompatibleProvider(model=model)
    if name == "rules":
        from .rules import RulesProvider
        return RulesProvider()
    raise ProviderError(f"Unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")
