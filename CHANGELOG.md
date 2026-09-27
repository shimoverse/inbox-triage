# Changelog

## 0.6.0

- **Jev through OpenRouter.** OpenRouter now sells Jev (`jev-latest`: $0.042 per million input tokens, output free) and accepts TypeSafe's request unchanged.
  - An OpenRouter key (`sk-or-…`) works as a Jev key, with no TypeSafe account.
  - Without a TypeSafe key, `OPENROUTER_API_KEY` runs both Jev and the notes assistant.
  - When OpenRouter reports what a call cost, runs record it as `jev_cost`, in USD.
- **Sponsored free beta: no key to paste.** An operator can pay for users' Jev calls by setting `INBOX_TRIAGE_SPONSORED_JEV_DAILY` to a number of calls per account per day.
  - Setup drops the *Connect Jev* step, the welcome page shows two steps, and the banner says *No API key needed*.
  - Users can still bring their own key in Settings; it is then used, with no limit.
  - Once the day's allowance is used, a run pauses like a Gmail break and carries on after midnight UTC.
  - A key that is out of credits (HTTP 402) pauses runs for an hour instead of marking emails as failed.
  - The server checks the sponsored key once at startup and logs the result.
  - Without the setting, a hosted server still never uses its own key for other people.
- **The beta limit is checked before Google's consent screen.** Google caps an unverified app at 100 people over its lifetime, counting everyone who approves it. So once the beta is full, *Continue with Google* is turned away before it reaches Google. Returning users enter their address to sign back in. A typed address always goes on to Google, so nobody can use it to check who has an account; anyone who isn't a member is turned away after Google, as before.
- **The first run labels straight away.** *Preview first* is still one switch away in setup.
- **Colour-coded labels.** New Gmail labels get colours that match the app. Labels made by older versions are coloured once, unless someone has already picked a colour.
- **Pauses say why:** Gmail asked for a break, the day's allowance is used up, or the Jev key is out of credits.
- **Chrome and Brave extension** (`extension/`, Manifest V3, plain JavaScript). It adds a live dashboard at the top of Gmail:
  - label chips with 7-day counts, which open the label in Gmail;
  - a chart of mail labeled per day, with Needs You highlighted, that shows a tooltip on hover or keyboard focus;
  - recently labeled mail with links to each email.

  When the unread count rises, it asks the server to sort new mail, so labels appear within about a minute while Gmail is open.
  - **Sign-in:** the extension uses `chrome.identity.launchWebAuthFlow` against the server's new `/connect` page, so it works in Brave too. It gets a one-time, PKCE-bound code and swaps it for a revocable 90-day token (`/api/ext/*`, CORS for extension origins, bearer tokens only).
  - **No keys:** the extension holds no Google token and no AI key.
  - **Hosted servers** hand tokens only to extensions listed in `INBOX_TRIAGE_EXTENSION_IDS`.
  - **Default schedule:** accounts connected from the extension get an hourly schedule if they had none.
  - **Run history** shows runs started from Gmail as *From Gmail*.

## 0.5.2

- **Gmail rate limits pause a run instead of failing it.** When Gmail says "slow down", the run keeps everything it has done, shows *Paused* (not *Failed*) with the time it carries on, and picks up where it stopped on its own once Gmail's break is over. This follows the time Gmail asks for (`Retry after …`), or waits 15 minutes when Gmail doesn't say. It resumes automatically up to 6 times; clicking Run now never uses those up. A paused preview isn't resumed (it keeps no progress, so it would only repeat the same Jev calls); run it again after the break. While the break lasts, scheduled runs wait too, and the paused run (with its own date range) goes first. After a pause, Run now is set to the same dates (and preview) so it continues the same job, and an open dashboard shows the automatic resume without a reload.
- **Gentler on Gmail.** Gmail limits each mailbox, whichever app is asking, so:
  - A run and the dashboard reading the same mailbox now share one budget: at most 4 requests at a time.
  - The pace starts at 10 requests a second and halves whenever Gmail pushes back. It creeps back up to 20 while requests succeed.
  - A long "retry after" stops at once. The reads still queued are dropped rather than sent into a wall.
  - Checking and labeling an email now takes 2–3 Gmail requests instead of 3–4, because Gmail's answer to a label change is used as the read-back.
  - The dashboard reuses sender and subject details for 10 minutes (in memory only), so reopening it doesn't spend quota a run needs.
- **The first run's context scan resumes.** Learning who you write to and what you've bought (up to 500 messages) is saved in chunks, so a throttled first run doesn't start the scan over.
- **Clearer problems.**
  - Failed and paused runs have a *Technical details* toggle with Google's exact message.
  - A failed batch still counts the emails it checked and labeled.
  - Server logs record Google's status and reason code (never mailbox content) and how often Gmail pushed back.
  - If Gmail can't be reached for the recent-mail list, it says "Details unavailable right now" instead of claiming the email was deleted.

## 0.5.1

- **First runs no longer fail with "Gmail API HTTP 403".** Gmail answers "slow down" with a 403, and a first run reads hundreds of messages in parallel to learn who you correspond with. Requests are now paced under Gmail's per-user limit (25 a second across all threads), and rate-limit 403s are retried with backoff (honouring `Retry-After`) instead of stopping the run.
- **Gmail errors say what went wrong.** Google's reason code and message are kept (for example `Gmail API HTTP 403 (ACCESS_TOKEN_SCOPE_INSUFFICIENT): …`), and the dashboard explains the common ones in plain words: rate limits, a missing Gmail permission, a disabled Gmail API, expired access.
- **Junk rules.** Next to *Important* and *Can wait* you can now mark a sender, domain or topic as *Junk*, in setup, in "Teach it more" or on the What matters tab. Junk mail gets a `Triage/Junk` label and is never sent to Jev, so it costs nothing. Like everything else it is only labeled, never deleted, archived or moved to Spam; a Gmail filter on the label can hide it. Security alerts are never marked Junk. The label is created only once you have a Junk rule. The notes assistant turns "that's junk" into Junk rules.
- **Schedules run in your time zone.** The hour you pick is saved with your browser's time zone and runs at that local time, not the server's clock. Before this, "Every day at 07:00" on a UTC server could show "next run tomorrow at 00:00" in California. Existing schedules adopt your time zone the next time you open the dashboard, and Settings shows which zone is used. A daily, weekly or monthly hour that repeats when clocks go back runs once. Saving other settings keeps the schedule's zone; it only changes when you change the schedule. On Windows, `tzdata` is installed for the time zone database.

## 0.5.0

- **Redesigned web app.** A calmer, friendlier look built around the labels themselves, with a matching dark mode.
  - **Welcome page** explains the four labels with an example inbox, the three setup steps, and the privacy promises before asking for Google access. "Continue with Google" is one click; typing an address is optional.
  - **Setup** has a clear progress bar. Emails get Important / Can wait toggles, rules can be removed before saving, the Jev key is checked inline, and a summary sentence says exactly what will happen ("We'll preview the last 7 days now, then sort new mail every day at 07:00").
  - **Dashboard** is split into Overview, What matters and Settings tabs. Overview leads with a status card (on, preview, paused or manual) and Run now, a colour-coded breakdown of the last run, recently labeled mail you can filter by label, and a compact run history.
  - **Accessibility:** real radio groups and switches with keyboard support, a skip link, focus moved to each new screen's heading, inline form errors, and AA contrast in light and dark.
  - Works on phones: tabs move under the header, and rows and controls stack.

## 0.4.2

- **For You is now only for people and your priorities.** Mass mail tailored "based on your activity" (listing alerts, picked-for-you offers) from senders you don't correspond with now goes to Later instead.
  - Security notices and real order or account events are never pushed to Later.
  - Mail from people you know still gets For You.

## 0.4.1

- **Free-beta controls for hosted servers.**
  - `INBOX_TRIAGE_MAX_ACCOUNTS` caps new sign-ups. Returning users always get in, and a turned-away user's Google grant is revoked.
  - `INBOX_TRIAGE_BETA_ENDS` sets the date shown in the banner "Free for the first N users · until …". No counts are shown.

## 0.4.0

- **Lighter: no runtime dependencies.** The app now talks to the Gmail REST API and Google OAuth (PKCE) using Python's standard library, replacing Google's client libraries (~110 MB installed, about 40 MB of RAM once loaded).
  - Memory: about 23 MB peak; startup: about 60 ms.
  - Existing token files keep working.
  - Workspace service accounts need `inbox-triage[workspace]`.
- **Disconnect now deletes everything stored for the account.** It revokes Google access and removes the Jev key, rules, settings, history, context and journal, then signs that account out of the session.
- **Built-in home page and privacy policy** (`/privacy`, including Google's Limited Use disclosure) for the OAuth consent screen. The contact comes from `INBOX_TRIAGE_SUPPORT_EMAIL`.

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
