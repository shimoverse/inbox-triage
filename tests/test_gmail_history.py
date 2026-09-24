from inbox_triage.gmail.client import GmailClient, GmailError


def test_history_skips_deleted_message_but_keeps_next_one():
    client = GmailClient.__new__(GmailClient)
    page = {"history": [{"id": "77", "messagesAdded": [{"message": {"id": "removed"}},
                                                      {"message": {"id": "available"}}]}]}
    seen = {}
    def request(method, path, params=None, body=None):
        seen.update(params or {})
        return page
    client._request = request
    def fetch(mid, *, full):
        if mid == "removed":
            raise GmailError(404)
        return {"id": mid}
    client._get = fetch
    result = list(client.iter_history("1", max_pages=1, page_size=10))
    assert result == [{"messages": [{"id": "available"}], "last_history_id": "77", "truncated": False}]
    assert seen["historyTypes"] == ["messageAdded"]
