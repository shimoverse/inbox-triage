# Inbox Triage extension — product, design and engineering review

Source baseline: `origin/main` at `e5a344c` (October 15, 2026 review), plus the proposed changes on `audit/extension-contract-20261015`. This is a code and public-site review, not a live authenticated Gmail, Chrome/Brave, assistive-technology, or Chrome Web Store acceptance test. Use synthetic mail for all QA.

## Recommendation

**Ship a truthful, narrow release after browser/store gates; do not promise an urgent-mail detector.** The core value is a quick, in-Gmail view of what the connected Inbox Triage server *recently labeled*, plus a manual check for new mail. The server, not the extension, reads messages via the Gmail API and adds labels after Google authorization. The extension needs the hosted service to be reachable, the right account to be connected, model access or sponsorship, and the Google OAuth consent to be usable. A Chrome Web Store approval alone does not clear those gates.

## The user problem and mechanism

A busy Gmail user cannot readily separate action-needed mail from updates, personalized content, and lower-priority mail without manually scanning. The hosted app or self-hosted server extracts a bounded message excerpt and context, asks Jev focused classification questions, applies conservative local rules and at most one attention label plus a Shopping topic label, verifies the Gmail label, or leaves uncertain mail alone. The Chrome/Brave extension is an *optional surface* inside Gmail: it reads the Gmail tab title to select the account and detect unread-count increases, renders a seven-day label-activity summary and recent sender/subject details from the server, and can nudge server sync. It stores a revocable server bearer token but not Google OAuth or model keys. It does not scrape message bodies from the page or perform Gmail label writes itself. See `README.md`, `extension/{background,content,lib}.js`, `src/inbox_triage/web/{app,extension}.py`.

## What is good

- The intended operation is additive labels, not send/delete/archive/mark-read/Spam; unsure mail is left unchanged. First-run and ongoing previews exist.
- It uses a narrow role for the extension, account-scoped token and same-account sign-in check; the account can disconnect the extension separately from server-side Gmail authorization.
- Seven-day chips deep-link to Gmail labels; recent items and the activity chart offer feedback without a second dashboard visit. The server has run history, pause/retry states and schedules.
- Plain Manifest V3 JavaScript, no remote code or analytics SDK; synthetic unit/API tests cover the decision and token paths. Privacy-preserving design depends on both server and extension operating as described.

## Issues and prioritized outcomes

**P0 — Trust and correctness (addressed in release candidate):** Consent and options said the extension held “no keys” and could not change mail, even though it stores a revocable server token and triggers server-side label writes. Distinguish extension disconnect from Gmail/server account deletion and clarify token revocation when offline. The last-run status could say “Sorted” for a dry run; a green/live state could appear before any run; the seven-day chips were not visibly defined as historical activity. Token issuance could race revocation, resurrecting a revoked token. Failed/disconnected sync requests consumed the local throttle and changing servers could suppress a good request. Candidate copy, status, sync and registry-lock fixes address these; inspect actual patch and test results before release.

**P1 — First successful outcome:** The icon opens an options page led by server URL, while connection starts inside Gmail; title-based account detection can yield a blank bar and floating fallback can cover mail. The candidate adds a short setup handoff. Follow-up: recover visibly from unrecognized Gmail titles/layouts and mismatched accounts without silently selecting another account. Test Chrome and Brave at 100% and 200% zoom with a synthetic mailbox.

**P1 — Attention and correction:** The current surface leads with activity counts/charts rather than the actionable Needs You message, and a wrong label has no nearby correction flow. Put a direct Needs You Gmail action and reviewed “Wrong label?” rule proposal ahead of analytics. Do not infer a present backlog from seven-day label events or auto-relabel a whole domain based on a single correction.

**P1 — Accessibility and failure recovery:** Refreshes can replace focused controls; the candidate removes the clock-only rebuild but changed server state still rebuilds. Verify keyboard focus, screen-reader chart/list semantics, target sizes, 200% zoom, dark mode, offline/401/429/500, and floating placement. Persist blocking errors and provide a retry path.

**P2 — Measured value:** Track (privacy-safe, aggregate and opt-in if required) first verified label or reviewed preview, opens from Needs You, user corrections, wrong-label rate, and no-change rate. Jev latency and seven-day processed count are operational metrics, not proof of time saved, reduced misses or trust. Validate precision/recall on a consented synthetic or privately labeled evaluation set before any claims about urgent-mail detection. Non-English behavior remains unproven.

## Release gates

1. Run Python and extension tests, static syntax, package validation and privacy/copy review against deployed service. Keep the public package free of tests, docs, credentials, private mail and development keys. Increment package version.
2. Exercise real Chrome and Brave connection, wrong-account, sync, preview, disconnect, status/error, keyboard/zoom and Gmail layout on a synthetic mailbox. Ensure no unintended Gmail action.
3. Verify publisher account, live Web Store item ID/version/visibility and listing/privacy declarations. Upload to the existing item; check distribution, submit, then read back review state. A submitted item is not downloadable until approval.
4. Verify hosted OAuth/account capacity, allowlisted store ID and public install URL independently. The anonymous site previously returned no store URL; do not display an install CTA until installability is real.

Design detail and scenario acceptance criteria are in the private local review at `/Users/shimo/.hermes/cache/scratch/inbox-triage-design-review-spec.md` (local-only). No private mailbox data belongs in this document or the public repository.
