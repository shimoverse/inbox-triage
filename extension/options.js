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
  const accounts = Object.entries(tokens || {}).filter(([key]) => key.startsWith(current + " ")).map(([, entry]) => entry);
  list.replaceChildren();
  if (!accounts.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "None yet.";
    return list.append(li);
  }
  for (const entry of accounts) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = entry.email;
    const button = document.createElement("button");
    button.className = "quiet";
    button.type = "button";
    button.textContent = "Disconnect";
    button.addEventListener("click", async () => {
      button.disabled = true;
      await chrome.runtime.sendMessage({ type: "disconnect", email: entry.email });
      draw();
    });
    li.append(name, button);
    list.append(li);
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
