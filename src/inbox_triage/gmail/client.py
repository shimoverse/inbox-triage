from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Iterator

from googleapiclient.errors import HttpError

SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Authentication-Results"]
# googleapiclient retries 429/5xx with exponential backoff when num_retries > 0.
RETRIES = 5
BATCH_SIZE = 50  # Google recommends <= 50 calls per Gmail batch to avoid rate limiting.


class HistoryExpired(RuntimeError):
    """The stored historyId is too old; the caller must do a full resync."""


def write_private(path: Path, text: str) -> None:
    """Atomically write a 0600 file so a crash never leaves a truncated token."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def execute(request):
    return request.execute(num_retries=RETRIES)


class GmailClient:
    """Thin Gmail wrapper. It never sends, deletes, archives, or marks read;
    label changes live in ``runner.apply_labels``."""

    def __init__(self, token_path: str | Path | None = None, *, service_account: str | Path | None = None,
                 subject: str | None = None):
        from googleapiclient.discovery import build
        if service_account:
            # Google Workspace domain-wide delegation: one key, many mailboxes.
            from google.oauth2 import service_account as sa
            creds = sa.Credentials.from_service_account_file(str(Path(service_account).expanduser()),
                                                             scopes=[SCOPE], subject=subject)
        else:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            if token_path is None:
                raise ValueError("A token path or service account is required")
            self.token_path = Path(token_path).expanduser()
            creds = Credentials.from_authorized_user_file(str(self.token_path))
            if not creds.valid and creds.refresh_token:
                creds.refresh(Request())
                # OAuth refresh updates only the external local token, never Gmail state.
                write_private(self.token_path, creds.to_json())
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def profile(self) -> dict[str, str]:
        raw = execute(self.service.users().getProfile(userId="me"))
        return {"emailAddress": str(raw.get("emailAddress", "unknown")),
                "historyId": str(raw.get("historyId", ""))}

    def account_id(self) -> str:
        return self.profile()["emailAddress"]

    def _request(self, message_id: str, full: bool):
        kwargs = {"userId": "me", "id": message_id, "format": "full" if full else "metadata"}
        if not full:
            kwargs["metadataHeaders"] = _METADATA_HEADERS
        return self.service.users().messages().get(**kwargs)

    def _get(self, message_id: str, *, full: bool) -> dict:
        return execute(self._request(message_id, full))

    def get_many(self, ids: Iterable[str], *, full: bool = False) -> list[dict]:
        """Fetch messages in batches, skipping ones deleted since they were listed."""
        ids = list(ids)
        out: dict[str, dict] = {}
        retry: list[str] = []
        batch_factory = getattr(self.service, "new_batch_http_request", None)
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start:start + BATCH_SIZE]
            if batch_factory is None:
                retry.extend(chunk)
                continue
            def callback(request_id, response, exception):
                if exception is None:
                    out[request_id] = response
                elif not (isinstance(exception, HttpError) and exception.resp.status == 404):
                    retry.append(request_id)  # usually a per-call 429; retried below with backoff
            batch = batch_factory(callback=callback)
            for mid in chunk:
                batch.add(self._request(mid, full), request_id=mid)
            batch.execute()
        for mid in retry:
            try:
                out[mid] = self._get(mid, full=full)
            except HttpError as exc:
                # Mail can be deleted between listing and fetching it.
                if exc.resp.status != 404:
                    raise
        return [out[mid] for mid in ids if mid in out]

    def list_ids(self, query: str, max_results: int | None = None) -> list[str]:
        ids, token = [], None
        while max_results is None or len(ids) < max_results:
            page_size = 500 if max_results is None else min(500, max_results - len(ids))
            page = execute(self.service.users().messages().list(
                userId="me", q=query, maxResults=page_size, pageToken=token))
            ids.extend(str(item["id"]) for item in page.get("messages", ()))
            token = page.get("nextPageToken")
            if not token:
                break
        return list(dict.fromkeys(ids))

    def iter_messages(self, query: str, max_results: int) -> Iterator[dict]:
        yield from self.get_many(self.list_ids(query, max_results), full=True)

    def iter_metadata(self, query: str, max_results: int) -> Iterator[dict]:
        yield from self.get_many(self.list_ids(query, max_results), full=False)

    def iter_history(self, start_history_id: str, *, max_pages: int, page_size: int) -> Iterator[dict]:
        """Yield pages of newly added messages plus the last history id they cover."""
        page_token = None
        for page_no in range(max_pages):
            try:
                response = execute(self.service.users().history().list(
                    userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"],
                    maxResults=min(500, page_size), pageToken=page_token))
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpired("Gmail history cursor expired") from None
                raise
            records = response.get("history", []) or []
            ids, seen = [], set()
            for record in records:
                for added in record.get("messagesAdded", []) or []:
                    mid = str((added.get("message") or {}).get("id", ""))
                    if mid and mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
            next_token = response.get("nextPageToken")
            last = str(records[-1].get("id", "")) if records else ""
            yield {"messages": self.get_many(ids, full=False), "last_history_id": last,
                   "truncated": bool(next_token and page_no + 1 >= max_pages)}
            if not next_token:
                break
            page_token = next_token

    def attachment_data(self, message_id: str, attachment_id: str) -> str:
        return str(execute(self.service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id)).get("data", ""))


# Backwards-compatible name; the client was never strictly read-only.
GmailReadOnlyClient = GmailClient
