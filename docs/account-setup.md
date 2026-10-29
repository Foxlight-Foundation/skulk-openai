# Capability-owned account setup

The account lifecycle belongs to this package. It uses no OpenAI-specific Skulk
core endpoint, worker, engine, configuration or State field. Setup is an explicit
owner-local action, separate from installing a package or granting a caller use
of the account. These commands currently exercise the session foundation;
connecting an account does **not** advertise live inference on the fabric.

## Operator commands

On the capability's trusted POSIX host, use a dedicated private storage directory:

```sh
uv run skulk-openai-account --state-dir ./openai-account status
uv run skulk-openai-account --state-dir ./openai-account connect
uv run skulk-openai-account --state-dir ./openai-account disconnect
```

Only run `connect` when you intend to sign in and grant permissions. It binds an
IPv4 loopback listener before opening the system browser. The displayed **Continue
with ChatGPT** link is a one-use local launch URL; authorization URLs containing
ID-token hints are never printed. `--no-browser` displays that same local link
without launching a browser. Open it on the host running setup. No remote callback
service or device-code flow is implemented.

Routine reconnection reuses the selected registration. `connect --new-account`
starts a separate registration; `connect --registration <opaque-registration-id>`
reauthorizes a saved one. The active account changes only after the returned
identity is verified and the replacement is saved. New attempts leave an existing
connection intact. The current CLI reports opaque registration IDs rather than
email addresses; a full account-picker interface remains future work.

`status` reads cached local facts without network requests. `identity_only` means
sign-in succeeded but `chatgpt.tokens.use.direct` was not granted. `authorized`
means an unexpired local token has that scope; it does not prove model access,
remaining quota, billing policy, caller authority or an active server session.

Disconnect fences new admission and pending sign-ins before clearing local
tokens. It then attempts renewable-session revocation with the retained in-memory
token. A failed remote request reports that revocation was not confirmed; use
ChatGPT Settings to disconnect the app if necessary. Host ID and account/client
mapping survive logout so returning sign-in does not invent a new registration.
An already running upstream request may have executed before disconnect.

## Implemented boundaries

- Fresh state, nonce and S256 PKCE per attempt; exact `127.0.0.1` HTTP callback
  path and per-attempt port; bounded, single-use callbacks.
- First registration uses `dynamic_agent_client`; token exchange and subsequent
  sign-ins use the issued client ID bound to the verified subject. The host ID is
  stable installation metadata, never proof of caller authority.
- Fixed official issuer/resource, validated discovery endpoints, no redirects
  or environment proxy inheritance, finite response size and request deadlines.
- Signed ID-token validation against the fixed issuer's JWKS, including issuer,
  issued-client audience, expiry, issued-at, nonce and selected account identity.
- Token-response scopes control plan permission. Callback scope is never a grant.
- Owner-only directory (`0700`) and SQLite database (`0600`), atomic credential
  replacement, bounded registrations and cross-process refresh serialization.
- Generation fences prevent a refresh or pending sign-in from restoring a
  disconnected account. Rotated token sets are replaced together; ambiguous
  upstream execution is not automatically retried.

This reviewed headless-storage approach protects against other local users under
normal POSIX permissions. It is not encrypted storage, a hardware vault or a
sandbox against a compromised host account. The operator and capability process
can access stored credentials. Do not copy the state directory into a source
checkout, support bundle or another installation. The default directory is ignored
by Git; arbitrary custom paths remain the operator's responsibility.

## Validation and remaining gates

Tests generate their own signing keys and identities and use `httpx.MockTransport`
for every issuer request. Loopback listener tests use actual local sockets. No
real account, production issuer, provider model or paid request is involved.

Live fabric admission additionally needs authenticated caller grants, bounded
usage admission, session-bound offer refresh, managed installation and live
qualification. The existing managed-child example remains mock-only. A linked
account must not be passed into that synthetic caller-authority example.

The implementation follows the current official [registration and sign-in
contract](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
and [account/session lifecycle](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions),
checked on 2026-10-29. Upstream availability and account eligibility still require
an actual consent attempt; source tests cannot establish them.
