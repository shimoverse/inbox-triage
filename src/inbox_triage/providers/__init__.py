"""Classification providers. All return the same signals; policy.py decides labels."""
from __future__ import annotations

from .base import Provider, ProviderError

PROVIDERS = ("jev", "anthropic", "openai", "openrouter", "ollama", "rules")
# Providers that can also turn an onboarding ramble into rules.
JSON_PROVIDERS = ("anthropic", "openai", "openrouter", "ollama")
LABELS = {"jev": "Jev (typesafe.ai)", "anthropic": "Claude (Anthropic)", "openai": "OpenAI",
          "openrouter": "OpenRouter (OpenAI, Claude, Gemini, Llama, ...)", "ollama": "Ollama (on this computer)",
          "rules": "Offline rules (no AI)"}
KEY_ENV = {"jev": "TYPESAFE_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "openrouter": "OPENROUTER_API_KEY", "ollama": "", "rules": ""}


def make_provider(name: str = "jev", model: str | None = None, api_key: str | None = None) -> Provider:
    if name == "jev":
        from .lean_jev import LeanJevProvider
        return LeanJevProvider(model=model, api_key=api_key or None)
    if name == "anthropic":
        from .llm import AnthropicProvider
        return AnthropicProvider(model=model or None, api_key=api_key or None)
    if name in ("openai", "openrouter", "ollama"):
        from .llm import OpenAICompatibleProvider
        return OpenAICompatibleProvider(model=model or None, api_key=api_key or None, preset=name)
    if name == "rules":
        from .rules import RulesProvider
        return RulesProvider()
    raise ProviderError(f"Unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}")
