"use strict";

// This owner surface handles public enrollment only. Client signing keys never
// enter its API, storage, export or cross-origin messages.
(() => {
  let snapshot = null;
  let refreshing = false;

  function enrollment(client) {
    return { client, target: snapshot.target };
  }

  function showExport(value) {
    $("client-export").value = JSON.stringify(value, null, 2);
    $("client-export-panel").hidden = false;
  }

  function renderClients() {
    const configured = snapshot?.configured === true;
    $("continuation-status").textContent = configured
      ? "Continuation is configured on this device. Approvals apply to the current account and saved model policy."
      : "Continuation is not configured. Enable protected checkpoint storage in this capability’s node configuration before approving clients. Existing full-history chat remains available.";
    $("approve-client").disabled = !configured || performing;
    for (const id of ["client-public-key", "client-models", "client-expiry"])
      $(id).disabled = !configured;
    const previous = new Set(
      [...$("client-models").selectedOptions].map((option) => option.value),
    );
    $("client-models").replaceChildren();
    for (const model of current?.cached_models ?? []) {
      if (!current.settings.models.includes(model.slug)) continue;
      const option = new Option(model.title, model.slug);
      option.selected = previous.has(model.slug);
      $("client-models").append(option);
    }
    $("client-list").replaceChildren();
    for (const client of snapshot?.clients ?? []) {
      const row = document.createElement("div");
      row.className = "client-row";
      const details = document.createElement("div");
      const key = document.createElement("code");
      key.textContent = client.public_key;
      const scope = document.createElement("p");
      scope.className = "hint";
      scope.textContent = `${client.models.join(", ")} · Expires ${new Date(client.expires_at * 1000).toLocaleString()}`;
      details.append(key, scope);
      const actions = document.createElement("div");
      actions.className = "actions";
      const exportButton = document.createElement("button");
      exportButton.type = "button";
      exportButton.textContent = "Export enrollment";
      exportButton.addEventListener("click", () => showExport(enrollment(client)));
      const revokeButton = document.createElement("button");
      revokeButton.type = "button";
      revokeButton.className = "danger";
      revokeButton.textContent = "Revoke client";
      revokeButton.addEventListener("click", () => action(async () => {
        await post("/api/clients/revoke", { caller_id: client.caller_id });
        $("client-export").value = "";
        $("client-export-panel").hidden = true;
        await refreshClients();
        notify("Client access revoked and its saved continuation state removed.");
      }).finally(renderClients));
      actions.append(exportButton, revokeButton);
      row.append(details, actions);
      $("client-list").append(row);
    }
    if (!snapshot?.clients?.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No active continuation clients for this account.";
      $("client-list").append(empty);
    }
  }

  async function refreshClients() {
    if (refreshing) return;
    refreshing = true;
    try {
      if (current === null) await refresh();
      const response = await fetch("/api/continuation", { cache: "no-store" });
      if (!response.ok) throw new Error("Client access could not be refreshed. Reload this page to restore the owner session.");
      snapshot = await response.json();
      renderClients();
    } finally {
      refreshing = false;
    }
  }

  $("refresh-clients").addEventListener("click", () =>
    refreshClients().catch((error) => notify(error.message)),
  );
  window.addEventListener("skulk:busy", () => {
    $("approve-client").disabled = !snapshot?.configured || performing;
  });
  $("client-enrollment").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!snapshot?.configured || !event.target.reportValidity()) return;
    action(async () => {
      const models = [...$("client-models").selectedOptions].map((option) => option.value);
      const result = await post("/api/clients/register", {
        public_key: $("client-public-key").value,
        models,
        expires_at: Date.now() / 1000 + Number($("client-expiry").value),
      });
      $("client-public-key").value = "";
      showExport(result);
      await refreshClients();
      notify("Client approved. Return the public enrollment to its matching client.");
    }).finally(renderClients);
  });
  refreshClients().catch((error) => notify(error.message));
  setInterval(() => {
    if (!performing && location.hash === "#access")
      refreshClients().catch((error) => notify(error.message));
  }, 5000);
})();
