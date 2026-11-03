# Skulk OpenAI Integrator

A public learning reference for a managed inference capability built with the
Skulk capability SDK. The capability owns the OpenAI connection, credentials,
account catalog, provider requests and integration settings. Skulk supplies the
generic capability host, chat and unified API.

## Install and configure

**Install the signed Integrator from the built-in Foxlight capability store.** Use a compatible
Skulk 2.0.3 dev build with the generic gateway support in
[Core PR1301](https://github.com/Foxlight-Foundation/Skulk/pull/1301) and catalog
hardening in [Core PR1302](https://github.com/Foxlight-Foundation/Skulk/pull/1302).
A stable Skulk release cut is separate from these dev-branch changes.

1. Open **Plugins → Browse**, choose **Skulk OpenAI Integrator**, review its signed
   permissions and install it. Open its management page from Browse or its
   capability node in Cluster.
2. Choose **Continue with ChatGPT** and complete the official account connection.
   Configure inference, external processing and ChatGPT app usage settings.
3. Select the account models you want to expose. Configure request defaults and
   opt into function tools or supported inline images when needed.
4. Under **Skulk access**, find the local gateway, review its public signing key
   and save settings to approve it for this account, model selection and limits.

The page follows the Skulk Video Studio design language and uses the Skulk mark.
Its primary job is configuration; Diagnostics is a secondary request check.
Normal updates retain account state and settings outside runtime generations.
A standalone page or connected account does not itself install the capability.
See [management](docs/management-ui.md) and [account setup](docs/account-setup.md).

Owner-enabled models appear in the compatible Skulk chat selector and
`GET /v1/models` as `capability/<callable-id>/<model-slug>`. Use the exact returned
ID with `POST /v1/chat/completions`. Chat supports code rendering and client
function tools; a mounted native TTS model can speak the generated prose.
Credentials stay in the capability. See [gateway contract](docs/gateway-chat.md)
for accepted request options and direct-owner authority.

The signed release publishes macOS arm64 and Linux glibc x86_64/aarch64 runtimes.
Normal account-host installation/update, model selection, real API text/tools,
stream cancellation/recovery, and browser code/native-TTS playback have been
exercised on macOS. These are scoped integration results, rather than broad
platform or distributed-routing qualification. The current owner restarts its
child after stream cancellation; reopen its current management link through
Skulk if the previous loopback URL stops answering.

## Try the offline learning example

Accepted milestones live on `main`; further experiments use
`initiative/openai-provider`. Install Python 3.13 and
[uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/Foxlight-Foundation/skulk-openai.git
cd skulk-openai
uv sync --frozen
uv run skulk-openai-demo
uv run pytest
node --test tests_web/*.test.cjs
```

This example runs without a model, GPU, provider account or fleet lease. Its
local-only policy chooses an independent fixture provider and rejects the
synthetic external offer. An explicit external-allowed policy prefers a mock
OpenAI provider. Both use the same text/terminal contract. No socket is opened
for these HTTP mocks, and no provider token is read.

The [managed account child](docs/managed-capability.md) consumes public SDK
interfaces. The [SDK exercise](docs/managed-example.md) describes the explicit
managed tests. Credential-free core CI runs independently of that environment;
it must not be mistaken for managed-owner or installed-runtime qualification.
Public SDK source/package acquisition and clean independent-author reproduction
remain separate work from this first-party signed runtime delivery.

Maintainers should follow the [release recipe](docs/releasing.md), including
immutable source/dependency identities, three-platform and independent rebuild
checks, protected signing, store publication and normal installation readback.
A source merge or unsigned artifact alone is not a published release.

## Contracts and boundaries

The reference includes shared inference semantics with independent callable
identities, processing/usage constraints before routing preferences, finite
signed caller admission and durable replay fences. It exercises fragmented
Responses SSE, explicit completion and errors, output/event limits, cancellation
and HTTP cleanup. See [caller grants](docs/caller-grants.md),
[contract](docs/contract.md) and [security boundaries](docs/security.md).

The original finite text contract remains available. The gateway chat reference
also supports function calls, explicit history, structured output and bounded
inline images. Large-media transfer, paired-client inference authority and
distributed/declarative/algorithmic routing retain their own acceptance gates.
The shared reference contract is not an adopted SDK standard.

The initiative branch also contains an experimental
[streamed request-body contract](docs/streamed-input.md) and text/PDF input
translation. These are capability-side source work; the current signed release
and unified API still use the 0.1 chat contract. Promotion requires generic
gateway support and installed end-to-end qualification.

Live requests require owner consent and capability-owned authority. The API
supports direct local-owner use; model discovery does not grant permission to
arbitrary LAN or paired clients. Hosted availability and usage follow the
connected account and its app settings. Included-only routing remains unavailable
until usage can be verified; no free-usage guarantee is made.

This reference is Apache-2.0 licensed. The authoring SDK 0.5.3 uses MIT; the SDK
owner runtime is separately licensed and its implementation is not vendored here.
OpenAI is a separate service. This repository is not an OpenAI product or an
endorsement.
