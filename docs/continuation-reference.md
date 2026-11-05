# Account-free continuation reference

`skulk_inference_reference.continuation` is an experimental, effect-free
reference. It is not an adopted SDK contract, a public HTTP extension, or an
enabled Integrator feature. Existing `chat.completions@0.1.0` and `0.2.0`
contracts and their installed behavior are unchanged. No account is needed:

```sh
uv run pytest tests/test_continuation.py
```

The tests use an authenticated gateway fixture, a simulated atomic store, an
independent JSON-cursor provider, and an incompatible binary-window provider.
They exercise the same semantics without importing OpenAI implementation code.
The simulated restart copies durable fixture state; it does not qualify real
process-crash durability, protected storage, encryption, or distributed authority.

## Ownership and explicit opt-in

A stock client continues sending full Chat Completions history. It gets no
hidden checkpoint, even if another client sends identical history through the
same gateway. Only a client and destination that explicitly agree to the
continuation contract can use its start/continue metadata and receipt.

The draft requires full public history even in continuation mode. Provider
items can replace the provider's working context later, while public history
remains available for recovery. This first reference does not implement
suffix-only input, automatic summarization, context thresholds, or compaction.

The trusted authenticator supplies `VerifiedCaller` out of band. A gateway key
identifies a gateway, not its individual callers. Request fields such as
`principal` and `user`, similar prompts, and possession of a handle confer no
authority. The fixture independently resolves a caller grant after verifying a
signature that covers both caller scope and the complete request metadata.
These mocks are not a production authenticator or distributed grant protocol.

The conversation binding includes:

- Authentication namespace, opaque subject and credential generation.
- Explicit random conversation ID and exact target node/callable/model/descriptor.
- Capability-local account and policy epochs, never upstream account IDs on wire.
- Exact request length and SHA-256, one-use attempt nonce and admission deadline.
- Ordered fingerprint of the last committed public message history.

Only object-key ordering is normalized in transcript hashing. Edited content,
reordered or removed messages, changed tool-call IDs/arguments/results, or an
incompatible representation fail closed. A future public adapter must specify
its canonical assistant-message assembly, including null versus omitted fields,
before advertising this contract. No substring or approximate-history matching
is permitted. Per-request options are covered by the signed complete body;
their provider-specific validation remains the destination's responsibility.

## Lifecycle and failure handling

| Phase | Required owner action | Recovery |
| --- | --- | --- |
| Start/continue | Authenticate, verify signature/body, recheck authority, fence nonce and atomically reserve the exact record version | Reject stale handles, incompatible history and concurrent attempts before provider use |
| Reserved | Load the private canonical checkpoint; recheck caller/account/policy and admission expiry | Before dispatch, release the reservation with compare-and-swap; its nonce remains spent |
| Dispatched | Persist this phase **before** any provider effect | Interruption or crash is uncertain; retain the old checkpoint, refuse automatic resubmission |
| Completed | Verify terminal success; atomically commit canonical provider window, public-history fingerprint, rotated handle and content-free receipt | Recover a lost receipt under fresh scoped authority without another inference request |
| Interrupted | Preserve recoverable public history and prior checkpoint | Explicitly abandon the uncertain attempt and start a new conversation from full history; never silently migrate |

The pure functions return proposed immutable state. A real adapter must perform
the indicated transactions, durable nonce fencing, checkpoint custody, freshness
checks, and compare-and-swap. Calling these functions without those effects is
not an enforcement implementation. The fixture models serialization without
await points; multi-process atomicity and crash consistency are separate gates.

An owner allocates unpredictable unique handles and fences allocation collisions
in the same commit transaction. Handles rotate on each committed turn and are
neither bearer authorization nor
fetchable URLs. Checkpoints stay inside the capability; receipts contain only
scope identifiers, history fingerprint, handle, attempt ID and expiry. Neither
checkpoint contents nor credentials belong in replicated state, logs, reasoning
summaries or routing telemetry. Receipt recovery rechecks current authority and
returns only the current committed receipt. Old receipts, revoked callers,
changed account/policy epochs and switched models/providers are refused.

A request's short admission deadline is checked before dispatch; it does not
truncate an already admitted stream. The reference's explicit local checkpoint
lifetime defaults to one hour and is bounded to one day. This is a test/reference
retention bound, not a proposed product allowance or a model context limit.
Expiry refuses checkpoint use without discarding the caller's public history.

## Boundary before product adoption

The gateway needs a real, separately authenticated caller/conversation identity
and an explicit negotiated, signed ABI. Existing gateway delegation alone is
insufficient. A capability then needs protected transactional checkpoint storage,
canonical provider item handling, real restart/concurrency/cancellation tests,
and provider-qualified context reduction. Routing must pin active tool loops
and checkpoints to their destination/account/model until an explicit transition
starts from portable public history. A router recommendation grants no authority.

OpenAI's HTTP ChatGPT-plan flow requires stateless input with `store:false` and
`stream:true`, omitting `previous_response_id`. Automatic compaction returns an
opaque item that must be preserved in subsequent input. Those provider details
belong in the capability adapter, outside this generic module. See the official
[plan-flow limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
and [compaction guide](https://developers.openai.com/api/docs/guides/compaction).
This reference does not enable or qualify either compaction endpoint.
