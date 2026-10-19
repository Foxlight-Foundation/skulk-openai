# Draft inference contract

`skulk_inference_reference` contains experimental, vendor-neutral semantics.
It never imports `skulk_openai`. A later accepted public SDK contract should
replace this reference; other providers need not import the OpenAI adapter.

The logical contract is `text.generate@0.1.0`. Callable IDs remain provider-specific:
`fixture.inference.generate` and `mock-openai.inference.generate` can coexist.
The recognized annotation in the SDK example is discovery data, not authorization.
There is no automatic generic discovery in core implied by this marker.

Requests contain bounded user/developer/assistant text messages and a local output
character limit. Encoded input is capped at 32 KiB, leaving control-envelope room;
this is not a general large-context input path. Tool execution, tool-call output,
images, audio, provider URLs and request-supplied grants are unsupported.

Offers carry model/callable identities, descriptor/offer revisions, processing
domain, usage class, readiness, context evidence and expiry. Expiry uses UTC epoch
seconds; offline fixtures inject a fixed clock. The managed example uses a bounded
five-minute synthetic snapshot and must be restarted after expiry. It makes no
claim about a real account's eligibility, credits or quota.

Selection first intersects caller requirements with the synthetic owner's grant.
Unknown context cannot satisfy a requirement; unknown usage is not free usage.
Only eligible offers enter preference ordering. Ties use stable offer identity;
duplicate identities are rejected. Dispatch binds principal, authority revision,
exact offer and request digest. The digest stays in process, not routine logs.

Common events are text deltas, completion or a bounded typed failure. Only an
observed `response.completed` produces completion. Errors, incomplete responses,
EOF, malformed events and output limits remain distinct failures. Unsupported
stream events fail closed in this text-only adapter. No automatic retry occurs.
SSE events are bounded to 16 KiB each, 256 events and 256 KiB total.

The managed bridge maps common text to SDK chunks, completion to its completed
terminal and common failures to `provider_error` with a fixed bounded code.
The SDK helper owns sequencing, media-channel authentication and cleanup
acknowledgement. The example does not invent new SDK terminal codes.

Current source references, checked 2026-10-19:

- [OpenAI plan-usage inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [OpenAI preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

The mock request preserves explicit input history and requests streaming without
storage. A local output cutoff is not an upstream billing cap.
