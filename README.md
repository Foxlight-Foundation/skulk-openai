# Skulk OpenAI Integrator

An experimental, public learning reference for building managed inference
capabilities with Skulk. Implementation lives on `initiative/openai-provider`.

The first slice provides a draft shared text-generation contract, deterministic
routing, a credential-free OpenAI Responses adapter, an independent fixture
provider, and a managed-child example using the public capability SDK interface.
It runs locally without a model, GPU, account or fleet lease.

The session foundation also provides explicit [owner-local account
setup](docs/account-setup.md), protected credentials, verified identity, serialized
refresh, disconnect fencing and account-bound catalog/Responses primitives. It is
separate from the older mock managed-child example. Automated tests need no account.

The [caller-grant experiment](docs/caller-grants.md) adds capability-owned signing
keys, finite admission budgets and durable replay fences. Its standalone
`InferenceCapability` boundary enforces hard policy before the HTTP client.
The [managed account child](docs/managed-capability.md) consumes public SDK
interfaces. Maintainer qualification has exercised live requests, isolated signed
installation, and Core selection/launch of the installed SDK owner. Those local
proofs use an explicit test host attestation; public distribution, clean-author
reproduction and distributed qualification remain open.

## Try the offline example

Install Python 3.13 and [uv](https://docs.astral.sh/uv/), then:

```sh
git clone --branch initiative/openai-provider https://github.com/Foxlight-Foundation/skulk-openai.git
cd skulk-openai
uv sync --frozen
uv run skulk-openai-demo
uv run pytest
```

The default local-only policy chooses the fixture provider and rejects the
synthetic external offer. An explicit external-allowed policy prefers the mock
OpenAI provider. Both return the same text/terminal contract. No socket is opened
for these HTTP mocks, and no provider token is read.

## Manage the capability

The [owner-local management page](docs/management-ui.md) provides connection
status, connect/reconnect/disconnect, account model selection and persistent
integration settings, using the Skulk Video Studio design language and Skulk mark.
A secondary diagnostic checks one streaming request. The experimental
[gateway chat contract](docs/gateway-chat.md) connects delegated models to Skulk
chat and its unified API on compatible builds:

```sh
uv run skulk-openai-ui --enable-inference
```

Opening the page sends no provider request. Account setup and prompts require
explicit actions. The managed child also declares this page as a generic SDK
surface. Plugins and topology show it only after the capability is installed
under the host's plugin manager; a standalone page or connected account does
not create an installation.

## What this proves

- Shared inference semantics with independent callable identities.
- Hard processing-domain, usage, context and synthetic-grant constraints before
  preferences; deterministic selection independent of offer ordering.
- Offer withdrawal and dispatch rechecks for grant, principal, target and input.
- Fragmented Responses SSE, explicit completion, incomplete/error handling,
  output/event limits, cancellation and HTTP resource cleanup.
- A real child process using public SDK startup, health and streaming interfaces;
  see the explicit [SDK exercise](docs/managed-example.md).

The SDK protocol harness is not the production managed owner, a connected Skulk
node, signed installation or a distributed-fabric test. Core CI runs without the
separately licensed SDK and does not claim those managed checks.

## Boundaries

The offline demo deliberately uses `httpx.MockTransport`. A separate owner-only
client supports explicit official API requests after consent; it is not exposed
through the synthetic managed child. The shared contract is a draft reference,
not an adopted SDK standard. The original finite text contract stays available;
the gateway chat experiment additionally supports function calls, explicit history,
structured output and bounded inline images. Large-media transfer and distributed
routing remain separate work. See [contract](docs/contract.md) and
[security boundaries](docs/security.md).

The offline demo's grants are trusted in-process test inputs. The separate
capability-owned signing experiment does not establish authenticated human caller
propagation in the SDK. Protected credentials and caller authority must be
enforced across every fabric entry path.
Live requests require explicit account consent and a finite signed caller grant.
Published packages, upgrade/rollback and cross-node qualification remain open.

This reference is Apache-2.0 licensed. The capability SDK has its own license and
is not included here. OpenAI is a separate service; this repository is not an
OpenAI product or an endorsement.
