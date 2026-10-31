# OpenAI management page

This capability owns a small web page for its account connection, account-specific
model choices and one explicit test request. It uses the same session storage,
catalog and signed admission boundary as the managed inference child. It adds no
OpenAI endpoint or UI code to Skulk core.

## Open the page

For a standalone owner-local session, from the initiative branch:

```sh
uv sync --frozen
uv run skulk-openai-ui --enable-inference
```

This opens the browser and prints the loopback URL. Use `--no-browser` to open it
yourself on the same host. Without `--enable-inference`, connection and model
management remain available and test generation is disabled. `--port` optionally
chooses an unprivileged port; the default chooses an available one. Stop the
command to close its page listener and cancel its active test and pending sign-in.

The default account store is the same stable private host directory used by
`skulk-openai-account`. To use another installation, pass its absolute private
directory with `--state-dir`. No account copy or new sign-in is needed to open a
page over an existing store. Loading or polling the page performs no OpenAI HTTP
request. **Refresh models** explicitly updates the catalog and may renew tokens.

The page provides:

- Connect/reconnect, explicit recovery of missing plan permission, and confirmed
  disconnect. A failed remote revocation is shown separately from local logout.
- Account-specific models in provider order; stale choices withdraw.
- A bounded prompt, streaming plain-text response, explicit terminal status,
  cancellation and no automatic inference retry.
- Processing and usage choices for this test. **Local only** and **Included usage
  only** block the request before any content or catalog request leaves the host.
  The provider cannot establish an included-only usage guarantee.

Selecting **Allow OpenAI** and **Use app usage settings**, then clicking
**Send test request**, authorizes one request under those choices. This may use
the connected plan and any usage mode enabled in the user's app settings. Output
is bounded to 1,024 characters and execution to 30 seconds; neither is a billing
cap. Cancellation closes the owned upstream stream but cannot undo work already
performed by OpenAI.

Each accepted test creates a host-held grant for one attempt and one model, with
a maximum lifetime of sixty seconds. It signs the exact target, prompt, catalog
revision and hard policy, then enters `InferenceCapability.generate`. The grant
is revoked after completion, rejection, cancellation or shutdown. Provider
tokens, grant signing keys and signed invocation proofs never enter the browser.
The browser keeps its prompt and output only in its current document. It stores
no account data or policy in local/session storage.

These controls do not configure fabric routing or authorize other callers.
Fabric callers still require independent signed grants, and managed inference
still respects the owner's `inference_enabled` setting.

## Plugins and topology

The manifest declares one `link` surface, `management`, titled **OpenAI**. The
running child reports its current credential-free loopback URL in correlated SDK
health replies. Existing generic Skulk adapters project it into the capability's
topology entry and **Open Skulk OpenAI Integrator** action. The page remains ready when the account
is disconnected or inference is disabled. The process owns and withdraws the
listener on stop; an old URL is not a persistent endpoint.

Cloning this repository, connecting an account or running the standalone page
does **not** install it in Skulk. Plugins lists actual installations under that
host's plugin manager, and topology reflects those reported capability nodes.
An accepted signed runtime must be installed and activated with the supported
SDK/Core toolchain before this appears there. A declared surface is not proof
of that deployment. Public distribution and full installed-node qualification
remain separate gates; do not manufacture inventory or copy a token to a fleet
host to make a node appear.

This first surface opens on the capability's own host. It is not a remote
account-management service, relay route, LAN listener or generic OAuth form in
the dashboard. A browser on a different host cannot use the loopback URL.

## Local HTTP contract

These are capability-owned endpoints on a dynamically chosen IPv4 loopback port,
not Skulk API routes. All responses prohibit caching, framing and external script
or style loading. Text/model/output rendering uses DOM text, never HTML.

| Method and path | Parameters | Behavior |
| --- | --- | --- |
| `GET /` | None | Static page; establishes a process-local HttpOnly, SameSite=Strict browser session. No provider request. |
| `GET /assets/app.css`, `GET /assets/app.js` | None | Packaged static assets, without CDN dependencies. |
| `GET /api/state` | Owner browser session | Cached connection state, fresh model choices/catalog revision, inference enablement, activity and a browser CSRF token. No identity or provider credential, and no upstream request. |
| `POST /api/connect` | `{ "request_plan_consent": false }` | Starts one explicit existing PKCE/OIDC loopback flow. Returns a one-use local launch URL to that browser. `true` requests plan permission recovery. Reconnection reuses the selected registration. |
| `POST /api/disconnect` | `{}` | Cancels pending sign-in/test, fences and clears local access, then attempts remote revocation. Returns `revocation_confirmed`; false is shown prominently. |
| `POST /api/models` | `{}` | Explicit bounded catalog refresh and token renewal if needed. |
| `POST /api/generate` | `model`, `prompt`, `catalog_revision`, `processing` (`local_only` / `allow_openai`), `usage` (`included_only` / `app_settings`) | At most one active test; returns bounded newline-delimited common generation events with explicit completion/failure. Enforces policy, account, freshness, signature, nonce and budget. |
| `POST /api/cancel` | `{}` | Cancels the current owner-local test and waits for upstream iterator/grant cleanup. It does not refund the attempt. |

Every mutation requires the exact listener Origin, a valid owner session,
`Content-Type: application/json` and the CSRF header obtained from the same-origin
state response. Unknown Host values, forwarded requests, duplicate headers,
transfer encoding and oversized bodies fail closed. Requests accept at most
16 KiB of headers and 16 KiB of JSON; the listener admits at most sixteen
concurrent connections, reads each request within five seconds and bounds each
handler to forty-five seconds. There is no CORS endpoint. Cross-site document
navigation may open the static page; it cannot read state or perform an action.

Loopback and browser protections assume a trusted host user/browser, like the
CLI. They do not isolate credentials from a compromised host account or browser.
The SDK child process is not an adversarial sandbox.

## Verification scope

Credential-free tests use generated identities, actual loopback sockets and
explicit mock upstream transports. They exercise session/Origin/CSRF refusal,
strict HTTP bounds, fresh and stale selection, policy denials, bounded signed
admission, cancellation, disconnect and shutdown. Explicit licensed-SDK process
tests verify the manifest/health surface, management while inference is disabled,
and listener withdrawal. Source, wheel, signed runtime, installed host and live
provider evidence remain distinct.


## Integration settings and chat

The page is settings-first, with Connection, Models, Request defaults, Skulk
access and Diagnostics sections. It uses the actual Skulk mark, Studio palette
and self-hosted Instrument Sans/JetBrains Mono fonts (licenses are packaged).
See [gateway chat](gateway-chat.md) for persistent settings and enforced routing.
An expired access token retains the connection: Refresh catalog renews it through
the existing refresh flow when possible, without another browser consent.

Additional owner-session/Origin/CSRF protected routes:

| Method and path | Parameters | Behavior |
| --- | --- | --- |
| `POST /api/settings` | Strict `IntegrationSettings` fields except `account_binding` | Atomically saves integration policy. The account fence is derived from the protected store. No inference or catalog request. |
| `POST /api/gateway` | Integer `port` in 1024–65535 | Reads only `/v1/inference/gateway` on IPv4 loopback, with bounded response/deadline and no proxy or redirect. Returns a public key to review; does not approve it. |
| `POST /api/notice` | `{}` | Persists dismissal of the non-binding first-use ChatGPT plan notice. |
| `GET /assets/skulk.svg`, font assets | None | Packaged mark and self-hosted fonts; no third-party browser fetch. |

`GET /api/state` also exposes editable settings without account binding and cached
model names, explicitly distinguishing stale choices from currently usable ones.
The UI links to ChatGPT usage settings and current preview limitations rather
than offering unsupported billing, temperature, token-limit or hosted-tool knobs.
