from types import SimpleNamespace

from googleapiclient.errors import HttpError
from httplib2 import Response
from inbox_triage.gmail.client import GmailReadOnlyClient


def test_history_skips_deleted_message_but_keeps_next_one():
    client = GmailReadOnlyClient.__new__(GmailReadOnlyClient)
    page = {"history":[{"messagesAdded":[{"message":{"id":"removed"}},
                                           {"message":{"id":"available"}}]}]}
    client.service = SimpleNamespace(users=lambda: SimpleNamespace(
        history=lambda: SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda **kw: page))))
    def fetch(mid, *, full):
        if mid == "removed": raise HttpError(Response({"status":"404"}), b'{}')
        return {"id": mid}
    client._get = fetch
    result = list(client.iter_history("1", max_pages=1, page_size=10))
    assert result == [{"messages":[{"id":"available"}],"last_history_id":"","truncated":False}]
