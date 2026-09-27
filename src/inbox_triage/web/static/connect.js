"use strict";
// Connects the Inbox Triage extension. The extension opens this page with chrome.identity.launchWebAuthFlow;
// once the person approves, the page sends the browser to the extension's own chromiumapp.org address with a
// one-time code, which the extension swaps for its token. Like app.js, it never parses HTML strings.

const params = new URLSearchParams(location.search);
const request = { redirect_uri: params.get("redirect_uri") || "", state: params.get("state") || "",
  code_challenge: params.get("code_challenge") || "" };
const hint = (params.get("login_hint") || "").trim().toLowerCase();
const box = document.getElementById("connect");

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}
function fill(node, ...children) { node.replaceChildren(...children.flat().filter((c) => c != null && c !== false)); }

async function api(path, method = "GET", body) {
  const res = await fetch("/api/" + path, { method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "inbox-triage" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function problem(text) { return text ? el("p", { class: "notice err" }, text) : null; }

function signIn(email, errorText) {
  const go = async (e) => {
    e.currentTarget.disabled = true;
    try {
      const { url } = await api("login", "POST", { email, next: location.pathname + location.search });
      location.href = url;
    } catch (err) { fill(box, signInCard(email, err.message, go)); }
  };
  return signInCard(email, errorText, go);
}
function signInCard(email, errorText, go) {
  return [
    el("div", { class: "page-head" },
      el("h1", {}, "Connect Inbox Triage to Gmail"),
      el("p", { class: "lead" }, email ? ["Sign in with Google as ", el("b", {}, email), " to show your dashboard in Gmail."]
        : "Sign in with Google to show your dashboard in Gmail.")),
    problem(errorText),
    el("p", { class: "small muted" }, "During the free beta, Google shows “Google hasn't verified this app”. " +
      "Choose Advanced, then continue to Inbox Triage. It only ever adds and removes its own labels."),
    go ? el("div", { class: "row" }, el("button", { class: "btn primary", type: "button", onclick: go }, "Continue with Google")) : null,
  ];
}

function approve(email, errorText) {
  const go = async (e) => {
    e.currentTarget.disabled = true;
    try {
      const { redirect } = await api("ext/authorize", "POST", { ...request, email });
      fill(box, el("p", { class: "loading" }, "Connected. You can close this window."));
      location.href = redirect;
    } catch (err) { fill(box, approve(email, err.message)); }
  };
  return [
    el("div", { class: "page-head" },
      el("h1", {}, "Connect the extension?"),
      el("p", { class: "lead" }, "The Inbox Triage extension will show your dashboard at the top of Gmail for ",
        el("b", {}, email), " and ask this server to sort new mail as it arrives.")),
    problem(errorText),
    el("p", { class: "small muted" }, "The extension can read your label counts and recently labeled emails' senders and " +
      "subjects. It can't read, send, delete or change your mail, and it holds no keys. Disconnect it any time from its settings."),
    el("div", { class: "row" },
      el("button", { class: "btn primary", type: "button", onclick: go }, "Connect"),
      el("button", { class: "btn ghost", type: "button", onclick: () => fill(box, signIn("")) }, "Use a different account")),
  ];
}

async function main() {
  const errorText = decodeURIComponent((location.hash.match(/error=([^&]*)/) || [])[1] || "");
  if (!request.redirect_uri || !request.state || !request.code_challenge) {
    return fill(box, el("div", { class: "page-head" }, el("h1", {}, "Open this from the extension"),
      el("p", { class: "lead" }, "Click Connect Gmail in the Inbox Triage bar at the top of Gmail.")));
  }
  const state = await api("state");
  const signedIn = state.accounts.filter((a) => a.connected).map((a) => a.email);
  const email = hint ? (signedIn.includes(hint) ? hint : "") : (signedIn.length === 1 ? signedIn[0] : "");
  fill(box, email && !errorText ? approve(email) : signIn(hint, errorText));
}

main().catch((err) => fill(box, problem(err.message)));
