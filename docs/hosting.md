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
- **every user brings their own Jev key** during onboarding. It's verified with Jev, stored per account (0600), and used only for that account's mail. A hosted server **ignores** any server-wide `TYPESAFE_API_KEY`, so the operator can never end up paying for other users' Jev usage;
- the OAuth client, the optional notes assistant (`OPENROUTER_API_KEY`, DeepSeek V4.1 Flash by default), and the contact shown on the built-in privacy policy (`INBOX_TRIAGE_SUPPORT_EMAIL`, `INBOX_TRIAGE_OPERATOR`) come from `/etc/inbox-triage/env`;
- **optional free-beta limits:** `INBOX_TRIAGE_MAX_ACCOUNTS=100` turns away new sign-ups once 100 accounts exist (returning users always get in, and a turned-away user's Google grant is revoked immediately). `INBOX_TRIAGE_BETA_ENDS=YYYY-MM-DD` (optional, unset by default) adds an end date to the banner. It's informational: the app keeps running, and you decide what happens next. Counts are never shown to users;
- the app serves its own home page (`/`) and privacy policy (`/privacy`), so the consent screen can use `https://<domain>/` and `https://<domain>/privacy`;
- **Disconnect account** revokes Google access and deletes all of that user's stored data;
- the built-in scheduler runs every account's schedule, so run exactly one instance;
- `/var/lib/inbox-triage` holds users' Google tokens, Jev keys, and rules. Use an encrypted disk and encrypted snapshots, and restrict SSH.

You are now processing other people's mail: publish a privacy policy, delete data when someone disconnects (the app revokes the Google grant and deletes the token), and follow Google's [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).

**Prefer containers?** A `Dockerfile` is included as an alternative: mount `/data`, pass the same environment variables, add `--public-url https://…`, and put any TLS proxy in front of port 8765.

## 5. Alternatives if you want zero Google setup

- **Google Workspace admins** can skip per-user sign-in with a service account and domain-wide delegation (`inbox-triage --service-account key.json --account …`). See the README.
- **A Google Apps Script port** would run inside each user's own Google account with only a consent click, and Google hosts it. It's on the roadmap; it trades this app's local privacy model for zero setup.
