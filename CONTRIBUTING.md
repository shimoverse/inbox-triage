# Contributing

Thanks for helping! Inbox Triage handles private mail, so a few rules are firmer than usual.

## Ground rules

- **Never commit real email.** Tests use synthetic messages only: no exports, screenshots, or "anonymized" real mail.
- **Label-only is a hard boundary.** PRs that add sending, deleting, archiving, marking read, or Spam moves won't be merged.
- **Models advise; policy decides.** New signals go through `policy.py`, stay conservative, and include a test showing uncertain mail stays unchanged.
- **Minimize what leaves the machine.** Any change to what providers receive (`providers/base.py:evidence_state`) needs a clear reason and a test asserting that IDs and addresses are not sent.

## Development

```bash
uv sync --dev
uv run pytest -q
```

CI runs the tests on Python 3.11–3.13. Keep dependencies minimal; new providers should use the vendor's official SDK as an optional extra, or the standard library.

## Jev and the assistant

- **Every per-email decision is a Jev answer.** New signals should be new Jev questions (`providers/base.py`), and `policy.py` should interpret them conservatively.
- **The optional assistant** (`assistant.py`, OpenRouter) only turns typed notes into rules that the user reviews. It must never classify mail.
- **Tests stub the network.** Use Jev's documented response shapes: `{"type": "noul", "noul": 0.9}` and `{"type": "choice", "choice": ..., "probabilities": {...}, "confidence": ...}`.
