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

async function connectionPage({
  connection = "disconnected",
  failModels = false,
} = {}) {
  const requests = [];
  const elements = new Map();
  let poll;
  let state = {
    connection,
    connecting: false,
    catalog_fresh: false,
    models: [],
    cached_models: [],
    csrf: "synthetic-csrf",
    settings: {
      models: [],
      plan_notice_seen: true,
      inference_enabled: true,
      processing: "allow_openai",
      usage: "app_settings",
    },
  };
  class Element {
    value = "";
    listeners = new Map();
    addEventListener(name, callback) {
      this.listeners.set(name, callback);
    }
    replaceChildren() {}
    append() {}
    setAttribute() {}
    reportValidity() {
      return true;
    }
  }
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  runInNewContext(source, {
    URL,
    Option: function () {},
    location: { hash: "" },
    history: { replaceState() {} },
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      createElement: () => new Element(),
    },
    window: { addEventListener() {}, scrollTo() {}, open() {} },
    setInterval(callback) {
      poll = callback;
    },
    async fetch(path, options) {
      requests.push({ path, options });
      let value;
      if (path === "/api/connect") {
        state = { ...state, connecting: true, notice: null };
        value = {
          launch_url: `http://127.0.0.1:49152/continue/${"a".repeat(43)}`,
        };
      } else if (path === "/api/models") {
        if (failModels)
          return {
            ok: false,
            json: async () => ({ error: "upstream_failed" }),
          };
        const models = [{ slug: "text-a", title: "Text A" }];
        state = {
          ...state,
          models,
          cached_models: models,
          catalog_fresh: true,
        };
        value = state;
      } else value = state;
      return { ok: true, json: async () => value };
    },
  });
  await setImmediate();
  return {
    element,
    requests,
    async complete(connection, notice) {
      state = { ...state, connection, notice, connecting: false };
      poll();
      await setImmediate();
    },
    async poll() {
      poll();
      await setImmediate();
    },
  };
}

test("successful consent clears the pending message and loads models automatically", async () => {
  const page = await connectionPage();
  await page.element("connect").listeners.get("click")();
  assert.match(page.element("notice").textContent, /Complete sign-in/);
  await page.complete("authorized", "connected");
  assert.match(page.element("notice").textContent, /Connection saved/);
  assert.equal(page.element("connection-badge").textContent, "Connected");
  const reads = page.requests.filter(
    (request) => request.path === "/api/models",
  );
  assert.equal(reads.length, 1);
  assert.equal(reads[0].options.headers["X-CSRF-Token"], "synthetic-csrf");
  await page.poll();
  assert.equal(
    page.requests.filter((request) => request.path === "/api/models").length,
    1,
  );
  assert.equal(
    page.requests.some((request) => request.path === "/api/settings"),
    false,
  );
});

test("an existing connected account loads its missing catalog on page entry", async () => {
  const page = await connectionPage({ connection: "authorized" });
  assert.equal(
    page.requests.filter((request) => request.path === "/api/models").length,
    1,
  );
});

test("missing plan consent clears the pending message without fetching models", async () => {
  const page = await connectionPage();
  await page.element("connect").listeners.get("click")();
  await page.complete("identity_only", "permission_missing");
  assert.match(page.element("notice").textContent, /grant ChatGPT plan usage/);
  assert.equal(
    page.requests.some((request) => request.path === "/api/models"),
    false,
  );
});

test("a catalog failure preserves connection status and does not poll the provider repeatedly", async () => {
  const page = await connectionPage({
    connection: "authorized",
    failModels: true,
  });
  assert.equal(page.element("connection-badge").textContent, "Connected");
  assert.match(
    page.element("notice").textContent,
    /Models could not be loaded/,
  );
  await page.poll();
  await page.poll();
  assert.equal(
    page.requests.filter((request) => request.path === "/api/models").length,
    1,
  );
});
