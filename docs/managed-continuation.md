# Managed continuation contract

Integrator 0.5.0 provides the author-defined
`openai.inference.continuation@0.1.0` callable using public SDK protocol 4.
The synthetic bundle uses `mock-openai.guarded.continuation@0.1.0` instead.

Its `inference.chat` annotation explicitly identifies the same-owner
`<chat-callable>@0.2.0` relationship for generic HTTP discovery. Existing seven
descriptors and v1/v2 catalog shapes are unchanged. The continuation service first
authenticates the original signed bytes and duplicate-free history. If a `model`
field is present, it must exactly equal the advertised public
`capability/<chat-callable>/<model-slug>` address; only that field is removed for
Responses translation. Unwrapped SDK bodies remain supported. Unknown fields,
different model addresses and duplicate JSON keys refuse before provider egress.
Unified HTTP/Chat use requires a compatible Skulk build advertising the exact
continuation destination. Existing full-history chat retains its behavior.

One bidirectional descriptor accepts two strict signed operation types:

| Input | Output chunks | Caller input |
| --- | --- | --- |
| `ContinuationProof` | `ContinuationEvent` | Exact signed JSON request bytes |
| `ReceiptProof` | `ReceiptOutcome` | Empty successful half-close; no media |

The schemas are in `skulk_inference_reference.continuation_auth` and
`skulk_inference_reference.continuation_wire`. These modules contain no OpenAI
implementation imports. The operation domains are distinct: a receipt proof
cannot authorize generation. The single entry preserves the seven existing
descriptors and keeps generation and lookup on the same exact destination. Neither operation
claims cached metadata admission: lookup spends a nonce and rechecks authority.

## Explicit owner enablement

The managed startup configuration accepts an optional `continuation` object.
Omitting it creates no checkpoint worker/key or continuation admission. Enabling
it requires an explicit storage policy; the following is an illustrative policy,
not a default or upstream allowance:

```json
{
  "inference_enabled": true,
  "continuation": {
    "storage": {
      "retention_seconds": 3600,
      "maximum_records": 128,
      "maximum_storage_bytes": 67108864
    },
    "profiles": []
  }
}
```

The existing selected account and saved integration policy must independently
permit the model and external processing. Only trusted owner code can currently
register/revoke the individual caller key through `CheckpointStore` or its
[owner-authenticated management endpoints](client-enrollment.md); the managed
inference callable never mints a grant. The account-free client CLI keeps private
material on the client. The management page approves public keys, chooses model
scope/expiry, exports public enrollment and revokes clients. Retention/storage
limits are explicit node configuration. No account token, caller private key,
provider URL or private window belongs in startup settings or a descriptor.

`profiles` contains independently established `CompactionProfile` records, at
most one per model. No profile means automatic compaction is off. A profile's
context/headroom, threshold and terminal-snapshot dialect cannot be supplied by
the inference caller or inferred from a response. See
[canonical continuation](canonical-continuation.md) for the qualified empty
terminal variant and the distinction between headroom and output caps. Do not
enable a profile from a model name alone.

## Generation and exact bytes

The control-plane `StreamInvoke` binds the callable ID/version/revision and
carries the client-signed `ContinuationProof`. Its target is the actual startup
transport node and continuation descriptor revision. The capability resolves
the caller's public key/model scope from its owner-issued registry. A shared
gateway key, fabric membership or handle alone grants no authority.

Caller media starts at sequence zero with an empty `started` frame. Sequenced
`chunk` frames carry inline `application/vnd.skulk.chat-request+json` media,
at most 1 MiB per chunk and 16 MiB for the whole signed body. An empty successful
`completed` frame half-closes input. No URL, local path or blob reference is
resolved. Authentication precedes reading document bytes; the complete body's
digest/length and current authority are rechecked before admission/egress.
Whitespace and byte encoding remain exactly as signed, without JSON re-encoding.

Public text, reasoning-summary and function-argument deltas are split into at
most 2,048 Unicode characters per fragment. Tool identity/name appear on their
first fragment; later fragments retain the same index and append arguments.
Worst-case JSON escaping plus bounded identity metadata fits beneath the SDK's
64 KiB packet header. Concatenation preserves the exact public assistant history
used for continuation validation. Opaque provider items never appear in chunks.

The semantic `completed` event includes a receipt only after atomic protected
commit and clean upstream completion/EOF. The SDK transport then completes with
an empty result and `StreamEnd`. A completed transport alone is not successful
inference: inspect the semantic event for failure and recovery classification.

## Receipt recovery

If terminal delivery is lost, sign a fresh `ReceiptQuery` using its distinct
`chat.continuation.receipt@0.1.0` domain. Bind the original request nonce,
conversation and exact generation target, plus a fresh query nonce/deadline.
Send its `ReceiptProof` through the same managed descriptor and half-close empty
input. Any attached media is refused before spending the lookup nonce.

Lookup returns only the current content-free receipt, never assistant content or
canonical state. It rechecks the individual caller/account/model/policy and
fences query replay. Another registered client cannot recover the receipt, even
with its conversation and original request nonce. Lookup performs no provider
submission and does not replay an interrupted attempt.

## Lifetime and qualification

The child holds one stable OS checkpoint-worker fence across all owned SDK calls
and their cleanup. Upload cancellation never reserves a turn. Cancellation after
dispatch retires it as uncertain; abrupt process death is reconciled by the next
exclusively fenced child. No interrupted request is automatically resubmitted.
Independent primary health remains responsive during streaming and lookup.

`tests_managed/test_continuation_child.py` uses actual subprocesses, authenticated
Unix media channels, SDK packet encoding, real client keys and encrypted SQLite.
It covers restart, lost receipt, cross-client lookup refusal, upload/active
cancellation, tampering, large Unicode/function fragments and SIGKILL recovery.
The upstream account/provider is generated and mocked. This public-wire harness
is not the production owner, signed installer, Core HTTP adapter or fleet.

```sh
uv run basedpyright src/skulk_openai/managed.py examples tests_managed
uv run pytest tests_managed
```

An additional account-free acceptance run used the normal Core 2.0.3 verifier,
offline installer and selector with a signed isolated mock runtime. Author SDK
0.5.5 and owner 0.10.4 passed public-key HTTP enrollment, generation, committed
receipt recovery, replay refusal, owner restart, continuation after restart and
revocation. Revoked generation and lookup produced complete refusals without
another provider submission; primary readiness remained available. This run
qualified macOS ARM64 and synthetic upstream behavior only.

Earlier owner/SDK versions could lose a semantic refusal or its transport terminal
when a child refused before consuming upload data. The pinned candidate pair
preserves buffered output and performs a clean output half-close; discarded
transport bytes never enter the handler. The original call deadline, input
validation and terminal/cleanup/EOF checks remain enforced.

Generic HTTP/dashboard adoption, broad model/context qualification and normal
signed installation still require their own evidence. This isolated run does not
enable automatic compaction or update an installed Integrator. No live account
access is needed by these tests.
