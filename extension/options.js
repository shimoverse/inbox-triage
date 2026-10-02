"use strict";
// Extension options: which Inbox Triage server to use, and which Gmail accounts are connected to it.
const L = self.InboxTriageLib;
const input = document.getElementById("server");
const msg = document.getElementById("server-msg");
const list = document.getElementById("accounts");

function say(text, kind) { msg.textContent = text; msg.className = "msg " + (kind || ""); }

async function draw() {
  const { server, tokens } = await chrome.storage.local.get(["server", "tokens"]);
  const current = L.serverOrigin(server || self.INBOX_TRIAGE_DEFAULT_SERVER);
  input.value = current;
  // Every token, grouped by the server it was issued by: one for a server you no longer use can still be revoked.
  const byServer = new Map();
  for (const [key, entry] of Object.entries(tokens || {})) {
    const origin = key.slice(0, key.lastIndexOf(" "));
    if (!byServer.has(origin)) byServer.set(origin, []);
    byServer.get(origin).push(entry);
  }
  list.replaceChildren();
  if (!byServer.size) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "None yet.";
    return list.append(li);
  }
  const servers = [...byServer.keys()].sort((a, b) => (a === current ? -1 : b === current ? 1 : a.localeCompare(b)));
  for (const origin of servers) {
    for (const entry of byServer.get(origin)) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = entry.email;
      if (origin !== current) {
        const where = document.createElement("span");
        where.className = "where";
        where.textContent = `on ${origin}`;
        name.append(" ", where);
      }
      const button = document.createElement("button");
      button.className = "quiet";
      button.type = "button";
      button.textContent = "Disconnect";
      button.addEventListener("click", async () => {
        button.disabled = true;
        await chrome.runtime.sendMessage({ type: "disconnect", email: entry.email, server: origin });
        draw();
      });
      li.append(name, button);
      list.append(li);
    }
  }
}

document.getElementById("server-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const origin = L.serverOrigin(input.value);
  if (!origin) return say("Use an https:// address, or http://127.0.0.1 for this computer.", "err");
  await chrome.storage.local.set({ server: origin });
  say(`Saved. Gmail tabs will use ${origin} after a reload.`, "ok");
  draw();
});

draw();
