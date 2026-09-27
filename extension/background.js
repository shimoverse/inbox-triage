// Inbox Triage extension service worker. It holds each account's extension token and talks to the
// Inbox Triage server; the Gmail bar only ever asks it for a summary or a sync. It never sees a
// Google token or a Jev key, and it never reads mail.
"use strict";
importScripts("config.js", "lib.js");
const L = self.InboxTriageLib;
const SYNC_GAP = 60 * 1000;  // the server allows one extension sync a minute per account anyway

// Tokens stay out of reach of content scripts where the browser supports it.
try { chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }); } catch { /* older browsers */ }

async function settings() {
  const { server, tokens, prefs } = await chrome.storage.local.get(["server", "tokens", "prefs"]);
  return { server: L.serverOrigin(server || self.INBOX_TRIAGE_DEFAULT_SERVER), tokens: tokens || {}, prefs: prefs || {} };
}
// Tokens are kept per server and account, so changing servers never sends one to the wrong place.
const tokenKey = (server, email) => `${server} ${email}`;

async function call(server, path, { method = "GET", token, body } = {}) {
  const headers = { "Content-Type": "application/json", "X-Requested-With": "inbox-triage" };
  if (token) headers.Authorization = "Bearer " + token;
  let res;
  try {
    res = await fetch(`${server}/api/${path}`, { method, headers, credentials: "omit", cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    const err = new Error(`Can't reach Inbox Triage at ${server}`);
    err.status = 0;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Inbox Triage answered ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// Sign in through the server's connect page (works in Chrome and Brave; getAuthToken doesn't work in Brave).
async function connect(email) {
  const { server } = await settings();
  if (!server) throw new Error("Set your Inbox Triage server in the extension's options first");
  const verifier = L.base64url(crypto.getRandomValues(new Uint8Array(48)));
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const state = L.base64url(crypto.getRandomValues(new Uint8Array(18)));
  const redirect = chrome.identity.getRedirectURL("cb");
  const url = `${server}/connect?` + new URLSearchParams({ redirect_uri: redirect, state,
    code_challenge: L.base64url(new Uint8Array(digest)), login_hint: email || "" });
  let back;
  try {
    back = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  } catch {
    throw new Error("Sign-in was closed before it finished");
  }
  const params = new URL(back).searchParams;
  if (params.get("state") !== state || !params.get("code")) throw new Error("Sign-in didn't complete; please try again");
  const token = await call(server, "ext/token", { method: "POST",
    body: { code: params.get("code"), code_verifier: verifier, redirect_uri: redirect } });
  const current = await settings();
  current.tokens[tokenKey(server, token.email)] = { token: token.token, email: token.email, expires_at: token.expires_at };
  await chrome.storage.local.set({ tokens: current.tokens });
  return { connected: true, email: token.email };
}

// Run fn with this account's token; a token the server no longer accepts is forgotten.
async function withToken(email, fn) {
  const { server, tokens } = await settings();
  const key = tokenKey(server, email);
  const entry = tokens[key];
  if (!entry) return { connected: false, server };
  try {
    return { connected: true, server, data: await fn(server, entry.token) };
  } catch (err) {
    if (err.status === 401) {
      delete tokens[key];
      await chrome.storage.local.set({ tokens });
      return { connected: false, server };
    }
    throw err;
  }
}

async function sync(email, force) {
  const now = Date.now();
  const { lastSync = {} } = await chrome.storage.session.get("lastSync");
  if (!force && now - (lastSync[email] || 0) < SYNC_GAP) return { connected: true, data: { started: false, reason: "recent" } };
  lastSync[email] = now;
  await chrome.storage.session.set({ lastSync });
  return withToken(email, (server, token) => call(server, "ext/sync", { method: "POST", token, body: {} }));
}

async function disconnect(email) {
  const result = await withToken(email, (server, token) => call(server, "ext/token", { method: "DELETE", token })
    .catch(() => null));  // forget it here even if the server can't be reached
  const { server, tokens } = await settings();
  delete tokens[tokenKey(server, email)];
  await chrome.storage.local.set({ tokens });
  return { connected: false, server: result.server };
}

async function handle(msg) {
  switch (msg && msg.type) {
    case "status": {
      const { server, tokens, prefs } = await settings();
      return { server, connected: Boolean(tokens[tokenKey(server, msg.email)]), prefs };
    }
    case "connect": return connect(msg.email);
    case "summary":
      return withToken(msg.email, (server, token) =>
        call(server, "ext/summary?" + new URLSearchParams({ tz: msg.tz || "" }), { token }));
    case "sync": return sync(msg.email, Boolean(msg.force));
    case "disconnect": return disconnect(msg.email);
    case "prefs": {
      const { prefs } = await settings();
      const next = { ...prefs, ...(msg.set || {}) };
      await chrome.storage.local.set({ prefs: next });
      return { prefs: next };
    }
    case "openApp": {
      const { server } = await settings();
      if (server) await chrome.tabs.create({ url: `${server}/#account=${encodeURIComponent(msg.email || "")}` });
      return { ok: true };
    }
    case "options":
      await chrome.runtime.openOptionsPage();
      return { ok: true };
    default:
      throw new Error("Unknown request");
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;  // only this extension's own pages and scripts
  handle(msg).then(sendResponse, (err) => sendResponse({ error: String((err && err.message) || err), status: (err && err.status) || 0 }));
  return true;  // answer asynchronously
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
