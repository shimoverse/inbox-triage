# Inbox Triage for Chrome and Brave

A live Inbox Triage dashboard at the top of Gmail. It shows:

- how many emails got each label in the last 7 days, as chips that open the label in Gmail;
- a chart of mail labeled per day, with Needs You highlighted;
- what was labeled most recently.

When new mail arrives, it asks your Inbox Triage server to sort it straight away.

The extension only draws the dashboard; the server does all the reading and labeling. So:

- **No keys in the extension.** It holds no Google token and no AI key, only a revocable token for its own server.
- **It adds a dashboard to Gmail's page but does not read Gmail message bodies there or change your mail itself.** Labels are added by the server with the Google permission you approve there, so they also show up on your phone.
- **Nothing to build.** It is plain JavaScript (Manifest V3) with no dependencies.

## Try it

1. Run Inbox Triage (`uv run inbox-triage-web`, or use a hosted server) and sign in with Google once.
2. Load the extension:
   - **Chrome:** open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose this `extension` folder.
   - **Brave:** open `brave://extensions` and do the same.
3. Check the server address. The extension points at the hosted service, `https://triage.shimoverse.com`, by default. If you run your own server, click the extension's icon to open its settings and enter its address: `http://127.0.0.1:8765` on this computer, or your server's `https://` address.
4. Open Gmail and click **Connect Gmail** in the Inbox Triage bar.
   - A window opens on your server's connect page.
   - Sign in with Google if you aren't already, then click **Connect**.
   - Google may show *"Google hasn't verified this app"*: choose **Advanced**, then continue.

After that, the bar fills in, and new mail is labeled within about a minute while Gmail is open. The rest of the time the server's schedule labels it (hourly for accounts connected from the extension).

## Permissions

| Permission | Why |
|---|---|
| `mail.google.com` (content script) | Draws the bar and reads the tab title to know which account is open and when unread mail arrives. It doesn't read emails. Chrome's install warning says *"Read and change your data on mail.google.com"*. |
| `identity` | `chrome.identity.launchWebAuthFlow` signs in through your server. It works in Chrome and Brave, unlike `getAuthToken`, which Brave doesn't support. No install warning. |
| `storage` | Keeps the server address, the tokens (out of reach of content scripts where the browser allows) and whether the bar is expanded. No install warning. |

The server is reached from the service worker through CORS with a bearer token, so its domain needs no host permission and doesn't appear in the install warning.

## For operators

- **List your extension's ID on the hosted server.** A hosted server hands tokens only to listed extensions: set `INBOX_TRIAGE_EXTENSION_IDS=<id>[,<id>…]`. Find the ID on `chrome://extensions`. A server running on `127.0.0.1` accepts any extension, but still asks the person to click **Connect**.
- **Keep the store ID stable without exposing a signing key.** Create a draft Chrome Web Store item, copy its **public** key from Package → View public key into the development manifest's `"key"` field, and check that the unpacked ID matches the store item. Do not put a private key in the package or repo. The current unpacked build ID `leagpjpjajkpjaiegjjenjnlegffkofj` must remain on the server allowlist until those testers move to the store build; a new manifest key would change their unpacked ID. See [the store listing worksheet](STORE_BETA.md#stable-id-and-installation-migration--no-private-key-in-the-repository).
- **Set the default server.** Put your server's address in `config.js` before packaging, so people don't have to type it.
- **Publish as Public in the Chrome Web Store.** Brave installs from there too. Review can take from a few days to a few weeks; a submitted item is not downloadable until approved.
  - In the store's privacy form, declare *authentication information*, *personal communications* (recent senders and subjects), and *personally identifiable information* (Gmail account address). See [the listing and privacy worksheet](STORE_BETA.md); capture a real, privacy-safe Gmail screenshot before submitting.

## Tests

```bash
node --test extension/test/*.test.js
```

The server side (connect page, codes with PKCE, tokens, summary, sync) is tested in `tests/test_extension_api.py`.
