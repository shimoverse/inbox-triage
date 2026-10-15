"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Exercise the real worker message entry point, with only Chrome storage and HTTP stubbed.
function worker() {
  let listener;
  let server = "https://one.example";
  let tokens = {};
  let session = {};
  const calls = [];
  let fail = false;
  const chrome = {
    runtime: { id: "test-extension", onMessage: { addListener: fn => { listener = fn; } }, openOptionsPage() {} },
    action: { onClicked: { addListener() {} } },
    storage: {
      local: {
        setAccessLevel() {},
        async get() { return { server, tokens, prefs: {} }; },
        async set(value) { if (value.tokens) tokens = value.tokens; },
      },
      session: {
        async get() { return session; },
        async set(value) { session = { ...session, ...value }; },
      },
    },
  };
  const context = vm.createContext({ chrome, self: {}, URL, URLSearchParams, Date,
    fetch: async (url) => {
      calls.push(url);
      if (fail) throw new Error("network down");
      return { ok: true, json: async () => ({ started: true }) };
    },
  });
  context.importScripts = (...names) => {
    for (const name of names) vm.runInContext(fs.readFileSync(path.join(__dirname, "..", name), "utf8"), context);
  };
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8"), context);
  return {
    calls,
    setServer(value) { server = value; },
    setToken(origin, email) { tokens[`${origin} ${email}`] = { token: "synthetic-token", email }; },
    setFailure(value) { fail = value; },
    send(message) { return new Promise(resolve => listener(message, { id: chrome.runtime.id }, resolve)); },
  };
}

test("disconnected sync does not claim success or suppress the first authorized sync", async () => {
  const w = worker();
  const email = "alice@example.org";
  assert.equal((await w.send({ type: "sync", email })).connected, false);
  w.setToken("https://one.example", email);
  assert.equal((await w.send({ type: "sync", email })).data.started, true);
  assert.equal(w.calls.length, 1);
});

test("sync throttle is per server and failed requests can be retried", async () => {
  const w = worker();
  const email = "alice@example.org";
  w.setToken("https://one.example", email);
  w.setToken("https://two.example", email);
  w.setFailure(true);
  assert.match((await w.send({ type: "sync", email })).error, /Can't reach/);
  w.setFailure(false);
  assert.equal((await w.send({ type: "sync", email })).data.started, true);
  w.setServer("https://two.example");
  assert.equal((await w.send({ type: "sync", email })).data.started, true);
  assert.deepEqual(w.calls, ["https://one.example/api/ext/sync", "https://one.example/api/ext/sync",
    "https://two.example/api/ext/sync"]);
});
