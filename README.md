# skulk-openai

An experimental, public learning reference for building managed inference
capabilities with Skulk. Implementation lives on `initiative/openai-provider`.

The first slice provides a draft shared text-generation contract, deterministic
routing, a credential-free OpenAI Responses adapter, an independent fixture
provider, and a managed-child example using the public capability SDK interface.
It runs locally without a model, GPU, account or fleet lease.

The session foundation also provides explicit [owner-local account
setup](docs/account-setup.md), protected credentials, verified identity, serialized
refresh, disconnect fencing and account-bound catalog/Responses primitives. It is
separate from the mock managed-child
example and does not enable live fabric calls. Automated tests need no account.

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
not an adopted SDK standard. Tool calls, larger contexts and additional modalities
remain future work. See [contract](docs/contract.md) and
[security boundaries](docs/security.md).

Synthetic grants are trusted in-process test inputs, not authenticated production
caller authority. Real accounts must not be made callable until protected
credentials and caller authority are enforced across fabric entry paths. Actual
account consent, live inference, publication and installed qualification remain
open work.

This reference is Apache-2.0 licensed. The capability SDK has its own license and
is not included here. OpenAI is a separate service; this repository is not an
OpenAI product or an endorsement.
