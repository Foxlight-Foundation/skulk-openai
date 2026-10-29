# Capability-owned caller admission experiment

Account consent and permission to invoke that account are separate. The current
public SDK does not hand the child an authenticated human/caller grant. This
reference implements its own second authorization layer inside the capability,
without OpenAI-specific Skulk changes or private SDK owner imports. It is an
experimental mechanism, not an adopted SDK authorization standard.

An owner issues a finite grant for selected models on one account generation.
The caller gets a distinct Ed25519 signing key; the provider stores its public
key and grant policy. That key cannot obtain, refresh or revoke OpenAI tokens.
Each invocation signs the exact managed target, descriptor revision, catalog
revision, model, input, hard requirements, nonce and expiry. Unordered policy
sets are canonicalized before signing.

The capability derives offer facts from its own protected catalog. Processing
is external and usage is unknown; a caller cannot relabel either. The shared
selector's hard policy checks run independently at this provider boundary.
`minimum_context_tokens: null` explicitly means no context-size requirement;
a positive requirement still rejects unknown context. The default remains a
positive bound and fails closed.

## Finite delegation

After owner consent, issue a caller credential locally:

```sh
uv run skulk-openai-account --state-dir ./openai-account issue-grant --model RETURNED_SLUG --maximum-attempts 1 --lifetime-seconds 300 --allow-external-unknown-usage
```

The command refreshes the catalog, writes a private `caller-<grant-id>.json` under
the protected state directory, and prints only its path and grant ID. Transfer
that file to the intended caller through an operator-controlled confidential
channel. Preserve owner-only permissions. Keep it out of ordinary settings,
logs, support bundles and source control. There is no automatic key distribution
or renewal service.

Bounds are eight models, 64 admission attempts, 4096 output characters per request
and one hour of validity. At most 32 unexpired grants are retained. These local
bounds are not upstream billing caps. Explicit acceptance of external processing
with unknown usage is required; there is no inferred included-credit guarantee.

Withdraw new admission locally with:

```sh
uv run skulk-openai-account --state-dir ./openai-account revoke-grant --grant-id GRANT_ID
```

Disconnect and reauthorization invalidate old grants through the account
generation. Refreshing the same session preserves that generation. Reconnection
requires new delegation and cannot silently resurrect a withdrawn grant.

## Signing and dispatch

`skulk-openai-sign` reads a private caller credential and one bounded common
request JSON from stdin. Supply the actual discovered target, descriptor and
catalog revisions and a granted model:

```sh
uv run skulk-openai-sign --credential-file CALLER_FILE --target-node TARGET_NODE --descriptor-revision DESCRIPTOR_REVISION --catalog-revision CATALOG_REVISION --model RETURNED_SLUG --allow-external-unknown-usage < request.json
```

Stdout is the confidential one-use invocation payload, including its input. Pass
it through the managed callable's negotiated schema; do not log it. The helper
sends no HTTP and reads no OpenAI token. Programmatic consumers may construct
`Invocation` with stricter requirements and use `CallerCredential.sign()`.

The provider checks the active account/generation, grant, exact target, fresh
catalog, signature, hard policy, deadline, nonce and remaining attempts. One
SQLite transaction reserves the nonce and decrements the budget before the
upstream client can submit content. This commit is the admission point.
Disconnect/revocation that commits first prevents admission. Work admitted
earlier may already be in flight; cancellation does not promise upstream refund.

No attempt is refunded after failure, cancellation or uncertain submission.
There is no automatic retry. Nonce fences survive restart and remain until the
grant expires. Expired grants can be pruned because their proof deadlines can no
longer be accepted. The journal stores grant policy and random nonces, without
prompts, responses or request digests. Discovery uses at most sixteen cached
model choices and never performs HTTP or refreshes tokens.

## Trust and qualification

The operator OS account, capability process and existing confidential fabric
transport remain trusted. A signing key authorizes its holder; a label or peer
ID does not authenticate a human. A captured valid invocation is short-lived
authorization material and could be raced once, so transport and request-log
confidentiality are required. This adds no OS sandbox or compromised-host custody
guarantee. Key theft permits the remaining delegation; revoke and replace it.

Source tests exercise actual signatures, durable admission and a mock HTTP sink:
forgery, target/input/policy alteration, replay, budget races, hard privacy/usage
denials, expiry, restart, revocation and account replacement. They do not prove
actual owner/core delivery, signed installation, cross-node transport or real
account behavior. A future public SDK authenticated-caller contract should
replace this experiment when available.
