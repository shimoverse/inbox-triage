# Hosting (Path 1) and packaging: users only click "Sign in with Google"

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

```bash
docker build -t inbox-triage .
docker run -d --name inbox-triage -p 127.0.0.1:8765:8765 \
  -e INBOX_TRIAGE_OAUTH_CLIENT_ID=... -e INBOX_TRIAGE_OAUTH_CLIENT_SECRET=... \
  -e OPENROUTER_API_KEY=... \
  -v inbox-triage-data:/data \
  inbox-triage --public-url https://triage.example.com
```

Put a TLS reverse proxy (Caddy, nginx, a cloud load balancer) in front of port 8765. In hosted mode:

- the server refuses plain-HTTP public URLs, and session cookies are marked `Secure`;
- each person signs in with Google and can only see their own account;
- **every user brings their own Jev key** during onboarding. It's verified with Jev, stored per account (0600), and used only for that account's mail, so the server never pays for users' Jev usage;
- the OAuth client and the optional notes assistant (`OPENROUTER_API_KEY`, DeepSeek V4.1 Flash by default) come from the operator's environment;
- the built-in scheduler runs every account's schedule, so keep one instance running;
- OAuth tokens, per-account state, and rules live under `/data`. Back it up **encrypted**: it grants access to users' mailboxes.

You are now processing other people's mail: publish a privacy policy, delete data when someone disconnects (the app revokes the Google grant and deletes the token), and follow Google's [API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).

## 5. Alternatives if you want zero Google setup

- **Google Workspace admins** can skip per-user sign-in with a service account and domain-wide delegation (`inbox-triage --service-account key.json --account …`). See the README.
- **A Google Apps Script port** would run inside each user's own Google account with only a consent click, and Google hosts it. It's on the roadmap; it trades this app's local privacy model for zero setup.
