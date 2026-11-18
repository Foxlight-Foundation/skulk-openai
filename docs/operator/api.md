---
title: Use the unified API
sidebar_position: 6
---

Connect another application to **Skulk's API**, using a Skulk client key and an enabled model ID returned by Skulk. Do not give the application the capability's OpenAI OAuth tokens.

## Create and authorize a client

1. In Skulk, open **Settings → Inference access**.
2. Create a separate client key for the application and save its one-time disclosure securely.
3. Select that client in the caller selector.
4. Approve the exact installed Integrator capabilities it needs, review external processing, and save.

A new key starts without capability permissions. Approving **This dashboard** does not approve an API client. The Integrator's account, model and gateway approvals must also be current.

## Configure an OpenAI-compatible application

| Application field | Value |
| --- | --- |
| Provider type | OpenAI-compatible, if offered by the client. |
| API base URL | Your reachable Skulk origin followed by `/v1`, for example `http://localhost:52415/v1`. |
| API key | The Skulk client key created above. |
| Model | An exact native model ID from Skulk's `/v1/models` response. |

`localhost` means the device running the client. For a client on another device, use its normally authorized Skulk endpoint. Do not expose a dashboard or bypass authentication to make an example work. Compatibility of every third-party client's individual features is not implied by OpenAI-compatible chat support.

## Discover models

Set these example variables in your shell. Use your actual API origin and key; keep the key out of scripts you publish or share.

```sh
SKULK_API_BASE='http://localhost:52415'
SKULK_CLIENT_KEY='your-skulk-client-key'

curl --fail-with-body "$SKULK_API_BASE/v1/models"   -H "Authorization: Bearer $SKULK_CLIENT_KEY"
```

Choose an enabled Integrator entry from the result. Its native model ID includes the installed capability identity, callable/version and provider model slug. Copy the complete ID; do not construct it from a display name or replace it with a provider slug.

## Send a conversation

Replace the model placeholder with that exact returned ID:

```sh
curl --fail-with-body "$SKULK_API_BASE/v1/chat/completions"   -H "Authorization: Bearer $SKULK_CLIENT_KEY"   -H 'Content-Type: application/json'   --data '{
    "model": "EXACT_MODEL_ID_FROM_SKULK",
    "stream": true,
    "messages": [{"role": "user", "content": "Explain a compute fabric in two sentences."}]
  }'
```

Use `stream: false` for a collected Chat Completions response. The capability still uses OpenAI's required upstream streaming transport internally. Treat an error or interrupted stream as a failed/uncertain request, not a successful completion because some text arrived.

Function tools and structured output use the compatible Chat Completions request fields. Keep assistant tool calls and matching tool-result messages together in later history. The calling application owns execution and permissions.

For all endpoint parameters, use the [Skulk unified API reference](https://docs.foxlight.ai/skulk/next/api-guide/). This manual explains the capability-specific setup rather than maintaining a second generic API specification.

## Revoke a client

Revoke the application's key in **Inference access** when it no longer needs access. Remove its retained capability permissions as appropriate. Do not share one key among unrelated applications if you need to revoke them independently.

An ordinary bearer key does not automatically enroll a protected-continuation client. [Context and compaction](context.md) explains that additional opt-in.
