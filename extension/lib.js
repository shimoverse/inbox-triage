// Inbox Triage extension: small pure helpers shared by the service worker, the Gmail bar,
// the options page, and the Node tests (extension/test). No DOM, no chrome.* calls.
(function (root) {
  "use strict";

  // The labels the server adds, in display order, with Gmail's chip colours (the server sets the same
  // colours on the labels, so the bar's chips look like the ones on each email row).
  const LABELS = [
    ["needs_you", "Needs You", "Triage/Needs You", "#ffc8af", "#7a2e0b"],
    ["updates", "Updates", "Triage/Updates", "#c9daf8", "#1c4587"],
    ["for_you", "For You", "Triage/For You", "#e4d7f5", "#41236d"],
    ["later", "Later", "Triage/Later", "#e7e7e7", "#464646"],
    ["junk", "Junk", "Triage/Junk", "#f2b2a8", "#8a1c0a"],
    ["shopping", "Shopping", "Topics/Shopping", "#98d7e4", "#0d3b44"],
  ].map(([key, name, gmail, bg, fg]) => ({ key, name, gmail, bg, fg }));
  const ATTENTION = ["needs_you", "updates", "for_you", "later", "junk"];  // at most one of these per email

  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;

  // Gmail's tab title: "Inbox (12) - you@gmail.com - Gmail", or "<subject> - you@gmail.com - Gmail" with a
  // thread open. The account is the LAST address: a subject may well contain one ("Fwd: invoice from billing@…").
  function accountFromTitle(title) {
    const all = String(title || "").match(EMAIL);
    return all ? all[all.length - 1].toLowerCase() : "";
  }
  // The unread count, "(12)", follows a short folder name in the first part of the title. A number in a
  // subject ("Re: report (2024)") is not one: subjects are long or carry a "Re:"/"Fwd:" colon. Null when the
  // title shows no count, as it doesn't while a thread is open.
  function unreadFromTitle(title) {
    const head = String(title || "").split(" - ")[0];
    const match = head.match(/^([^(:]{1,40})\((\d[\d,.\s]*)\)\s*$/);
    return match ? parseInt(match[2].replace(/\D/g, ""), 10) || 0 : null;
  }

  // Gmail's own address for a label, e.g. #label/Triage%2FNeeds+You.
  function labelHash(gmailName) {
    return "#label/" + encodeURIComponent(gmailName).replace(/%20/g, "+");
  }

  // Only an https server, or one on this computer: a token must never travel in plain text.
  function serverOrigin(value) {
    let url;
    try { url = new URL(String(value || "").trim()); } catch { return ""; }
    const local = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) return "";
    if (url.username || url.password) return "";
    return url.origin;
  }

  function base64url(bytes) {
    let text = "";
    for (const b of bytes) text += String.fromCharCode(b);
    const b64 = typeof btoa === "function" ? btoa(text) : Buffer.from(text, "binary").toString("base64");
    return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function ago(ts, now) {
    const s = Math.max(0, Math.round(now - ts));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    const d = Math.round(s / 86400);
    return `${d} day${d === 1 ? "" : "s"} ago`;
  }
  function until(ts, now) {
    const s = Math.max(0, Math.round(ts - now));
    if (s < 60) return "in a moment";
    if (s < 3600) return `in ${Math.round(s / 60)} min`;
    if (s < 86400) return `in ${Math.round(s / 3600)} h`;
    const d = Math.round(s / 86400);
    return `in ${d} day${d === 1 ? "" : "s"}`;
  }

  // "Dana Lee <dana@example.org>" -> "Dana Lee"; a bare address stays as it is.
  function senderName(from) {
    const text = String(from || "").trim();
    const name = text.replace(/<[^>]*>/, "").trim().replace(/^"|"$/g, "").trim();
    return name || text.replace(/[<>]/g, "");
  }

  // One line saying what Inbox Triage is doing for this account right now.
  function statusLine(s, now) {
    if (!s) return { kind: "idle", text: "" };
    if (!s.jev_connected) return { kind: "warn", text: "Needs a Jev key before it can sort: open Inbox Triage to add one" };
    if (s.running) return { kind: "live", text: "Sorting new mail now…" };
    const last = s.last_run;
    if (last && last.status === "paused") {
      const why = last.reason === "sponsored_limit" ? "Today's free sorting allowance is used up"
        : last.reason === "jev_credits" ? "Jev is out of credits"
        : last.reason === "jev_unavailable" ? "Jev isn't answering right now" : "Gmail asked for a short break";
      if (s.resume_at) return { kind: "warn", text: `${why}. Carries on ${until(s.resume_at, now)}` };
      return { kind: "warn", text: `${why}. Open Inbox Triage and click Run now to carry on` };
    }
    if (last && last.status === "error") return { kind: "warn", text: "The last run didn't finish. Open Inbox Triage for details" };
    if (s.preview) return { kind: "idle", text: "Preview mode: labels aren't added to Gmail" };
    if (!last) return { kind: "live", text: "Getting ready to sort your recent mail" };
    const when = last.finished || last.started;
    return { kind: "live", text: when ? `Sorted ${ago(when, now)}` : "Sorted recently" };
  }

  // Columns for the 7-day chart: Needs You (the one that matters) against every other label.
  function dayColumns(days) {
    return (days || []).map((d) => {
      const needs = d.needs_you || 0;
      const other = ATTENTION.filter((k) => k !== "needs_you").reduce((n, k) => n + (d[k] || 0), 0);
      return { day: d.day, needs, other, total: needs + other, row: d };
    });
  }

  // A clean top for the value axis: 1, 2, 5, 10, 20, 50, …
  function niceMax(value) {
    if (value <= 1) return 1;
    const step = Math.pow(10, Math.floor(Math.log10(value)));
    for (const m of [1, 2, 5, 10]) if (m * step >= value) return m * step;
    return 10 * step;
  }

  const api = { LABELS, ATTENTION, accountFromTitle, unreadFromTitle, labelHash, serverOrigin, base64url,
    ago, until, senderName, statusLine, dayColumns, niceMax };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.InboxTriageLib = api;
})(typeof self !== "undefined" ? self : globalThis);
