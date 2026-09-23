"""Jev's existing safety questions with the optional Shopping topic.

No large-model orchestrator, raw MIME, attachments, or prior email text is sent.
"""
from __future__ import annotations

from ..models import ContextPack, JevSignals, MailEvidence
from .jev import JevError, JevProvider, _BINARY

LIVE_TOPICS = ("shopping",)


class LeanJevProvider(JevProvider):
    def payload(self, e: MailEvidence, c: ContextPack) -> dict:
        data = super().payload(e, c)
        state = data["state"]
        state["subject"] = state["subject"][:200]
        state["excerpt"] = state["excerpt"][:1000]
        # MIME attachment content was never sent. The types are not needed for
        # this rubric either; suppress them to reduce exposure and tokens.
        state.pop("attachment_types", None)
        keep = set(_BINARY) | {"category", *("topic_" + t for t in LIVE_TOPICS)}
        data["questions"] = {k: v for k, v in data["questions"].items() if k in keep}
        return data

    def classify_with_usage(self, e: MailEvidence, c: ContextPack) -> tuple[JevSignals, dict]:
        raw = self._post(self.payload(e, c))
        answers = raw.get("answers")
        if not isinstance(answers, dict):
            raise JevError("Invalid Jev response")
        probabilities = {}
        for key in _BINARY:
            choice, conf = self._answer(answers, key, {"yes", "no"})
            probabilities[key] = conf if choice == "yes" and conf >= .5 else 0.0 if conf < .5 else 1 - conf
        category, conf = self._answer(answers, "category", {"action", "update", "personalized", "low_priority", "spam"})
        topics = {}
        for key in LIVE_TOPICS:
            choice, confidence = self._answer(answers, "topic_" + key, {"yes", "no"})
            topics[key] = confidence if choice == "yes" and confidence >= .5 else 0.0 if confidence < .5 else 1 - confidence
        usage = raw.get("usage") or {}
        return JevSignals(**probabilities, category=category, category_confidence=conf, topics=topics), {
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
        }

    def classify(self, evidence: MailEvidence, context: ContextPack) -> JevSignals:
        return self.classify_with_usage(evidence, context)[0]
