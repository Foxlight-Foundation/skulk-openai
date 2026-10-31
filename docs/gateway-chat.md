# Experimental gateway chat

Skulk OpenAI Integrator is a managed capability. It owns account sessions,
request translation, upstream HTTP, model choices, and permission enforcement.
A provider-neutral Skulk adapter can expose delegated models at `GET /v1/models`
and route `POST /v1/chat/completions` to its public SDK descriptors. This requires
a compatible experimental Skulk build and an installed capability; launching the
standalone management page does not create either integration.

The shared `chat.completions@0.1.0` contract is an experiment, not a stable SDK ABI.
It is independent of OpenAI modules. Another provider can implement the same
catalog, signature envelope and delta/terminal protocol with unrelated identities.

## Configure

1. Connect the account through **Continue with ChatGPT**.
2. Refresh the account catalog, enable the desired models, and choose the
   integration's processing/usage policy. Defaults deny external use.
3. In **Skulk access**, find the compatible local gateway's public key. Review the
   selected models and request bounds, then save to delegate this account to it.
4. Use the exact capability model ID returned by Skulk, for example
   `capability/openai.inference.chat/<account-model-slug>`, in chat or the API.

Inference uses direct owner authority under Skulk's existing localhost/verified
Tailscale dashboard rules. Requests include the same-origin `Origin` and
`X-Skulk-Dashboard: pairing-v1` headers. Ordinary LAN requests, forwarded requests,
relay requests and paired-device bearers do not inherit permission. This first
adapter discovers capabilities installed on its own API host; remote provider
routing and per-device inference grants are not implemented here.

## Ownership and admission

The gateway creates a protected local Ed25519 key. Its identity endpoint returns
only the public key, which alone grants nothing. The owner explicitly approves
that key in the capability's protected account journal. No account token or
private signing key is sent to the browser or the other process.

Every request is signed over its exact target, descriptor revision, catalog and
policy revision, model, full input, nonce and short expiry. The capability checks
its own current settings, account generation, enabled model allowlist and
signature, then atomically reserves the nonce and the shared UTC-hour attempt
budget before submitting content. Disconnect, account switch and reconnection
withdraw the old account delegation. Saving new settings withdraws stale catalog
proofs. Calls already admitted may be in flight. Budgets are local operational
bounds, not provider billing guarantees; failed attempts are not refunded.

Unknown provider usage stays `unknown`. A local-only or included-only hard
requirement fails before provider submission. Choosing a model and enabling
external processing does not establish an included-only guarantee. App limits,
credits and plan eligibility remain managed in [ChatGPT usage settings](https://chatgpt.com/settings/usage).

## Request and stream support

The capability translates full Chat Completions history to explicit Responses
input, mapping system instructions to developer instructions. It supports normal
text/code, reasoning summaries, JSON/JSON-schema output, and client-executed
function tools with tool-result history. Function tools and inline image input
require separate owner settings. Only bounded inline PNG/JPEG/WebP image data is
accepted; the signed JSON input limit is 48 KiB, so this is not a large-media path.
Model-specific feature support remains subject to the selected account model.

The adapter returns standard chat text/reasoning/tool deltas and finish reasons,
with provider usage when supplied. It requires common completion **and** transport
completion before sending a success finish and `[DONE]`. Failure, incomplete
output, usage-limit errors, interruption and output bounds remain failures.
Disconnect closes the provider session. Non-streaming requests are collected over
the same cancellable stream; an error after headers is an error JSON body rather
than a fabricated successful assistant turn.

Skulk chat uses its normal Markdown/code rendering and mounted speech path. The
capability does not implement TTS or execute code. Hosted Code Interpreter is not
available in this ChatGPT connection. Temperature, token limits, background/stored
conversations, hosted MCP, image generation, file search and OpenAI audio endpoints
are also excluded by the [preview contract](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).
Unsupported request fields fail closed instead of being silently ignored.

## Settings

The settings page persists inference enablement, processing/usage policy, enabled
models, diagnostics default model, developer instructions, reasoning effort,
verbosity, function-tool/image permissions, output-character bound, per-hour
attempt limit, gateway public key and the one-time plan-use notice. Credentials
and the account-generation fence never appear in browser state. The request
tester is a secondary diagnostic and obeys saved integration policy.
