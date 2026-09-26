"use strict";
// Inbox Triage web UI. No framework; every string from email or the server is
// inserted as text (never HTML). The strict CSP forbids inline style attributes,
// so dynamic sizes go through the CSSOM (node.style.x = …) only.

const FREEMAIL = new Set(["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
  "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com"]);
const WINDOWS = [[1, "Last day"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const FREQ_LABEL = { off: "Manual", hourly: "Hourly", daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
// The labels Inbox Triage adds, in display order. `gmail` is the Gmail label name.
const LABELS = {
  needs_you: { name: "Needs You", cls: "needs", gmail: "Triage/Needs You",
    help: "A person is waiting on your reply, or something needs you to act." },
  updates: { name: "Updates", cls: "updates", gmail: "Triage/Updates",
    help: "Receipts, deliveries, statements and account notices worth a glance." },
  for_you: { name: "For You", cls: "foryou", gmail: "Triage/For You",
    help: "From people you know and the topics you said matter." },
  later: { name: "Later", cls: "later", gmail: "Triage/Later",
    help: "Newsletters, promotions and mass mail that can wait until you have time." },
  shopping: { name: "Shopping", cls: "shopping", gmail: "Topics/Shopping" },
  junk: { name: "Junk", cls: "junk", gmail: "Triage/Junk" },
};
const ATTENTION = ["needs_you", "updates", "for_you", "later"];
const SORTED = [...ATTENTION, "junk"];  // every destination that gets a label, for run breakdowns
// What a personal rule does. Junk is decided before Jev is asked, so junk never reaches it.
const ACTION = {
  important: { name: "Important", cls: "foryou", icon: "foryou", sub: "Gets at least For You" },
  not_important: { name: "Can wait", cls: "later", icon: "later", sub: "Goes to Later" },
  junk: { name: "Junk", cls: "junk", icon: "ban", sub: "Labeled Junk, never sent to Jev" },
};
const RULE_KIND = { domain: "Domain", sender: "Sender", keyword: "Keyword" };
const RULE_HINT = { domain: "school.example", sender: "boss@work.example", keyword: "a word or phrase, like invoice" };
const TITLES = { landing: "Inbox Triage", signin: "Sign in · Inbox Triage", setup: "Setup · Inbox Triage",
  onboarding: "Setup · Inbox Triage", dashboard: "Inbox Triage" };

let STATE = null;
let current = null;           // selected account email
let tab = "overview";         // dashboard tab: overview | rules | settings
let focusNext = false;        // move focus to the new screen's heading after a user-initiated change
let lastAccount = null;       // the account to return to when "Add another account" is cancelled
const historyCache = {};      // last run history per account, drawn at once so re-renders don't jump
const onboard = {};           // per-account wizard state
let pollTimer = null;

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);  // CSSOM; inline style attributes are blocked by the CSP
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (k === "checked" || k === "selected" || k === "disabled") node[k] = !!v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}
function fill(node, ...kids) {
  node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  return node;
}

const ICONS = {
  needs: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v6M12 16.5v.5" }]],
  updates: [["path", { d: "M6 16v-5a6 6 0 1 1 12 0v5l2 2H4z" }], ["path", { d: "M10 21h4" }]],
  foryou: [["path", { d: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" }]],
  later: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v5l3 2" }]],
  shopping: [["path", { d: "M6 8h12l-1 12H7z" }], ["path", { d: "M9 8V6a3 3 0 0 1 6 0v2" }]],
  check: [["path", { d: "M20 6 9 17l-5-5" }]],
  x: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  arrow: [["path", { d: "M5 12h14M13 6l6 6-6 6" }]],
  external: [["path", { d: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" }]],
  chevron: [["path", { d: "m6 9 6 6 6-6" }]],
  play: [["path", { d: "M7 5v14l12-7z", fill: "currentColor", stroke: "none" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  pen: [["path", { d: "M4 20h4L19 9l-4-4L4 16z" }]],
  shield: [["path", { d: "M12 3 5 6v5c0 4.5 3 8.5 7 10 4-1.5 7-5.5 7-10V6z" }], ["path", { d: "m9 12 2 2 4-4" }]],
  lock: [["rect", { x: 5, y: 11, width: 14, height: 10, rx: 2 }], ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }]],
  trash: [["path", { d: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" }]],
  sync: [["path", { d: "M20 12a8 8 0 1 1-2.3-5.7" }], ["path", { d: "M20 4v5h-5" }]],
  eye: [["path", { d: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" }], ["circle", { cx: 12, cy: 12, r: 3 }]],
  hand: [["path", { d: "M7 5v14l12-7z" }]],
  calendar: [["rect", { x: 3, y: 5, width: 18, height: 16, rx: 2 }], ["path", { d: "M3 10h18M8 3v4M16 3v4" }]],
  zap: [["path", { d: "M13 3 4 14h7l-1 7 9-11h-7z" }]],
  ban: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M5.6 5.6l12.8 12.8" }]],
};
const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name, size = 16, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  const attrs = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": size <= 14 ? 2.4 : 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: ("icon " + cls).trim() };
  for (const [k, v] of Object.entries(attrs)) svg.setAttribute(k, v);
  for (const [tag, props] of ICONS[name]) {
    const part = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(props)) part.setAttribute(k, v);
    svg.append(part);
  }
  return svg;
}
const LABEL_ICON = { needs_you: "needs", updates: "updates", for_you: "foryou", later: "later", shopping: "shopping", junk: "ban" };
function pill(key, big = false) {
  const meta = LABELS[key];
  return el("span", { class: `pill ${meta.cls}${big ? " lg" : ""}` }, icon(LABEL_ICON[key], big ? 15 : 13), meta.name);
}
const labelKey = (gmailName) => Object.keys(LABELS).find((k) => LABELS[k].gmail.toLowerCase() === String(gmailName).toLowerCase());
const LABEL_ORDER = Object.keys(LABELS);
const byLabelOrder = (names) => [...names].sort((x, y) => (LABEL_ORDER.indexOf(labelKey(x)) + 1 || 99) - (LABEL_ORDER.indexOf(labelKey(y)) + 1 || 99));
function pillForName(name) {
  const key = labelKey(name);
  return key ? pill(key) : el("span", { class: "pill" }, String(name).replace(/^(Triage|Topics)\//, ""));
}

const $view = () => document.getElementById("view");
function mount(...nodes) { fill($view(), ...nodes); }
function show(screen, node) {
  document.body.dataset.screen = screen;
  document.title = TITLES[screen] || "Inbox Triage";
  renderHeader(screen);
  const view = $view();
  mount(screen === "dashboard" ? el("div", { role: "tabpanel", id: "panel", "aria-labelledby": "tab-" + tab }, node) : node);
  if (focusNext) {
    focusNext = false;
    window.scrollTo(0, 0);
    (view.querySelector("h1") || view).focus({ preventScroll: true });
  }
}
function navigate(fn) { focusNext = true; fn(); }
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.classList.remove("show"); setTimeout(() => { if (!t.classList.contains("show")) t.textContent = ""; }, 250); }, 4500);
}
async function api(path, method = "GET", body) {
  const res = await fetch("/api/" + path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "inbox-triage" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}
const acctPath = (email, action) => `accounts/${encodeURIComponent(email)}/${action}`;
const fmt = (n) => Number(n || 0).toLocaleString();
const plural = (n, word) => `${fmt(n)} ${word}${n === 1 ? "" : "s"}`;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const hourText = (h) => `${String(h).padStart(2, "0")}:00`;
const time = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function dayDiff(d) {
  const a = new Date(d); a.setHours(0, 0, 0, 0);
  const b = new Date(); b.setHours(0, 0, 0, 0);
  return Math.round((a - b) / 86400000);
}
// "Today, 07:00" · "Yesterday, 07:00" · "Sep 24, 21:14"
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000), diff = dayDiff(d);
  const day = diff === 0 ? "Today" : diff === -1 ? "Yesterday" : diff === 1 ? "Tomorrow"
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
  return `${day}, ${time(d)}`;
}
// "today at 07:00" · "tomorrow at 07:00" · "Mon, Sep 29 at 07:00"
function upcoming(ts) {
  if (!ts) return "not scheduled";
  const d = new Date(ts * 1000), diff = dayDiff(d);
  const day = diff === 0 ? "today" : diff === 1 ? "tomorrow" : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  return `${day} at ${time(d)}`;
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}
function scheduleText(s) {
  if (!s || s.frequency === "off") return "only when you click Run now";
  const hour = hourText(s.hour);
  if (s.frequency === "hourly") return "every hour";
  if (s.frequency === "daily") return `every day at ${hour}`;
  if (s.frequency === "weekly") return `every ${WEEKDAYS[s.weekday]} at ${hour}`;
  return `on the last day of each month at ${hour}`;
}
function senderName(from) {
  const m = /^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/.exec(from || "");
  return m ? (m[1].trim() || m[2]) : (from || "");
}
const acct = () => STATE.accounts.find((a) => a.email === current);
// The zone schedule hours are picked in; the server runs the schedule in it.
const BROWSER_TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();
const withZone = (sched) => ({ ...sched, tz: BROWSER_TZ || sched.tz || "" });
// Gmail and Google errors as the person reading them needs them.
function explainError(text) {
  const t = String(text || "unknown error");
  if (/HTTP 429\b|HTTP 403 \((rateLimitExceeded|userRateLimitExceeded|RATE_LIMIT_EXCEEDED|RESOURCE_EXHAUSTED)\)/.test(t)) return "Gmail asked Inbox Triage to slow down. Nothing was lost: the next run picks up where this one stopped.";
  if (/HTTP 403 \((insufficientPermissions|ACCESS_TOKEN_SCOPE_INSUFFICIENT)\)/.test(t)) return "Google didn't grant the Gmail permission. Sign out, sign in again, and tick the Gmail box.";
  if (/HTTP 403 \((accessNotConfigured|SERVICE_DISABLED)\)/.test(t)) return "The Gmail API is turned off for this server's Google Cloud project. Whoever runs the server needs to enable it.";
  if (/HTTP 403 \((dailyLimitExceeded|quotaExceeded)\)/.test(t)) return "This app reached Gmail's daily limit. It will work again tomorrow.";
  if (/^Gmail API HTTP 403$/.test(t)) return "Gmail turned down a request (HTTP 403), most likely because the first run read mail too fast. Runs are now paced and retried: click Run now to try again.";
  if (/invalid_grant|HTTP 401/.test(t)) return "Google access has expired. Sign out and sign in again to reconnect.";
  return t;
}

// Google's own words, one click away, for whoever needs to look into a problem.
function techDetails(raw, shown) {
  const text = String(raw || "");
  return text && text !== shown ? el("details", { class: "tech" }, el("summary", {}, "Technical details"), el("code", {}, text)) : null;
}
function problem(lead, raw) {
  const text = explainError(raw);
  return el("div", { class: "notice err wide" }, el("p", {}, lead, text), techDetails(raw, text));
}
// When a run Gmail paused carries on by itself.
function resumeText(a) {
  if (!a.resume_at) return a.last_run?.mode === "dry-run" ? "A preview starts over, so run it again once Gmail's break is over."
    : "Click Run now to pick up where it stopped.";
  return a.resume_at <= Date.now() / 1000 ? "It picks up where it stopped in a moment, on its own."
    : `It picks up where it stopped ${upcoming(a.resume_at)}, on its own.`;
}
const TRIGGERS = { schedule: "Scheduled", resume: "Resumed" };

// A row of role=radio buttons with arrow-key support (a real radio group, drawn as buttons).
function radioGroup({ label, options, value, onChange, cls = "seg", itemClass = "" }) {
  const group = el("div", { role: "radiogroup", "aria-label": label, class: cls });
  const buttons = options.map((o) => el("button", { type: "button", role: "radio", class: [itemClass, o.cls].filter(Boolean).join(" ") },
    ...(o.content ? o.content : [o.label])));
  const pick = (i, focus) => {
    buttons.forEach((b, j) => { b.setAttribute("aria-checked", String(i === j)); b.tabIndex = i === j ? 0 : -1; });
    if (focus) buttons[i].focus();
  };
  buttons.forEach((b, i) => {
    b.addEventListener("click", () => { pick(i); onChange(options[i].value); });
    b.addEventListener("keydown", (e) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      const edge = { Home: 0, End: buttons.length - 1 }[e.key];
      if (!step && edge === undefined) return;
      e.preventDefault();
      const j = edge ?? (i + step + buttons.length) % buttons.length;
      pick(j, true); onChange(options[j].value);
    });
  });
  pick(Math.max(0, options.findIndex((o) => o.value === value)), false);
  group.append(...buttons);
  return group;
}

// ---------------------------------------------------------------- boot + routing
async function refresh() {
  STATE = await api("state");
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("error")) { toast(hash.get("error")); history.replaceState(null, "", "/"); }
  if (hash.get("account")) { current = hash.get("account"); tab = "overview"; history.replaceState(null, "", "/"); }
  if (!STATE.accounts.some((a) => a.email === current)) current = STATE.accounts[0]?.email || null;
  render();
}

function render() {
  clearTimeout(pollTimer);
  if (!current) return renderSignin();
  const a = acct();
  if (!a.connected) return renderSignin(a.email, "This account needs to reconnect to Google.");
  if (!a.settings.onboarded || onboard[a.email]?.mode) return renderOnboarding(a);
  renderDashboard(a);
}

async function logout() {
  await api("logout", "POST", {});
  current = null;
  navigate(() => refresh());
}

function betaBanner() {
  const b = STATE && STATE.beta;
  const slot = document.getElementById("beta");
  if (!slot) return;
  if (!b || !b.enabled) return fill(slot);
  const until = b.ends ? new Date(b.ends + "T00:00:00").toLocaleDateString(undefined, { dateStyle: "long" }) : null;
  const parts = [b.ended ? "The free beta has ended"
    : b.max_accounts ? `Free for the first ${b.max_accounts === 1 ? "user" : `${b.max_accounts} users`}` : "Free beta"];
  if (until && !b.ended) parts[0] += ` until ${until}`;
  parts.push("Bring your own Jev key");
  fill(slot, parts.map((p) => el("span", {}, p + " ·")),
    el("a", { href: "https://github.com/shimoverse/inbox-triage", target: "_blank", rel: "noopener" }, "Open source, self-host anytime"));
}

function renderHeader(screen) {
  betaBanner();
  const right = document.getElementById("accounts");
  const signedIn = STATE.accounts.length > 0;
  fill(right, signedIn ? accountMenu() : [
    screen === "landing" ? el("button", { type: "button", class: "navlink wide-only", onclick: () => document.getElementById("how")?.scrollIntoView({ behavior: "smooth" }) }, "How it works") : null,
    el("a", { class: "navlink", href: "/privacy" }, "Privacy"),
    el("a", { class: "navlink wide-only", href: "https://github.com/shimoverse/inbox-triage" }, "Open source"),
  ]);
  renderTabs(screen === "dashboard");
}

function renderTabs(visible) {
  const nav = document.getElementById("tabs");
  if (!visible) { nav.hidden = true; return fill(nav); }
  nav.hidden = false;
  const a = acct();
  const defs = [["overview", "Overview"], ["rules", "What matters", a.rules], ["settings", "Settings"]];
  const buttons = defs.map(([key, label, count]) => el("button", {
    type: "button", role: "tab", class: "tab", id: "tab-" + key, "aria-controls": "panel",
    "aria-selected": String(tab === key), tabindex: tab === key ? "0" : "-1",
    onclick: () => selectTab(key) },
    label, count ? el("span", { class: "count" }, String(count)) : null));
  buttons.forEach((b, i) => b.addEventListener("keydown", (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    const edge = { Home: 0, End: defs.length - 1 }[e.key];
    if (!step && edge === undefined) return;
    e.preventDefault();
    selectTab(defs[edge ?? (i + step + defs.length) % defs.length][0]);
  }));
  fill(nav, buttons);
}
function selectTab(key) {
  if (tab !== key) { tab = key; render(); window.scrollTo(0, 0); }
  document.getElementById("tab-" + key)?.focus();
}

function accountMenu() {
  const a = current ? acct() : null;
  const menu = el("details", { class: "acctmenu" },
    el("summary", { "aria-label": a ? `Account menu, signed in as ${a.email}` : "Account menu" },
      el("span", { class: "avatar", "aria-hidden": "true" }, (a?.email || "+").charAt(0)),
      el("span", { class: "email" }, a ? a.email : "Accounts"), icon("chevron", 16, "chev")),
    el("div", { class: "menu" },
      el("div", { class: "label" }, STATE.accounts.length > 1 ? "Your accounts" : "Signed in as"),
      STATE.accounts.map((x) => el("button", { type: "button", "aria-current": x.email === current ? "true" : null,
        onclick: () => navigate(() => { current = x.email; tab = "overview"; render(); }) },
        el("span", { class: "avatar", "aria-hidden": "true" }, x.email.charAt(0)), el("span", { class: "email" }, x.email),
        x.email === current ? icon("check", 16) : null)),
      el("hr"),
      el("button", { type: "button", onclick: () => navigate(() => { lastAccount = current; current = null; render(); }) }, icon("plus", 16), "Add another account"),
      el("button", { type: "button", onclick: logout }, "Sign out")));
  menu.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.open) { menu.open = false; menu.querySelector("summary").focus(); }
  });
  return menu;
}
document.addEventListener("click", (e) => {
  for (const d of document.querySelectorAll("details.acctmenu[open]")) if (!d.contains(e.target)) d.open = false;
});

// ---------------------------------------------------------------- sign in
async function startGoogle(email, btn) {
  if (btn) btn.disabled = true;
  try {
    const { url } = await api("login", "POST", { email: (email || "").trim() });
    location.href = url;
  } catch (err) { toast(err.message); if (btn) btn.disabled = false; }
}
const PERMISSION_NOTE = "Google will ask to let Inbox Triage “view and modify” Gmail. That's the narrowest permission " +
  "that allows adding labels; the app only ever adds or removes its own.";

function renderSignin(prefill = "", note = "") {
  if (!STATE.oauth_configured) return renderOAuthSetup();
  if (!STATE.accounts.length) return renderLanding();
  // Adding another account, or reconnecting one: a short card instead of the full welcome page.
  const email = el("input", { type: "email", id: "signin-email", value: prefill, placeholder: "you@gmail.com", autocomplete: "email" });
  const go = (e) => { e.preventDefault(); startGoogle(email.value, e.submitter); };
  const back = STATE.accounts.find((x) => x.email === lastAccount && x.email !== prefill && x.connected)
    || STATE.accounts.find((x) => x.email !== prefill && x.connected);
  show("signin", el("div", { class: "wrap tiny" },
    el("form", { class: "card pad stack", onsubmit: go },
      el("div", { class: "page-head" },
        el("h1", { tabindex: "-1" }, note ? "Reconnect to Google" : "Add another Gmail account"),
        el("p", { class: "lead" }, note || "Each account gets its own rules and schedule.")),
      el("div", { class: "field" },
        el("label", { for: "signin-email" }, "Gmail or Google Workspace address ", el("span", { class: "opt" }, "(optional)")), email),
      el("button", { class: "btn primary lg block", type: "submit" }, "Continue with Google", icon("arrow", 18)),
      el("p", { class: "fine" }, PERMISSION_NOTE),
      back ? el("button", { type: "button", class: "btn ghost", onclick: () => navigate(() => { current = back.email; render(); }) }, "Cancel") : null)));
}

const EXAMPLE_INBOX = [
  ["Maya Chen", "Could you sign the permission slip by Friday?", ["needs_you"]],
  ["Northside Elementary", "Field trip details for next week", ["for_you"]],
  ["Acme Bank", "Your monthly statement is ready", ["updates"]],
  ["Parcel Post", "Your order is out for delivery", ["updates", "shopping"]],
  ["Homes Near You", "12 new listings picked for you", ["later"]],
  ["The Weekly Digest", "This week's top stories", ["later"]],
];

function renderLanding() {
  const email = el("input", { type: "email", id: "hint-email", placeholder: "you@gmail.com", autocomplete: "email" });
  const hint = el("div", { class: "field", hidden: true },
    el("label", { for: "hint-email" }, "Which Google account?"), email);
  const specific = el("button", { type: "button", class: "btn link", onclick: () => { hint.hidden = false; specific.hidden = true; email.focus(); } },
    "Use a specific account");
  const go = (e) => { e.preventDefault(); startGoogle(email.value, e.submitter); };
  const preview = el("div", { class: "pile" },
    el("div", { class: "preview", role: "img", "aria-label": "Example: an inbox after a run, with Needs You, For You, Updates, Shopping and Later labels" },
      el("div", { class: "preview-head" }, el("b", {}, "Inbox"), el("span", {}, "Sorted 2 minutes ago")),
      EXAMPLE_INBOX.map(([from, subject, keys]) => el("div", { class: "prow" },
        el("span", { class: "avatar " + LABELS[keys[0]].cls }, from.charAt(0)),
        el("div", { class: "who" }, el("span", { class: "from" }, from), el("span", { class: "subj" }, subject)),
        el("span", { class: "pills" }, keys.map((k) => pill(k)))))));
  const trust = [
    ["shield", "i-good", "Label-only, always", "Never sends, deletes, archives, forwards, or marks mail read, and never moves anything to Spam."],
    ["lock", "i-blue", "Jev sees very little", "Only the sender's domain, the subject and a short cleaned excerpt. Never addresses, attachments, or your Google token."],
    ["trash", "i-warm", "Leave anytime", "Disconnect revokes Google access and deletes everything stored for your account."],
  ];
  show("landing", el("div", { class: "landing" },
    el("section", { class: "hero" },
      el("div", { class: "hero-copy" },
        el("span", { class: "eyebrow" }, el("span", { class: "dot", "aria-hidden": "true" }), "Gmail triage, powered by Jev"),
        el("h1", { class: "display", tabindex: "-1" }, "Know what needs you. Let the rest wait."),
        el("p", { class: "lead" }, "Inbox Triage adds four simple labels to your Gmail on a schedule you choose. " +
          "It never sends, deletes, archives, or marks anything read."),
        el("form", { class: "cta", onsubmit: go },
          hint,
          el("div", { class: "cta-row" }, el("button", { type: "submit", class: "btn primary lg" }, "Continue with Google", icon("arrow", 18)), specific),
          el("p", { class: "fine" }, PERMISSION_NOTE))),
      preview),
    el("div", { class: "band" }, el("section", { class: "section", "aria-labelledby": "labels-h" },
      el("div", { class: "intro" },
        el("h2", { class: "h-big", id: "labels-h" }, "Four labels. Nothing else changes."),
        el("p", {}, "Your mail stays in your inbox, exactly where it was. Each email gets at most one of these, " +
          "so you can see at a glance what to open first.")),
      el("ul", { class: "label-cards" }, ATTENTION.map((k) => el("li", {}, pill(k, true), el("p", {}, LABELS[k].help)))),
      el("p", { class: "note-line" }, pill("shopping"), "is added to orders and deliveries too. Anything Jev isn't sure about stays unlabeled."))),
    el("section", { class: "section", id: "how", "aria-labelledby": "how-h" },
      el("h2", { class: "h-big", id: "how-h" }, "Three steps, then it runs on its own"),
      el("ol", { class: "steps3" },
        el("li", {}, el("span", { class: "n" }, "Step 1"), el("h3", {}, "Sign in with Google"),
          el("p", {}, "Approve one permission so Inbox Triage can add its labels to your Gmail.")),
        el("li", {}, el("span", { class: "n" }, "Step 2"), el("h3", {}, "Connect Jev"),
          el("p", {}, "Jev by TypeSafe makes a fast yes-or-no call on each email. Paste your own key from ",
            el("a", { href: STATE.jev.signup_url, target: "_blank", rel: "noopener" }, "console.typesafe.ai"), ".")),
        el("li", {}, el("span", { class: "n" }, "Step 3"), el("h3", {}, "Say what matters"),
          el("p", {}, "Tap Important, Can wait or Junk on a few emails, or describe it in your own words. Pick a schedule and you're done.")))),
    el("section", { class: "trust", "aria-label": "Privacy and safety" },
      el("ul", { class: "trust-grid" }, trust.map(([ic, cls, title, text]) => el("li", {}, icon(ic, 26, cls), el("h3", {}, title), el("p", {}, text)))),
      el("div", { class: "trust-cta" },
        el("p", {}, "Ready for a calmer inbox?"),
        el("button", { type: "button", class: "btn invert lg", onclick: (e) => startGoogle(email.value, e.currentTarget) }, "Continue with Google", icon("arrow", 18))))));
}

function renderOAuthSetup() {
  if (STATE.hosted) {
    return show("setup", el("div", { class: "wrap tiny" }, el("div", { class: "card pad stack" },
      el("div", { class: "page-head" }, el("h1", { tabindex: "-1" }, "Almost ready"),
        el("p", { class: "lead" }, "The person running this server still needs to connect it to Google. Please check back soon.")))));
  }
  const box = el("textarea", { id: "oauth-json", placeholder: '{"installed": {"client_id": "…", "client_secret": "…"}}', spellcheck: "false" });
  const save = async (e) => {
    e.preventDefault();
    try { await api("oauth-client", "POST", { json: box.value }); toast("Google sign-in is ready."); navigate(() => refresh()); }
    catch (err) { toast(err.message); }
  };
  const link = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);
  show("setup", el("div", { class: "wrap slim" },
    el("div", { class: "page-head" },
      el("span", { class: "kicker" }, "One-time setup for whoever runs this app"),
      el("h1", { tabindex: "-1" }, "Connect this copy to Google"),
      el("p", { class: "lead" }, "People who use Inbox Triage just click “Continue with Google”. For that to work, the person running " +
        "this copy connects it to Google once. If you downloaded a packaged build, this step is already done.")),
    el("form", { class: "card pad stack", onsubmit: save },
      el("ol", { class: "numbered" },
        el("li", {}, "Open the ", link("https://console.cloud.google.com/projectcreate", "Google Cloud console"), " and create a project."),
        el("li", {}, "Enable the ", link("https://console.cloud.google.com/apis/library/gmail.googleapis.com", "Gmail API"), "."),
        el("li", {}, "In ", el("b", {}, "Google Auth Platform"), ", set up branding and audience, then click ", el("b", {}, "Publish app"), " (otherwise logins expire after 7 days)."),
        el("li", {}, "Create a client: ", el("b", {}, "Clients → Create client → Desktop app"), ", and download the JSON."),
        el("li", {}, "Paste the JSON below.")),
      el("div", { class: "field" }, el("label", { for: "oauth-json" }, "OAuth client JSON"), box),
      el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit" }, "Save and continue")))));
}

// ---------------------------------------------------------------- onboarding
function newWizard(a, extra = {}) {
  return { step: 1, emails: null, tags: {}, notes: "", proposed: null, days: 7, preview: true,
    schedule: { frequency: "daily", hour: 7, weekday: 0 }, ...extra };
}
function renderOnboarding(a) {
  const st = (onboard[a.email] ||= newWizard(a));
  if (st.step === 1 && a.jev_connected && !st.changeKey && !st.mode) st.step = 2;
  if (!a.jev_connected && !st.mode) st.step = 1;
  const body = st.step === 1 ? stepJev(a, st) : st.step === 2 ? stepContext(a, st) : stepSchedule(a, st);
  show("onboarding", body);
}
function goStep(st, step) { navigate(() => { st.step = step; render(); }); }
function leaveWizard(a, toTab) { navigate(() => { delete onboard[a.email]; tab = toTab; render(); }); }

function stepper(step) {
  return el("ol", { class: "stepper", "aria-label": "Setup progress" }, ["Connect Jev", "What matters", "Schedule"].map((label, i) => {
    const n = i + 1;
    return el("li", { class: n < step ? "done" : "", "aria-current": n === step ? "step" : null },
      el("span", { class: "num", "aria-hidden": "true" }, n < step ? icon("check", 15) : String(n)),
      el("span", { class: "txt" }, label), n < step ? el("span", { class: "sr-only" }, " (done)") : null);
  }));
}

// Jev is required: every triage decision is a typed Jev answer.
function jevKeyForm(a, onDone, extra = null) {
  const key = el("input", { type: "password", id: "jev-key", placeholder: "Paste your Jev API key", autocomplete: "off",
    spellcheck: "false", "aria-describedby": "jev-key-help jev-key-err" });
  const error = el("p", { class: "field-err", id: "jev-key-err", role: "alert" });
  const submit = el("button", { class: "btn primary", type: "submit" }, "Connect Jev");
  const go = async (e) => {
    e.preventDefault();
    fill(error); key.removeAttribute("aria-invalid");
    if (!key.value.trim()) { error.textContent = "Paste your Jev API key first."; key.setAttribute("aria-invalid", "true"); return key.focus(); }
    submit.disabled = true; submit.textContent = "Checking with Jev…";
    try {
      await api(acctPath(a.email, "jev-key"), "PUT", { key: key.value.trim() });
      STATE = await api("state"); toast("Jev connected."); onDone();
    } catch (err) {
      error.textContent = err.message; key.setAttribute("aria-invalid", "true");
      submit.disabled = false; submit.textContent = "Connect Jev"; key.focus();
    }
  };
  return el("form", { class: "stack", onsubmit: go },
    el("div", { class: "field" }, el("label", { for: "jev-key" }, "Your Jev API key"), key,
      el("span", { class: "help", id: "jev-key-help" }, "We check it with Jev before saving. It's used only for your mailbox."), error),
    el("div", { class: "row" }, submit,
      el("a", { class: "btn", href: STATE.jev.signup_url, target: "_blank", rel: "noopener" }, "Get a Jev key", icon("external", 15)), extra));
}

function stepJev(a, st) {
  const keyMode = st.mode === "key";
  const done = () => (keyMode ? leaveWizard(a, "settings") : goStep(st, 2));
  const extra = keyMode ? el("button", { type: "button", class: "btn ghost", onclick: () => leaveWizard(a, "settings") }, "Cancel")
    : a.jev_connected ? el("button", { type: "button", class: "btn ghost", onclick: () => { st.changeKey = false; goStep(st, 2); } }, "Keep current key")
    : null;
  return el("div", { class: "wrap narrow" },
    keyMode ? null : stepper(1),
    el("div", { class: "page-head" },
      keyMode ? null : el("span", { class: "kicker" }, `Welcome, ${a.email}`),
      el("h1", { tabindex: "-1" }, keyMode ? "Change your Jev key" : "Connect Jev, the engine that sorts your mail"),
      el("p", { class: "lead" }, keyMode ? "The new key is checked with Jev before it replaces the current one." : "Three quick steps. You can change everything later.")),
    el("div", { class: "split" },
      el("div", { class: "card pad stack" },
        el("p", { class: "intro-text" }, "Jev by TypeSafe answers quick yes-or-no questions about each email: is someone waiting on you, " +
          "is it an update, is it bulk mail or phishing? It decides in a fraction of a second, for a tiny fraction of a cent. " +
          "Fixed rules then turn those answers into labels."),
        jevKeyForm(a, done, extra)),
      el("aside", { class: "aside", "aria-label": "What Jev sees" },
        el("h2", {}, "What Jev sees"),
        el("ul", { class: "checklist" }, ["The sender's domain", "The subject and a short cleaned excerpt", "Hints like “you've emailed this person”"]
          .map((t) => el("li", {}, icon("check", 18, "yes"), t))),
        el("h2", {}, "Never"),
        el("ul", { class: "checklist" }, ["Email addresses or attachments", "Your older mail or your Google token"]
          .map((t) => el("li", {}, icon("x", 18, "no"), t))))));
}

const ruleKey = (r) => `${r.kind}:${String(r.value).trim().toLowerCase().replace(/^@/, "")}`;
function withoutDuplicates(rules) {
  const byKey = new Map();
  for (const r of rules) { byKey.delete(ruleKey(r)); byKey.set(ruleKey(r), r); }
  return [...byKey.values()];
}
function ruleForEmail(m, action) {
  const personal = FREEMAIL.has(m.domain);
  return personal ? { kind: "sender", value: m.address, action, note: m.subject.slice(0, 80) }
                  : { kind: "domain", value: m.domain, action, note: m.subject.slice(0, 80) };
}

function ruleRow(r, onRemove, label, showAction = true) {
  return el("div", { class: "rrow" },
    showAction ? el("span", { class: "pill " + (ACTION[r.action] || ACTION.not_important).cls },
      icon((ACTION[r.action] || ACTION.not_important).icon, 13), (ACTION[r.action] || ACTION.not_important).name) : null,
    el("span", { class: "kind" }, RULE_KIND[r.kind] || r.kind),
    el("span", { class: "what" }, el("span", { class: "val" }, r.value), r.note ? el("span", { class: "note" }, r.note) : null),
    onRemove ? el("button", { type: "button", class: "btn icon", "aria-label": label || `Remove rule ${r.value}`, onclick: onRemove }, icon("x", 16)) : null);
}

function stepContext(a, st) {
  const teach = st.mode === "teach";
  const list = el("ul", { class: "mlist", "aria-label": "Recent emails" }, el("li", { class: "loading" }, "Loading your recent emails…"));
  const notes = el("textarea", { id: "notes", value: st.notes, "aria-describedby": "notes-help",
    placeholder: "For example: “#2 is from my kids' school, that's always important. I don't care about real estate emails. " +
      "The bank alerts matter. Newsletters from shops can wait.”",
    oninput: () => (st.notes = notes.value) });
  const rowSync = {};
  const rulesBox = el("div", { class: "stack" });
  const saveBtn = el("button", { class: "btn primary", type: "button", onclick: () => save(false) });

  const quickRules = () => Object.entries(st.tags).filter(([, v]) => v).map(([id, action]) => {
    const m = st.emails.find((x) => x.id === id);
    return { ...ruleForEmail(m, action), _tag: id };
  });
  // Marking an email wins over a suggestion for the same sender or domain.
  // Only the emails on screen: they're the ones the notes can refer to, and all that goes to the assistant.
  const shownEmails = () => (st.emails ? (st.showAll ? st.emails : st.emails.slice(0, 8)) : []);
  const allRules = () => withoutDuplicates([...(st.proposed?.rules || []), ...(st.emails ? quickRules() : [])]);
  const drawRules = () => {
    const rules = allRules();
    fill(rulesBox,
      el("div", { class: "row between" }, el("h2", {}, teach ? "New rules" : "Rules so far"),
        el("span", { class: "card-sub" }, rules.length ? plural(rules.length, "rule") : "")),
      rules.length ? el("div", { class: "rlist" }, rules.map((r, i) => ruleRow(r, () => {
        if (r._tag) { st.tags[r._tag] = undefined; rowSync[r._tag]?.(); }
        else st.proposed.rules = st.proposed.rules.filter((x) => ruleKey(x) !== ruleKey(r));
        drawRules();
        const left = rulesBox.querySelectorAll(".rrow .btn.icon");
        (left[i] || left[i - 1] || saveBtn).focus();
      }))) : el("p", { class: "muted small" }, "Mark a few emails or write some notes, then turn them into rules."),
      st.proposed?.summary ? el("p", { class: "notice" }, el("b", {}, "What Jev keeps in mind: "), st.proposed.summary) : null);
    const n = rules.length;
    saveBtn.textContent = teach ? (n ? `Save ${plural(n, "rule")}` : "Done") : (n ? `Save ${plural(n, "rule")} and continue` : "Continue");
    if (!teach) saveBtn.append(icon("arrow", 17));
  };

  const drawList = () => {
    if (!st.emails) return;
    if (!st.emails.length) return fill(list, el("li", { class: "empty" }, "No recent inbox mail found. You can still describe what matters in your own words."));
    const shown = shownEmails();
    const rest = st.emails.length - shown.length;
    fill(list, shown.map((m, i) => {
      const imp = el("button", { type: "button", class: "toggle important" });
      const wait = el("button", { type: "button", class: "toggle wait" });
      const junk = el("button", { type: "button", class: "toggle junk" });
      const li = el("li", {},
        el("div", { class: "meta" }, el("span", { class: "num" }, `#${i + 1}`), el("span", { class: "from" }, senderName(m.from) || m.address)),
        el("span", { class: "subj" }, m.subject || "(no subject)"),
        el("div", { class: "acts", role: "group", "aria-label": `#${i + 1}: ${m.subject || "(no subject)"}` }, imp, wait, junk,
          el("button", { type: "button", class: "mention", onclick: () => insertRef(notes, st, i + 1, m) }, "Mention in notes")));
      const sync = rowSync[m.id] = () => {
        const tag = st.tags[m.id];
        li.className = tag || "";
        for (const [b, action, label] of [[imp, "important", "Important"], [wait, "not_important", "Can wait"], [junk, "junk", "Junk"]]) {
          b.setAttribute("aria-pressed", String(tag === action));
          fill(b, tag === action ? icon("check", 14) : null, label);
        }
      };
      const mark = (action) => { st.tags[m.id] = st.tags[m.id] === action ? undefined : action; sync(); drawRules(); };
      imp.addEventListener("click", () => mark("important"));
      wait.addEventListener("click", () => mark("not_important"));
      junk.addEventListener("click", () => mark("junk"));
      sync();
      return li;
    }), rest > 0 ? el("li", { class: "more" }, el("button", { type: "button", class: "btn ghost sm", onclick: () => {
      st.showAll = true; drawList(); list.querySelectorAll("li")[shown.length]?.querySelector("button")?.focus();
    } }, `Show ${plural(rest, "more email")}`)) : null);
  };
  if (!st.emails) {
    api(acctPath(a.email, "emails") + "?days=14").then((d) => { st.emails = d.emails; drawList(); drawRules(); })
      .catch((err) => fill(list, el("li", { class: "empty" }, el("span", { class: "err" }, err.message))));
  }

  const interpretBtn = el("button", { class: "btn", type: "button", onclick: (e) => interpret(e.currentTarget) }, icon("pen", 15), "Turn notes into rules");
  const interpret = async (btn) => {
    if (!st.notes.trim()) { toast("Write a few words about what matters first."); return notes.focus(); }
    btn.disabled = true; fill(btn, "Reading your notes…");
    try {
      const refs = shownEmails().map((m) => ({ from: m.from, domain: m.domain, subject: m.subject }));
      st.proposed = await api(acctPath(a.email, "interpret"), "POST", { notes: st.notes, emails: refs });
      drawRules();
      toast(`Found ${plural(st.proposed.rules.length, "rule")}. Review them below.`);
    } catch (err) { toast(err.message); }
    btn.disabled = !STATE.assistant.available; fill(btn, icon("pen", 15), "Turn notes into rules");
  };
  const assistantBox = el("div", { class: "stack" });
  const drawAssistant = () => {
    interpretBtn.disabled = !STATE.assistant.available;
    assistantBox.hidden = STATE.assistant.available;
    if (STATE.assistant.available) return fill(assistantBox);
    fill(assistantBox, el("p", { class: "notice small" },
      "Turning notes into rules uses an optional assistant (", STATE.assistant.model, " via OpenRouter). ",
      STATE.hosted ? "It isn't enabled on this server, so mark emails instead." : "Add an OpenRouter key to enable it, or just mark emails."),
      STATE.hosted ? null : assistantKeyForm(drawAssistant));
  };
  drawAssistant();
  drawList();
  drawRules();

  const save = async (skip) => {
    try {
      const rules = allRules().map(({ _tag, ...r }) => r);
      if (!skip && rules.length) {
        const existing = await api(acctPath(a.email, "preferences"));
        const summary = [existing.summary, st.proposed?.summary].filter(Boolean).join(" ");
        const saved = await api(acctPath(a.email, "preferences"), "PUT", { rules: withoutDuplicates([...existing.rules, ...rules]), summary });
        a.rules = saved.rules.length;
      }
      if (teach) {
        if (!skip && rules.length) toast(`Saved ${plural(rules.length, "rule")}.`);
        return leaveWizard(a, "rules");
      }
      goStep(st, 3);
    } catch (err) { toast(err.message); }
  };

  return el("div", { class: "wrap" },
    teach ? null : stepper(2),
    el("div", { class: "page-head" },
      el("h1", { tabindex: "-1" }, teach ? "Teach it more" : "Tell it what matters"),
      el("p", { class: "lead" }, "Mark a few emails, write a few words, or both. You'll see every rule before it's saved.")),
    el("div", { class: "split even" },
      el("section", { class: "card clip", "aria-labelledby": "recent-emails-h" },
        el("div", { class: "card-head" }, el("div", {},
          el("h2", { id: "recent-emails-h" }, "Your recent emails"),
          el("p", { class: "card-sub" }, "Mark a few as Important, Can wait or Junk."))),
        list),
      el("div", { class: "col" },
        el("section", { class: "card pad stack" },
          el("div", {}, el("h2", {}, el("label", { for: "notes" }, "Or just say it")),
            el("p", { class: "card-sub", id: "notes-help" }, "In your own words. Mention emails by number.")),
          notes,
          el("div", { class: "notes-foot" },
            el("span", { class: "help" }, "Prefer talking? Dictate into any voice-to-text app and paste."), interpretBtn),
          assistantBox,
          el("p", { class: "help" }, "Your notes go once to the assistant to propose rules; Jev makes every email decision.")),
        el("section", { class: "card pad" }, rulesBox))),
    el("div", { class: "actions" },
      teach ? el("button", { class: "btn ghost", type: "button", onclick: () => leaveWizard(a, "rules") }, "Cancel")
        : el("button", { class: "btn ghost", type: "button", onclick: () => { st.changeKey = true; goStep(st, 1); } }, "Back"),
      el("div", { class: "right" },
        teach ? null : el("button", { class: "btn", type: "button", onclick: () => save(true) }, "Skip for now"),
        saveBtn)));
}

function insertRef(textarea, st, n, m) {
  const ref = `#${n} (${m.domain || m.address}: ${m.subject.slice(0, 50)}) `;
  const at = textarea.selectionStart ?? textarea.value.length;
  textarea.value = textarea.value.slice(0, at) + ref + textarea.value.slice(at);
  st.notes = textarea.value;
  textarea.focus();
  textarea.setSelectionRange(at + ref.length, at + ref.length);
}

function assistantKeyForm(onDone) {
  const key = el("input", { type: "password", id: "assistant-key", placeholder: "OpenRouter API key", autocomplete: "off", "aria-label": "OpenRouter API key" });
  const save = async () => {
    try { await api("keys", "PUT", { role: "assistant", key: key.value.trim() }); STATE = await api("state"); toast("Assistant saved."); onDone(); }
    catch (err) { toast(err.message); }
  };
  return el("div", { class: "row" }, key, el("button", { class: "btn", type: "button", onclick: save }, "Save key"),
    el("a", { href: "https://openrouter.ai/keys", target: "_blank", rel: "noopener", class: "small" }, "Get an OpenRouter key"));
}

function scheduleFields(sched, onChange) {
  const hour = el("select", { id: "sched-hour", onchange: () => { sched.hour = +hour.value; onChange?.(); } },
    [...Array(24).keys()].map((h) => el("option", { value: h, selected: h === sched.hour }, hourText(h))));
  const day = el("select", { id: "sched-day", "aria-label": "Day of the week", onchange: () => { sched.weekday = +day.value; onChange?.(); } },
    WEEKDAYS.map((d, i) => el("option", { value: i, selected: i === sched.weekday }, d)));
  const extra = el("div", { class: "at" });
  const draw = () => fill(extra,
    sched.frequency === "weekly" ? el("span", { class: "pair" }, el("label", { for: "sched-day" }, "on"), day) : null,
    ["daily", "weekly", "monthly"].includes(sched.frequency) ? el("span", { class: "pair" }, el("label", { for: "sched-hour" }, "at"), hour) : null);
  const freq = radioGroup({ label: "How often", value: sched.frequency,
    options: STATE.frequencies.map((f) => ({ value: f, label: FREQ_LABEL[f] || f })),
    onChange: (f) => { sched.frequency = f; draw(); onChange?.(); } });
  draw();
  return el("div", { class: "sched" }, freq, extra);
}

function stepSchedule(a, st) {
  const summary = el("div", { class: "summary", role: "status" });
  const drawSummary = () => {
    const windowText = (WINDOWS.find(([d]) => d === st.days)?.[1] || `last ${st.days} days`).toLowerCase();
    const s = st.schedule;
    fill(summary, icon("calendar", 22), el("span", {}, "We'll ", el("b", {}, `${st.preview ? "preview" : "sort"} the ${windowText}`), " now",
      st.preview ? " (nothing in Gmail changes)" : "",
      s.frequency === "off" ? ["; after that, it sorts only when you click ", el("b", {}, "Run now"), "."]
        : [", then sort new mail ", el("b", {}, scheduleText(s)), "."]));
  };
  const choices = radioGroup({ label: "Which emails to sort first", cls: "choices", itemClass: "choice", value: st.days,
    options: WINDOWS.map(([d, label]) => ({ value: d, content: [
      el("span", { class: "t" }, label, icon("check", 18, "check-i")),
      el("span", { class: "d" }, d > 7 ? "Takes a few minutes" : d === 7 ? "Quick · a good start" : "Quick")] })),
    onChange: (d) => { st.days = d; drawSummary(); } });
  const preview = el("input", { type: "checkbox", role: "switch", class: "switch", id: "preview", checked: st.preview,
    onchange: () => { st.preview = preview.checked; drawSummary(); } });
  const finish = async (btn) => {
    btn.disabled = true;
    try {
      await api(acctPath(a.email, "settings"), "PUT", { schedule: withZone(st.schedule), onboarded: true });
      await api(acctPath(a.email, "run"), "POST", { days: st.days, dry_run: st.preview });
      toast(st.preview ? "Preview started. Nothing in Gmail will change." : "Sorting started.");
      delete onboard[a.email];
      tab = "overview";
      navigate(() => refresh());
    } catch (err) { toast(err.message); btn.disabled = false; }
  };
  drawSummary();
  return el("div", { class: "wrap narrow" },
    stepper(3),
    el("div", { class: "page-head" },
      el("h1", { tabindex: "-1" }, "When should it sort?"),
      el("p", { class: "lead" }, "Start with recent mail, then keep new mail sorted automatically.")),
    el("section", { class: "card pad stack", "aria-labelledby": "first-h" },
      el("h2", { id: "first-h" }, "Sort these emails first"),
      choices,
      el("label", { class: "switch-row", for: "preview" }, preview,
        el("span", { class: "text" }, el("b", {}, "Preview first"),
          el("span", {}, "See what would be labeled without changing Gmail. Scheduled runs after this add labels for real.")))),
    el("section", { class: "card pad stack", "aria-labelledby": "then-h" },
      el("h2", { id: "then-h" }, "Then keep it sorted"),
      scheduleFields(st.schedule, drawSummary),
      el("p", { class: "help" }, `Times are in your time zone${BROWSER_TZ ? ` (${BROWSER_TZ})` : ""}. ` +
        "Runs happen while Inbox Triage is running on this computer or your server. Each run picks up where the last one stopped.")),
    summary,
    el("div", { class: "actions" },
      el("button", { class: "btn ghost", type: "button", onclick: () => goStep(st, 2) }, "Back"),
      el("div", { class: "right" },
        el("button", { class: "btn primary lg", type: "button", onclick: (e) => finish(e.currentTarget) }, "Finish and start sorting", icon("arrow", 17)))));
}

// ---------------------------------------------------------------- dashboard
function renderDashboard(a) {
  if (!["overview", "rules", "settings"].includes(tab)) tab = "overview";
  show("dashboard", tab === "rules" ? rulesPane(a) : tab === "settings" ? settingsPane(a) : overviewPane(a));
  adoptTimeZone(a);
}

// Schedules saved before time zones were recorded ran on the server's clock, so "07:00" could mean
// midnight here. The hour was picked in this browser, so record this browser's zone once.
const zoneAdopted = new Set();
async function adoptTimeZone(a) {
  const s = a.settings.schedule;
  if (s.tz || !BROWSER_TZ || s.frequency === "off" || zoneAdopted.has(a.email)) return;
  zoneAdopted.add(a.email);
  try {
    await api(acctPath(a.email, "settings"), "PUT", { schedule: withZone(s) });
    STATE = await api("state");
    // Update the status line in place: a re-render would pull keyboard focus off the page.
    const line = document.querySelector("#status-h + p"), live = acct();
    if (line && live && current === a.email) line.textContent = statusText(live)[3];
  } catch { /* keep the server's clock; saving Settings records the zone later */ }
}

// While a run goes, or waits out a break Gmail asked for, keep the overview in step with the server,
// so an automatic resume and its result show up without a reload.
const resumeCheck = (a) => Math.min(60000, Math.max(5000, (a.resume_at - Date.now() / 1000) * 1000 + 3000));
function pollJob(a, delay = 2500) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const fresh = await api("state");
      const b = fresh.accounts.find((x) => x.email === a.email);
      STATE = fresh;
      if (!b) return render();
      const watching = a.job?.status === "running" ? a.job.started : null;
      if (b.job?.status === "running" && b.job.started === watching) {
        const line = document.getElementById("run-started");
        if (line) line.textContent = `Started ${ago(b.job.started)}. It keeps going if you leave this page.`;
        return pollJob(b);
      }
      // A run ended: the one on screen, or an automatic resume that started and finished between looks.
      const ended = b.job && b.job.status !== "running" && (b.job.started === watching || b.job.started !== a.job?.started);
      if (ended) {
        if (b.job.status === "ok") toast(`Sorting finished: ${plural(b.job.result.processed || 0, "email")} checked.`);
        else if (b.job.status === "paused") toast(`Gmail asked for a break. ${resumeText(b)}`);
        else if (b.job.status === "error") toast("The run stopped. See the details on the dashboard.");
      }
      if (ended || b.job?.status === "running") return current === b.email && tab === "overview" ? render() : undefined;
      if (b.resume_at) pollJob(b, resumeCheck(b));  // still paused: look again once the break is over
    } catch { pollJob(a, delay); }
  }, delay);
}

function overviewPane(a) {
  if (a.job?.status === "running") pollJob(a);
  else if (a.resume_at) pollJob(a, resumeCheck(a));
  const recent = el("section", { class: "card clip", "aria-labelledby": "recent-h" },
    el("div", { class: "card-head" }, el("h2", { id: "recent-h" }, "Recently labeled")), el("p", { class: "loading" }, "Loading…"));
  const history = el("section", { class: "card clip", "aria-labelledby": "history-h" },
    el("div", { class: "card-head" }, el("h2", { id: "history-h" }, "Run history")), el("p", { class: "loading" }, "Loading…"));
  const draw = (h) => { drawRecent(recent, h.decisions, a.email, h.details !== false); drawHistory(history, h.runs); };
  if (historyCache[a.email]) draw(historyCache[a.email]);
  api(acctPath(a.email, "history")).then((h) => { historyCache[a.email] = h; if (recent.isConnected) draw(h); })
    .catch((err) => {
      if (historyCache[a.email]) return;
      fill(recent, el("div", { class: "card-head" }, el("h2", { id: "recent-h" }, "Recently labeled")), el("p", { class: "empty err" }, err.message));
      history.hidden = true;
    });
  return el("div", { class: "wrap" }, statusCard(a), lastRunCard(a), recent, history);
}

function statusText(a) {
  const job = a.job, running = job?.status === "running", s = a.settings.schedule;
  let kind, glyph, title, sub;
  if (!a.jev_connected) [kind, glyph, title, sub] = ["paused", "needs", "Sorting is paused", "Inbox Triage needs Jev to make decisions. Reconnect it to keep sorting."];
  else if (running) [kind, glyph, title, sub] = ["running", "sync", "Sorting now…", `Started ${ago(job.started)}. It keeps going if you leave this page.`];
  else if (a.last_run?.status === "paused") [kind, glyph, title, sub] = ["paused", "later", "Sorting is taking a short break",
    `Gmail asked for a slower pace. Nothing is lost. ${resumeText(a)}`];
  else if (a.settings.dry_run) [kind, glyph, title, sub] = ["preview", "eye", "Preview mode is on",
    `Runs show what would be labeled; Gmail isn't changed. ${s.frequency === "off" ? "No schedule." : `Runs ${scheduleText(s)}.`}`];
  else if (s.frequency === "off") [kind, glyph, title, sub] = ["manual", "hand", "Sorting runs when you ask", "No schedule is set. Click Run now, or pick one in Settings."];
  else [kind, glyph, title, sub] = ["ok", "sync", "Sorting is on", `${cap(scheduleText(s))} · next run ${upcoming(a.next_run)}`];
  return [kind, glyph, title, sub];
}

function statusCard(a) {
  const job = a.job, running = job?.status === "running";
  const [kind, glyph, title, sub] = statusText(a);
  const days = el("select", { "aria-label": "Which emails to sort" }, el("option", { value: "" }, "New mail since last run"),
    WINDOWS.map(([d, l]) => el("option", { value: d }, l)));
  const dry = el("input", { type: "checkbox", id: "dry" });
  // After a pause, Run now picks up the same job: same dates, and a preview stays a preview.
  const carry = a.last_run?.status === "paused" ? a.last_run : null;
  if (carry?.days) {
    if (!WINDOWS.some(([d]) => d === carry.days)) days.append(el("option", { value: carry.days }, `Last ${carry.days} days`));
    days.value = String(carry.days);
  }
  dry.checked = carry?.mode === "dry-run";
  const runBtn = el("button", { class: "btn primary", type: "button", disabled: running, onclick: async () => {
    runBtn.disabled = true;
    try {
      await api(acctPath(a.email, "run"), "POST", { days: days.value ? +days.value : null, dry_run: dry.checked });
      toast(dry.checked || a.settings.dry_run ? "Preview started." : "Sorting started.");
      await refresh();
    } catch (err) { toast(err.message); runBtn.disabled = false; } } },
    running ? null : icon("play", 15), running ? "Running…" : "Run now");
  const failed = job?.status === "error" && (!a.last_run || a.last_run.started < job.started);
  return el("section", { class: "card status", "aria-labelledby": "status-h" },
    el("div", { class: "status-main" },
      el("span", { class: "status-icon " + kind, "aria-hidden": "true" }, icon(glyph, 26)),
      el("div", {}, el("h1", { id: "status-h", tabindex: "-1" }, title), el("p", { id: running ? "run-started" : null }, sub))),
    a.jev_connected ? el("div", { class: "runctl" }, days,
      a.settings.dry_run ? null : el("label", { class: "check", for: "dry" }, dry, el("span", {}, "Preview", el("span", { class: "d-only" }, " only"))), runBtn) : null,
    running ? el("div", { class: "progress wide", role: "progressbar", "aria-label": "Sorting in progress" }) : null,
    failed ? problem("Your last manual run didn't finish. ", job.result.message || job.result.error) : null,
    a.jev_connected ? null : el("div", { class: "wide" }, jevKeyForm(a, () => refresh())));
}

function outcomeParts(outcomes) {
  const counts = outcomes || {};
  const total = Object.values(counts).reduce((n, v) => n + (+v || 0), 0);
  const labeled = SORTED.reduce((n, k) => n + (counts[k] || 0), 0);
  const shown = SORTED.filter((k) => k !== "junk" || counts.junk);  // Junk only appears once it's used
  return { total, labeled, parts: [...shown.map((k) => [LABELS[k].name, LABELS[k].cls + "-c", counts[k] || 0]),
    ["Left as is", "rest-c", Math.max(0, total - labeled)]] };
}
function outcomeBar(outcomes, thin = false) {
  const { total, parts } = outcomeParts(outcomes);
  if (!total) return null;
  return el("div", { class: "bar" + (thin ? " thin" : ""), role: thin ? null : "img", "aria-hidden": thin ? "true" : null,
    "aria-label": thin ? null : parts.map(([n, , v]) => `${n}: ${v}`).join(", ") },
    parts.filter(([, , v]) => v > 0).map(([, cls, v]) => el("span", { class: cls, style: { flexGrow: String(v) } })));
}

function lastRunCard(a) {
  const r = a.last_run;
  if (!r) {
    return el("section", { class: "card pad stack", "aria-labelledby": "last-h" },
      el("h2", { id: "last-h" }, "No runs yet"),
      el("p", { class: "muted" }, a.jev_connected ? "Click Run now to sort your recent mail. Labeled emails will show up here." : "Connect Jev to start sorting."));
  }
  const preview = r.mode === "dry-run";
  const { labeled, parts, total } = outcomeParts(r.outcomes);
  const done = preview ? labeled : (r.gmail_changes || 0);
  const main = el("div", { class: "main" },
    el("div", { class: "row between" }, el("h2", { id: "last-h" }, "Last run"),
      el("span", { class: "small muted" }, [ago(r.started), (TRIGGERS[r.trigger] || "Manual").toLowerCase(), preview ? "preview" : null].filter(Boolean).join(" · "))),
    r.status === "ok" || (r.status === "paused" && r.processed) ? [
      el("p", { class: "big" }, el("b", {}, fmt(r.processed)), ` ${r.processed === 1 ? "email" : "emails"} checked, `, el("b", {}, fmt(done)),
        preview ? " would be labeled" : " labeled"),
      total ? outcomeBar(r.outcomes) : null,
      total ? el("ul", { class: "legend" }, parts.map(([name, cls, v]) => el("li", {}, el("span", { class: "sw " + cls }), name, " ", el("b", {}, fmt(v))))) : null,
      preview ? el("p", { class: "small muted" }, "Preview: Gmail wasn't changed.") : null,
      r.remaining && r.status === "ok" ? el("p", { class: "small muted" }, "More mail is left; the next run picks up where this one stopped.") : null,
    ] : null,
    r.status === "paused" ? el("div", { class: "notice warn" },
      el("p", {}, el("b", {}, "Paused. "), `Gmail asked Inbox Triage to slow down, so this run stopped early. Nothing is lost. ${resumeText(a)}`),
      techDetails(r.message)) : null,
    r.status === "error" ? problem("This run didn't finish. ", r.message || r.error) : null);
  const side = el("div", { class: "side" },
    el("span", { class: "kv" }, icon("zap", 16), "Jev decisions"),
    el("span", { class: "bignum" }, r.jev_calls ? fmt(r.jev_calls) : "—"),
    el("span", { class: "small" }, r.jev_calls ? `${Math.round(r.jev_ms / r.jev_calls)} ms average per email` : a.settings.model || "jev-latest"));
  return el("section", { class: "card lastrun", "aria-labelledby": "last-h" }, main, side);
}

function gmailLink(email, id) {
  return `https://mail.google.com/mail/?authuser=${encodeURIComponent(email)}#all/${encodeURIComponent(id)}`;
}

function drawRecent(node, items, email, details = true) {
  const head = el("div", { class: "card-head" }, el("h2", { id: "recent-h" }, "Recently labeled"));
  if (!items.length) {
    return fill(node, head, el("p", { class: "empty" }, "Nothing labeled yet. After a run, labeled emails show up here with a link to open each one in Gmail."));
  }
  const rows = items.map((d) => ({ ...d, keys: (d.names || []).map(labelKey).filter(Boolean) }));
  const counts = {};
  for (const d of rows) for (const k of d.keys) counts[k] = (counts[k] || 0) + 1;
  let filter = "all";
  const list = el("ul", { class: "mrows" });
  const drawList = () => {
    const shown = rows.filter((d) => filter === "all" || d.keys.includes(filter));
    fill(list, shown.map((d) => {
      const gone = d.from === undefined;
      const subject = gone ? (details ? "No longer available in Gmail" : "Details unavailable right now") : d.subject || "(no subject)";
      return el("li", { class: "mrow" },
        el("span", { class: "from", title: d.from || null }, gone ? "—" : senderName(d.from) || "—"),
        el("span", { class: "subj" + (gone ? " gone" : "") }, subject),
        el("span", { class: "pills" }, (d.names || []).length ? byLabelOrder(d.names).map(pillForName) : el("span", { class: "muted small" }, "Unchanged")),
        el("a", { class: "btn icon", href: gmailLink(email, d.id), target: "_blank", rel: "noopener", title: "Open in Gmail",
          "aria-label": gone ? "Open in Gmail" : `Open “${subject}” in Gmail` }, icon("external", 17)));
    }));
  };
  const keys = ["all", ...Object.keys(LABELS).filter((k) => counts[k])];
  const chips = keys.map((k) => el("button", { type: "button", class: "fchip", "aria-pressed": String(k === filter) },
    k === "all" ? "All" : `${LABELS[k].name} · ${counts[k]}`));
  chips.forEach((c, i) => c.addEventListener("click", () => {
    filter = keys[i];
    chips.forEach((x, j) => x.setAttribute("aria-pressed", String(i === j)));
    drawList();
  }));
  head.append(el("div", { class: "filters", role: "group", "aria-label": "Filter by label" }, chips));
  drawList();
  fill(node, head, list);
}

function historyRow(r) {
  const ok = r.status === "ok", paused = r.status === "paused", preview = r.mode === "dry-run";
  const trigger = TRIGGERS[r.trigger] || "Manual";
  const window = r.days ? `Last ${r.days} days · ` : "";
  const counts = `${window}${plural(r.processed ?? 0, "email")} · ${preview ? "preview, Gmail unchanged" : `${fmt(r.gmail_changes ?? 0)} labeled`}`;
  const summary = ok ? counts : paused ? `${counts} · Gmail asked for a break`
    : `Didn't finish. ${explainError(r.message || r.error)}`;
  return el("li", { class: "hrow" },
    el("span", { class: "when" }, when(r.started)),
    el("span", { class: "badge" + (r.trigger === "schedule" || r.trigger === "resume" ? "" : " manual") }, trigger),
    el("span", { class: "sum" + (ok || paused ? "" : " err-text"), title: ok ? null : r.message || null }, el("span", { class: "m-only" }, trigger + " · "), summary),
    ((ok || paused) && outcomeBar(r.outcomes, true)) || el("span", { class: "bar-slot" }),
    ok ? el("span", { class: "state ok" }, icon("check", 16), r.remaining ? "Done, more left" : "Done")
      : paused ? el("span", { class: "state warn" }, icon("later", 16), "Paused")
      : el("span", { class: "state err" }, icon("needs", 16), "Failed"));
}

function drawHistory(node, runs) {
  const head = el("div", { class: "card-head" }, el("h2", { id: "history-h" }, "Run history"));
  if (!runs.length) return fill(node, head, el("p", { class: "empty" }, "No runs yet."));
  const LIMIT = 6;
  let open = false;
  const list = el("ul", { class: "hrows" });
  const more = runs.length > LIMIT ? el("button", { type: "button", class: "btn ghost sm", "aria-expanded": "false" }) : null;
  const draw = () => {
    fill(list, (open ? runs : runs.slice(0, LIMIT)).map(historyRow));
    if (more) { more.textContent = open ? "Show fewer" : `Show all ${runs.length} runs`; more.setAttribute("aria-expanded", String(open)); }
  };
  more?.addEventListener("click", () => { open = !open; draw(); });
  draw();
  fill(node, head, list, more ? el("div", { class: "card-foot" }, more) : null);
}

function rulesPane(a) {
  const body = el("div", { class: "stack" }, el("p", { class: "muted" }, "Loading your rules…"));
  const teach = () => navigate(() => {
    onboard[a.email] = newWizard(a, { step: 2, mode: "teach", preview: false, schedule: { ...a.settings.schedule } });
    render();
  });
  const put = async (p) => {
    const saved = await api(acctPath(a.email, "preferences"), "PUT", p);
    a.rules = saved.rules.length;
    const live = acct();
    if (live) live.rules = a.rules;
    renderTabs(true);
    return saved;
  };
  let action = "important", kindValue = "domain";
  const draw = (p, focusId) => {
    const remove = async (i) => {
      const [gone] = p.rules.splice(i, 1);
      try { draw(await put(p), "col-" + gone.action); toast(`Removed ${gone.value}.`); }
      catch (err) { toast(err.message); load(); }
    };
    const EMPTY = { important: "No important senders or topics yet.", not_important: "Nothing marked as able to wait yet.",
      junk: "Nothing marked as junk yet." };
    const column = (act) => {
      const mine = p.rules.map((r, i) => [r, i]).filter(([r]) => r.action === act);
      return el("section", { class: "card clip rule-col", "aria-labelledby": "col-" + act },
        el("div", { class: "card-head" }, el("h2", { class: "col-title", id: "col-" + act, tabindex: "-1" },
          el("span", { class: `pill ${ACTION[act].cls} lg` }, icon(ACTION[act].icon, 15), ACTION[act].name),
          el("span", { class: "card-sub" }, ACTION[act].sub))),
        mine.length ? el("div", { class: "rlist" }, mine.map(([r, i]) => ruleRow(r, () => remove(i), `Remove rule ${r.value}`, false)))
          : el("p", { class: "empty" }, EMPTY[act]));
    };
    const kind = el("select", { id: "rule-kind", "aria-label": "Match on", onchange: () => { kindValue = kind.value; value.placeholder = RULE_HINT[kind.value]; } },
      Object.entries(RULE_KIND).map(([k, label]) => el("option", { value: k, selected: k === kindValue }, label)));
    const value = el("input", { type: "text", id: "rule-value", "aria-label": "Rule value", placeholder: RULE_HINT[kindValue], "aria-describedby": "rule-err" });
    const error = el("p", { class: "field-err", id: "rule-err", role: "alert" });
    const add = async (e) => {
      e.preventDefault();
      fill(error); value.removeAttribute("aria-invalid");
      const v = value.value.trim();
      if (!v) { error.textContent = "Type a domain, an email address, or a word first."; value.setAttribute("aria-invalid", "true"); return value.focus(); }
      const rule = { kind: kind.value, value: v, action, note: "" };
      const same = p.rules.find((r) => ruleKey(r) === ruleKey(rule));
      if (same && same.action === action) {
        error.textContent = `${v} is already in ${ACTION[action].name}.`;
        return value.focus();
      }
      const others = p.rules.filter((r) => r !== same);
      try {
        const saved = await put({ ...p, rules: [...others, same ? { ...same, action } : rule] });
        if (saved.rules.length === others.length) {
          error.textContent = kind.value === "domain" ? "That doesn't look like a domain. Try something like school.example."
            : kind.value === "sender" ? "That doesn't look like an email address." : "That rule doesn't look valid.";
          value.setAttribute("aria-invalid", "true");
          return value.focus();
        }
        toast(same ? `Moved ${v} to ${ACTION[action].name}.` : `Added ${v}.`);
        draw(saved, "rule-value");
      } catch (err) { toast(err.message); }
    };
    fill(body,
      p.summary ? el("section", { class: "card keep", "aria-label": "What Jev keeps in mind" }, el("span", { class: "k" }, "What Jev keeps in mind"), el("p", {}, p.summary)) : null,
      el("div", { class: "cols3" }, Object.keys(ACTION).map(column)),
      el("section", { class: "card pad stack", "aria-labelledby": "add-h" },
        el("h2", { id: "add-h" }, "Add a rule"),
        el("form", { class: "rule-add", onsubmit: add },
          radioGroup({ label: "Rule type", value: action, onChange: (v) => (action = v),
            options: Object.entries(ACTION).map(([value, a]) => ({ value, label: a.name, cls: value === "not_important" ? "" : value })) }),
          kind, value, el("button", { class: "btn primary", type: "submit" }, icon("plus", 16), "Add rule")),
        error),
      el("p", { class: "safety" }, icon("shield", 20), el("span", {}, "Sender and domain rules only apply to authenticated mail, so they can't be spoofed. " +
        "Suspected phishing is never promoted, and security alerts are never pushed to Later or Junk. " +
        "Junk gets a Junk label; nothing is ever deleted, archived or moved to Spam.")));
    if (focusId) document.getElementById(focusId)?.focus();
  };
  const load = () => api(acctPath(a.email, "preferences")).then((p) => draw(p))
    .catch((err) => fill(body, el("p", { class: "notice err" }, err.message)));
  load();
  return el("div", { class: "wrap tabpane" },
    el("div", { class: "page-head row-head" },
      el("div", { class: "page-head" }, el("h1", { tabindex: "-1" }, "What matters to you"),
        el("p", { class: "lead" }, "Your rules come first, within a few safety limits.")),
      el("button", { class: "btn", type: "button", onclick: teach }, icon("pen", 15), "Teach it more")),
    body);
}

function settingsPane(a) {
  const sched = { ...a.settings.schedule };
  const model = el("input", { type: "text", id: "model", value: a.settings.model || "", placeholder: "jev-latest", spellcheck: "false", "aria-describedby": "model-help" });
  const previewText = el("span", { class: "text" });
  const preview = el("input", { type: "checkbox", role: "switch", class: "switch", id: "pm", checked: a.settings.dry_run,
    onchange: () => drawPreview() });
  const drawPreview = () => fill(previewText,
    el("b", {}, preview.checked ? "On: Gmail is never changed" : "Off: labels are added to Gmail"),
    el("span", {}, preview.checked ? "Runs only show what would be labeled." : "Turn on to see what would be labeled without changing anything."));
  drawPreview();
  const saveBtn = el("button", { class: "btn primary", type: "submit" }, "Save changes");
  const save = async (e) => {
    e.preventDefault();
    saveBtn.disabled = true;
    try {
      const changes = { model: model.value.trim(), dry_run: preview.checked };
      // Only a changed schedule is saved (with this browser's zone); otherwise its saved zone stays as it is.
      if (["frequency", "hour", "weekday"].some((k) => sched[k] !== a.settings.schedule[k])) changes.schedule = withZone(sched);
      await api(acctPath(a.email, "settings"), "PUT", changes);
      STATE = await api("state");
      toast("Settings saved.");
    } catch (err) { toast(err.message); }
    saveBtn.disabled = false;
  };
  const disconnect = async () => {
    if (!confirm(`Disconnect ${a.email}? This revokes access and permanently deletes your rules, settings, Jev key and history here. Labels already in Gmail stay.`)) return;
    try {
      await api(`accounts/${encodeURIComponent(a.email)}`, "DELETE");
      toast(`Disconnected ${a.email} and deleted its data.`);
      tab = "overview";
      navigate(() => refresh());
    } catch (err) { toast(err.message); }
  };
  const assistantBox = el("div", { class: "ctl tight" });
  const drawAssistant = () => fill(assistantBox,
    el("span", { class: "val" }, el("b", {}, STATE.assistant.model), " via OpenRouter"),
    el("span", { class: "help" }, "Runs only when you click “Turn notes into rules”. It never classifies email."),
    STATE.assistant.available ? el("span", { class: "conn ok" }, "Available")
      : STATE.hosted ? el("span", { class: "conn off" }, "Not enabled on this server") : assistantKeyForm(drawAssistant));
  drawAssistant();
  const changeKey = () => navigate(() => {
    onboard[a.email] = newWizard(a, { step: 1, mode: "key", changeKey: true, preview: false, schedule: { ...a.settings.schedule } });
    render();
  });
  const setrow = (id, title, sub, ...ctl) => el("div", { class: "setrow" },
    el("div", { class: "lbl" }, el("h2", { id }, title), el("p", {}, sub)), el("div", { class: "ctl" }, ...ctl));
  return el("div", { class: "wrap slim tabpane" },
    el("div", { class: "page-head" }, el("h1", { tabindex: "-1" }, "Settings")),
    el("form", { class: "card", onsubmit: save, "aria-label": "Settings" },
      setrow("set-sched", "Schedule", "When new mail gets sorted.", scheduleFields(sched),
        el("p", { class: "help" }, `Times are in ${sched.tz || BROWSER_TZ || "the server's time zone"}.`)),
      setrow("set-preview", "Preview mode", "Try it without touching Gmail.",
        el("label", { class: "switch-row plain", for: "pm" }, preview, previewText)),
      setrow("set-jev", "Jev", "Makes every sorting decision.",
        el("div", { class: "row between" },
          a.jev_connected ? el("span", { class: "conn ok" }, "Connected") : el("span", { class: "conn warn" }, "Not connected"),
          el("button", { class: "btn", type: "button", onclick: changeKey }, a.jev_connected ? "Change key" : "Connect Jev")),
        el("div", { class: "field" }, el("label", { for: "model" }, "Model"), model,
          el("span", { class: "help", id: "model-help" }, "Leave empty to use jev-latest."))),
      el("div", { class: "setrow" }, el("div", { class: "lbl" }, el("h2", {}, "Notes assistant"), el("p", {}, "Optional. Turns your notes into rules.")), assistantBox),
      el("div", { class: "set-foot" }, saveBtn)),
    el("section", { class: "card danger", "aria-labelledby": "danger-h" },
      el("div", { class: "setrow" },
        el("div", { class: "lbl" }, el("h2", { id: "danger-h" }, "Disconnect"), el("p", {}, "Leave and delete your data.")),
        el("div", { class: "ctl side" },
          el("p", {}, "Revokes Google access and permanently deletes your rules, settings, Jev key and history here. Labels already in Gmail stay."),
          el("button", { class: "btn danger", type: "button", onclick: disconnect }, icon("trash", 16), "Disconnect account")))));
}

window.addEventListener("hashchange", () => {
  if (/(^|[#&])(account|error)=/.test(location.hash)) refresh();
});
refresh().catch((err) => mount(el("div", { class: "wrap tiny" }, el("p", { class: "notice err" }, err.message))));
