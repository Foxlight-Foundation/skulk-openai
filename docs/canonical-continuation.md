# Canonical Responses continuation

`skulk_openai.continuation_service.ContinuationService` composes the real caller
registry and protected journal with the normal capability-owned provider client.
This initiative source is not an installed descriptor, public enrollment flow,
unified API extension or enabled compaction setting. Existing full-history chat
continues to use its existing contract.

The caller signs the exact bounded request bytes and always submits full public
history. The journal authenticates its independently issued client identity,
account, model, descriptor, policy, nonce and exact committed public prefix.
Translation validates all history, including tool-result matching, before
appending only new messages to the protected canonical provider window. Provider
items are never accepted from the caller as continuation state.

The provider request retains `store: false`, `stream: true` and the stateless
input array. No response ID, server conversation, alternate endpoint, implicit
retry or fallback to a fresh history is introduced. Dispatch uncertainty is
durable before provider I/O. Only a successful terminal event followed by clean
EOF can atomically publish a new canonical window and its content-free receipt.

Ordered output-item added/done pairs must agree in identity and kind; terminal
output must agree with the staged stream. Canonical assistant text and function
calls must also match the public deltas used in the history fingerprint. Only
the supplied functions and permitted parallel-call behavior may be forwarded;
the capability does not execute tools. Reasoning summaries remain public summary
text; encrypted reasoning/compaction items stay in the private canonical window.

Malformed, incomplete, failed or cancelled responses never commit a receipt.
Cancellation closes the actual HTTP stream and retires the reservation. A
post-dispatch failure preserves uncertainty rather than silently retrying.
Revocation or an owner/account change during generation prevents late commits.
If cleanup storage fails, the already durable dispatch phase is reconciled by
the next exclusively fenced worker; no receipt is synthesized from output.

## Automatic compaction

No model profile is inferred from a name. A trusted owner must explicitly supply
`CompactionProfile` with established context size, reserved output headroom and,
optionally, a threshold. Without a profile, the parameter is omitted and
unsolicited compaction is refused. The default threshold for an explicit profile
is the smaller of 75% of its context and context minus reserved output headroom.
This headroom is not a `max_output_tokens` parameter or an owner output ceiling.
Changing the profile requires an explicit new context transition.

For automatic compaction, keep the latest opaque compaction item and everything
following it; drop only preceding items. This follows the official
[stateless compaction guidance](https://developers.openai.com/api/docs/guides/compaction).
The separate compact endpoint is not used or advertised. The provider window
keeps the profile revision, so disabling/changing compaction cannot silently
reuse a differently managed checkpoint.

Canonical storage remains bounded to 16 MiB and 4,096 items. The capture path
allows bounded larger SSE events for opaque items (about 16 MiB/event,
64 MiB/stream), with the existing 8,192-event and 120-second stream bounds.
These limits protect local resources; they are not provider allowance or model
context declarations. Legacy chat stream bounds remain unchanged.

## Qualification

`tests/test_canonical_continuation.py` exercises actual caller signatures,
encrypted SQLite, a provider HTTP mock, canonical pruning, tool-result suffixes,
profile changes, stream/canonical mismatch, unsolicited tool refusal,
revocation and actual response cleanup on cancellation. No account or fleet is
required. Run it with:

```sh
uv run pytest tests/test_canonical_continuation.py
```

Those tests establish source behavior only. Real plan-connection compaction,
meaningful context reduction, multi-turn tool continuity, managed child media,
unified API/client enrollment and signed installation require separate evidence.
