"""Offline provider: no network, no model. Uses Gmail's own categories and headers.

It is intentionally timid: it only produces strong signals for mail Gmail has
already categorized and that carries bulk-mail headers, so most mail stays
unchanged. Useful for trying the tool without any API key.
"""
from __future__ import annotations

import re

from ..models import ContextPack, JevSignals, MailEvidence
from .base import Provider

_ACTION = re.compile(r"\b(?:action required|please (?:reply|respond|confirm|review|approve|sign)|"
                     r"(?:payment|invoice) (?:due|overdue)|deadline|rsvp)\b", re.I)
_SHOPPING = re.compile(r"\b(?:order|shipped|shipment|delivery|delivered|return|refund|receipt)\b", re.I)


class RulesProvider(Provider):
    name = "rules"

    def classify_with_usage(self, e: MailEvidence, c: ContextPack) -> tuple[JevSignals, dict]:
        labels = e.labels
        bulk = e.bulk or e.list_unsubscribe
        known = c.sender_relationship in {"active_thread", "known_person"}
        promo = "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels
        signals = JevSignals(
            requires_action=.85 if known and not bulk and _ACTION.search(e.subject) else 0.0,
            human_waiting=.8 if c.thread_participation and not bulk else 0.0,
            service_update=.85 if "CATEGORY_UPDATES" in labels and e.auth.get("dmarc") == "pass" else 0.0,
            personal_relevance=.8 if known and not bulk else 0.0,
            unsolicited_bulk=.95 if promo and bulk and not known else 0.0,
            category="low_priority" if promo and bulk else "unknown",
            category_confidence=.9 if promo and bulk else 0.0,
            topics={"shopping": .9 if _SHOPPING.search(e.subject) and e.auth.get("dmarc") == "pass" else 0.0},
        )
        return signals, {"input_tokens": 0, "output_tokens": 0}
