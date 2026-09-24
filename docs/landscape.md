# Related projects and roadmap

Inbox Triage is deliberately narrow: a local, label-only, conservative sorter. Here is how it compares with other open-source approaches, as surveyed in September 2026. Star counts are approximate.

| Project | Approach | What we took or might take |
|---|---|---|
| [elie222/inbox-zero](https://github.com/elie222/inbox-zero) (~12k★) | Full SaaS or self-hosted assistant: plain-English rules, cold-email blocker, Pub/Sub push, many LLM providers | Provider abstraction; later, learned sender patterns (without letting them override explicit rules) |
| [Mail-0/Zero](https://github.com/Mail-0/Zero) (~11k★) | Open-source email client with AI features | Out of scope: we are not a client |
| [ColeMurray/gmail-llm-labeler](https://github.com/ColeMurray/gmail-llm-labeler) | Python ETL labeler, OpenAI or Ollama, SQLite, dry-run | Same shape; confirms Ollama support is expected |
| [arestivo/gmail-classifier](https://github.com/arestivo/gmail-classifier) | Ollama, a marker label as state, `evaluate` export to CSV | An evaluation command (roadmap) |
| [local-mail-organizer](https://github.com/marcorau1993-spec/local-mail-organizer) | IMAP, deterministic protection rules before the AI, per-account corrections, undo | Matches our "policy decides" design; correction learning (roadmap) |
| n8n / Apps Script + Gemini labelers | Polling with JSON-schema-constrained output | Structured outputs, now used by the Claude and OpenAI-compatible providers |

## Lessons adopted in this release

- **Pluggable providers:** Claude, any OpenAI-compatible endpoint (local Ollama by default), Jev, and an offline rules provider.
- **Structured JSON output** with a strict schema, validated again locally.
- **Resilient sync:** a full resync when the Gmail History API returns 404, cursor advance on truncated history, batched metadata reads (up to 50 per batch), and backoff on 429/5xx.
- **Multi-account by default:** per-account token discovery (`--all`) and Workspace domain-wide delegation.
- **Hardened context:** purchase facts require DMARC-aligned, non-freemail senders and never come from Sent, Spam, or Trash.

## Roadmap (not yet implemented)

1. **Evaluation harness:** `inbox-triage eval` over a synthetic labeled JSONL set, to compare providers, models, and thresholds before changing defaults.
2. **Learn from corrections:** detect when the user removes or changes one of our labels (History API `labelRemoved`) and use it as a local sender override or few-shot example.
3. **Sender priors:** if most recent mail from a sender got the same verified label, use that as a signal. Explicit rules still win.
4. **Push instead of polling (optional):** Gmail `users.watch` with Pub/Sub for near-real-time triage. This needs a public webhook or pull subscription and daily watch renewal, so polling stays the default for a local CLI.
5. **Model cascade:** a cheap or local model first, escalating to a stronger model only when confidence is low.
