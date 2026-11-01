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
const launchPath = `/continue/${"a".repeat(43)}`;

async function clickConnect(launchUrl, connection = "disconnected") {
  const elements = new Map();
  const opened = [];
  const requests = [];
  const state = {
    csrf: "synthetic-csrf",
    connection,
    cached_models: [],
    models: [],
    settings: { models: [], plan_notice_seen: true },
  };
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        value: "",
        listeners: new Map(),
        addEventListener(name, callback) { this.listeners.set(name, callback); },
        replaceChildren() {},
        append() {},
      });
    }
    return elements.get(id);
  }
  runInNewContext(source, {
    URL,
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      createElement: () => ({ append() {} }),
    },
    Option: function () {},
    location: { hash: "" },
    history: { replaceState() {} },
    window: {
      addEventListener() {},
      scrollTo() {},
      open: (...args) => opened.push(args),
    },
    setInterval() {},
    async fetch(path, options) {
      requests.push({ path, options });
      return {
        ok: true,
        json: async () => path === "/api/connect" ? { launch_url: launchUrl } : state,
      };
    },
  });
  await setImmediate();
  await element("connect").listeners.get("click")();
  return { opened, requests, notice: element("notice").textContent };
}

for (const connection of ["disconnected", "authorized", "identity_only"]) {
  test(`account button opens the loopback launcher from ${connection}`, async () => {
    const url = `http://127.0.0.1:49152${launchPath}`;
    const result = await clickConnect(url, connection);
    assert.deepEqual(result.opened, [[url, "_blank", "noopener,noreferrer"]]);
    const request = result.requests.find((item) => item.path === "/api/connect");
    assert.equal(request.options.headers["X-CSRF-Token"], "synthetic-csrf");
    assert.deepEqual(JSON.parse(request.options.body), {
      request_plan_consent: connection === "identity_only",
    });
    assert.match(result.notice, /Complete sign-in/);
  });
}

for (const url of [
  `https://auth.openai.com${launchPath}`,
  `http://example.com:49152${launchPath}`,
  `http://localhost:49152${launchPath}`,
  `http://127.0.0.1${launchPath}`,
  `http://127.0.0.1:0${launchPath}`,
  `http://user:password@127.0.0.1:49152${launchPath}`,
  `http://127.0.0.1:49152${launchPath}?redirect=elsewhere`,
  `http://127.0.0.1:49152${launchPath}#fragment`,
  "http://127.0.0.1:49152/auth/callback",
  "http://127.0.0.1:49152/continue/short",
]) {
  test(`account button refuses unexpected destination ${url}`, async () => {
    const result = await clickConnect(url);
    assert.deepEqual(result.opened, []);
    assert.equal(result.notice, "Unexpected authorization destination.");
  });
}
