# Contributing

Thanks for helping! Inbox Triage handles private mail, so a few rules are firmer than usual.

## Ground rules

- **Never commit real email.** Tests use synthetic messages only: no exports, screenshots, or "anonymized" real mail.
- **Label-only is a hard boundary.** PRs that add sending, deleting, archiving, marking read, or Spam moves won't be merged.
- **Models advise; policy decides.** New signals go through `policy.py`, stay conservative, and include a test showing uncertain mail stays unchanged.
- **Minimize what leaves the machine.** Any change to what providers receive (`providers/base.py:evidence_state`) needs a clear reason and a test asserting that IDs and addresses are not sent.

## Development

```bash
uv sync --dev --extra anthropic
uv run pytest -q
```

CI runs the tests on Python 3.11–3.13. Keep dependencies minimal; new providers should use the vendor's official SDK as an optional extra, or the standard library.

## Adding a provider

Subclass `providers.base.Provider`, implement `classify_with_usage()` so it returns `(JevSignals, usage)`, and build its answers with `questions()` and `parse_answers()`. Then register it in `providers/__init__.py`. Tests must stub the network.
