"use strict";
// Inbox Triage web UI. No framework; every string from email or the server is
// inserted as text (never HTML).

const FREEMAIL = new Set(["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
  "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com"]);
const WINDOWS = [[1, "Last day"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const FREQ_LABEL = { off: "Off (run manually)", hourly: "Every hour", daily: "Every day", weekly: "Every week", monthly: "Last day of every month" };
const PROVIDER_ORDER = ["openrouter", "openai", "anthropic", "ollama", "jev", "rules"];
const byOrder = (list) => [...list].sort((a, b) => PROVIDER_ORDER.indexOf(a.id) - PROVIDER_ORDER.indexOf(b.id));
const OUTCOME_LABEL = { needs_you: "Needs You", updates: "Updates", for_you: "For You", later: "Later", unchanged: "Unchanged",
  excluded: "Skipped (sent/spam)", error: "Model error", deleted: "Deleted", spam: "Suspicious (unchanged)" };

let STATE = null;
let current = null;           // selected account email
const onboard = {};           // per-account wizard state
let pollTimer = null;

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
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
const $view = () => document.getElementById("view");
function mount(...nodes) { const v = $view(); fill(v, ...nodes); }
function toast(msg) {
  const t = document.getElementById("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 4500);
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
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function scheduleText(s) {
  if (!s || s.frequency === "off") return "Manual only";
  const hour = `${String(s.hour).padStart(2, "0")}:00`;
  if (s.frequency === "hourly") return "Every hour";
  if (s.frequency === "daily") return `Daily at ${hour}`;
  if (s.frequency === "weekly") return `${WEEKDAYS[s.weekday]}s at ${hour}`;
  return `Last day of month at ${hour}`;
}
const acct = () => STATE.accounts.find((a) => a.email === current);
const providerInfo = (id) => STATE.providers.find((p) => p.id === id);

// ---------------------------------------------------------------- boot + routing
async function refresh() {
  STATE = await api("state");
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("error")) { toast(hash.get("error")); history.replaceState(null, "", "/"); }
  if (hash.get("account")) { current = hash.get("account"); history.replaceState(null, "", "/"); }
  if (!STATE.accounts.some((a) => a.email === current)) current = STATE.accounts[0]?.email || null;
  renderAccounts();
  render();
}

function renderAccounts() {
  const nav = document.getElementById("accounts");
  fill(nav, 
    ...STATE.accounts.map((a) => el("button", { class: "acct" + (a.email === current ? " active" : ""),
      onclick: () => { current = a.email; renderAccounts(); render(); } }, a.email)),
    STATE.accounts.length ? el("button", { class: "acct", onclick: () => { current = null; renderSignin(); } }, "+ Add account") : null,
    STATE.accounts.length ? el("button", { class: "acct", onclick: logout }, "Sign out") : null,
  );
}

function render() {
  clearTimeout(pollTimer);
  if (!current) return renderSignin();
  const a = acct();
  if (!a.connected) return renderSignin(a.email, "This account needs to reconnect to Google.");
  if (!a.settings.onboarded) return renderOnboarding(a);
  renderDashboard(a);
}

async function logout() { await api("logout", "POST", {}); current = null; await refresh(); }

// ---------------------------------------------------------------- sign in
function renderSignin(prefill = "", note = "") {
  if (!STATE.oauth_configured) return renderOAuthSetup();
  const email = el("input", { type: "email", placeholder: "you@gmail.com", value: prefill, autocomplete: "email", "aria-label": "Email address" });
  const go = async (e) => {
    e.preventDefault();
    try { const { url } = await api("login", "POST", { email: email.value.trim() }); location.href = url; }
    catch (err) { toast(err.message); }
  };
  mount(el("div", { class: "hero" },
    el("h1", {}, "Sort your inbox, safely."),
    el("p", { class: "muted" }, "Inbox Triage reads new mail and adds helpful Gmail labels — Needs You, Updates, For You, Later. " +
      "It never sends, deletes, archives, or marks anything read."),
    note ? el("p", { class: "notice" }, note) : null,
    el("form", { class: "card stack", onsubmit: go },
      el("label", { class: "small muted" }, "Your Gmail or Google Workspace address"), email,
      el("button", { class: "btn primary google", type: "submit" }, "Sign in with Google"),
      el("p", { class: "small muted" }, "Google will ask you to allow Inbox Triage to “view and modify” Gmail. " +
        "That is the narrowest permission Google offers for adding labels; the app only ever adds or removes its own labels.")),
  ));
}

function renderOAuthSetup() {
  if (STATE.hosted) {
    return mount(el("div", { class: "hero card" }, el("h2", {}, "Almost ready"),
      el("p", { class: "muted" }, "The person running this server still needs to connect it to Google. Please check back soon.")));
  }
  const box = el("textarea", { placeholder: '{"installed": {"client_id": "…", "client_secret": "…"}}', "aria-label": "OAuth client JSON" });
  const save = async () => {
    try { await api("oauth-client", "POST", { json: box.value }); toast("Google sign-in is ready."); await refresh(); }
    catch (err) { toast(err.message); }
  };
  mount(el("div", { class: "card stack" },
    el("h2", {}, "One-time setup for whoever runs this app"),
    el("p", { class: "muted" }, "People who use Inbox Triage just click “Sign in with Google”. For that to work, the person running " +
      "this copy connects it to Google once. If you downloaded a packaged build, this step is already done and you won't see this."),
    el("ol", { class: "small" },
      el("li", {}, "Open ", el("a", { href: "https://console.cloud.google.com/projectcreate", target: "_blank", rel: "noopener" }, "Google Cloud console"), " and create a project."),
      el("li", {}, "Enable the ", el("a", { href: "https://console.cloud.google.com/apis/library/gmail.googleapis.com", target: "_blank", rel: "noopener" }, "Gmail API"), "."),
      el("li", {}, "In ", el("b", {}, "Google Auth Platform"), ", set up branding and audience, then click ", el("b", {}, "Publish app"), " (otherwise logins expire after 7 days)."),
      el("li", {}, "Create a client: ", el("b", {}, "Clients → Create client → Desktop app"), ", and download the JSON."),
      el("li", {}, "Paste the JSON below.")),
    box, el("div", { class: "row" }, el("button", { class: "btn primary", onclick: save }, "Save and continue"))));
}

// ---------------------------------------------------------------- onboarding
function renderOnboarding(a) {
  const st = (onboard[a.email] ||= { step: 1, emails: null, tags: {}, notes: "", proposed: null, days: 7, preview: true,
    schedule: { frequency: "daily", hour: 7, weekday: 0 } });
  const steps = ["Choose your AI", "Tell us what matters", "Timeframe & schedule"];
  const header = el("div", { class: "steps" }, steps.map((s, i) =>
    el("div", { class: "step" + (i + 1 === st.step ? " now" : i + 1 < st.step ? " done" : "") }, `${i + 1}. ${s}`)));
  const body = st.step === 1 ? stepProvider(a, st) : st.step === 2 ? stepContext(a, st) : stepSchedule(a, st);
  mount(el("div", {}, el("h2", {}, `Welcome, ${a.email}`),
    el("p", { class: "muted" }, "Three quick steps. You can change everything later."), header, body));
}

function stepProvider(a, st) {
  let chosen = a.settings.provider || (STATE.providers.find((p) => p.available && p.id !== "rules")?.id) || "openrouter";
  const model = el("input", { type: "text", value: a.settings.model || "", placeholder: "Default model (recommended)" });
  const key = el("input", { type: "password", placeholder: "Paste API key", autocomplete: "off" });
  const keyRow = el("div", { class: "stack" });
  const opts = el("div", { class: "options" });
  const blurb = {
    openrouter: "Recommended. One key for OpenAI, Claude, Gemini, Llama and more. Easiest way to try different models.",
    openai: "OpenAI's API (a ChatGPT subscription doesn't include API access; you need an API key).",
    anthropic: "Claude by Anthropic. Strong at spotting phishing and what needs you.",
    ollama: "Runs on this computer with Ollama. Mail never leaves your machine.",
    jev: "Jev by typesafe.ai, a small model built for yes/no judgments.",
    rules: "No AI at all. Uses Gmail's own categories. Only labels obvious promotions.",
  };
  const drawKey = () => {
    const p = providerInfo(chosen);
    fill(keyRow, );
    if (p.key_env && !STATE.hosted) {
      keyRow.append(el("label", { class: "small muted" }, p.available ? `API key saved (${p.key_env}). Paste a new one to replace it.` : `API key (${p.key_env})`), key);
    } else if (p.key_env && STATE.hosted && !p.available) {
      keyRow.append(el("p", { class: "notice" }, "This server hasn't enabled that provider yet."));
    }
    if (chosen !== "rules") keyRow.append(el("label", { class: "small muted" }, "Model (optional)"), model);
  };
  const draw = () => {
    fill(opts, ...byOrder(STATE.providers).map((p) => el("button", { type: "button", class: "option" + (p.id === chosen ? " selected" : ""),
      onclick: () => { chosen = p.id; draw(); } }, el("div", { class: "t" }, p.label), el("div", { class: "small muted" }, blurb[p.id] || ""),
      p.available ? el("span", { class: "chip" }, "ready") : null)));
    drawKey();
  };
  draw();
  const next = async () => {
    try {
      if (key.value.trim()) await api("keys", "PUT", { provider: chosen, key: key.value.trim() });
      await api(acctPath(a.email, "settings"), "PUT", { provider: chosen, model: model.value.trim() });
      STATE = await api("state");
      if (!providerInfo(chosen).available) return toast("Add an API key for this provider first.");
      st.step = 2; render();
    } catch (err) { toast(err.message); }
  };
  return el("div", { class: "card stack" }, el("h3", {}, "Which AI should read your mail?"),
    el("p", { class: "small muted" }, "It only sees the sender's domain, subject, and a short cleaned excerpt, and answers yes/no questions. Your labels are decided by fixed local rules."),
    opts, keyRow, el("div", { class: "row" }, el("button", { class: "btn primary", onclick: next }, "Continue")));
}

function ruleForEmail(m, action) {
  const personal = FREEMAIL.has(m.domain);
  return personal ? { kind: "sender", value: m.address, action, note: m.subject.slice(0, 80) }
                  : { kind: "domain", value: m.domain, action, note: m.subject.slice(0, 80) };
}

function stepContext(a, st) {
  const list = el("ul", { class: "mail", "aria-label": "Recent emails" }, el("li", { class: "muted" }, "Loading your recent emails…"));
  const notes = el("textarea", { value: st.notes, placeholder: "Talk or type as you look through your emails. For example:\n" +
    "“#2 is from my kids' school — that's always important. I don't care about real estate emails. " +
    "The bank alerts matter. Newsletters from shops can wait.”", "aria-label": "Your notes", oninput: () => (st.notes = notes.value) });
  const proposedBox = el("div", { class: "stack" });
  const mic = el("button", { class: "btn mic", type: "button" }, "🎤 Dictate");
  setupDictation(mic, notes, st);

  const drawList = () => {
    if (!st.emails) return;
    if (!st.emails.length) return fill(list, el("li", { class: "muted" }, "No recent inbox mail found."));
    fill(list, ...st.emails.map((m, i) => {
      const tag = st.tags[m.id];
      const mark = (action) => { st.tags[m.id] = st.tags[m.id] === action ? undefined : action; drawList(); };
      return el("li", {},
        el("div", { class: "from" }, el("span", { class: "num" }, `#${i + 1}`), m.from || m.address),
        el("div", { class: "subj" }, m.subject || "(no subject)"),
        el("div", { class: "snip" }, m.snippet),
        el("div", { class: "acts" },
          el("button", { class: "btn small good", type: "button", onclick: () => mark("important") }, tag === "important" ? "✓ Important" : "Important"),
          el("button", { class: "btn small bad", type: "button", onclick: () => mark("not_important") }, tag === "not_important" ? "✓ Not important" : "Not important"),
          el("button", { class: "btn small ghost", type: "button", onclick: () => insertRef(notes, st, i + 1, m) }, "Mention in notes")));
    }));
  };
  if (!st.emails) {
    api(acctPath(a.email, "emails") + "?days=14").then((d) => { st.emails = d.emails; drawList(); })
      .catch((err) => fill(list, el("li", { class: "err" }, err.message)));
  } else drawList();

  const interpret = async (btn) => {
    btn.disabled = true; btn.textContent = "Reading your notes…";
    try {
      const refs = (st.emails || []).map((m) => ({ from: m.from, domain: m.domain, subject: m.subject }));
      st.proposed = await api(acctPath(a.email, "interpret"), "POST", { notes: st.notes, emails: refs });
      drawProposed();
    } catch (err) { toast(err.message); }
    btn.disabled = false; btn.textContent = "Turn my notes into rules";
  };
  const interpretBtn = el("button", { class: "btn", type: "button", onclick: (e) => interpret(e.currentTarget) }, "Turn my notes into rules");
  if (!providerInfo(a.settings.provider)?.interprets) {
    interpretBtn.disabled = true;
    interpretBtn.title = "Choose Claude, OpenAI, OpenRouter or Ollama to interpret notes";
  }

  const allRules = () => {
    const quick = Object.entries(st.tags).filter(([, v]) => v).map(([id, action]) => ruleForEmail(st.emails.find((m) => m.id === id), action));
    return [...(st.proposed?.rules || []), ...quick];
  };
  const drawProposed = () => {
    const rules = allRules();
    fill(proposedBox, 
      el("h3", {}, "Rules we'll use"),
      rules.length ? el("div", {}, rules.map((r) => el("div", { class: "rule" },
        el("span", { class: "tagged " + r.action }, r.action === "important" ? "Important" : "Can wait"),
        el("span", { class: "v" }, `${r.kind}: ${r.value}`), el("span", { class: "small muted note" }, r.note || "")))) :
        el("p", { class: "small muted" }, "Tag a few emails or write some notes, then turn them into rules."),
      st.proposed?.summary ? el("p", { class: "notice small" }, "What the assistant will keep in mind: ", st.proposed.summary) : null);
  };
  drawProposed();
  const observer = () => drawProposed();
  list.addEventListener("click", observer);

  const save = async (skip) => {
    try {
      if (!skip) {
        const existing = await api(acctPath(a.email, "preferences"));
        const rules = [...existing.rules, ...allRules()];
        const summary = [existing.summary, st.proposed?.summary].filter(Boolean).join(" ");
        await api(acctPath(a.email, "preferences"), "PUT", { rules, summary });
      }
      st.step = 3; render();
    } catch (err) { toast(err.message); }
  };
  return el("div", {},
    el("div", { class: "grid2" },
      el("div", { class: "card stack" }, el("h3", {}, "Your recent emails"),
        el("p", { class: "small muted" }, "Tap Important or Not important on a few. These stay on your computer as rules."), list),
      el("div", { class: "card stack" }, el("h3", {}, "Ramble about your inbox"),
        el("p", { class: "small muted" }, "Say what matters and what doesn't, in your own words. Mention emails by number."),
        notes, el("div", { class: "row" }, mic, interpretBtn),
        el("p", { class: "small muted" }, "Dictation uses your browser's speech recognition (in Chrome, audio goes to Google). Your notes go to the AI you chose, once, to create rules."),
        proposedBox)),
    el("div", { class: "row" },
      el("button", { class: "btn ghost", onclick: () => { st.step = 1; render(); } }, "Back"),
      el("button", { class: "btn", onclick: () => save(true) }, "Skip for now"),
      el("button", { class: "btn primary", onclick: () => save(false) }, "Save rules and continue")));
}

function insertRef(textarea, st, n, m) {
  const ref = `#${n} (${m.domain || m.address}: ${m.subject.slice(0, 50)}) `;
  const at = textarea.selectionStart ?? textarea.value.length;
  textarea.value = textarea.value.slice(0, at) + ref + textarea.value.slice(at);
  st.notes = textarea.value; textarea.focus();
}

function setupDictation(button, textarea, st) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { button.disabled = true; button.title = "Dictation isn't supported in this browser; type instead."; return; }
  let rec = null;
  button.addEventListener("click", () => {
    if (rec) { rec.stop(); return; }
    rec = new SR(); rec.continuous = true; rec.interimResults = false; rec.lang = navigator.language || "en-US";
    rec.onresult = (ev) => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) if (ev.results[i].isFinal)
        textarea.value += (textarea.value && !textarea.value.endsWith(" ") ? " " : "") + ev.results[i][0].transcript.trim() + " ";
      st.notes = textarea.value;
    };
    rec.onend = () => { rec = null; button.classList.remove("live"); button.textContent = "🎤 Dictate"; };
    rec.onerror = (e) => toast("Dictation stopped: " + e.error);
    rec.start(); button.classList.add("live"); button.textContent = "■ Stop";
  });
}

function scheduleFields(sched, onChange) {
  const freq = el("select", { "aria-label": "How often", onchange: () => { sched.frequency = freq.value; draw(); onChange?.(); } },
    STATE.frequencies.map((f) => el("option", { value: f, selected: f === sched.frequency }, FREQ_LABEL[f])));
  const hour = el("select", { "aria-label": "Hour", onchange: () => { sched.hour = +hour.value; onChange?.(); } },
    [...Array(24).keys()].map((h) => el("option", { value: h, selected: h === sched.hour }, `${String(h).padStart(2, "0")}:00`)));
  const day = el("select", { "aria-label": "Weekday", onchange: () => { sched.weekday = +day.value; onChange?.(); } },
    WEEKDAYS.map((d, i) => el("option", { value: i, selected: i === sched.weekday }, d)));
  const wrap = el("div", { class: "row" });
  const draw = () => fill(wrap, freq,
    ["daily", "weekly", "monthly"].includes(sched.frequency) ? el("span", { class: "muted" }, "at") : null,
    ["daily", "weekly", "monthly"].includes(sched.frequency) ? hour : null,
    sched.frequency === "weekly" ? el("span", { class: "muted" }, "on") : null,
    sched.frequency === "weekly" ? day : null);
  draw();
  return wrap;
}

function stepSchedule(a, st) {
  const windows = el("div", { class: "options" });
  const drawWindows = () => fill(windows, ...WINDOWS.map(([d, label]) => el("button", {
    type: "button", class: "option" + (st.days === d ? " selected" : ""), onclick: () => { st.days = d; drawWindows(); } },
    el("div", { class: "t" }, label), el("div", { class: "small muted" }, d > 7 ? "Takes a few minutes" : "Quick"))));
  drawWindows();
  const preview = el("input", { type: "checkbox", checked: st.preview, onchange: () => (st.preview = preview.checked), id: "preview" });
  const finish = async (btn) => {
    btn.disabled = true;
    try {
      await api(acctPath(a.email, "settings"), "PUT", { schedule: st.schedule, onboarded: true });
      await api(acctPath(a.email, "run"), "POST", { days: st.days, dry_run: st.preview });
      toast(st.preview ? "Preview started — nothing in Gmail will change." : "Sorting started.");
      await refresh();
    } catch (err) { toast(err.message); btn.disabled = false; }
  };
  return el("div", { class: "card stack" },
    el("h3", {}, "Which emails should we sort first?"), windows,
    el("label", { class: "row", for: "preview" }, preview, "Preview only (show what would happen, don't add labels yet)"),
    el("h3", {}, "Keep it sorted automatically"),
    scheduleFields(st.schedule),
    el("p", { class: "small muted" }, "Scheduled runs happen while Inbox Triage is running on this computer (or on your server). " +
      "Each run picks up from where the last one stopped."),
    el("div", { class: "row" },
      el("button", { class: "btn ghost", onclick: () => { st.step = 2; render(); } }, "Back"),
      el("button", { class: "btn primary", onclick: (e) => finish(e.currentTarget) }, "Finish and start")));
}

// ---------------------------------------------------------------- dashboard
function renderDashboard(a) {
  const job = a.job;
  const last = a.last_run;
  const stats = el("div", { class: "grid4" },
    stat("Last run", last ? ago(last.started) : "never", last ? (last.status === "ok" ? `${last.processed} emails · ${last.gmail_changes} labeled` : last.error || "") : ""),
    stat("Next run", a.next_run ? when(a.next_run) : "—", scheduleText(a.settings.schedule)),
    stat("AI", providerInfo(a.settings.provider)?.label || "Default", a.settings.model || "default model"),
    stat("Mode", a.settings.dry_run ? "Preview only" : "Adding labels", `${a.rules} personal rules`));

  const days = el("select", { "aria-label": "Window" }, el("option", { value: "" }, "New mail since last run"),
    WINDOWS.map(([d, l]) => el("option", { value: d }, l)));
  const dry = el("input", { type: "checkbox", id: "dry" });
  const running = job && job.status === "running";
  const runBtn = el("button", { class: "btn primary", disabled: running, onclick: async () => {
    try { await api(acctPath(a.email, "run"), "POST", { days: days.value ? +days.value : null, dry_run: dry.checked }); await refresh(); }
    catch (err) { toast(err.message); } } }, running ? "Running…" : "Run now");
  const jobLine = job ? el("p", { class: "small " + (job.status === "running" ? "run" : job.status === "ok" ? "ok" : "err") },
    job.status === "running" ? `Sorting started ${ago(job.started)}…` :
      job.status === "ok" ? `Last manual run finished: ${job.result.processed || 0} emails, ${job.result.gmail_changes || 0} labels changed.` :
        `Run failed: ${job.result.message || job.result.error}`) : null;
  if (running) pollTimer = setTimeout(refresh, 2500);

  const history = el("div", {}, el("p", { class: "muted small" }, "Loading history…"));
  const decisions = el("div", {});
  api(acctPath(a.email, "history")).then((h) => { drawHistory(history, h.runs); drawDecisions(decisions, h.decisions, a.email); })
    .catch((err) => fill(history, el("p", { class: "err" }, err.message)));

  mount(el("div", {},
    stats,
    el("div", { class: "card stack" }, el("h2", {}, "Run"),
      el("div", { class: "row" }, days, el("label", { class: "row", for: "dry" }, dry, "Preview only"), runBtn), jobLine),
    el("div", { class: "card" }, el("h2", {}, "Recently labeled"), decisions),
    el("div", { class: "card" }, el("h2", {}, "Run history"), history),
    preferencesCard(a),
    settingsCard(a)));
}

function stat(k, v, sub) {
  return el("div", { class: "card stat" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "small muted" }, sub || ""));
}

function drawHistory(node, runs) {
  if (!runs.length) return fill(node, el("p", { class: "muted small" }, "No runs yet."));
  fill(node, el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["When", "Trigger", "Window", "Emails", "Labels changed", "Outcome", "Status"].map((h) => el("th", {}, h)))),
    el("tbody", {}, runs.map((r) => el("tr", {},
      el("td", {}, when(r.started)), el("td", {}, r.trigger), el("td", {}, r.days ? `${r.days} days` : "since last"),
      el("td", {}, r.processed ?? 0), el("td", {}, r.mode === "dry-run" ? "preview" : r.gmail_changes ?? 0),
      el("td", {}, Object.entries(r.outcomes || {}).map(([k, v]) => el("span", { class: "chip" }, `${OUTCOME_LABEL[k] || k}: ${v}`))),
      el("td", { class: r.status === "ok" ? "ok" : "err" }, r.status === "ok" ? (r.remaining ? "ok (more left)" : "ok") : r.message || r.error)))))));
}

function drawDecisions(node, items, email) {
  if (!items.length) return fill(node, el("p", { class: "muted small" }, "Nothing labeled yet."));
  fill(node, el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["From", "Subject", "Labels", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, items.map((d) => el("tr", {},
      el("td", {}, d.from || "—"), el("td", {}, d.subject || el("span", { class: "muted" }, "(no longer available)")),
      el("td", {}, (d.names || []).length ? d.names.map((n) => el("span", { class: "chip" }, n.replace("Triage/", "").replace("Topics/", ""))) : el("span", { class: "muted small" }, "unchanged")),
      el("td", {}, el("a", { href: `https://mail.google.com/mail/?authuser=${encodeURIComponent(email)}#all/${encodeURIComponent(d.id)}`, target: "_blank", rel: "noopener" }, "Open"))))))));
}

function preferencesCard(a) {
  const body = el("div", {}, el("p", { class: "muted small" }, "Loading…"));
  const load = () => api(acctPath(a.email, "preferences")).then((p) => {
    const remove = async (i) => { p.rules.splice(i, 1); await api(acctPath(a.email, "preferences"), "PUT", p); load(); };
    const kind = el("select", {}, ["domain", "sender", "keyword"].map((k) => el("option", { value: k }, k)));
    const value = el("input", { type: "text", placeholder: "school.example, boss@work.example, real estate" });
    const action = el("select", {}, el("option", { value: "important" }, "Important"), el("option", { value: "not_important" }, "Can wait"));
    const add = async () => {
      p.rules.push({ kind: kind.value, value: value.value.trim(), action: action.value, note: "" });
      const saved = await api(acctPath(a.email, "preferences"), "PUT", p);
      if (saved.rules.length === p.rules.length - 1) toast("That rule doesn't look valid.");
      load();
    };
    fill(body, 
      p.summary ? el("p", { class: "notice small" }, p.summary) : null,
      p.rules.length ? el("div", {}, p.rules.map((r, i) => el("div", { class: "rule" },
        el("span", { class: "tagged " + r.action }, r.action === "important" ? "Important" : "Can wait"),
        el("span", { class: "v" }, `${r.kind}: ${r.value}`), el("span", { class: "small muted note" }, r.note || ""),
        el("button", { class: "btn small ghost", onclick: () => remove(i) }, "Remove")))) : el("p", { class: "muted small" }, "No personal rules yet."),
      el("div", { class: "row" }, kind, value, action, el("button", { class: "btn", onclick: add }, "Add rule")));
  });
  load();
  return el("div", { class: "card stack" },
    el("div", { class: "row" }, el("h2", {}, "What matters to you"),
      el("button", { class: "btn small", onclick: () => { onboard[a.email] = { ...(onboard[a.email] || {}), step: 2, emails: null, tags: {}, notes: "", proposed: null,
        days: 7, preview: false, schedule: { ...a.settings.schedule } }; renderOnboarding(a); } }, "Teach it more")),
    el("p", { class: "small muted" }, "Sender and domain rules only apply to authenticated mail. Suspected phishing is never promoted, and security alerts are never pushed to Later."),
    body);
}

function settingsCard(a) {
  const sched = { ...a.settings.schedule };
  const provider = el("select", {}, byOrder(STATE.providers).map((p) => el("option", { value: p.id, selected: p.id === a.settings.provider }, p.label + (p.available ? "" : " (needs key)"))));
  const model = el("input", { type: "text", value: a.settings.model || "", placeholder: "Default model" });
  const preview = el("input", { type: "checkbox", checked: a.settings.dry_run, id: "pm" });
  const save = async () => {
    try {
      await api(acctPath(a.email, "settings"), "PUT", { provider: provider.value, model: model.value.trim(), dry_run: preview.checked, schedule: sched });
      toast("Settings saved."); await refresh();
    } catch (err) { toast(err.message); }
  };
  const disconnect = async () => {
    if (!confirm(`Disconnect ${a.email}? Existing labels stay in Gmail; Inbox Triage stops until you sign in again.`)) return;
    await api(`accounts/${encodeURIComponent(a.email)}`, "DELETE"); await refresh();
  };
  return el("div", { class: "card stack" }, el("h2", {}, "Settings"),
    el("div", { class: "grid2" },
      el("div", { class: "stack" }, el("label", { class: "small muted" }, "AI provider"), provider, el("label", { class: "small muted" }, "Model"), model),
      el("div", { class: "stack" }, el("label", { class: "small muted" }, "Schedule"), scheduleFields(sched),
        el("label", { class: "row", for: "pm" }, preview, "Preview mode (never change Gmail)"))),
    el("div", { class: "row" }, el("button", { class: "btn primary", onclick: save }, "Save settings"),
      el("button", { class: "btn ghost", onclick: () => { onboard[a.email] = null; a.settings.onboarded = false; renderOnboarding(a); } }, "Change AI provider or key"),
      el("button", { class: "btn bad", onclick: disconnect }, "Disconnect account")));
}

window.addEventListener("hashchange", refresh);
refresh().catch((err) => mount(el("div", { class: "card err" }, err.message)));
