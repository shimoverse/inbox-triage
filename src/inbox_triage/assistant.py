"""Optional onboarding assistant: turns a typed "ramble" into rules.

Jev makes every per-email decision; it doesn't write text. Reading free-form
notes and proposing rules is a language task, so it goes to a general model via
OpenRouter (default: DeepSeek V4.1 Flash). It runs only when the user clicks
"Turn my notes into rules" and needs ``OPENROUTER_API_KEY``. Without a key,
people can still tag emails and add rules by hand.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .providers.base import ProviderError

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class Assistant:
    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None,
                 timeout: float = 90, retries: int = 2):
        if not api_key:
            raise ProviderError("Add an OpenRouter key (OPENROUTER_API_KEY) to turn notes into rules")
        self.api_key = api_key
        self.model = model or os.environ.get("INBOX_TRIAGE_ASSIST_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout, self.retries = timeout, retries

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.base_url + "/chat/completions", method="POST",
                                     data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.api_key,
            # Optional OpenRouter app attribution.
            "HTTP-Referer": "https://github.com/shimoverse/inbox-triage",
            "X-OpenRouter-Title": "Inbox Triage", "X-Title": "Inbox Triage"})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    error = ProviderError(f"Assistant HTTP {exc.code}")
                    error.status = exc.code
                    raise error from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                if attempt >= self.retries:
                    raise ProviderError("Assistant transport or response error") from None
            time.sleep(.5 * (2 ** attempt))
        raise ProviderError("Assistant request failed")

    def complete_json(self, system: str, user: str, schema: dict, name: str = "result") -> tuple[dict, dict]:
        body = {"model": self.model,
                "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
                # Route only to endpoints that honour structured outputs.
                "provider": {"require_parameters": True},
                "messages": [{"role": "system", "content": system + " Reply with JSON only."},
                             {"role": "user", "content": user}]}
        try:
            raw = self._post(body)
        except ProviderError as exc:
            if getattr(exc, "status", None) not in {400, 404}:
                raise
            # No endpoint offers JSON-schema mode for this model: use JSON mode and validate locally.
            body["response_format"] = {"type": "json_object"}
            body.pop("provider")
            body["messages"][0]["content"] += " Follow this JSON schema exactly: " + json.dumps(schema)
            raw = self._post(body)
        try:
            data = json.loads(raw["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ProviderError("Assistant returned invalid JSON") from None
        usage = raw.get("usage") or {}
        return data, {"input_tokens": int(usage.get("prompt_tokens", 0)),
                      "output_tokens": int(usage.get("completion_tokens", 0))}
