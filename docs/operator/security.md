---
title: Privacy and access
sidebar_position: 8
---

OpenAI inference is external processing. The capability sends the selected conversation content and attachments to OpenAI under the connected account. Review what an application includes before submitting sensitive material.

## Where information lives

| Information | Owner and use |
| --- | --- |
| OpenAI account sessions and refresh tokens | Stored by the capability on its host; not handed to Chat or external API applications. |
| Skulk API client key | Held by that application; authenticates it to Skulk, not directly to OpenAI. |
| Continuation private signing key | Held by the continuation client. Only its public key is enrolled with the capability. |
| Encrypted provider checkpoints | Retained by the capability only when protected storage is configured, under its storage policy. |
| Tool definitions, calls and results | Supplied/handled by the calling application; relevant conversation content goes to the provider. |

## Understand the permissions

Account consent, gateway approval and caller approval are independent. Each request must also satisfy current model and integration policy. Discovering a model or joining the cluster does not grant inference authority.

A signed application update can change the implementation identity you approved. Review the new release and renew the affected caller permissions explicitly; approving an old signed runtime is not blanket approval of all future code.

## Protect the host and its state

Checkpoint encryption does not mean every file in the state directory is encrypted. The host OS account and capability process can access account credentials. Protect that OS account and the private state directory; do not copy it into a repository, public folder or support bundle.

A new installation should use a new private state directory. Updating an existing installation should retain its existing directory. Moving populated state is a separate explicit operation; do not copy another installation's state and expect its authority to transfer.

Do not restore security records backward to resurrect revoked credentials or replay old requests. Application data recovery and credential enrollment are separate concerns.

## Provider storage and cancellation

The adapter uses OpenAI's required `store: false` mode and explicit request history. That API setting is not a general promise about the provider's logging, retention or account policies. Review the provider's terms and connected-app settings for those policies.

Stopping a client stream or disconnecting fences local admission and closes owned transport. It does not prove already dispatched provider work or its usage was undone.

## Share diagnostics safely

Share application version, host compatibility, the failed step and sanitized error category. Exclude API keys, OAuth tokens, state databases, continuation private keys, prompts, attachments and raw account responses. If a screenshot includes credentials or private account/workspace details, remove those details before sharing it.
