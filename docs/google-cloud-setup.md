# Google Cloud setup (once, by whoever runs the app)

End users never do this. Path 1 (the hosted app) needs it done once by the operator; Path 2 (self-hosting) needs it done once by the person running their copy.

## What you end up with

- A Google Cloud project with the **Gmail API** enabled.
- A **published** OAuth consent screen (Google Auth Platform) requesting one scope: `https://www.googleapis.com/auth/gmail.modify`.
- One OAuth client:
  - **Web application** for a hosted server, with the redirect URI `https://<your-domain>/oauth/callback`;
  - **Desktop app** for local or packaged use, where the loopback redirect `http://127.0.0.1:8765/oauth/callback` is allowed automatically.
- The client ID and secret given to the app:
  - hosted: `INBOX_TRIAGE_OAUTH_CLIENT_ID` and `INBOX_TRIAGE_OAUTH_CLIENT_SECRET`;
  - local: paste the downloaded JSON on the app's first screen.

## Steps

1. **Create the project.** Go to [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate); a name like `inbox-triage` is fine.
2. **Enable the Gmail API.** APIs & Services → Library → *Gmail API* → Enable.
3. **Branding.** Under Google Auth Platform → Branding, set:
   - the app name (*Inbox Triage*), support email and logo;
   - the home page URL `https://<your-domain>/` and privacy policy URL `https://<your-domain>/privacy`, both served by the app (required for Path 1);
   - an authorized domain (your app's domain, verified in Search Console).
4. **Audience.** Choose *External*, or *Internal* if every user is in your own Google Workspace. Then click **Publish app**. Apps left in *Testing* only allow listed test users and expire refresh tokens after 7 days.
5. **Data access.** Add the scope `https://www.googleapis.com/auth/gmail.modify`. It's a *restricted* scope.
6. **Clients → Create client:**
   - Path 1: type *Web application*, authorized redirect URI `https://<your-domain>/oauth/callback`, authorized JavaScript origin `https://<your-domain>`;
   - Path 2: type *Desktop app*.

   Download the JSON and store it in a secret manager, never in git.
7. **Test.** Open the app, sign in with an account you own, and confirm the dashboard loads. The "Google hasn't verified this app" warning is expected until step 8.
8. **Verification (Path 1 only, before a public launch).**
   - Until the app is verified, it's limited to **100 users in total** and shows the unverified-app warning.
   - Submit it for [restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification). You'll need:
     - a justification: "labels incoming mail; never sends, deletes, or archives";
     - a demo video of the consent screen and the label-only behaviour;
     - the privacy policy;
     - a **CASA** security assessment by an authorised lab, repeated every 12 months (roughly $500–$4,500 a year).
   - Internal Workspace apps are exempt.

## Hand-off values

| Value | Where it goes |
|---|---|
| OAuth client ID / secret | `INBOX_TRIAGE_OAUTH_CLIENT_ID` / `INBOX_TRIAGE_OAUTH_CLIENT_SECRET` (server secret store) |
| Public URL | `inbox-triage-web --public-url https://<your-domain>` |
| Project ID, support email, privacy policy URL | Operator runbook |
