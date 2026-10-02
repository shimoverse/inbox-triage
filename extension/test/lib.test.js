// Run with: node --test extension/test/*.test.js
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../lib.js");

test("reads the account and unread count from Gmail's tab title", () => {
  assert.equal(L.accountFromTitle("Inbox (12) - Dana.Lee@Example.org - Gmail"), "dana.lee@example.org");
  assert.equal(L.unreadFromTitle("Inbox (12) - dana@example.org - Gmail"), 12);
  assert.equal(L.unreadFromTitle("Inbox (1,204) - dana@example.org - Gmail"), 1204);
  assert.equal(L.unreadFromTitle("Posteingang (3) - dana@example.org - Gmail"), 3);
  assert.equal(L.unreadFromTitle("Inbox - dana@example.org - Gmail"), null);  // no count shown
  assert.equal(L.accountFromTitle("Gmail"), "");
  assert.equal(L.accountFromTitle("Inbox - dana@example.org - Acme Corp Mail"), "dana@example.org");  // Workspace
});

test("an address in an open thread's subject is not the account, and its numbers are not unread counts", () => {
  const title = "Fwd: invoice (2024) from billing@vendor.example - dana@example.org - Gmail";
  assert.equal(L.accountFromTitle(title), "dana@example.org");
  assert.equal(L.unreadFromTitle(title), null);
  assert.equal(L.unreadFromTitle("Re: Meeting (2) - dana@example.org - Gmail"), null);
});

test("links a label the way Gmail does", () => {
  assert.equal(L.labelHash("Triage/Needs You"), "#label/Triage%2FNeeds+You");
  assert.equal(L.labelHash("Topics/Shopping"), "#label/Topics%2FShopping");
});

test("tokens only go to https servers or this computer", () => {
  assert.equal(L.serverOrigin("https://triage.example.com/"), "https://triage.example.com");
  assert.equal(L.serverOrigin(" http://127.0.0.1:8765/app "), "http://127.0.0.1:8765");
  assert.equal(L.serverOrigin("http://localhost:8765"), "http://localhost:8765");
  assert.equal(L.serverOrigin("http://triage.example.com"), "");
  assert.equal(L.serverOrigin("https://user:pass@triage.example.com"), "");
  assert.equal(L.serverOrigin("javascript:alert(1)"), "");
  assert.equal(L.serverOrigin(""), "");
});

test("base64url has no padding or unsafe characters", () => {
  assert.equal(L.base64url(new Uint8Array([251, 255, 191])), "-_-_");
  assert.equal(L.base64url(new Uint8Array([1, 2])), "AQI");
});

test("says what is happening in plain words", () => {
  const now = 1_000_000;
  assert.equal(L.statusLine({ jev_connected: true, running: true }, now).text, "Sorting new mail now…");
  const paused = { jev_connected: true, resume_at: now + 7200,
    last_run: { status: "paused", reason: "sponsored_limit", started: now - 60 } };
  assert.match(L.statusLine(paused, now).text, /^Today's free sorting allowance is used up\. Carries on in 2 h$/);
  assert.equal(L.statusLine({ jev_connected: true, last_run: { status: "ok", started: now - 300, finished: now - 120 } }, now).text,
    "Sorted 2 min ago");
  assert.equal(L.statusLine({ jev_connected: false }, now).kind, "warn");
  // Paused with nothing to resume automatically, a Jev outage, and a run with no timestamps all read sensibly.
  assert.match(L.statusLine({ jev_connected: true, last_run: { status: "paused", reason: "jev_credits" } }, now).text,
    /^Jev is out of credits\. Open Inbox Triage/);
  assert.match(L.statusLine({ jev_connected: true, resume_at: now + 900, last_run: { status: "paused", reason: "jev_unavailable" } }, now).text,
    /^Jev isn't answering right now\. Carries on in 15 min$/);
  assert.equal(L.statusLine({ jev_connected: true, last_run: { status: "ok" } }, now).text, "Sorted recently");
  assert.equal(L.until(now + 3 * 86400, now), "in 3 days");
  assert.equal(L.until(now + 7200, now), "in 2 h");
  assert.equal(L.ago(now - 86400, now), "1 day ago");
});

test("the chart compares Needs You with every other label, never double-counting Shopping", () => {
  const cols = L.dayColumns([{ day: "2026-09-21", needs_you: 2, updates: 3, for_you: 1, later: 5, junk: 1, shopping: 4 }]);
  assert.deepEqual(cols.map(({ needs, other, total }) => ({ needs, other, total })), [{ needs: 2, other: 10, total: 12 }]);
  assert.deepEqual([0, 1, 3, 7, 12, 48, 180].map(L.niceMax), [1, 1, 5, 10, 20, 50, 200]);
});

test("names a sender without the address", () => {
  assert.equal(L.senderName('"Dana Lee" <dana@example.org>'), "Dana Lee");
  assert.equal(L.senderName("dana@example.org"), "dana@example.org");
  assert.equal(L.senderName("<dana@example.org>"), "dana@example.org");
});

test("chip colours match the Gmail label colours the server sets", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const runner = fs.readFileSync(path.join(__dirname, "../../src/inbox_triage/runner.py"), "utf8");
  for (const label of L.LABELS) {
    assert.ok(runner.includes(`"${label.gmail}": ("${label.bg}", "${label.fg}")`), label.gmail);
  }
});
