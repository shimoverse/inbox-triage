from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

class Destination(StrEnum):
    NEEDS_YOU = "needs_you"
    UPDATES = "updates"
    FOR_YOU = "for_you"
    LATER = "later"
    SPAM = "spam"
    UNCHANGED = "unchanged"

ALLOWED_TOPICS = frozenset({"shopping", "home", "work_projects", "travel", "health", "money"})

@dataclass(frozen=True)
class AttachmentMeta:
    filename: str
    mime_type: str

@dataclass(frozen=True)
class MailEvidence:
    message_id: str
    thread_id: str = ""
    sender: str = ""
    sender_domain: str = ""
    subject: str = ""
    received_at: str = ""
    labels: frozenset[str] = frozenset()
    excerpt: str = ""
    expanded_excerpt: str = ""
    attachments: tuple[AttachmentMeta, ...] = ()
    auth: Mapping[str, str] = field(default_factory=dict)
    list_unsubscribe: bool = False
    bulk: bool = False
    user_replied: bool = False
    protected_kinds: frozenset[str] = frozenset()

@dataclass(frozen=True)
class ContextPack:
    sender_relationship: str = "unknown"
    thread_participation: bool = False
    recent_purchase: bool = False
    active_subscription: bool = False
    active_priorities: tuple[str, ...] = ()
    priorities_relevant: bool = False
    owned_topics: tuple[str, ...] = ()
    similar_read: int = 0
    similar_replied: int = 0
    similar_archived_unread: int = 0
    user_notes: str = ""

    def outbound(self) -> dict:
        # Deliberately coarse: no historical text, addresses, IDs, or timestamps leave the host.
        # user_notes is the user's own onboarding summary of what matters to them.
        extra = {"user_preferences": self.user_notes[:600]} if self.user_notes else {}
        return {**extra, "sender_relationship": self.sender_relationship,
                "thread_participation": self.thread_participation,
                "recent_purchase": self.recent_purchase,
                "active_subscription": self.active_subscription,
                "active_priorities": list(self.active_priorities[:5]) if self.priorities_relevant else [],
                "owned_topics": list(self.owned_topics[:5]),
                "similar_read": min(self.similar_read, 100),
                "similar_replied": min(self.similar_replied, 100),
                "similar_archived_unread": min(self.similar_archived_unread, 100)}

@dataclass(frozen=True)
class JevSignals:
    requires_action: float = 0.0
    service_update: float = 0.0
    personal_relevance: float = 0.0
    personalized_offer: float = 0.0
    unsolicited_bulk: float = 0.0
    deceptive: float = 0.0
    human_waiting: float = 0.0
    category: str = "unknown"
    category_confidence: float = 0.0
    topics: Mapping[str, float] = field(default_factory=dict)

@dataclass(frozen=True)
class TopicDecision:
    topics: tuple[str, ...] = ()
    confidence: Mapping[str, float] = field(default_factory=dict)

@dataclass(frozen=True)
class RoutingDecision:
    destination: Destination
    reason: str
    confidence: float
    topics: TopicDecision = TopicDecision()
    would_archive: bool = False
