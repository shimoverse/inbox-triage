# Chrome Web Store release — operator worksheet

The extension is submitted for review as a **Public** listing with automatic publication after approval. This worksheet records the intended copy and privacy declarations; verify the live dashboard before each update. The hosted service still has a limited-account rollout and Google's Gmail OAuth verification is separate from Web Store review.

## Listing

- **Name:** Inbox Triage (the package manifest uses the same name).
- **Category:** Productivity
- **Language:** English
- **Short description (manifest):** A live Inbox Triage dashboard at the top of Gmail: what needs you, what can wait, sorted as mail arrives.
- **Detailed description:**

  Inbox Triage adds a dashboard above Gmail with seven-day counts for its labels, a daily activity chart, and recently labeled messages. Click a label count to open that label in Gmail. While Gmail is open, the extension asks your connected Inbox Triage server to check for new mail when the unread count rises; the server also runs on a schedule.

  To start, open Gmail, click Connect Gmail in the dashboard, sign in on the Inbox Triage server, and approve Gmail access. The extension itself does not read message bodies from Gmail's page or hold a Google OAuth token or an AI API key. The server reads mail through the Gmail API and applies labels after the separate Google authorization. The dashboard receives label counts and recent senders and subjects from that server. No messages are sent, deleted, archived, marked read, or moved to Spam by Inbox Triage.

  Google's OAuth consent may display an unverified-app warning. You can also point the extension at your own Inbox Triage server in its settings. Chrome and Brave are supported.

  (Keep the account limit out of the store text: the listing is reviewed and cached, while the live banner on the website always shows the current limit.)

- **Support/homepage:** https://triage.shimoverse.com/ (confirm reachable and current before entering)
- **Privacy policy:** https://triage.shimoverse.com/privacy (confirm reachable, correct operator/support contact, deletion/backup details, and Chrome Web Store Limited Use statement after deployment before entering)
- **Distribution:** Public; free; choose supported regions deliberately. No paid features are sold by the extension.

## Privacy tab — proposed answers to verify against the actual dashboard wording

**Single purpose:** Show Inbox Triage label summaries in Gmail and trigger a connected Inbox Triage server to label incoming mail.

**Permission justifications:**

- `https://mail.google.com/*` content script: place the dashboard in Gmail, read Gmail's tab title for the account address and unread-count changes, and render label summaries returned by the server. It does not scrape Gmail messages. Chrome's broad *read and change your data on mail.google.com* warning reflects page access, not the extension's intended operations.
- `identity`: use `launchWebAuthFlow` for the user-initiated server sign-in, with a one-time authorization code and PKCE redirect.
- `storage`: store the chosen server URL, revocable 90-day server bearer tokens keyed by server/account, and expanded/collapsed dashboard preferences; service worker session storage also rate-limits sync requests. These tokens are **authentication information** and should not be described as Google tokens.

**User data categories:** Declare **authentication information** (extension server token), **personal communications** (recent Gmail senders and subjects returned by the server and displayed in the dashboard), and **personally identifiable information** (Gmail account email address from Gmail's tab title, used to select the right account/token). Assess any additional category the live form asks about; do not answer “no data collected” merely because the extension does not scrape message bodies. The connected server processes Gmail messages through Google OAuth and sends trimmed subject/excerpt/domain evidence to the classification provider as detailed in the linked privacy policy. The extension itself sends the account address and bearer token to the selected server, and receives summaries including senders and subjects; it does not store message content persistently. The server may cache dashboard details in memory and retain message IDs/label decisions on disk as stated in its privacy policy.

**Data handling:** Used only for the feature, not sold, not used for ads, and not used for creditworthiness. No extension analytics SDK or remotely hosted executable code. Transport to the hosted server is HTTPS; a user-chosen loopback server may use HTTP on `127.0.0.1`. Access to `storage.local` is restricted to trusted extension contexts where supported. Server token can be revoked by disconnecting; account deletion on the server is a separate action. Verify the exact Chrome Web Store certifications against the deployed service and policy before checking them.

## Assets and review prerequisites

- Package icon: `icons/icon128.png` (128×128 PNG); store promo: `store-assets/promo-small.png` (440×280 PNG). The promo is an illustration, **not** a screenshot of the app.
- **Still needed:** at least one genuine 1280×800 or 640×400 screenshot of the working extension in Gmail, using a synthetic/test mailbox with no real personal data. Do not substitute a mockup for an app screenshot. Check against current dashboard requirements when uploading. Optional marquee and video only if requested/available.
- Before review: verify Gmail bar placement, Chrome and Brave `launchWebAuthFlow`, hosted `/privacy`, server availability, account capacity and tester instructions. Do not give reviewer private credentials in this repo.

## Stable ID and installation migration — no private key in the repository

The existing unpacked installation has ID `leagpjpjajkpjaiegjjenjnlegffkofj`. The hosted server currently allowlists that ID. **Do not add an arbitrary manifest `key` now:** that would change its ID and strand the connected unpacked installation. The source `manifest.json` deliberately has no `key` until a Chrome Web Store item has been created; its folder-derived unpacked ID is not evidence of a reusable public key.

1. Zip only the contents of `extension/` (with `manifest.json` at archive root), excluding `test/`, `STORE_BETA.md`, `store-assets/`, local credentials, `.pem` files and private data. Upload as a **draft item only**, without submitting for review or publishing. This requires the developer account. Chrome Web Store allocates the item's permanent ID at this step. Do **not** upload a local private key or `key.pem`.
2. On the draft item's **Package → View public key**, copy the base64 text between the PEM headers and remove line breaks. This is the **public** key, not a private key. Set `"key": "<public-key-base64>"` in the development manifest, or in a separate local unpacked copy. Confirm the ID shown at `chrome://extensions` equals the draft item's ID before using that copy for testing. Keep the uploaded package and future store updates free of private keys. The public manifest key is for matching the store ID in unpacked development; the published item keeps its store-assigned ID automatically. If the dashboard rejects a manifest containing `key` on an update, strip only that public field from the upload copy, leaving the development copy intact.
3. Before inviting store testers, add the **new store ID alongside** `leagpjpjajkpjaiegjjenjnlegffkofj` in the hosted server's comma-separated `INBOX_TRIAGE_EXTENSION_IDS`, restart/redeploy, and verify both IDs can reach the connect flow. Never replace the old ID first. Keep it until all existing unpacked testers migrate. An allowlisted ID is only a token-issuance gate, not a secret or proof of extension code provenance.
4. The store extension is a **different installation** from the old unpacked one; local extension storage and server token do not transfer. A tester should connect Gmail again in the store build, then remove the unpacked build after confirming the new dashboard works. Server-side Gmail authorization and schedule remain with the account; do not delete them as part of the ID change. Retire the old allowlist entry after migration and revoke old extension tokens as appropriate.

References: [Chrome manifest `key` documentation](https://developer.chrome.com/docs/extensions/reference/manifest/key), [store images](https://developer.chrome.com/docs/webstore/images), [listing](https://developer.chrome.com/docs/webstore/cws-dashboard-listing), and [distribution](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution).
