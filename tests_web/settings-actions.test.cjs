"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { setImmediate } = require("node:timers/promises");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(
  join(__dirname, "../src/skulk_openai/web/app.js"), "utf8",
);

async function settingsPage() {
  const elements = new Map();
  const writes = [];
  let state = {
    csrf: "synthetic-csrf", connection: "authorized", cached_models: [],
    models: [], settings: { models: [], plan_notice_seen: true },
  };
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", listeners: new Map(),
      addEventListener(name, callback) { this.listeners.set(name, callback); },
      replaceChildren() {}, append() {}, reportValidity() { return true; },
    });
    return elements.get(id);
  }
  runInNewContext(source, {
    URL, Option: function () {}, location: { hash: "" },
    document: {
      getElementById: element, querySelectorAll: () => [],
      createElement: () => ({ append() {} }),
    },
    history: { replaceState() {} },
    window: { addEventListener() {}, scrollTo() {} }, setInterval() {},
    async fetch(path, options) {
      if (path === "/api/settings") {
        const settings = JSON.parse(options.body);
        writes.push(settings);
        state = { ...state, settings };
      }
      return {
        ok: true,
        json: async () => path === "/api/gateway"
          ? { public_key: "a".repeat(64) } : state,
      };
    },
  });
  await setImmediate();
  return { element, writes };
}

test("finding a gateway enables Save and persists its discovered key", async () => {
  const { element, writes } = await settingsPage();
  assert.equal(element("save").disabled, true);
  element("gateway-port").value = "52415";
  await element("find-gateway").listeners.get("click")();
  assert.equal(element("save").disabled, false);
  assert.equal(element("discard").disabled, false);
  await element("save").listeners.get("click")();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].gateway_public_key, "a".repeat(64));
  assert.equal(element("save").disabled, true);
});

test("a discovered gateway draft can be discarded without saving", async () => {
  const { element, writes } = await settingsPage();
  await element("find-gateway").listeners.get("click")();
  assert.equal(element("discard").disabled, false);
  await element("discard").listeners.get("click")();
  assert.equal(element("gateway-key").value, "");
  assert.equal(element("save").disabled, true);
  assert.equal(element("discard").disabled, true);
  assert.equal(writes.length, 0);
});
