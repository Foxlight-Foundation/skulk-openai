# Experimental streamed chat input

This is the capability side of `chat.completions@0.2.0`, on the initiative
branch. It is not part of the installed 0.4.4 release or the current Skulk unified
API adapter. A compatible generic gateway, signed publication and installed
end-to-end qualification are still required. The existing 0.1 callable remains.

## Why a separate input channel

Version 0.1 signs the entire request inside its 48 KiB control envelope. Version
0.2 signs an `InputBody` containing the byte length, SHA-256 and fixed content
type instead. UTF-8 request JSON travels as raw inline media on the invocation's
existing public SDK bidirectional stream. No SDK owner implementation is used or
modified, and no provider endpoint or account state enters the generic contract.

The cached `*.chat-catalog-v2@0.2.0` descriptor publishes fresh delegated models
and the exact `*.chat@0.2.0` descriptor revision. The catalog has the existing
independent metadata admission annotation. Inference retains workload admission.
The same callable ID keeps model addressing stable when a future gateway chooses
the newer version; gateways must negotiate and prefer one version rather than
list duplicate models or silently downgrade a large request.

## Input protocol

1. Authenticate the API caller and resolve processing/usage authority.
2. Encode request JSON using `encode_body`, without a top-level `model`; the
   selected model is already bound in the signed invocation.
3. Sign `StreamedInvocation.canonical()` with the owner-delegated gateway key.
   The signature covers contract, gateway key, target, callable, descriptor and
   catalog revisions, model, body digest/length/type, nonce and expiry.
4. Open the exact bidirectional descriptor with `StreamedProof` as its initial
   payload. Start caller input, send ordered `InlineMedia` chunks with
   `application/vnd.skulk.chat-request+json`, then complete input with an empty
   successful half-close. Chunk payload may be absent or `{}`.
5. Read the same `ChatEvent` output contract used by v1. Common completion and
   successful SDK transport completion are both required for success.

Operational bounds are 16 MiB per encoded request, 1 MiB per packet, 4,096
messages and eight documents, each at most 8 MiB decoded. The aggregate request
bound still applies to base64 expansion and all history. These are transport
bounds, not model context windows, owner output caps or provider allowances.

The capability checks the signature and current delegation before reading body
media. It verifies exact length/digest and strict request translation before
reserving the nonce/shared hourly attempt budget. Immediately before reservation
it rechecks current account, catalog and owner policy. Invalid, truncated,
cancelled, expired or modified input causes no upstream request. Failed admitted
provider attempts retain the existing no-refund/no-replay semantics.

## Document input

User message content accepts the Chat Completions file shape:

```json
{
  "type": "file",
  "file": {
    "filename": "notes.txt",
    "file_data": "data:text/plain;base64,SGVsbG8="
  }
}
```

`.txt` uses UTF-8 `text/plain`; `.pdf` uses `application/pdf`. The adapter validates
base64, filename, decoded size and basic format, then sends an exact Responses
`input_file` part. It does not upload through the Files API, parse documents
locally, follow remote URLs, read filesystem paths or resolve provider file IDs.
The existing external-processing permission and explicit request attachment
selection apply. Model-specific acceptance remains an upstream decision; this
slice does not advertise every account model as file-qualified.

OpenAI documents [file input](https://developers.openai.com/api/docs/guides/file-inputs)
for supporting models. The [ChatGPT-plan preview contract](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
allows file inputs but excludes the Files upload API. This adapter uses Responses
file input for text as well as PDF; it does not claim the upstream Chat
Completions service itself accepts text documents.

## Qualification still required

Core's generic gateway must negotiate v2, sign body references and own sending,
backpressure, cancellation and cleanup across the HTTP request lifecycle.
Its API types need a provider-neutral attachment representation and documented
request-size behavior. Exercise a second independent provider, the real owner
bridge, cancellation during upload, metadata during upload/generation, policy
revocation, malformed packets and cleanup before promotion.

Compaction requires an additional continuation contract. This body channel alone
does not preserve encrypted provider state, reduce context or enable compaction.
Do not add `context_management` to ordinary chat requests while discarding its
output checkpoints.
