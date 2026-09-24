"""Jev's safety questions with only the Shopping topic, over a shorter excerpt.

No large-model orchestrator, raw MIME, attachments, or prior email text is sent.
"""
from __future__ import annotations

from ..models import ContextPack, MailEvidence
from .base import LIVE_TOPICS, evidence_state, questions
from .jev import JevError, JevProvider, _BINARY  # noqa: F401 - re-exported for compatibility


class LeanJevProvider(JevProvider):
    topics = LIVE_TOPICS

    def payload(self, e: MailEvidence, c: ContextPack) -> dict:
        return {"model": self.model, "state": evidence_state(e, c), "questions": questions(self.topics)}
