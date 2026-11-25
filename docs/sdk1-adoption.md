# SDK1 coordinated-release candidate

This initiative's `0.7.0` source candidate selects the published SDK 1.0.1 packages.
It does not replace the signed `0.6.3` Store preview or promote Skulk to its
release branch. Routing is deferred until after the coordinated release.

| Input | Candidate |
| --- | --- |
| Public Python SDK | `1.0.1` |
| Public author CLI | `0.2.0` |
| Compiled capability kernel | `0.7.0` |
| Separately installed owner runtime | `0.18.1` |
| Application source and native manifest | `0.7.0` |

The application version uses the compiled manifest validator's supported version
syntax. It identifies this source candidate; it is not evidence of publication.
The SDK packages are published. This application remains a coordinated-release
candidate until its matching installed-package qualification and publication pass.

## What changes

The native entrypoint and both author locks select the published public SDK and
kernel. Authoring uses the public CLI without installing Core or the owner into
the author environment. The optional `runtime` dependency group selects the
owner separately. No provider behavior, account connection, management UI or
routing policy changes as part of this adoption.

OAuth, provider credentials, model selection, settings, signed application
requests, encrypted checkpoints and receipts remain capability-owned. The host
continues to enforce installation trust and caller permissions independently.

The regression fixture in `tests_managed/fixtures/sdk010-native4-identities.json`
was measured from the preceding source with SDK `0.5.10rc1` and kernel
`0.7.0rc4`. Tests compare all eight application/native contract bindings, the
complete native declaration excluding the advancing bundle version, and default
settings. They cover both live and distinct mock identities. This detects an
accidental identity, consent, configuration or request-schema change without
deriving the expected values from the new implementation.

## Validate and build

Follow [public authoring](public-authoring.md) for isolated environments and
native source builds. The three-platform CI matrix checks macOS ARM64 and glibc
Linux x86_64/ARM64 independently. Every managed job loads the actual compiled
kernel; every author job installs the hash-locked public packages and builds
the distinct mock native archive. No provider account is used.

The [runtime acquisition guide](native-runtime.md) describes the issued
`0.6.3` closure. Do not use that closure for this source candidate. Its SDK and
kernel requirements differ. The newer generic owner recipe also needs this
application's additional dependencies, including PyJWT; it is not the complete
Integrator runtime by itself. The candidate's frozen `uv.lock` and native spec
are the inputs for assembling that complete closure.

## Acceptance boundaries

Passing source/process tests and building an unsigned archive are prerequisites
for installed qualification. A matched, independently signed candidate must
still pass normal Core/owner installation, fresh disconnected setup, protected
state adoption, authorized Chat/API calls, continuation/receipt recovery and
update/rollback checks. Synthetic fixtures must not use an existing account
store or contact OpenAI. Real-account model, compaction and speech observations
in the [0.6.3 record](native-qualification-0.6.3.md) remain scoped to its recorded
implementation; this dependency update does not rerun those experiments.

Application publication, coordinated Skulk admission and refreshed
operator documentation remain separate release steps. The candidate must retain
account state and current replay/CAS history during update and rollback;
restoring an older journal is not a valid rollback test.
