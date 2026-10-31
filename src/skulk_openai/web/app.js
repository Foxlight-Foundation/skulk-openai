"use strict";
const $ = (id) => document.getElementById(id);
let current = null;
let dirty = false;
let performing = false;
let draftModels = new Set();
let testController = null;
const messages = {
  connected:
    "Connection saved. Choose models and configure Skulk access when you are ready.",
  disconnected:
    "Account disconnected. Gateway access to this account is withdrawn.",
  permission_missing: "Sign in again to grant ChatGPT plan usage.",
  connection_failed:
    "The connection could not be completed. Try Continue with ChatGPT again.",
  revocation_unconfirmed:
    "Disconnected locally. Remote revocation could not be confirmed; review connected apps in ChatGPT.",
  policy_denied:
    "The request is blocked by the integration policy or its connection is no longer current.",
  invalid_request:
    "This request uses an unavailable option or is outside the input bounds.",
  operation_failed:
    "The operation could not finish. Check the connection and try again.",
  owner_action_required: "Refresh this page to restore the owner session.",
  busy: "Another operation is still in progress.",
  upstream_failed: "The provider could not complete the request.",
  output_limit: "The response reached the local output bound.",
  interrupted: "The request was interrupted.",
  invalid_stream: "The provider returned an invalid stream.",
  incomplete: "The provider marked this response incomplete.",
  usage_limit:
    "ChatGPT usage limit reached. Open Manage usage to review the applicable limit.",
  usage_unavailable:
    "ChatGPT plan usage is unavailable. Open Manage usage to review your app settings.",
};
function notify(text) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}
function changePage(page) {
  for (const section of document.querySelectorAll(".page"))
    section.hidden = section.id !== `page-${page}`;
  for (const button of document.querySelectorAll("nav button")) {
    if (button.dataset.page === page)
      button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  history.replaceState(null, "", `#${page}`);
  window.scrollTo({ top: 0 });
}
for (const button of document.querySelectorAll("[data-page]"))
  button.addEventListener("click", () => changePage(button.dataset.page));
for (const button of document.querySelectorAll("[data-go]"))
  button.addEventListener("click", () => changePage(button.dataset.go));
function changed() {
  dirty = true;
  $("save-status").textContent = "Unsaved changes";
  $("save").disabled = performing;
  $("discard").disabled = performing;
}
function settings() {
  return {
    inference_enabled: $("enabled").checked,
    processing: $("processing").value,
    usage: $("usage").value,
    models: [...draftModels],
    default_model: $("default-model").value || null,
    instructions: $("instructions").value,
    reasoning_effort: $("reasoning").value || null,
    verbosity: $("verbosity").value || null,
    allow_function_tools: $("tools").checked,
    allow_images: $("images").checked,
    maximum_output_characters: Number($("output-limit").value),
    maximum_requests_per_hour: Number($("request-limit").value),
    gateway_public_key: $("gateway-key").value || null,
    plan_notice_seen: current?.settings.plan_notice_seen ?? false,
  };
}
function populate(s) {
  $("enabled").checked = s.inference_enabled;
  $("processing").value = s.processing;
  $("usage").value = s.usage;
  $("instructions").value = s.instructions;
  $("reasoning").value = s.reasoning_effort || "";
  $("verbosity").value = s.verbosity || "";
  $("tools").checked = s.allow_function_tools;
  $("images").checked = s.allow_images;
  $("output-limit").value = s.maximum_output_characters;
  $("request-limit").value = s.maximum_requests_per_hour;
  $("gateway-key").value = s.gateway_public_key || "";
  draftModels = new Set(s.models);
  renderModels();
  $("default-model").value = s.default_model || "";
  dirty = false;
  $("save").disabled = true;
  $("discard").disabled = true;
  $("save-status").textContent = "Settings apply to this capability";
}
function renderModels() {
  const available = current?.cached_models ?? [];
  const missing = [...draftModels].filter(
    (slug) => !available.some((model) => model.slug === slug),
  );
  const models = [
    ...available,
    ...missing.map((slug) => ({
      slug,
      title: `${slug} (unavailable; remove selection)`,
    })),
  ];
  const render = (target, limit) => {
    target.replaceChildren();
    if (!models.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent =
        current?.connection === "authorized"
          ? "Refresh the catalog to see account models."
          : "Connect your ChatGPT account to discover models.";
      target.append(empty);
      return;
    }
    for (const model of models.slice(0, limit)) {
      const row = document.createElement("div");
      row.className = "model-row";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = draftModels.has(model.slug);
      checkbox.setAttribute("aria-label", `Enable ${model.title}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) draftModels.add(model.slug);
        else draftModels.delete(model.slug);
        const previous = $("default-model").value;
        renderModels();
        $("default-model").value = draftModels.has(previous) ? previous : "";
        changed();
      });
      const label = document.createElement("label");
      const name = document.createElement("span");
      name.textContent = model.title;
      const code = document.createElement("code");
      code.textContent = model.slug;
      label.append(checkbox, name, code);
      row.append(label);
      target.append(row);
    }
  };
  render($("model-preview"), 2);
  render($("model-list"), 256);
  const previous = $("default-model").value;
  $("default-model").replaceChildren(new Option("Choose a model", ""));
  for (const model of models.filter((m) => draftModels.has(m.slug)))
    $("default-model").append(new Option(model.title, model.slug));
  $("default-model").value = previous;
}
function update(state) {
  const previousCatalog = JSON.stringify(current?.cached_models);
  current = state;
  const connected = state.connection === "authorized";
  const renewable = connected || state.connection === "expired";
  const labels = {
    authorized: "Connected",
    expired: "Refresh required",
    identity_only: "Plan access needed",
    disconnected: "Disconnected",
    sign_in_required: "Sign in required",
  };
  $("connection-badge").textContent = state.connecting
    ? "Connecting…"
    : labels[state.connection] || "Not connected";
  $("connection-detail").textContent = connected
    ? "Connected on this device"
    : state.connection === "expired"
      ? "Saved connection; refresh the catalog to renew access"
      : state.connecting
        ? "Waiting for browser consent"
        : "No active plan connection";
  $("plan-access").textContent = connected
    ? "Available"
    : state.connection === "expired"
      ? "Refresh required"
      : state.connection === "identity_only"
        ? "Consent required"
        : "Unavailable";
  $("plan-label").textContent = connected
    ? "Using ChatGPT plan"
    : "ChatGPT connection";
  $("connect").textContent = state.connecting
    ? "Restart sign-in"
    : renewable
      ? "Reconnect"
      : "Continue with ChatGPT";
  $("connect").disabled = performing;
  $("disconnect").hidden = !renewable && state.connection !== "identity_only";
  $("disconnect").disabled = performing;
  for (const button of document.querySelectorAll(".refresh"))
    button.disabled = !renewable || performing;
  $("catalog-status").textContent = state.catalog_fresh
    ? "Account catalog is current. Server ordering is preserved."
    : "Catalog is stale or missing. Refresh before sending a request.";
  if (!dirty) populate(state.settings);
  else if (previousCatalog !== JSON.stringify(state.cached_models))
    renderModels();
  const selectedTest = $("model").value;
  $("model").replaceChildren(new Option("Choose a model", ""));
  for (const model of state.models.filter((m) =>
    state.settings.models.includes(m.slug),
  ))
    $("model").append(new Option(model.title, model.slug));
  $("model").value = selectedTest || state.settings.default_model || "";
  const allowed =
    connected &&
    state.inference_enabled &&
    state.settings.inference_enabled &&
    state.settings.processing === "allow_openai" &&
    state.settings.usage === "app_settings";
  $("send").disabled =
    !allowed ||
    !state.catalog_fresh ||
    !state.settings.models.length ||
    state.busy ||
    performing;
  $("cancel").disabled = !state.busy && !testController;
  $("inference-off").hidden = allowed;
  $("access-status").textContent = state.gateway_delegation_current
    ? "Gateway key approved for the saved account and policy."
    : state.settings.gateway_public_key
      ? "Connection changed. Save settings to approve this gateway for the current account."
      : "No gateway delegated. Save a key to make models routable.";
  if (connected && !state.settings.plan_notice_seen && !$("welcome").open)
    $("welcome").showModal();
}
async function refresh() {
  const response = await fetch("/api/state", { cache: "no-store" });
  if (!response.ok)
    throw new Error("Refresh this page to restore your session.");
  update(await response.json());
}
async function post(path, value = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": current.csrf,
    },
    body: JSON.stringify(value),
  });
  const data = await response.json();
  if (!response.ok)
    throw new Error(messages[data.error] || "The operation could not finish.");
  return data;
}
async function action(work) {
  if (performing) return;
  performing = true;
  try {
    await work();
  } catch (error) {
    notify(error.message);
  } finally {
    performing = false;
    await refresh().catch((error) => notify(error.message));
  }
}
for (const input of document.querySelectorAll(
  "main input:not(#gateway-port):not(#gateway-key), main select:not(#model), #instructions",
)) {
  input.addEventListener("input", changed);
}
$("discard").addEventListener("click", () => {
  populate(current.settings);
  notify("Changes discarded.");
});
$("save").addEventListener("click", () =>
  action(async () => {
    for (const id of ["output-limit", "request-limit"])
      if (!$(id).reportValidity()) return;
    const next = await post("/api/settings", settings());
    dirty = false;
    update(next);
    notify("Settings saved.");
  }),
);
$("connect").addEventListener("click", () =>
  action(async () => {
    const next = await post("/api/connect", {
      request_plan_consent: current.connection === "identity_only",
    });
    const url = new URL(next.launch_url);
    if (url.protocol !== "https:" || url.hostname !== "auth.openai.com")
      throw new Error("Unexpected authorization destination.");
    window.open(url.href, "_blank", "noopener,noreferrer");
    notify(
      "Complete sign-in in the OpenAI tab. This page will update when the connection is saved.",
    );
  }),
);
$("disconnect").addEventListener("click", () => {
  $("disconnect-confirmation").hidden = false;
});
$("keep-connected").addEventListener("click", () => {
  $("disconnect-confirmation").hidden = true;
});
$("confirm-disconnect").addEventListener("click", () =>
  action(async () => {
    const result = await post("/api/disconnect");
    $("disconnect-confirmation").hidden = true;
    notify(
      messages[
        result.revocation_confirmed ? "disconnected" : "revocation_unconfirmed"
      ],
    );
  }),
);
for (const button of document.querySelectorAll(".refresh"))
  button.addEventListener("click", () =>
    action(async () => {
      update(await post("/api/models"));
      notify("Account catalog refreshed.");
    }),
  );
$("find-gateway").addEventListener("click", () =>
  action(async () => {
    if (!$("gateway-port").reportValidity()) return;
    const gateway = await post("/api/gateway", {
      port: Number($("gateway-port").value),
    });
    $("gateway-key").value = gateway.public_key;
    changed();
    notify(
      "Gateway found. Review models and limits, then Save settings to approve access.",
    );
  }),
);
$("revoke-gateway").addEventListener("click", () => {
  $("gateway-key").value = "";
  changed();
  notify("Save settings to remove gateway access.");
});
$("got-it").addEventListener("click", () =>
  action(async () => {
    await post("/api/notice");
    current.settings.plan_notice_seen = true;
    $("welcome").close();
  }),
);
$("welcome").addEventListener("cancel", (event) => {
  event.preventDefault();
  $("got-it").click();
});
$("test-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (testController || $("send").disabled || !event.target.reportValidity())
    return;
  testController = new AbortController();
  $("response").textContent = "";
  $("send").disabled = true;
  $("cancel").disabled = false;
  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": current.csrf,
      },
      signal: testController.signal,
      body: JSON.stringify({
        model: $("model").value,
        prompt: $("prompt").value,
        catalog_revision: current.catalog_revision,
        processing: current.settings.processing,
        usage: current.settings.usage,
      }),
    });
    if (!response.ok || !response.body) {
      const failure = await response.json();
      throw new Error(
        messages[failure.error] || "The request could not start.",
      );
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    let terminal = false;
    while (true) {
      const result = await reader.read();
      pending += decoder.decode(result.value || new Uint8Array(), {
        stream: !result.done,
      });
      let end;
      while ((end = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, end);
        pending = pending.slice(end + 1);
        if (!line) continue;
        const item = JSON.parse(line);
        if (item.kind === "text") $("response").textContent += item.text;
        else {
          terminal = true;
          notify(
            item.kind === "completed"
              ? "Test request completed."
              : messages[item.error] || "The request failed.",
          );
        }
      }
      if (pending.length > 65536)
        throw new Error("The response exceeded the browser buffer.");
      if (result.done) break;
    }
    if (!terminal) throw new Error("The request ended without completion.");
  } catch (error) {
    notify(
      error.name === "AbortError" ? "Test request cancelled." : error.message,
    );
  } finally {
    testController = null;
    await refresh().catch((error) => notify(error.message));
  }
});
$("cancel").addEventListener("click", () =>
  action(async () => {
    testController?.abort();
    await post("/api/cancel");
  }),
);
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
changePage(
  ["connection", "models", "defaults", "access", "diagnostics"].includes(
    location.hash.slice(1),
  )
    ? location.hash.slice(1)
    : "connection",
);
refresh().catch((error) => notify(error.message));
setInterval(() => {
  if (!performing) refresh().catch((error) => notify(error.message));
}, 5000);
