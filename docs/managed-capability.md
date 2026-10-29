# Managed account capability

`skulk_openai.managed` is the package's public-SDK child entrypoint. It owns
connection health, cached discovery, signed caller admission, streaming and
cleanup. No provider-specific endpoint, engine or state field is added to Skulk.

The live bundle identity is `reference.openai`; its three descriptors are:

- `openai.inference.offers@0.1.0`: cached choices, target transport node and catalog
  revision. Discovery initiates no upstream request.
- `openai.inference.generate@0.1.0`: server streaming with a `SignedInvocation`
  input, common text chunks and explicit completion/failure.
- `openai.inference.status@0.1.0`: cached connection state and this child's
  request-start count, without account identity or credentials.

The generate descriptor advertises `text.generate@0.1.0` and the experimental
`reference.ed25519@0.1.0` authorization requirement. Its exact schema/revision is
part of each signed call. The manifest declares a governed effect; the child
independently enforces a finite [caller grant](caller-grants.md).

## State, settings and setup

Owner settings contain `inference_enabled` (default false) and an optional
absolute `state_directory`. They contain no provider URL, token, caller key or
callback URL. Generic owner enablement starts the child; inference additionally
requires `inference_enabled`, an authorized account, fresh choices and a valid
caller grant. Management remains available when inference offers are withdrawn.

The default account store is `~/.local/state/skulk-openai`, stable for this user's
tool installation on this host. Setup and the child use the same helper;
neither derives credentials from the shell directory or an ephemeral SDK
generation. For isolated or separate installations, choose a dedicated absolute
`state_directory` and pass that directory as `--state-dir` to setup. This is an
operator-chosen location, not a private SDK internal coordinate.

`__setup__.py` delegates to the [owner-local account command](account-setup.md).
A compatible signed installation can invoke it through
`skulk-plugin-service setup-plugin MANAGED_ID -- ...`. That command's existence
does not mean this reference passed signed installation. Account consent, setup
and grant issuance never automatically enable inference.

For an explicitly enabled live profile, an owned worker refreshes account model
metadata every thirty seconds, including serialized token renewal as necessary.
It never generates text or renews caller grants. Snapshots expire after sixty
seconds; failures preserve cached facts only until their original expiry, then
offers withdraw. Health, status and discovery read local facts without initiating
refresh. The worker is cancelled and joined at shutdown.

Disconnect explicitly clears/fences local access and attempts remote revocation.
Removing a child or package does not itself prove remote revocation; disconnect
through setup or ChatGPT Settings when retiring the account connection.

## Account-free validation

The immutable test identity `reference.openai.mock` has distinct
`mock-openai.guarded.*` descriptor IDs. It requires an isolated explicit state
directory, uses a finite mock Responses transport, and never runs the live
metadata worker. It cannot advertise the live profile's callable IDs.

Core CI has no separately licensed SDK and excludes this entrypoint from its
type check. With SDK access, run the additional checks:

```sh
uv run basedpyright src/skulk_openai/managed.py examples tests_managed
uv run pytest tests_managed
```

Twelve SDK process tests cover the legacy fixture/mock examples and the guarded
child: startup, health, discovery, signed stream, direct forgery, replay,
disable/disconnect, restart and cancellation without nonce refund. All accounts,
keys and upstream responses are synthetic. A disconnected live-profile exercise
also proves its idle metadata worker shuts down without account access.

An additional isolated qualification used the actual managed owner and current
Skulk dev adapter with a separately installed reference wheel, no resident node
and no peer discovery. It exercised the independent fixture and guarded provider
on one owner, streaming, direct replay rejection, cached management and disconnect
withdrawal. It also routed the same input under local-only and external policies,
rejected a direct hard-policy bypass, and verified owner-driven cancellation,
restart and preservation of the consumed budget. This is source/wheel evidence,
not signed installation,
real-provider access or cross-node fleet qualification. Private host code remains
outside this public reference.

## Signed delivery gate

`capability.toml` declares the entrypoint, setup shim, validator and license.
The validator requires exact descriptor/settings schemas and a governed effect.
The available publisher rejects this reference's additional `jwt` dependency and
sibling `skulk_inference_reference` namespace: capability-specific dependency
roots are not supported by its signed inventory. This was reproduced with an
ordinary public-SDK wheel in the publisher environment.

Resolve that generic contract through declared, locked and verified dependencies
and an explicit package/namespace solution. Do not hide dependencies in a host
launcher, vendor private SDK source or skip inventory verification. Until that
gate passes, no signed install, upgrade/rollback, clean-author reproduction or
leased distributed regression is claimed.
