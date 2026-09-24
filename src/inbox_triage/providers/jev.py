"""Jev (typesafe.ai) provider: focused yes/no questions over a trimmed excerpt."""
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


class JevProvider(Provider):
    name = "jev"
    topics: tuple[str, ...] = tuple(_TOPICS)

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None,
                 timeout: float = 30, retries: int = 2):
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        self.base_url = (base_url or os.environ.get("TYPESAFE_API_BASE", "https://api.typesafe.ai")).rstrip("/")
        self.model, self.timeout, self.retries = model or "jev-latest", timeout, retries
        if not self.api_key:
            raise JevError(f"A Jev API key (TYPESAFE_API_KEY) is required. Get one at {SIGNUP_URL}")

    def payload(self, e: MailEvidence, c: ContextPack) -> dict:
        state = evidence_state(e, c, subject_chars=1000, excerpt_chars=1800)
        state["attachment_types"] = sorted({x.mime_type for x in e.attachments[:20]})
        return {"model": self.model, "state": state, "questions": questions(self.topics)}

    def _post(self, payload: dict) -> dict:
        data = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(self.base_url + "/v1/systemone", data=data, method="POST",
              headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
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
        return signals, {"input_tokens": int(usage.get("input_tokens", 0)),
                         "output_tokens": int(usage.get("output_tokens", 0))}
