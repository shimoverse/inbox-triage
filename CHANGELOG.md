# Changelog

## 0.3.0

- **Web app** (`inbox-triage-web`):
  - Google sign-in (PKCE);
  - onboarding: connect Jev, tag emails and/or type notes, pick a timeframe and schedule;
  - dashboard with run history, Jev decision stats, and a rules editor.
- **Jev is required and makes every triage decision.** Native `noul`/`choice` answers, retries on 429 and 529, and a key check before saving. Keys are per account, and hosted servers ignore server-wide keys.
- **Optional notes assistant:** DeepSeek V4.1 Flash via OpenRouter turns typed notes into rules that the user reviews.
- **Personal rules** applied within safety limits: spoof-proof sender and domain rules, phishing never promoted, security alerts never sent to Later.
- **Schedules:** hourly, daily, weekly, or the last day of the month. The CLI gains `--due` and `--days`, and runs are recorded in history.
- **Production deploy without Docker:** `deploy/install.sh` sets up a systemd service and Caddy for HTTPS. There's also a `/healthz` endpoint and an optional `Dockerfile`.
- **Docs:** two paths (hosted app vs clone and self-host), Google Cloud setup, and hosting.

## 0.2.0

- **Bug fixes:**
  - the purchase search now matches any keyword rather than requiring all of them;
  - only authenticated shops' receipts protect a sender;
  - expired Gmail history is resynced;
  - the history cursor advances past pages it has already read;
  - deleted messages are skipped;
  - a message that keeps failing is skipped instead of stalling the account;
  - suspected phishing is never labeled Needs You;
  - the journal survives a partially written line;
  - token writes are atomic.
- **Features:** multi-account support, Workspace delegation, batched Gmail calls with retries, CI, and contributor docs.

## 0.1.0

- Initial label-only Gmail triage with Jev.
