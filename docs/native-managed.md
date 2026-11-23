# Native managed entrypoint

The experimental `skulk-openai-native` entrypoint consumes public SDK
**0.5.16rc1** and native **0.7.0rc7** in the `0.7.0` source candidate.
See the [SDK1 adoption scope](sdk1-adoption.md); the issued `0.6.3` runtime remains
on its independently recorded dependency closure. OAuth, sessions, model catalog, settings,
provider requests and encrypted continuation history remain capability-owned.
The legacy entrypoint remains available and uses the same application services.

```sh
uv sync --frozen --group managed --group native-runtime
uv run --no-sync basedpyright --project pyrightconfig.managed.json
uv run --no-sync pytest tests_managed
```

These process checks use generated accounts, actual native codecs and mock
upstream transports. They are separate from normally signed host installation,
account-backed model qualification and stable release admission.

An earlier macOS 12-check installed gate exercised the actual source bundle
through the normal signed publisher, Core installer and owner with an explicitly
built owner 0.18 candidate. It recovered a populated legacy checkpoint, enrolled
the actual installed child, discovered all eight native contracts, performed a
bearer-authorized bidirectional continuation, disabled the child normally,
recovered the new receipt through legacy rollback and refused signature replay.
Private backup verification and quarantined restore also passed. This synthetic
result does not qualify a real account, other platforms, distribution, native UI
opening or the unified chat adapter.

## Independent contract identities

`skulk_openai.native_contracts.build_native_manifest` builds and actually admits
all eight declarations with the released compiled kernel. Native descriptors
have complete 64-character revisions. Existing application proof contracts and
checkpoint targets retain their original 16-character revisions. Neither value
is padded, truncated or reinterpreted as the other.

A native request uses this explicit envelope:

```json
{
  "contract": "openai.application@1.0.0",
  "revision": "<original application revision>",
  "payload": {}
}
```

Signed descriptor annotations select `skulk.application-envelope@1.0.0` with
`inference.envelope`. `inference.envelope.contract` names the opaque envelope
contract. `inference.application.id`, `.version` and `.revision` identify the
independent application proof destination. The `inference.catalog`,
`inference.contract`, `inference.authorization` and `inference.chat` annotations
retain the generic catalog/chat/continuation relationships. Core can interpret
these declarations without importing the provider or recognizing its name.
The envelope's payload is validated by the unchanged strict application models.
This preserves their Unicode identifier rules, which cannot be re-exported as
portable/2 regular expressions. The envelope is a distinct native contract;
it does not claim that its outer schema replaces domain validation.

Metadata reads declare local processing. Generation, both chat versions and
continuation declare external inference. SDK startup, composition, exact target,
mode, native revision and request-byte checks precede application behavior.
Application grants, signatures, nonces, account generation, model permissions
and owner settings remain independently enforced. A context or migration receipt
cannot grant caller authority.

JSON control values obey portable/2's 64 KiB bound. Large request bodies use
binary caller media. Large text and function-argument deltas are fragmented
without truncation. A completed transport terminal establishes transport cleanup;
clients must also interpret the inner chat/continuation outcome.

The native adapter drains remaining caller input before publishing a common
transport terminal, including a refusal issued before authentication consumes
the body. This avoids closing the duplex socket while the host is writing the
input opener. Discarded input is bounded to 1 MiB per chunk and 16 MiB across
the call, with a 30-second remaining-input cleanup deadline. It is not parsed,
persisted or sent to the provider. Cancellation, overflow and malformed input
still fail closed; draining never retries or grants a rejected request. The
cleanup deadline is an I/O resource bound, not a generation duration ceiling.

Native HTTP continuation accepts its declared native chat ID/version and the
signed model slug in the `model` address. Core independently owns and verifies
the installation routing component; that label confers no account authority.
The capability verifies the original signed body bytes before interpreting the
transport selector. It never substitutes a legacy address or re-encodes the
body to make a signature pass. Legacy hosting retains its exact legacy-address
validation; SDK callers may omit the HTTP `model` envelope as before.

This candidate changes native descriptor revisions. Existing experimental
installations must explicitly enroll the new exact declarations under the
normal offline snapshot procedure. There is no identity conversion or automatic
grant advancement. No customer compatibility migration is required for this
development candidate; preserve any connected development account and its
useful state when cutting it over.

## Fresh setup

The `0.6.0` preview candidate can initialize a new disconnected private state root
without a legacy node identifier or migration receipt. Its actual installed
child becomes the new independent application proof target. The protected
`native-initialization.json` record binds the journal's host identity, child,
bundle and exact contract families. It grants no account or caller authority.
An existing populated root is never silently repurposed. See the
[operator setup guide](native-setup.md) for configuration and access steps.

Fresh creation stages its journal under a private directory and publishes it
without replacing existing files. The shared pending marker quarantines a
partial publication for both new and older application code. Continuation
storage and its encryption key are not enabled by this setup.

## Protected state adoption

Existing-state adoption supplies an explicit original proof target and private root:

```json
{
  "legacy_target_node": "unchanged-application-proof-target",
  "application": {
    "state_directory": "/absolute/private/state",
    "inference_enabled": true
  }
}
```

The inherited startup supplies the actual installed native child identity. The
existing-state path requires a private adoption receipt matching that child,
bundle, original store identity and both contract families before it opens or
recovers the application journal. There is no default-root fallback.

Stop the installed provider through its owner before offline operations. This
also applies to older children that predate the application fence. The tools
use stable process/session fences, a database write fence, private files and
coherent SQLite/key snapshots. They never unlink worker-lock inodes.

```sh
uv run skulk-openai-state snapshot \
  --state-root /absolute/private/state --output /absolute/private/backup
uv run skulk-openai-state verify --backup /absolute/private/backup
uv run skulk-openai-state adopt \
  --state-root /absolute/private/state --backup /absolute/private/backup \
  --native-node-id actual-installed-child \
  --legacy-target-node unchanged-application-proof-target
```

Use `--mock` only for the independently identified synthetic bundle. A stale
snapshot is refused after a revocation, nonce use or other journal mutation.
Snapshots contain provider secrets and remain private; they are not release
artifacts. Core API credentials and manager identity are separate custody and
are not included in capability snapshots.

Rollback selects the legacy runtime against the **current** journal and original
logical proof target. It does not restore an older snapshot or reset replay/CAS
history. Process tests exercise legacy → native → legacy continuation with the
same keys, receipts and encrypted records.

Disaster restore always creates a new quarantined root:

```sh
uv run skulk-openai-state restore \
  --backup /absolute/private/backup --output /absolute/private/recovered
```

It retains encrypted records, known nonces, budgets and CAS versions, advances
account/sign-in fences, revokes old application grants and continuation clients,
clears gateway enrollment and disables inference. Fresh explicit enrollment is
required. An interrupted restore retains a pending marker that prevents serving.
The recognized credential journal is published only after the copied state is
reverified and its authority is quarantined, protecting older readers that do not
understand the pending marker. Publication refuses an existing journal.
Existing roots are never overwritten and uncertain submissions are never replayed.

## Native models in Chat and the unified API

The coordinated [Core candidate](https://github.com/Foxlight-Foundation/Skulk/pull/1390)
adds generic native model discovery, Chat Completions and continuation-receipt
adapters. It remains on the experimental startup branch; this is not a stable
Skulk release or a newly published Integrator runtime.

An authorized Core owner or enrolled client discovers its permitted native
models through `GET /models` or `GET /v1/models`. Use the returned model ID
unchanged in `POST /v1/chat/completions`:

```text
native/{installation}/{native-chat-id}@{version}/{model-slug}
```

Core caller enrollment and exact native grants are separate from capability
account/model policy and application signatures. A dummy local-model API key
does not inherit owner access. Chat uses authenticated discovery and keeps an
unavailable native selection until the user explicitly chooses another model.

Continuation receipt lookup uses `POST /v1/chat/continuation/receipt` with the
saved model ID in its `native_model` query parameter. The signed application
proof target and original body bytes remain unchanged. Receipt recovery never
resubmits inference or falls back to a legacy transport. Code, tools, media and
existing mounted-speech composition use the shared Chat serializers; the
capability remains responsible for provider translation and account settings.

Qualification covers one installed Integrator. Broader catalogs retain a Core
follow-up: caller permission filtering must precede the eight-metadata-candidate
read limit so inaccessible earlier catalogs cannot hide a permitted later one.

## Native candidate publication

The [public authoring guide](public-authoring.md#explicit-native-candidate-tools)
identifies the separate SDK/tools candidate and its explicit build path. The
native declaration, runtime owner and Core remain independently selected.
Qualified signed macOS installation with synthetic populated state now covers
26 checks through the normal installer and owner: exact caller enrollment,
metadata and model discovery, streamed/collected Chat API, independent application
signature refusal, continuation and receipt recovery without replay, management
UI/settings, browser key approval, two-tab continuation, withdrawal,
current-journal rollback and quarantined restore. Provider requests use a mock
transport. This does not qualify account-backed native inference or actual
mounted speech, Linux Integrator installation, public first-user onboarding or
coordinated runtime/catalog distribution. The owner/Core host layer has separate
three-platform qualification. No existing connected account or history was
changed by this synthetic gate.

## Management surface

The same capability-owned settings UI runs under native setup/cleanup. Frozen
native health accepts HTTPS links or private proxy ports; the local HTTP UI
therefore declares a proxied surface and reports its actual private port. It
does not publish an HTTP URL under a false HTTPS scheme. A qualified native
owner can bind that private listener to an isolated loopback management origin;
Core exposes its ready Open action. Browser settings and CSRF are handled by
the capability. The coordinated native Chat/API candidate above supplies discovery. Replacing
an existing connected development installation still requires compatible signed
packages, exact caller enrollment and preservation of its useful account and
conversation state.
