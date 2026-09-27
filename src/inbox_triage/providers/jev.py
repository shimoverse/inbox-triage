"""Jev (typesafe.ai) provider: focused yes/no questions over a trimmed excerpt.

Jev is reachable two ways with the same request and answer shapes: TypeSafe's own API
with a TypeSafe key, or OpenRouter with an OpenRouter key (``sk-or-…``), which bills
per input token and needs no TypeSafe account.
See https://openrouter.ai/docs/guides/community/typesafe-sdk.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from ..models import ContextPack, JevSignals, MailEvidence
from .base import BINARY as _BINARY, TOPICS as _TOPICS, Provider, ProviderError, evidence_state, parse_answers, questions  # noqa: F401


class JevError(ProviderError):
    status: int | None = None


# 429 rate limited and 529 overloaded are documented as retry-with-backoff.
RETRYABLE = {429, 500, 502, 503, 504, 529}
SIGNUP_URL = "https://console.typesafe.ai/"
TYPESAFE_BASE = "https://api.typesafe.ai"
OPENROUTER_BASE = "https://openrouter.ai/api"


def is_openrouter_key(key: str) -> bool:
    return key.startswith("sk-or-")


class JevProvider(Provider):
    name = "jev"
    topics: tuple[str, ...] = tuple(_TOPICS)

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None,
                 timeout: float = 30, retries: int = 2):
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        # An OpenRouter key goes to OpenRouter; an explicit TYPESAFE_API_BASE always wins.
        default = OPENROUTER_BASE if is_openrouter_key(self.api_key) else TYPESAFE_BASE
        self.base_url = (base_url or os.environ.get("TYPESAFE_API_BASE") or default).rstrip("/")
        self.model, self.timeout, self.retries = model or "jev-latest", timeout, retries
        if not self.api_key:
            raise JevError("A Jev key is required: a TypeSafe key (TYPESAFE_API_KEY) from "
                           f"{SIGNUP_URL}, or an OpenRouter key (OPENROUTER_API_KEY)")

    def payload(self, e: MailEvidence, c: ContextPack) -> dict:
        state = evidence_state(e, c, subject_chars=1000, excerpt_chars=1800)
        state["attachment_types"] = sorted({x.mime_type for x in e.attachments[:20]})
        return {"model": self.model, "state": state, "questions": questions(self.topics)}

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key}
        if self.base_url == OPENROUTER_BASE:
            headers["X-Title"] = "Inbox Triage"  # optional OpenRouter app attribution
        req = urllib.request.Request(self.base_url + "/v1/systemone", data=data, method="POST", headers=headers)
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE or attempt >= self.retries:
                    error = JevError(f"Jev HTTP {exc.code}")
                    error.status = exc.code
                    raise error from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                if attempt >= self.retries:
                    raise JevError("Jev transport or response error") from None
            time.sleep(.25 * (2 ** attempt))
        raise JevError("Jev request failed")

    def verify(self) -> None:
        """One tiny call to confirm the key works before onboarding continues."""
        self._post({"model": self.model, "state": "Connection check.",
                    "questions": {"ok": {"type": "noul", "instructions": "Is this a connection check?",
                                         "criteria": {"true": "Yes.", "false": "No."}}}})

    def classify_with_usage(self, e: MailEvidence, c: ContextPack) -> tuple[JevSignals, dict]:
        raw = self._post(self.payload(e, c))
        signals = parse_answers(raw.get("answers"), self.topics)
        usage = raw.get("usage") or {}
        out = {"input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0))}
        if isinstance(usage.get("cost"), (int, float)) and not isinstance(usage["cost"], bool):
            out["cost"] = float(usage["cost"])  # OpenRouter reports what the call cost in USD
        return signals, out
