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

The manifest also declares a credential-free, loopback [management web
surface](management-ui.md). Correlated health replies report its actual URL;
existing generic Skulk adapters can show its open action. Management stays
available while inference is disabled or the account is disconnected. The child
owns the listener and cancels browser tests/sign-in on shutdown. Declaring this
surface does not install or activate the package in a running host.

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
`skulk-plugin-service setup-plugin MANAGED_ID -- ...`. The local installed proof
covers managed inference; the installed setup journey is a separate check.
Account consent, setup and grant issuance never automatically enable inference.

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

The SDK process tests cover the legacy fixture/mock examples and the guarded
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

## Declared packaging and delivery

`capability.toml` declares `PyJWT>=2.10,<3` and the project-local
`skulk_inference_reference` source package alongside the entrypoint, setup shim,
validator and license. The candidate toolchain is author SDK 0.5.1 with owner
0.10.0. It adds declared capability roots to the complete signed wheel closure;
it does not require an OpenAI-specific publisher or Skulk integration.

`skulk_openai.managed:build_manifest` is the public publishing factory. For
example, after installing the separately licensed author SDK:

```python
from skulk_openai.managed import build_manifest

manifest = build_manifest(
    executable="bundle.pyz",
    executable_sha256="0" * 64,
)
```

The publisher replaces that placeholder with the measured archive hash. The
factory declares `runtime_dependencies=("pyjwt",)`, a governed effect and the
exact descriptor/settings schemas. The validator refuses drift in those claims.
The mock factory profile has its own bundle and callable identities.

The publisher's lock must contain the licensed owner/SDK packages and declared
capability libraries. This repository's core-only `uv.lock` does not contain the
licensed owner. With a compatible publisher lock, `capability-wheelhouse --spec
capability.toml --lock <publisher-lock> --output <wheelhouse>` builds the union.
Use that wheelhouse with `capability-build --spec capability.toml`; declared
dependencies require its isolated v2 runtime. Missing, extra, incompatible or
tampered wheels remain refused. Account state stays outside the generation.

Maintainer-local qualification has built and installed a signed 29-wheel runtime
with real library wheels and no private SDK source in the capability archive.
An exported-Core harness with an explicit test host attestation exercised the
generic installer, selector and launcher, selected SDK owner, live completion,
hard-policy denial and replay rejection. These are local integration proofs;
they do not establish a deployed physical node or public package availability.
The SDK protocol tests above remain fully synthetic.

Obtain SDK packages through their supported licensed distribution/access process.
Public toolchain distribution, clean-author reproduction, installed setup,
upgrade/rollback and cross-node qualification remain separate delivery checks.
Do not hide dependencies in a host launcher, vendor private SDK source or bypass
inventory verification.
