from __future__ import annotations

from dataclasses import dataclass
import re
from .models import ALLOWED_TOPICS, ContextPack, Destination, JevSignals, MailEvidence, RoutingDecision, TopicDecision

@dataclass(frozen=True)
class Thresholds:
    action: float = .80
    update: float = .80
    relevant: float = .78
    later: float = .92
    spam: float = .98
    topic: float = .82
    # Any real deception signal blocks attention labels: a phish saying
    # "Action required" must never be promoted to Needs You or Updates.
    suspicious: float = .6

# Broad words in newsletter bodies ("account", "doctor", "order") are not
# evidence of a personal service update. Prefer abstention to a false Updates
# label; important mail remains visible in its original Gmail location.
_PERSONAL_EVENT = re.compile(
    r"\b(?:your (?:order|receipt|shipment|delivery|return|refund|invoice|payment|"
    r"appointment|reservation|booking|account (?:alert|update|status)|security alert)|"
    r"order\s*#|payment (?:failed|received|due)|refund (?:issued|processed)|"
    r"(?:appointment|reservation|booking) (?:confirmed|changed|cancelled)|"
    r"verification code|school (?:notice|update)|action required|security alert|"
    r"new sign[ -]?in|new login|transaction (?:decline|declined)|"
    r"your (?:subscription (?:has been )?cancelled|cancellation confirmation|"
    r"account summary|statement)|password (?:changed|reset)|"
    r"you (?:allowed|shared) .{0,70}account data)\b", re.I)
_PROMOTIONAL_SUBJECT = re.compile(
    r"(?:\b(?:sale|deal|discount|promo|offer|bonus|collection|giveaway|"
    r"cash ?back|sitewide|last chance|last call)\b|\d+\s*%\s*off|"
    r"\$\d+(?:\.\d+)?\s*(?:off|\/wk)|free tickets|save (?:up to|\d+))", re.I)

def personal_event_subject(e: MailEvidence) -> bool:
    return bool(_PERSONAL_EVENT.search(e.subject) and not _PROMOTIONAL_SUBJECT.search(e.subject))

def decide(e: MailEvidence, c: ContextPack, s: JevSignals, t: Thresholds = Thresholds(),
           preference=None) -> RoutingDecision:
    """Model signals -> at most one attention label; a matching user rule
    (``preferences.Rule``) then adjusts it, within the safety limits."""
    base = _decide(e, c, s, t)
    if preference is None or s.deceptive >= t.suspicious:
        return base
    if preference.action == "important":
        if base.destination in {Destination.NEEDS_YOU, Destination.UPDATES, Destination.FOR_YOU}:
            return base
        return RoutingDecision(Destination.FOR_YOU, "You told us mail like this matters to you.", .9, base.topics)
    if preference.action == "not_important":
        if "security" in e.protected_kinds:
            return base  # never bury sign-in or verification alerts
        return RoutingDecision(Destination.LATER, "You told us mail like this can wait.", .9, base.topics, True)
    return base


def _decide(e: MailEvidence, c: ContextPack, s: JevSignals, t: Thresholds) -> RoutingDecision:
    topic_scores = {k: float(v) for k, v in s.topics.items() if k in ALLOWED_TOPICS and 0 <= float(v) <= 1 and float(v) >= t.topic}
    topics = TopicDecision(tuple(sorted(topic_scores)), topic_scores)
    protected = bool(e.protected_kinds or e.user_replied or "IMPORTANT" in e.labels or "CATEGORY_UPDATES" in e.labels or c.thread_participation or c.recent_purchase or c.active_subscription or c.similar_replied or c.sender_relationship in {"active_thread", "known_person"})
    promotional = bool(_PROMOTIONAL_SUBJECT.search(e.subject))
    if s.deceptive >= t.suspicious:
        return RoutingDecision(Destination.UNCHANGED, "Possible deception; the message is left untouched for your judgement.", s.deceptive, topics)
    if not promotional and max(s.requires_action, s.human_waiting) >= t.action:
        return RoutingDecision(Destination.NEEDS_YOU, "A reply, decision, deadline, or fix appears necessary.", max(s.requires_action, s.human_waiting), topics)
    if personal_event_subject(e) and (e.protected_kinds or s.service_update >= t.update):
        confidence = max(s.service_update, .85 if e.protected_kinds else 0)
        return RoutingDecision(Destination.UPDATES, "Protected account, transaction, school, security, government, or health evidence.", confidence, topics)
    mass_mail = e.bulk or e.list_unsubscribe
    relationship = bool(c.thread_participation or c.sender_relationship in {"active_thread", "known_person"}
                        or c.priorities_relevant)
    # For You means a person or a priority of yours, not marketing "based on your activity".
    if s.personal_relevance >= t.relevant and (not mass_mail or relationship):
        return RoutingDecision(Destination.FOR_YOU, "The message is specifically relevant to an active relationship or priority.", s.personal_relevance, topics)
    # Mass mail tailored from your activity (listings, "picked for you" offers) can wait,
    # unless it's a security notice, a real account/order event, or from someone you know.
    # Only when nothing protects it: no account/order/security/medical evidence, no Gmail
    # Important/Updates signal, no service update, no relationship. Protected mail that merely
    # looks tailored stays unchanged (never For You, never buried).
    tailored = max(s.personal_relevance, s.personalized_offer)
    if (mass_mail and tailored >= t.relevant and not protected and s.service_update < t.update
            and not personal_event_subject(e)):
        return RoutingDecision(Destination.LATER, "Mass mail tailored to your activity; it can wait.", tailored, topics, True)
    authenticated = e.auth.get("dmarc") == "pass" or (e.auth.get("spf") == "pass" and e.auth.get("dkim") == "pass")
    if not protected and (e.bulk or e.list_unsubscribe) and s.deceptive >= t.spam and s.unsolicited_bulk >= .90 and not authenticated:
        return RoutingDecision(Destination.SPAM, "Very strong deception and unsolicited evidence without a protected relationship.", min(s.deceptive, s.unsolicited_bulk), topics, True)
    later_score = min(s.unsolicited_bulk, 1 - max(s.personal_relevance, s.service_update, s.requires_action, s.human_waiting))
    if not protected and later_score >= t.later and (e.bulk or e.list_unsubscribe):
        return RoutingDecision(Destination.LATER, "High-confidence legitimate bulk mail with no protected or active-thread evidence.", later_score, topics, True)
    return RoutingDecision(Destination.UNCHANGED, "Uncertainty or conflicting evidence keeps the message visible.", max(.5, 1-later_score), topics)
