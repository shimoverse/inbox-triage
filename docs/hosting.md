# Hosting (Path 1) and packaging: users only click "Continue with Google"

Gmail access always needs a Google OAuth client, and someone has to create it once. This guide is for that someone: a maintainer shipping a build, or an operator running a server. End users never see any of it.

## 1. Create the Google OAuth client (once)

The full checklist is in [google-cloud-setup.md](google-cloud-setup.md). In short:

1. In the [Google Cloud console](https://console.cloud.google.com/projectcreate), create a project and enable the **Gmail API**.
2. **Google Auth Platform → Branding:** set the app name, a support email, and (for hosting) your homepage and privacy policy URLs.
3. **Audience:** choose *External*, or *Internal* if everyone is in your Google Workspace; internal apps need no verification. Click **Publish app**. While the app is left in *Testing*, Google [expires refresh tokens after 7 days](https://developers.google.com/identity/protocols/oauth2#expiration) and only listed test users can sign in.
4. **Data access:** add the scope `https://www.googleapis.com/auth/gmail.modify`.
5. **Clients → Create client:**
   - **Desktop app** for a local or packaged build. Its redirect is the loopback address `http://127.0.0.1:8765/oauth/callback`, which Google allows for desktop clients.
   - **Web application** for a hosted server. Add `https://your-domain/oauth/callback` as an authorized redirect URI.

## 2. Give the client to the app

The app looks for a client in this order:

| Source | Use it for |
|---|---|
| `INBOX_TRIAGE_OAUTH_CLIENT_ID` + `INBOX_TRIAGE_OAUTH_CLIENT_SECRET` env vars | Servers and containers |
| `~/.config/inbox-triage/client_secret.json` (or paste the JSON on the app's first screen) | Self-hosting |
| `src/inbox_triage/web/oauth_client.json` inside the package | Packaged builds: drop the downloaded JSON there before `uv build`. It's git-ignored so it isn't committed by accident, but it's included in the wheel. |

Shipping a *Desktop* client secret is expected: Google's own docs say an installed app's client secret ["is obviously not treated as a secret"](https://developers.google.com/identity/protocols/oauth2#installed). PKCE protects the sign-in instead, and the app always uses it. Never ship a *Web application* client secret.

## 3. Google's verification (what "anyone can sign in" costs)

`gmail.modify` is a **restricted** scope. Until Google verifies your app:

- users see a *"Google hasn't verified this app"* screen (Advanced → continue), and
- the app is limited to **100 users in total**.

That's fine for yourself, family, a team, or a beta. To remove the cap for the public you need [restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification): a verified domain, a privacy policy, a demo video, and an annual third-party **CASA security assessment**. Budget roughly $500–$4,500 a year for the assessment, depending on the lab and tier. Apps used only inside one Google Workspace organisation (*Internal*) are exempt.

## 4. Run a hosted server

You need one small Linux VM, a DNS name pointing at it, and ports 80 and 443 open. **Docker isn't needed.**

The app has no third-party dependencies and uses about 25 MB of RAM; Caddy adds about 30 MB. A Google Cloud **e2-micro** (1 GB RAM) is enough. One e2-micro in `us-central1`, `us-west1` or `us-east1` falls under Google Cloud's free tier; expect a few dollars a month at most for the external IP address and snapshots.

```bash
git clone https://github.com/shimoverse/inbox-triage.git && cd inbox-triage
sudo DOMAIN=triage.example.com bash deploy/install.sh
sudoedit /etc/inbox-triage/env        # paste the Google OAuth client ID/secret (+ optional OPENROUTER_API_KEY)
sudo systemctl restart inbox-triage
curl -s https://triage.example.com/healthz
```

The script installs:
- the app from `uv.lock` into `/opt/inbox-triage`;
- a hardened **systemd** service (`deploy/inbox-triage.service`), which runs as its own user, can write only to `/var/lib/inbox-triage`, and restarts on failure;
- **Caddy**, which gets and renews the HTTPS certificate automatically.

Update later with `sudo bash /opt/inbox-triage/deploy/update.sh`. Logs are in `journalctl -u inbox-triage`; they contain run summaries with opaque account tags, never addresses or mail content.

In hosted mode:

- the server refuses plain-HTTP public URLs, and session cookies are marked `Secure`;
- each person signs in with Google and can only see their own account;
- **by default, every user brings their own Jev key** during onboarding. It's verified with Jev, stored per account (0600), and used only for that account's mail. A hosted server **ignores** its own Jev key unless you run a [sponsored beta](#5-sponsored-beta-the-operator-pays-for-jev), so the operator never pays for other users by accident;
- the OAuth client, the optional notes assistant (`OPENROUTER_API_KEY`, DeepSeek V4.1 Flash by default), and the contact shown on the built-in privacy policy (`INBOX_TRIAGE_SUPPORT_EMAIL`, `INBOX_TRIAGE_OPERATOR`) come from `/etc/inbox-triage/env`;
- **optional free-beta limits:**
  - `INBOX_TRIAGE_MAX_ACCOUNTS=90`: once 90 accounts exist, *Continue with Google* is turned away before Google's consent screen.
  - Returning users choose *Use a specific account* and enter their address.
  - A typed address always goes on to Google, so the server never reveals who has an account. Anyone who isn't a member is turned away after Google, and their fresh grant is revoked straight away.
  - Keep the limit below 100. Google's cap for an unverified app counts every person who ever approved it, including your own test accounts and anyone turned away by versions before 0.6.
  - `INBOX_TRIAGE_BETA_ENDS=YYYY-MM-DD` (optional, unset by default) adds an end date to the banner. It's informational: the app keeps running, and you decide what happens next.
  - Counts are never shown to users;
- the app serves its own home page (`/`) and privacy policy (`/privacy`), so the consent screen can use `https://<domain>/` and `https://<domain>/privacy`;
- **Disconnect account** removes that account's token and active per-account data (including keys, settings, rules, history, context and extension access) and attempts Google grant revocation. A failed local deletion returns an error for retry; Google revocation is best effort;
- the built-in scheduler runs every account's schedule, so run exactly one instance;
- `/var/lib/inbox-triage` holds users' Google tokens, Jev keys, and rules. Use an encrypted disk and encrypted snapshots, and restrict SSH.

You are now processing other people's mail: publish a privacy policy, keep a documented encrypted-backup retention/deletion policy, and follow Google's [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy). Revocation at Google alone does not notify the server or remove local data: users must also Disconnect or contact the operator for deletion. A Disconnect removes active server data, not copies already held in backups or labels already applied in Gmail.

**Prefer containers?** A `Dockerfile` is included as an alternative: mount `/data`, pass the same environment variables, add `--public-url https://…`, and put any TLS proxy in front of port 8765.

## 5. Sponsored beta: the operator pays for Jev

To take the key step out of onboarding, the server can pay for users' Jev calls, up to a daily limit per account. Jev is sold on OpenRouter, so one OpenRouter key is enough.

1. **Create an OpenRouter key for this server only.** In its settings, set a **credit limit** with a monthly reset (for example $50). It is the circuit breaker if anything goes wrong.
2. **Turn on zero data retention** for the OpenRouter account (Settings → Privacy), so no provider keeps users' email excerpts.
3. **Add both settings** to `/etc/inbox-triage/env`, then restart:
   ```bash
   OPENROUTER_API_KEY=sk-or-v1-…
   INBOX_TRIAGE_SPONSORED_JEV_DAILY=500   # Jev calls per account per day (UTC)
   ```
4. **Check the log.** `journalctl -u inbox-triage` should show `sponsored Jev key check ok`. On `failed status=401` the key is wrong; on `failed status=402` it has no credits.

**What changes for users:**

- The *Connect Jev* step disappears, and the banner says *No API key needed*.
- A user can still bring their own key in Settings. Their calls then go on their key, with no limit.
- The first run labels the last 7 days straight away.

**When a limit is hit, runs pause instead of failing**, the same way they pause for Gmail:

- **Daily allowance used up:** the account carries on after midnight UTC.
- **Key out of credits** (OpenRouter's `402`, which is also what a key at its credit limit returns): each account tries again an hour later, up to six times, and after that at its next schedule or when someone clicks Run now.
- The dashboard says which of these happened.

**What it costs:** Inbox Triage sends about 1,500 tokens per email, and Jev costs $0.042 per million input tokens with output free, so each email costs about $0.00006. For 100 users, each with a 1,000-email first backfill plus 60 new emails a day, that is about **$18 the first month and $11 a month after**, plus OpenRouter's 5.5% fee on credit purchases. Each run's log line includes `jev_cost=` in USD, so you can track spending per run.

## 6. The Chrome and Brave extension

The extension ([extension/README.md](../extension/README.md)) draws a live dashboard at the top of Gmail and asks the server to sort new mail as it arrives.

- **Sign-in:** the extension signs in with `chrome.identity.launchWebAuthFlow` against this server's `/connect` page. That works in Chrome and Brave.
  - The person approves the extension there, and the server hands it a one-time code bound to a PKCE challenge.
  - The extension swaps the code for a token. The token lasts 90 days, can only read that account's dashboard and ask for a sync, and is revoked by *Disconnect* in the extension or on the website.
- **List the extension's ID:** a hosted server hands tokens only to extensions listed in `INBOX_TRIAGE_EXTENSION_IDS` (comma-separated IDs). Without it, extension sign-in is turned off. The existing unpacked beta ID is `leagpjpjajkpjaiegjjenjnlegffkofj`; when the Chrome Web Store assigns a different ID, allowlist **both** IDs before distribution, then remove the unpacked ID only after its testers have migrated. Store installs have separate extension storage, so testers must reconnect. See [the store beta worksheet](../extension/STORE_BETA.md#stable-id-and-beta-migration--no-private-key-in-the-repository).
- **Default schedule:** accounts connected from the extension get an hourly schedule if they don't have one yet, so mail keeps getting labeled between visits.
- **Syncs cost little:** each account's extension can start at most one sync a minute. A sync continues from the last checkpoint and skips mail it has already labeled, so it costs Jev calls only for new mail.

## 7. Alternatives if you want zero Google setup

- **Google Workspace admins** can skip per-user sign-in with a service account and domain-wide delegation (`inbox-triage --service-account key.json --account …`). See the README.
- **A Google Apps Script port** would run inside each user's own Google account with only a consent click, and Google hosts it. It's on the roadmap; it trades this app's local privacy model for zero setup.
