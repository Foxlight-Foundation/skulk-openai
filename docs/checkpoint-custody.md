# Caller keys and protected checkpoint custody

The initiative branch implements real caller-key verification and encrypted
transactional storage in `skulk_openai.checkpoints`. This remains source-only:
no installed SDK descriptor, unified API extension, settings control or compaction
feature advertises it yet. Tests require no account or fleet:

```sh
uv run pytest tests/test_checkpoints.py
```

## Caller identity

Trusted owner code issues a distinct Ed25519 key per client, with explicit model
scope and expiry. Only its public key is persisted. The returned credential has
no upstream token, provider account identifier or permission to refresh an
account. There is no request/output allowance attached to it; existing optional
owner ceilings remain independently enforced through the shared hourly journal.

`skulk_inference_reference.continuation_auth` provides strict, provider-neutral
request and receipt-query proofs. Signatures bind the registry identity,
operation domain, exact target/model/descriptor, conversation, body digest and
length, nonce and deadline. The capability derives the authenticated subject
from its owner-issued registry, never from a client-chosen principal. The
gateway key and a checkpoint handle do not authenticate an individual client.

Issuance/revocation are trusted owner Python APIs, not remotely callable inference
operations. This is not yet a production client-enrollment UI, gateway bearer
credential bridge, distributed delegation protocol or adopted SDK identity ABI.
Stock full-history chat paths remain unaffected.

## Custody and commits

Canonical provider-window bytes, continuation metadata and the current receipt
are sealed together with AES-256-GCM before SQLite sees them. Authenticated
associated data binds the row's scope, caller, version, phase and expiry;
an indexed handle digest also verifies against authenticated metadata. Handles
are unpredictable, rotate on each commit and have a unique index. A collision
fails without replacing another conversation.

The owner-only 256-bit key is atomically published and fsynced separately from
the database. Missing or malformed keys and corrupted/swapped records fail
closed. Existing encrypted records never cause automatic key replacement or a
plaintext/fresh-history fallback. Keep the matching key with authorized backups.

This protects checkpoint payloads in a database-only copy. The host OS account
and capability process remain trusted; a process that can read both key and
database can decrypt them. Existing OAuth credential storage retains its own
owner-only protection. This does not add an OS sandbox or encrypted OAuth store.

Every reservation, dispatch, commit and receipt lookup rechecks fresh caller,
selected account/generation, model and owner policy in the same SQL transaction.
Nonce fencing, existing optional attempt ceilings, record-version comparison,
checkpoint and receipt publication are atomic. No database write lock spans a
provider request. Independent chats can remain in flight concurrently.
Admission charges the exact worst-case interruption allocation as well as the
sealed payload, so dispatch, cleanup and crash recovery still fit at capacity.
Startup prunes expired records before reconciling abandoned attempts.

Account switch/reconnection/disconnect atomically purge checkpoints and revoke
old caller grants. An actual owner-policy edit purges old checkpoints, preventing
off/on or restored settings from resurrecting them. Identical settings saves,
ordinary token replacement and freshness-only catalog renewal preserve compatible
state. A late completion or cleanup never restores deleted/newer records.

## Crash and cancellation boundaries

One serving process holds a stable OS file lock. Another live worker cannot
recover or serve its calls. On process death the kernel releases the fence;
the replacement reconciles journal phases before admitting work:

- A reserved, undispatched attempt can release its reservation. Its nonce stays
  spent, so only a fresh signed attempt can proceed.
- A dispatched attempt becomes uncertain/interrupted. Preserve its old window
  and public history, and refuse automatic replay.
- A committed attempt recovers its current content-free receipt through a
  separately signed one-use lookup, without another inference submission.

The provider adapter must persist dispatch before any provider effect, validate
terminal/canonical output before commit, and close its lease on every exit.
Closing a worker retires its leases; already admitted upstream effects cannot
be promised cancelled or refunded. A caller must explicitly abandon uncertainty
and start a new conversation from retained public history.

## Bounds and qualification

Retention seconds, record count and total sealed-payload bytes are required,
strict owner-supplied storage policies. This source implementation bounds
retention to one day, records to 4,096, total sealed payloads to 2 GiB, and each
canonical window to 16 MiB. These are storage/protocol safeguards, not provider
allowance or model context sizes. There is no new default usage or output cap.
The client registry is bounded to 128 entries; spent nonces to one million,
retained until credential expiry/revocation. Expired/disabled records are pruned.
Public enrollment, retention controls and longer-term storage policy are not
enabled by these owner-local primitives.

Tests use real SQLite, keys and encryption, concurrent threads, abrupt spawned
process death, signature/body/scope refusal, replay and receipt recovery,
revocation, policy/account changes, key loss, ciphertext/metadata corruption,
handle collision, capacity refusal and write-then-fail rollback. Their provider
effect is a synthetic file sink. They do not qualify OpenAI canonical item
handling, real compaction, the managed child/HTTP path or installed continuation.
