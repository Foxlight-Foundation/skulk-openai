# Client-owned continuation keys

Continuation clients can now generate their own Ed25519 signing keys and request
explicit owner approval of the public key. The capability stores the public key,
model scope, expiry and random caller ID; it never needs the client's private key.
This is an initiative-source management/UI/API/CLI flow. Normal signed installation
and unified dashboard adoption remain separate work.

In **Skulk access → Continuation clients**, paste the public key, choose saved
enabled models and a 1-hour, 24-hour or 7-day expiry, then **Approve public key**.
Return the displayed public enrollment JSON to the matching client. Active grants
can be exported again or revoked; revocation removes that client's checkpoints.
Approval requires explicit protected-storage configuration and current account
authority. Merely presenting a key during inference never enrolls it.

## Create a client key

On the client host, using the installed package (or `python -m
skulk_openai.continuation_client` from source):

```sh
skulk-openai-continuation keygen --key-file client-key.json
skulk-openai-continuation public-key --key-file client-key.json
```

Both commands print only `{"public_key":"..."}`. `keygen` creates an owner-only
regular file and refuses to replace a file or follow a link. It requires no
provider account, SDK owner, network request or fleet. Keep the private file on
the client; transfer only the public key for owner approval.

The key command and signer refuse symlinks, nonregular/private files with loose
permissions and oversized records. Public enrollment metadata may be shared; the
client independently verifies its approved public key matches the private key.

## Owner management endpoints

These endpoints belong to the capability's loopback management server, alongside
its existing account settings. They are not Skulk inference API endpoints and
do not confer paired or remote management authority. All require the owner-local
HttpOnly session; POST additionally requires the exact Origin, JSON content type
and current CSRF header. A key presented by an inference caller is never enrolled
automatically.

| Method/path | Input | Behavior |
| --- | --- | --- |
| `GET /api/continuation` | Empty body | Returns `configured`, current exact `target`, and active owner-local public `clients`. No account IDs, private keys, canonical windows or provider request. |
| `POST /api/clients/register` | `public_key`: 64 lowercase hex characters; nonempty unique `models`; explicit positive finite Unix `expires_at` | Requires explicitly configured continuation and current owner/account/model authority. Returns `ClientEnrollment` with a random caller ID, public approved scope and exact destination. Unknown models, expired approvals, duplicate public keys and unavailable authority refuse atomically. |
| `POST /api/clients/revoke` | `caller_id`: 32 lowercase hex characters | Idempotently revokes that caller and deletes its checkpoints. Remains an owner action; revocation does not require provider HTTP. |

Unconfigured continuation returns `409 continuation_not_configured` on mutations
without creating a checkpoint key/store. Invalid JSON/schema fields return 400;
missing owner session/CSRF/Origin returns 403 (malformed Host/forwarding requests
are rejected with 400). Registry/policy conflicts return 409 with a bounded fixed
failure category. Storage unavailability is a refusal, never a new authority grant.
The request rejects private-key fields; no private-key upload is supported.

The operator must explicitly choose models and expiry. Registration does not
widen the integration's allowed models or external-processing policy and creates
no new request/output ceiling. Only the currently selected account generation's
grants are listed. Account changes revoke the old grants; invocation admission
still rechecks actual policy and entitlement rather than trusting the public
enrollment document.

Save the returned public `ClientEnrollment` JSON on the client as
`enrollment.json`. Its `target.model` is initially `unselected`; the client selects
one of its approved models when signing. Destination node, callable and descriptor
remain exact. Tampering with public scope cannot change the server's registry.

## Sign a generation or receipt lookup

Use an explicit random 64-hex conversation ID and retain the full public history:

```sh
skulk-openai-continuation sign \
  --key-file client-key.json --enrollment-file enrollment.json \
  --model MODEL --conversation CONVERSATION_ID < request.json
```

The command prints a `ContinuationProof`. Its digest binds the exact stdin bytes,
without JSON re-encoding. Send those same bytes through the
[managed media contract](managed-continuation.md). For a later turn, supply
`--handle` with the last committed receipt and include its exact public history.
No handle or shared gateway key substitutes for the client signature.

After lost terminal delivery, use the original request nonce without resubmitting:

```sh
skulk-openai-continuation receipt \
  --key-file client-key.json --enrollment-file enrollment.json \
  --model MODEL --conversation CONVERSATION_ID --request-nonce ORIGINAL_NONCE
```

Send the resulting `ReceiptProof` with empty half-closed input. Each command uses
a fresh nonce and a short deadline bounded by the approved credential expiry.
Expired enrollment, a model outside its scope or a mismatched private/public key
refuses locally. The server independently enforces all authority and replay rules.
No command automatically retries, refreshes a provider token or discovers a
conversation by similarity.

## Qualification

Core tests exercise owner HTTP/CSRF approval, scope/expiry/duplicate refusal,
client-only private files and actual CLI proofs admitted by the encrypted journal.
A real managed subprocess test follows key generation, owner HTTP approval,
offline signing, generation, receipt recovery and revocation. Accounts and upstream
responses are synthetic; no real account or unified HTTP continuation claim
follows from those tests. A separate signed isolated-runtime acceptance exercised
the normal Core installer/selector and production owner with SDK 0.5.5 / owner
0.10.4, including restart and revocation. It did not change the installed
Integrator or qualify automatic compaction. See [managed continuation](managed-continuation.md).
