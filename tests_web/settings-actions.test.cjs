"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { setImmediate } = require("node:timers/promises");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const source = readFileSync(
  join(__dirname, "../src/skulk_openai/web/app.js"),
  "utf8",
);

async function settingsPage(saved = {}) {
  const elements = new Map();
  const writes = [];
  let state = {
    csrf: "synthetic-csrf",
    connection: "authorized",
    cached_models: [],
    models: [],
    settings: {
      models: [],
      plan_notice_seen: true,
      allow_images: true,
      maximum_output_characters: null,
      maximum_requests_per_hour: null,
      ...saved,
    },
  };
  function element(id) {
    if (!elements.has(id))
      elements.set(id, {
        value: "",
        listeners: new Map(),
        addEventListener(name, callback) {
          this.listeners.set(name, callback);
        },
        replaceChildren() {},
        append() {},
        reportValidity() {
          return true;
        },
      });
    return elements.get(id);
  }
  runInNewContext(source, {
    URL,
    Option: function () {},
    location: { hash: "" },
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      createElement: () => ({ append() {} }),
    },
    history: { replaceState() {} },
    window: { addEventListener() {}, scrollTo() {} },
    setInterval() {},
    async fetch(path, options) {
      if (path === "/api/settings") {
        const settings = JSON.parse(options.body);
        writes.push(settings);
        state = { ...state, settings };
      }
      return {
        ok: true,
        json: async () =>
          path === "/api/gateway" ? { public_key: "a".repeat(64) } : state,
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

test("no local caps are sent until an owner opts in", async () => {
  const { element, writes } = await settingsPage();
  for (const kind of ["request", "output"]) {
    assert.equal(element(`${kind}-cap-enabled`).checked, false);
    assert.equal(element(`${kind}-limit-row`).hidden, true);
    assert.equal(element(`${kind}-limit`).disabled, true);
  }
  assert.equal(element("images").checked, true);
  await element("save").listeners.get("click")();
  assert.equal(writes[0].maximum_requests_per_hour, null);
  assert.equal(writes[0].maximum_output_characters, null);
  assert.equal(writes[0].allow_images, true);
});

test("existing caps can be disabled, discarded, and independently re-enabled", async () => {
  const { element, writes } = await settingsPage({
    maximum_requests_per_hour: 60,
    maximum_output_characters: 32768,
    allow_images: false,
  });
  assert.equal(element("request-cap-enabled").checked, true);
  assert.equal(element("output-cap-enabled").checked, true);
  assert.equal(element("images").checked, false);
  element("request-cap-enabled").checked = false;
  element("request-cap-enabled").listeners.get("change")();
  assert.equal(element("request-limit-row").hidden, true);
  assert.equal(element("save").disabled, false);
  await element("discard").listeners.get("click")();
  assert.equal(element("request-cap-enabled").checked, true);
  for (const kind of ["request", "output"]) {
    element(`${kind}-cap-enabled`).checked = false;
    element(`${kind}-cap-enabled`).listeners.get("change")();
  }
  await element("save").listeners.get("click")();
  assert.equal(writes[0].maximum_requests_per_hour, null);
  assert.equal(writes[0].maximum_output_characters, null);
  element("request-cap-enabled").checked = true;
  element("request-cap-enabled").listeners.get("change")();
  element("request-limit").value = "2000";
  await element("save").listeners.get("click")();
  assert.equal(writes[1].maximum_requests_per_hour, 2000);
  assert.equal(writes[1].maximum_output_characters, null);
  assert.equal(element("request-limit").disabled, false);
});
