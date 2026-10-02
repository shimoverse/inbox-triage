// Inbox Triage bar: a small live dashboard at the top of Gmail. It draws what the Inbox Triage server
// already knows (label counts, recent decisions, run status) and nudges it to sort new mail; the server
// does all the reading and labeling. Everything is built with createElement/textContent inside a closed
// shadow root, so Gmail's CSS and Trusted Types rules can't reach it, and it never changes Gmail's page.
(() => {
  "use strict";
  // Only the main Gmail window: not a frame, and not a popout compose or print view (?view=cm, ?view=pt).
  if (window.top !== window || /[?&]view=/.test(location.search)) return;
  // A bar left by an earlier version of this script (the extension was updated or reloaded) is replaced.
  for (const old of document.querySelectorAll("div[data-inbox-triage]")) old.remove();

  const L = self.InboxTriageLib;
  const REFRESH_MS = 60 * 1000;  // while the tab is visible
  const RUNNING_MS = 5 * 1000;   // while a run is sorting mail
  const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } })();
  const SVG = "http://www.w3.org/2000/svg";
  const CSS = `
    :host { all: initial; }
    .bar { --surface: #fcfcfb; --ink: #1f1f1f; --ink-2: #52514e; --muted: #6b6a66; --line: #e6e4df; --grid: #efeee9;
      --needs: #eb6834; --other: #8d8b85; --focus: #1f56c9; --live: #1e8e3e; --warn: #b06000;
      position: relative; box-sizing: border-box; margin: 8px 16px 8px 0; padding: 6px 12px; border: 1px solid var(--line);
      border-radius: 12px; background: var(--surface); color: var(--ink);
      font: 13px/1.45 "Google Sans", Roboto, "Segoe UI", Arial, sans-serif; }
    .bar.dark { --surface: #1a1a19; --ink: #f2f2f0; --ink-2: #c3c2b7; --muted: #a3a29b; --line: #34332f; --grid: #2a2a28;
      --needs: #d95926; --other: #8a8983; --focus: #8fb0ff; --live: #6fd3a0; --warn: #ffb870; }
    .bar.floating { position: fixed; left: 16px; bottom: 16px; z-index: 2147483000; margin: 0;
      max-width: min(760px, calc(100vw - 32px)); box-shadow: 0 6px 24px rgba(0, 0, 0, .18); }
    .bar:empty { display: none; }
    .head { display: flex; align-items: center; gap: 8px 12px; flex-wrap: wrap; min-height: 32px; }
    .brand { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; white-space: nowrap; }
    .brand svg { flex: none; }
    .muted { color: var(--ink-2); }
    .small { font-size: 12px; }
    .chips { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .chips .span { color: var(--ink-2); font-size: 12px; margin-right: 2px; }
    .chip { display: inline-flex; align-items: baseline; gap: 6px; padding: 1px 8px; border-radius: 4px; font-size: 12.5px;
      line-height: 20px; text-decoration: none; white-space: nowrap; }
    .chip b { font-weight: 700; }
    .chip:hover { filter: brightness(.96); }
    .status { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; color: var(--ink-2); }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--other); flex: none; }
    .dot.live { background: var(--live); } .dot.warn { background: var(--warn); }
    button { font: inherit; color: inherit; cursor: pointer; border-radius: 8px; }
    .btn { padding: 4px 12px; border: 1px solid var(--line); background: transparent; font-weight: 600; }
    .btn.primary { background: var(--ink); color: var(--surface); border-color: var(--ink); }
    .btn:disabled { opacity: .6; cursor: default; }
    .icon { display: inline-grid; place-items: center; width: 28px; height: 28px; border: 0; background: transparent; }
    .icon:hover, .btn:hover:not(:disabled) { background: var(--grid); }
    .btn.primary:hover:not(:disabled) { background: var(--ink); filter: brightness(1.2); }
    a:focus-visible, button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
    .panel { display: grid; grid-template-columns: minmax(280px, 1fr) minmax(280px, 1.25fr); gap: 12px 24px;
      margin-top: 6px; padding: 10px 0 6px; border-top: 1px solid var(--line); }
    @media (max-width: 1000px) { .panel { grid-template-columns: 1fr; } }
    h3 { margin: 0 0 6px; font-size: 13px; font-weight: 600; }
    .legend { display: flex; gap: 12px; margin: 0 0 4px; color: var(--ink-2); font-size: 12px; }
    .legend span { display: inline-flex; align-items: center; gap: 6px; }
    .sw { width: 10px; height: 10px; border-radius: 2px; }
    .chart { position: relative; }
    svg text { fill: var(--muted); font-size: 11px; font-family: inherit; }
    svg .mid { text-anchor: middle; }
    .grid { stroke: var(--grid); stroke-width: 1; }
    .base { stroke: var(--line); stroke-width: 1; }
    .seg.needs, .sw.needs, .key.needs { fill: var(--needs); background: var(--needs); }
    .seg.other, .sw.other, .key.other { fill: var(--other); background: var(--other); }
    .hit { fill: transparent; }
    .col { outline: none; cursor: default; }
    .col:hover .seg, .col:focus .seg { opacity: .82; }
    .col:focus-visible .hit { stroke: var(--focus); stroke-width: 2; }
    .tip { position: absolute; z-index: 3; pointer-events: none; min-width: 150px; padding: 8px 10px; border-radius: 8px;
      background: var(--surface); border: 1px solid var(--line); box-shadow: 0 4px 16px rgba(0, 0, 0, .14); font-size: 12px; }
    .tip .lead { font-size: 13px; } .tip .lead b { font-size: 14px; }
    .tip .row { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
    .tip .row b { margin-left: auto; padding-left: 12px; }
    .key { width: 12px; height: 2px; border-radius: 1px; }
    .recent { list-style: none; margin: 0; padding: 0; display: grid; align-items: center; gap: 6px 10px;
      grid-template-columns: max-content minmax(0, 11em) minmax(0, 1fr) max-content; }
    .recent li { display: contents; }
    .recent .chip { justify-self: start; }
    .recent a { color: inherit; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .recent a:hover { text-decoration: underline; }
    .recent .from { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .recent .when { color: var(--muted); font-size: 12px; white-space: nowrap; }
    .recent .chip { font-size: 11.5px; line-height: 18px; padding: 0 6px; }
    .actions { grid-column: 1 / -1; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .note { color: var(--ink-2); font-size: 12px; }
    .err { color: var(--warn); }
    .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `;

  // ---------------------------------------------------------------- DOM helpers (never HTML strings)
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    setAttrs(node, attrs);
    for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function s(tag, attrs, ...children) {
    const node = document.createElementNS(SVG, tag);
    setAttrs(node, attrs);
    for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
    return node;
  }
  function setAttrs(node, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  function fill(node, ...children) { node.replaceChildren(...children.flat().filter((c) => c != null && c !== false)); }

  // ---------------------------------------------------------------- the host, in its own shadow root
  const host = document.createElement("div");
  host.dataset.inboxTriage = "1";
  host.style.setProperty("all", "initial", "important");
  host.style.setProperty("display", "block", "important");
  const shadow = host.attachShadow({ mode: "closed" });
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    shadow.adoptedStyleSheets = [sheet];
  } catch {
    shadow.append(h("style", {}, CSS));
  }
  const root = h("section", { class: "bar", "aria-label": "Inbox Triage" });
  shadow.append(root);

  const state = { email: "", unread: null, connected: null, server: "", summary: null, error: "", notice: "",
    expanded: false, busy: false, alive: true };
  let refreshTimer = 0;
  let placeTimer = 0;
  let titleWatch = null;
  let drawn = "";  // what the bar currently shows, so a refresh that changes nothing leaves the DOM (and focus) alone

  // Above Gmail's main column; if Gmail's layout ever changes, a small floating bar instead.
  function place() {
    const main = document.querySelector('div[role="main"]');
    if (main) {
      if (host.parentNode !== main) main.prepend(host);
      root.classList.remove("floating");
    } else {
      if (host.parentNode !== document.body) document.body.append(host);
      root.classList.add("floating");
    }
  }

  function isDark() {
    for (const node of [document.querySelector('div[role="main"]'), document.body]) {
      if (!node) continue;
      const parts = (getComputedStyle(node).backgroundColor.match(/[\d.]+/g) || []).map(Number);
      if (parts.length >= 3 && (parts.length < 4 || parts[3] > 0.5)) {
        return (0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]) / 255 < 0.45;
      }
    }
    return matchMedia("(prefers-color-scheme: dark)").matches;
  }

  // ---------------------------------------------------------------- talking to the service worker
  async function ask(msg) {
    let res;
    try {
      res = await chrome.runtime.sendMessage(msg);
    } catch (err) {
      if (!(chrome.runtime && chrome.runtime.id)) stop();  // the extension was updated or removed
      // Chrome ends the worker when one request runs past five minutes (a long Google sign-in, say).
      if (/message port closed|receiving end does not exist/i.test(String(err && err.message))) {
        throw new Error(msg.type === "connect" ? "Sign-in took too long. Please try again" : "Inbox Triage didn't answer; it will try again shortly");
      }
      throw err;
    }
    if (res && res.error) throw Object.assign(new Error(res.error), { status: res.status });
    return res || {};
  }
  function stop() {
    state.alive = false;
    clearTimeout(refreshTimer);
    clearInterval(placeTimer);
    if (titleWatch) titleWatch.disconnect();
    host.remove();
  }

  async function start() {
    clearTimeout(refreshTimer);
    try {
      const status = await ask({ type: "status", email: state.email });
      Object.assign(state, { connected: status.connected, server: status.server,
        expanded: Boolean(status.prefs && status.prefs.expanded), error: status.server ? "" :
          "Set your Inbox Triage server in the extension's settings" });
    } catch (err) {
      state.error = err.message;
    }
    render();
    if (state.connected) {
      await refresh();
      nudge();
    }
  }

  async function refresh() {
    clearTimeout(refreshTimer);
    if (!state.alive || !state.email) return;
    const email = state.email;
    try {
      const res = await ask({ type: "summary", email, tz: TZ });
      if (email !== state.email) return;  // the tab moved to another account meanwhile: not this one's answer
      state.connected = res.connected;
      state.summary = res.connected ? res.data : null;
      state.error = "";
    } catch (err) {
      if (email !== state.email) return;
      state.error = err.message;
    }
    render();
    const wait = state.summary && state.summary.running ? RUNNING_MS : REFRESH_MS;
    refreshTimer = setTimeout(() => { if (document.visibilityState === "visible") refresh(); }, wait);
  }

  // Ask the server to sort new mail. The worker and the server each allow this once a minute.
  async function nudge(force = false) {
    if (!state.connected) return;
    try {
      const res = await ask({ type: "sync", email: state.email, force });
      const data = res.data || {};
      if (force) state.notice = data.started ? "Sorting new mail…" : data.reason === "running" ? "Already sorting."
        : data.reason === "paused" ? "Sorting is paused for now; it carries on by itself."
        : data.reason === "recent" ? "It sorted a moment ago; new mail will be picked up shortly." : (data.message || "");
      if (data.started) setTimeout(refresh, 1500);
      else if (force) render();
    } catch (err) {
      if (force) { state.notice = err.message; render(); }
    }
  }

  async function connect() {
    state.busy = true;
    state.error = "";
    render();
    try {
      await ask({ type: "connect", email: state.email });  // a token for another account is refused by the worker
      state.connected = true;
      await refresh();
      nudge(true);
    } catch (err) {
      state.error = err.message;
    }
    state.busy = false;
    render();
  }

  async function toggle() {
    state.expanded = !state.expanded;
    render();
    try { await ask({ type: "prefs", set: { expanded: state.expanded } }); } catch { /* the view already changed */ }
  }

  // ---------------------------------------------------------------- views
  function mark() {
    return s("svg", { width: 18, height: 18, viewBox: "0 0 28 28", "aria-hidden": "true" },
      s("rect", { width: 28, height: 28, rx: 7, fill: "#1D1B17" }),
      s("path", { d: "M8 10h12", stroke: "#F4A06B", "stroke-width": 2.6, "stroke-linecap": "round" }),
      s("path", { d: "M8 14.5h8", stroke: "#8FB0FF", "stroke-width": 2.6, "stroke-linecap": "round" }),
      s("path", { d: "M8 19h4.5", stroke: "#C3A6FF", "stroke-width": 2.6, "stroke-linecap": "round" }));
  }
  function brand() { return h("span", { class: "brand" }, mark(), "Inbox Triage"); }

  function chip(label, count, small) {
    const node = h("a", { class: "chip", href: L.labelHash(label.gmail), title: `Open ${label.gmail} in Gmail` },
      label.name, count == null ? null : h("b", {}, String(count)));
    node.style.backgroundColor = label.bg;  // CSSOM, not a style attribute: fine under any page CSP
    node.style.color = label.fg;
    if (small) node.classList.add("small");
    return node;
  }

  function chips(summary) {
    const totals = summary.totals || {};
    return h("div", { class: "chips", role: "group", "aria-label": "Labeled in the last 7 days" },
      h("span", { class: "span" }, "Last 7 days"),
      L.LABELS.filter((l) => (l.key !== "junk" && l.key !== "shopping") || totals[l.key])
        .map((l) => chip(l, totals[l.key] || 0)));
  }

  function statusView(summary) {
    const line = L.statusLine(summary, Date.now() / 1000);
    // Without a Jev key nothing gets sorted; the status itself opens the app, where the key is added.
    const text = state.error ? state.error : line.text;
    const body = !state.error && !summary.jev_connected
      ? h("button", { class: "btn", type: "button", onclick: () => ask({ type: "openApp", email: state.email }) }, text)
      : h("span", {}, text);
    return h("span", { class: "status", role: "status" }, h("span", { class: "dot " + line.kind, "aria-hidden": "true" }), body);
  }

  function toggleButton() {
    return h("button", { class: "icon", type: "button", "aria-expanded": String(state.expanded),
      "aria-label": state.expanded ? "Hide details" : "Show details", title: state.expanded ? "Hide details" : "Show details",
      onclick: toggle },
    s("svg", { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" },
      s("path", { d: state.expanded ? "M4 10l4-4 4 4" : "M4 6l4 4 4-4", fill: "none", stroke: "currentColor",
        "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" })));
  }

  function connectView() {
    return h("div", { class: "head" }, brand(),
      h("span", { class: "muted" }, "Sort this inbox automatically: see what needs you, and let the rest wait."),
      h("span", { class: "status" },
        state.error ? h("span", { class: "err" }, state.error) : null,
        h("button", { class: "btn primary", type: "button", disabled: state.busy || !state.server, onclick: connect },
          state.busy ? "Connecting…" : "Connect Gmail")),
      state.server ? null : h("button", { class: "btn", type: "button", onclick: () => ask({ type: "options" }) }, "Settings"));
  }

  function weekday(day, long) {
    const date = new Date(day + "T12:00:00");
    return date.toLocaleDateString(undefined, long ? { weekday: "short", day: "numeric", month: "short" } : { weekday: "short" });
  }

  // Rounded 4px at the data end (the top), square at the baseline.
  function barPath(x, y, w, height, round) {
    const r = round ? Math.min(4, w / 2, height) : 0;
    return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + height}Z`;
  }

  function chart(days) {
    const cols = L.dayColumns(days);
    const W = 340, H = 136, top = 16, bottom = 22, left = 4, right = 4;
    const band = (W - left - right) / Math.max(1, cols.length);
    const barW = Math.min(24, band * 0.55);
    const max = L.niceMax(Math.max(1, ...cols.map((c) => c.total)));
    const span = H - bottom - top;
    const box = h("div", { class: "chart" });
    const tip = h("div", { class: "tip", hidden: true });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
      "aria-label": "Emails labeled per day, last 7 days: " + cols.map((c) => `${weekday(c.day)} ${c.total}`).join(", ") },
    s("line", { class: "grid", x1: left, x2: W - right, y1: top, y2: top }),
    s("text", { x: left, y: top - 5 }, String(max)),
    s("line", { class: "base", x1: left, x2: W - right, y1: H - bottom + 0.5, y2: H - bottom + 0.5 }));
    const showTip = (c, x) => {
      const others = L.ATTENTION.filter((k) => k !== "needs_you" && c.row[k])
        .map((k) => `${L.LABELS.find((l) => l.key === k).name} ${c.row[k]}`).join(" · ");
      fill(tip,
        h("div", { class: "lead" }, h("b", {}, String(c.total)), ` labeled · ${weekday(c.day, true)}`),
        h("div", { class: "row" }, h("span", { class: "key needs" }), "Needs You", h("b", {}, String(c.needs))),
        h("div", { class: "row" }, h("span", { class: "key other" }), "Other labels", h("b", {}, String(c.other))),
        others ? h("div", { class: "small muted" }, others) : null);
      tip.hidden = false;
      tip.style.left = `${Math.min(Math.max(0, (x / W) * 100 - 20), 55)}%`;
      tip.style.top = "8px";
    };
    cols.forEach((c, i) => {
      const x = left + band * i + (band - barW) / 2;
      const label = `${weekday(c.day, true)}: ${c.total} labeled, ${c.needs} Needs You`;
      const g = s("g", { class: "col", tabindex: 0, role: "img", "aria-label": label,
        onpointerenter: () => showTip(c, x), onfocus: () => showTip(c, x),
        onpointerleave: () => { tip.hidden = true; }, onblur: () => { tip.hidden = true; } },
      s("rect", { class: "hit", x: left + band * i + 1, y: top, width: band - 2, height: span + bottom - 4, rx: 4 }));
      let base = H - bottom;
      const segs = [["needs", c.needs], ["other", c.other]].filter(([, v]) => v > 0);
      segs.forEach(([cls, v], j) => {
        const height = Math.max(2, (v / max) * span);
        const gap = j > 0 ? 2 : 0;  // a 2px surface gap between stacked segments
        g.append(s("path", { class: "seg " + cls, d: barPath(x, base - height, barW, height - gap, j === segs.length - 1) }));
        base -= height;
      });
      g.append(s("text", { class: "mid", x: x + barW / 2, y: H - 6 }, weekday(c.day)));
      svg.append(g);
    });
    // The same numbers as a table, for screen readers.
    const table = h("table", { class: "sr" }, h("caption", {}, "Emails labeled per day"),
      h("tr", {}, h("th", {}, "Day"), h("th", {}, "Needs You"), h("th", {}, "Other labels")),
      cols.map((c) => h("tr", {}, h("td", {}, weekday(c.day, true)), h("td", {}, String(c.needs)), h("td", {}, String(c.other)))));
    fill(box, svg, tip, table);
    return box;
  }

  function recentList(summary) {
    const now = Date.now() / 1000;
    const items = (summary.recent || []).slice(0, 6);
    if (!items.length) return h("p", { class: "note" }, "Labeled emails show up here after the first run.");
    return h("ul", { class: "recent" }, items.map((d) => {
      const label = L.LABELS.find((l) => (d.names || []).includes(l.gmail) && L.ATTENTION.includes(l.key))
        || L.LABELS.find((l) => (d.names || []).includes(l.gmail));
      const subject = d.subject || (summary.details === false ? "Details unavailable right now" : "(no subject)");
      return h("li", {}, label ? chip(label, null, true) : h("span"),
        h("span", { class: "from", title: d.from || "" }, L.senderName(d.from) || "—"),
        h("a", { href: "#all/" + encodeURIComponent(d.id), title: subject }, subject),
        h("span", { class: "when" }, d.ts ? L.ago(d.ts, now) : ""));
    }));
  }

  function panel(summary) {
    return h("div", { class: "panel" },
      h("div", {},
        h("h3", {}, "Labeled per day"),
        h("div", { class: "legend" }, h("span", {}, h("span", { class: "sw needs" }), "Needs You"),
          h("span", {}, h("span", { class: "sw other" }), "Other labels")),
        chart(summary.days)),
      h("div", {}, h("h3", {}, "Recently labeled"), recentList(summary)),
      h("div", { class: "actions" },
        h("button", { class: "btn", type: "button", onclick: () => nudge(true) }, "Sort new mail now"),
        h("button", { class: "btn", type: "button", onclick: () => ask({ type: "openApp", email: state.email }) }, "Rules and schedule"),
        h("button", { class: "btn", type: "button", onclick: () => ask({ type: "options" }) }, "Extension settings"),
        state.notice ? h("span", { class: "note", role: "status" }, state.notice) : null,
        summary.preview ? h("span", { class: "note" }, "Preview mode is on, so Gmail isn't changed.") : null));
  }

  function render() {
    if (!state.alive) return;
    place();
    root.classList.toggle("dark", isDark());
    const floating = root.classList.contains("floating");
    // A refresh that found nothing new must not rebuild the bar: that would drop keyboard focus and the chart tip.
    const key = JSON.stringify([state.email, state.connected, state.error, state.notice, state.busy, state.expanded,
      floating, state.summary, Math.floor(Date.now() / 60000)]);
    if (key === drawn) return;
    drawn = key;
    if (!state.email) return fill(root);
    if (state.connected === false || (state.connected === null && state.error)) return fill(root, connectView());
    if (!state.summary) {
      return fill(root, h("div", { class: "head" }, brand(), h("span", { class: "muted" }, state.error || "Loading…")));
    }
    fill(root, h("div", { class: "head" }, brand(), chips(state.summary), statusView(state.summary), floating ? null : toggleButton()),
      state.expanded && !floating ? panel(state.summary) : null);
  }

  // ---------------------------------------------------------------- watching Gmail
  // The tab title names the account and its unread count: "Inbox (12) - you@gmail.com - Gmail".
  function readTitle() {
    const email = L.accountFromTitle(document.title);
    const unread = L.unreadFromTitle(document.title);  // null while a thread is open: the count isn't shown then
    if (email && email !== state.email) {
      Object.assign(state, { email, unread, connected: null, summary: null, error: "", notice: "" });
      start();
    } else if (email && unread !== null && state.unread !== null && unread > state.unread) {
      state.unread = unread;
      nudge();  // new mail arrived: sort it now instead of waiting for the schedule
    } else if (unread !== null) {
      state.unread = unread;  // fewer unread, or the first count seen: nothing to sort
    }
  }

  // Gmail rewrites the <title> text; watch that node rather than everything under <head>.
  function watchTitle() {
    const node = document.querySelector("title");
    titleWatch = new MutationObserver(readTitle);
    if (node) titleWatch.observe(node, { childList: true, characterData: true, subtree: true });
    else titleWatch.observe(document.head || document.documentElement, { childList: true, subtree: true, characterData: true });
  }
  watchTitle();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.connected) {
      refresh();
      nudge();
    }
  });
  placeTimer = setInterval(() => { if (state.alive) place(); }, 1500);
  readTitle();
})();
