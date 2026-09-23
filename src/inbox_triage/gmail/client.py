from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date"]

class GmailReadOnlyClient:
    """Gmail reader with no label creation, modification, send, or delete methods."""
    def __init__(self, token_path: str | Path):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        self.token_path = Path(token_path).expanduser()
        creds = Credentials.from_authorized_user_file(str(self.token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # OAuth refresh updates only the external local token, never Gmail state.
            self.token_path.write_text(json.dumps(json.loads(creds.to_json()), indent=2))
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    def profile(self) -> dict[str, str]:
        raw = self.service.users().getProfile(userId="me").execute()
        return {"emailAddress": str(raw.get("emailAddress", "unknown")),
                "historyId": str(raw.get("historyId", ""))}
    def account_id(self) -> str:
        return self.profile()["emailAddress"]
    def _get(self, message_id: str, *, full: bool) -> dict:
        kwargs = {"userId":"me", "id":message_id, "format":"full" if full else "metadata"}
        if not full: kwargs["metadataHeaders"] = _METADATA_HEADERS
        return self.service.users().messages().get(**kwargs).execute()
    def iter_messages(self, query: str, max_results: int) -> Iterator[dict]:
        yielded, page = 0, None
        while yielded < max_results:
            response = self.service.users().messages().list(userId="me", q=query, maxResults=min(100, max_results-yielded), pageToken=page).execute()
            for item in response.get("messages", []):
                if yielded >= max_results: break
                yield self._get(str(item["id"]), full=True); yielded += 1
            page = response.get("nextPageToken")
            if not page: break
    def iter_metadata(self, query: str, max_results: int) -> Iterator[dict]:
        yielded, page = 0, None
        while yielded < max_results:
            response = self.service.users().messages().list(userId="me", q=query, maxResults=min(100, max_results-yielded), pageToken=page).execute()
            for item in response.get("messages", []):
                if yielded >= max_results: break
                yield self._get(str(item["id"]), full=False); yielded += 1
            page = response.get("nextPageToken")
            if not page: break
    def iter_history(self, start_history_id: str, *, max_pages: int, page_size: int) -> Iterator[dict]:
        page_token = None
        for page_no in range(max_pages):
            response = self.service.users().history().list(
                userId="me", startHistoryId=start_history_id,
                maxResults=min(500, page_size), pageToken=page_token).execute()
            ids, seen = [], set()
            for record in response.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    message = added.get("message") or {}
                    mid = str(message.get("id", ""))
                    if mid and mid not in seen: seen.add(mid); ids.append(mid)
            next_token = response.get("nextPageToken")
            yield {"messages":[self._get(mid, full=False) for mid in ids],
                   "truncated": bool(next_token and page_no + 1 >= max_pages)}
            if not next_token: break
            page_token = next_token
    def attachment_data(self, message_id: str, attachment_id: str) -> str:
        return str(self.service.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute().get("data", ""))
