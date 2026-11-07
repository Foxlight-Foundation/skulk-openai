# Preparing an Integrator release

This is the maintainer path for the experimental managed capability. It uses
the SDK's publisher and catalog tools; it does not publish a release or make
the SDK available to other developers. Obtain those tools through the SDK's
licensed acquisition process. A source checkout or a locally built wheel is
not evidence of supported public package delivery.

## Inputs and custody

Prepare these inputs outside the source checkout:

- A reviewed source revision with matching project and manifest versions.
- A compatible author SDK, owner and publisher. Integrator 0.5.1 uses
  author SDK 0.5.5 and owner 0.10.5, with cryptography 50.0.2 and AnyIO 4.16.0
  in its qualified lock. Publisher-only aiohttp is 3.14.5. The preceding
  Integrator 0.5.0 used owner 0.10.4, including early-refusal stream cleanup fixes.
  Installed Integrator 0.4.5 was qualified with SDK 0.5.3 and owner 0.10.2;
  candidate source checks do not update that installation.
- The publisher's lock and complete wheelhouse, including the owner
  and MIT SDK plus this capability's declared dependencies. The root
  `uv.lock` now includes the opt-in `publisher` group; ordinary core checks
  use the default development group without SDK access.
- A publisher identity and protected Ed25519 private-key file. Never commit
  the key, paste it into command arguments or package it with the capability.
- A new monotonic release sequence, expiry and permanent HTTPS feed address.
  Do not reuse an issued sequence for different bytes. Catalog revisions
  have their own monotonic history.
- The Skulk source used for provenance and a compatible installed reader.
  The manifest's version range controls admission; a provenance digest is
  not an exact-build compatibility allowlist.

Keep account state, browser consent, provider tokens, caller grants, package
credentials and logs outside release inputs. Package only the source namespaces
and data files declared in `capability.toml`. SDK implementation belongs in its
separately licensed runtime artifacts, never in this Apache-licensed archive.

## Build with the public interfaces

Run from the reviewed source checkout with the licensed author tools available.
Set the following shell variables to absolute, operator-controlled paths or
release values: `INTEGRATOR_BUILD`, `PUBLISHER_LOCK`, `PUBLISHER_KEY`,
`PUBLISHER_ID`, `RELEASE_SEQUENCE`, `RELEASE_EXPIRY`, `CATALOG_REVISION`,
`CATALOG_EXPIRY`, `RELEASE_FEED_URL` and `SKULK_SOURCE`. The build directory
must be new and private; the feed URL must identify this release's immutable
HTTPS directory and end with `/`. Expiries are UTC Unix seconds.

```sh
umask 077
mkdir "$INTEGRATOR_BUILD"
export INTEGRATOR_BUILD
uv run python - <<'PY'
import os
from pathlib import Path
from skulk_openai.managed import build_manifest, validate_manifest

manifest = build_manifest(executable="bundle.pyz", executable_sha256="0" * 64)
validate_manifest(manifest)
target = Path(os.environ["INTEGRATOR_BUILD"]) / "manifest.json"
target.write_text(manifest.model_dump_json())
target.chmod(0o600)
PY

capability-wheelhouse --spec capability.toml --lock "$PUBLISHER_LOCK" \
  --output "$INTEGRATOR_BUILD/wheelhouse"

capability-build --spec capability.toml \
  --manifest "$INTEGRATOR_BUILD/manifest.json" \
  --wheelhouse "$INTEGRATOR_BUILD/wheelhouse" \
  --private-key "$PUBLISHER_KEY" --publisher "$PUBLISHER_ID" \
  --sequence "$RELEASE_SEQUENCE" --expires-at "$RELEASE_EXPIRY" \
  --state-schema openai-reference-v1 \
  --permission 'Owner-controlled account connection and external inference under the connected provider account usage settings' \
  --permission 'Keep encrypted continuation checkpoints on this device only when owner-enabled' \
  --skulk-source "$SKULK_SOURCE" --output "$INTEGRATOR_BUILD/release"

capability-catalog --publisher "$PUBLISHER_ID" \
  --private-key "$PUBLISHER_KEY" --revision "$CATALOG_REVISION" \
  --expires-at "$CATALOG_EXPIRY" \
  --release "$INTEGRATOR_BUILD/release" "$RELEASE_FEED_URL" \
  --output "$INTEGRATOR_BUILD/catalog.json"
```

The publisher replaces the placeholder executable hash with the measured
archive hash and verifies the runtime closure. Do not bypass a missing,
extra, incompatible or tampered-wheel refusal. Build on each target platform
and qualify it before claiming that platform; one macOS build does not
establish Linux delivery.

## Verify before publication

Record source revision, tool versions, lock digest, every artifact's size and
SHA-256, publisher public key, sequence, expiry and runtime platform. Read back
the catalog and release with the target Skulk reader and verify every named
artifact. Independently repeat the build with identical inputs and compare the
archive and wheel inventory. Never treat a signature alone as behavioral proof.

Use a private test feed first. Through normal **Plugins → Browse**, connect
the catalog, review the exact release, accept its permissions and install it.
Check the displayed title, usage disclosures and settings. An absent billable
Steward declaration is not a free-usage guarantee. Hosted models come from the
connected provider account, not the native Model Store.

Set a dedicated account-state directory for an isolated test. Confirm management
works while disconnected and inference is disabled. On the authorized account
host, separately exercise connection/settings, enabled model discovery, chat,
the documented direct-owner API, cancellation and restart. Do not move account
tokens to another host to qualify installation. Verify update, rollback and
uninstall while preserving account state and unrelated installed capabilities.

For publication, use a durable supported HTTPS location and approved publisher
trust. Retain issued release bytes unchanged; renew expiring catalog metadata
through its supported signing process. A temporary test server, short-lived
test trust or workflow artifact is not a supported distribution channel.
Supported package acquisition and independent clean-author reproduction remain
separate gates from a maintainer installation. See [managed packaging](managed-capability.md)
and [gateway access boundaries](gateway-chat.md).

## Central first-party publication

Integrator 0.5.0 requires Skulk 2.0.3 or newer and retains owner-local gateway
access. Install the maintained publisher environment explicitly with
`uv sync --frozen --group publisher`; use the root lock for wheelhouse generation.
The SDK source pins in this group are immutable maintainer inputs, not a claim
that private repository access is the public SDK acquisition experience.

The central publisher builds this public repository at a full reviewed source
commit through its generic external-capability workflow. It qualifies the
three runtime platforms and an independent rebuild, then signs through the
existing protected publisher environment. Store publication is a separate
verified step. Neither provider credentials nor publisher keys belong in this
repository or its release inputs. Core-only CI remains credential-free.

Normal installation uses Plugins and the built-in capability store. Keep the
connected account on its current host; settings and account state live outside
SDK generations. After Core's generic gateway support is present, use the
Integrator's settings to enable its model catalog and authorize the local gateway.
A main-branch source merge or an unsigned workflow artifact alone does not prove
a published or installed release.

Version 0.4.2 opts cached offers, status and chat discovery into the SDK's bounded
metadata admission lane, keeping those local reads independent of a long model
response. Generation retains the ordinary single-workload admission limit.
The account state schema and permissions are unchanged, so normal updates retain
the existing connection and settings.

Version 0.4.3 keeps gateway authority stable when an account refresh renews only
the expiry of identical model facts. Current freshness and independent proof
expiry remain enforced. Account generation, model claims and every owner policy
field still participate in the gateway revision; changes reject before egress.

Version 0.4.4 makes the local request and output caps opt-in and enables image
input by default. Existing saved numeric caps and image choices are preserved;
owners can disable each cap independently in settings. Account state, permission
declarations and the state schema are unchanged.

Version 0.4.5 adds bounded signed media input and inline UTF-8 text/PDF
documents, plus model/policy-aware document metadata on the v2 catalog. The v1
contract remains compatible. Account state, permission declarations and the
state schema are unchanged; no additional document permission switch is added.

Version 0.5.0 adds independently approved client keys, exact-body managed
continuation, encrypted checkpoint custody, receipt lookup and public-key
management controls. The first seven descriptors and their v1/v2 catalog shapes
remain unchanged. New HTTP/Chat use requires explicit continuation metadata from
the gateway; version compatibility alone does not establish that adapter's
availability. The signed permission summary discloses opt-in local checkpoint
storage. Normal updates retain the connected account and integration settings;
checkpoint storage remains off unless explicitly configured. Automatic compaction
has a separate model/context qualification requirement and stays off without a
profile. Do not infer token savings from smaller encrypted records.
