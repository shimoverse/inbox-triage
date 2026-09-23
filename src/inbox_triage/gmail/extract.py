from __future__ import annotations

import base64
import html
import re
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Callable

from ..models import AttachmentMeta, MailEvidence

DEFAULT_CHARS = 1800
EXPANDED_CHARS = 7000
_INVISIBLE = re.compile(r"[\u00ad\u034f\u200a-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_QUOTE_LINES = re.compile(r"(?im)^\s*(?:On .{0,200}wrote:|From:\s|Sent:\s|-----Original Message-----).*$")
_SIGNATURE = re.compile(r"(?im)^\s*(?:--\s*$|_{3,}\s*$)")
_FOOTER = re.compile(r"(?is)(unsubscribe|manage (?:email )?preferences|privacy policy).{0,1200}$")
_PROTECTED_PATTERNS = {
    "security": re.compile(r"\b(?:security alert|sign[ -]?in|verification code|one[- ]time (?:code|password)|2fa|otp)\b", re.I),
    "transaction": re.compile(r"\b(?:receipt|order|shipment|delivery|return|refund|invoice|payment)\b", re.I),
    "account": re.compile(r"\b(?:account|subscription|renewal|password|billing)\b", re.I),
    "school": re.compile(r"\b(?:school|teacher|student|classroom|after[- ]school)\b", re.I),
    "government": re.compile(r"\b(?:irs|government|tax notice|dmv)\b", re.I),
    "medical": re.compile(r"\b(?:medical|doctor|clinic|appointment|prescription|healthcare)\b", re.I),
}

class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "head"}: self.skip += 1
        elif not self.skip and tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}: self.out.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "head"} and self.skip: self.skip -= 1
        elif not self.skip and tag in {"p", "div", "li", "tr"}: self.out.append("\n")
    def handle_data(self, data):
        if not self.skip: self.out.append(data)

def html_to_text(value: str) -> str:
    parser = _TextHTML()
    try: parser.feed(value)
    except Exception: return ""
    return "".join(parser.out)

def _decode(data: str, charset: str = "utf-8") -> str:
    try: raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except Exception: return ""
    for enc in (charset, "utf-8", "latin-1"):
        try: return raw.decode(enc)
        except (UnicodeDecodeError, LookupError): pass
    return raw.decode("utf-8", "replace")

def clean_text(text: str) -> str:
    text = html.unescape(_INVISIBLE.sub("", text)).replace("\r", "")
    cuts = [m.start() for rx in (_QUOTE_LINES, _SIGNATURE) if (m := rx.search(text))]
    if cuts: text = text[:min(cuts)]
    text = _FOOTER.sub("", text)
    lines, seen = [], set()
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or line.startswith(">") or re.fullmatch(r"https?://\S+", line): continue
        key = line.casefold()
        if key in seen: continue
        seen.add(key); lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def _headers(payload: dict) -> dict[str, str]:
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", [])}

def _charset(part: dict) -> str:
    for h in part.get("headers", []):
        if str(h.get("name", "")).lower() == "content-type":
            m = re.search(r"charset=[\"']?([^;\"']+)", str(h.get("value", "")), re.I)
            if m: return m.group(1)
    return "utf-8"

def extract_gmail_message(message: dict, attachment_fetcher: Callable[[str], str] | None = None) -> MailEvidence:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    plain, rich, attachments = [], [], []
    def walk(part: dict):
        mime = str(part.get("mimeType", "")).lower()
        filename = str(part.get("filename", ""))
        body = part.get("body") or {}
        if filename: attachments.append(AttachmentMeta(filename=filename[:255], mime_type=mime[:127]))
        if mime.startswith("multipart/"):
            for child in part.get("parts", []) or []: walk(child)
            return
        if mime not in {"text/plain", "text/html"} or filename: return
        data = body.get("data")
        if not data and body.get("attachmentId") and attachment_fetcher:
            data = attachment_fetcher(str(body["attachmentId"]))
        if not data: return
        text = _decode(str(data), _charset(part))
        (plain if mime == "text/plain" else rich).append(text)
    walk(payload)
    candidates = plain or [html_to_text(x) for x in rich]
    text = clean_text("\n\n".join(candidates))
    if not text: text = clean_text(str(message.get("snippet", "")))
    sender = headers.get("from", "")
    address = parseaddr(sender)[1].lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    auth_raw = headers.get("authentication-results", "").lower()
    auth = {k: (m.group(1) if (m := re.search(rf"\b{k}=(pass|fail|softfail|neutral|none|temperror|permerror)", auth_raw)) else "unknown") for k in ("spf", "dkim", "dmarc")}
    precedence = headers.get("precedence", "").lower()
    bulk = bool(headers.get("list-id") or precedence in {"bulk", "list", "junk"})
    combined = f"{headers.get('subject','')}\n{text[:4000]}"
    protected = frozenset(k for k, rx in _PROTECTED_PATTERNS.items() if rx.search(combined))
    return MailEvidence(message_id=str(message.get("id", "")), thread_id=str(message.get("threadId", "")),
        sender=sender, sender_domain=domain, subject=headers.get("subject", "")[:1000], received_at=headers.get("date", "")[:200],
        labels=frozenset(str(x) for x in message.get("labelIds", []) or []), excerpt=text[:DEFAULT_CHARS],
        expanded_excerpt=text[:EXPANDED_CHARS], attachments=tuple(attachments), auth=auth,
        list_unsubscribe=bool(headers.get("list-unsubscribe")), bulk=bulk,
        user_replied=bool(message.get("user_replied", False)), protected_kinds=protected)
