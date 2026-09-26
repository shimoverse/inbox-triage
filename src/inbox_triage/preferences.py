"""Personal rules learned during onboarding ("my kids' school is important").

Rules are applied locally and deterministically after the model answers, so a
user's explicit preference always wins over a model guess, with two safety
exceptions: suspected phishing is never promoted, and security alerts are never
buried in Later or Junk. A Junk rule is decided before the model is asked, so
junk never reaches Jev.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import MailEvidence

KINDS = ("sender", "domain", "keyword")
ACTIONS = ("important", "not_important", "junk")
MAX_RULES = 200
MAX_SUMMARY = 600


@dataclass(frozen=True)
class Rule:
    kind: str
    value: str
    action: str
    note: str = ""

    @classmethod
    def parse(cls, raw) -> "Rule | None":
        if not isinstance(raw, dict):
            return None
        kind, action = str(raw.get("kind", "")).strip(), str(raw.get("action", "")).strip()
        value = str(raw.get("value", "")).strip().casefold()
        if kind not in KINDS or action not in ACTIONS or not value or len(value) > 120:
            return None
        if kind == "domain":
            value = value.lstrip("@").removeprefix("www.")
            if "." not in value or " " in value:
                return None
        if kind == "sender" and "@" not in value:
            return None
        if kind == "keyword" and len(value) < 3:
            return None
        return cls(kind, value, action, str(raw.get("note", ""))[:200])


@dataclass(frozen=True)
class Preferences:
    rules: tuple[Rule, ...] = ()
    summary: str = ""

    def to_json(self) -> dict:
        return {"rules": [asdict(r) for r in self.rules], "summary": self.summary}

    @classmethod
    def from_json(cls, data) -> "Preferences":
        data = data if isinstance(data, dict) else {}
        rules, seen = [], set()
        for raw in data.get("rules", []) if isinstance(data.get("rules"), list) else []:
            rule = Rule.parse(raw)
            if rule and (rule.kind, rule.value) not in seen:
                seen.add((rule.kind, rule.value))
                rules.append(rule)
        return cls(tuple(rules[:MAX_RULES]), str(data.get("summary", ""))[:MAX_SUMMARY])

    def merged(self, other: "Preferences") -> "Preferences":
        """Newer rules for the same sender/domain/keyword replace older ones."""
        newer = {(r.kind, r.value) for r in other.rules}
        rules = [r for r in self.rules if (r.kind, r.value) not in newer] + list(other.rules)
        summary = " ".join(x for x in (self.summary, other.summary) if x)[-MAX_SUMMARY:]
        return Preferences.from_json({"rules": [asdict(r) for r in rules], "summary": summary})


def load(path: Path) -> Preferences:
    try:
        return Preferences.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return Preferences()


def _authenticated(e: MailEvidence) -> bool:
    return e.auth.get("dmarc") == "pass" or (e.auth.get("spf") == "pass" and e.auth.get("dkim") == "pass")


def match(prefs: Preferences, e: MailEvidence) -> Rule | None:
    """Most specific rule wins: sender, then domain, then keyword.

    Sender and domain rules require an authenticated message, otherwise anyone
    could spoof "school.example" to get promoted."""
    from email.utils import parseaddr
    address = parseaddr(e.sender)[1].casefold()
    domain = e.sender_domain.casefold()
    auth = _authenticated(e)
    text = f"{e.subject}\n{e.excerpt[:600]}".casefold()
    best: dict[str, Rule] = {}
    for rule in prefs.rules:
        if rule.kind == "sender" and auth and address == rule.value:
            best.setdefault("sender", rule)
        elif rule.kind == "domain" and auth and (domain == rule.value or domain.endswith("." + rule.value)):
            best.setdefault("domain", rule)
        elif rule.kind == "keyword" and re.search(r"(?<!\w)" + re.escape(rule.value) + r"(?!\w)", text):
            best.setdefault("keyword", rule)
    for kind in KINDS:
        if kind in best:
            return best[kind]
    return None
