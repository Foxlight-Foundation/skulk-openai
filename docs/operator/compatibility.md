---
title: Compatibility and supported features
sidebar_position: 10
---

## Release and host requirements

This manual describes the signed native **Integrator 0.6.3 preview**. Packages are published for macOS ARM64 and glibc Linux x86_64/ARM64. The Linux native runtime requires glibc 2.34 or newer.

The compatible experimental Core source is `b203d3aea1750757046c50adce359511cab59691`, retained on the native owner/Core initiative. Use that exact preview cohort; this manual does not establish compatibility with stable Skulk. A `2.0.3` version string alone does not establish compatibility.

The issued native runtime uses owner `0.18.0rc3`, runtime SDK `0.5.10rc1` and kernel `0.7.0rc4`. Use the compatible signed closure; do not assemble an arbitrary mix of similarly numbered wheels. The public author SDK/tools release is a separate developer input.

Until a normal Skulk release includes the required host, follow the preview's [published runtime and host requirements](https://developers.foxlight.ai/build/sdk/native-preview/). Do not replace a working stable installation solely because an Integrator package is visible in the Store.

## SDK1 preview 0.7.1

The SDK1 preview uses public **SDK 1.0.1**, compiled **kernel 0.7.0** and the
separately installed **owner 0.18.1**. These replace the older runtime closure
above as a coordinated set. Do not mix the old owner or SDK into this package.
The signed Store listing determines live availability; a signed build and a
Store upload are separate release steps.

Its qualified Core source is `1574a38dce323d6a5ca4a79c9bbc0ad7eb9fbf25`, accepted
through [Core1446](https://github.com/Foxlight-Foundation/Skulk/pull/1446) on the
SDK1 initiative. A subsequent Windows-manual-only merge does not change those
runtime bytes. Skulk main has not been promoted. The SDK1 package requires this
matching host, even if an unrelated build reports the same `2.0.3` version.

On the installed Linux ARM64 packaged host, 0.7.1 passes real Chat continuation,
API function-call/result, PNG input and strict JSON-schema checks. After an
interrupted SQLite write, normal service startup recovers the journal, preserves
the encrypted checkpoint and consumed nonces, and recovers its receipt with
zero new generation submissions. Existing account, model settings and native
initialization survive; no account reconnection or old-backup restore is used.
A new API client sees no native models until its two exact discovery/generation
grants are approved. Both temporary test clients are revoked afterward.

Three-platform account-free package checks cover install, update, restart,
rollback and the actual disconnected app’s interrupted-write recovery. These
checks do not imply real-account or OS-service qualification on every platform.
Broader model, effective compaction and mounted-speech results below retain their
0.6.3 scope; they were not repeated as a full 0.7.1 matrix. Windows management
nodes do not establish Windows native capability hosting. See the
[0.7.1 qualification record](https://github.com/Foxlight-Foundation/skulk-openai/blob/initiative/openai-provider/docs/native-qualification-0.7.1.md)
for the exact inputs and limits.

## Features in this preview

| Feature | Integrator support |
| --- | --- |
| Text, code and streaming | Supported through the unified Chat Completions path. |
| Image, text and PDF input | Supported for accepting models, subject to integration permissions and transport bounds. |
| Function tools | Supported as client-supplied definitions/calls/results. The client executes them. |
| Structured output and reasoning summaries | Supported request forms, with model-specific behavior. Summaries are not raw reasoning. |
| Reasoning effort and verbosity | Reasoning has a supported per-request override; verbosity is an integration setting only. |
| Speech playback | Through a separately mounted Skulk TTS model. |
| Protected continuation | Optional storage and client enrollment. |
| Automatic compaction | Optional, qualified for the recorded Luna/Sol paths. |
| OpenAI hosted web search, Code Interpreter, image generation, file search or hosted MCP | Not exposed by this Integrator version. |
| OpenAI audio/transcription endpoints, audio/video attachments and Files upload API | Not exposed by this connection/integration path. |
| Temperature, top-p and upstream output-token controls | Not exposed on this plan-usage adapter. Local optional output ceilings count characters. |

OpenAI's [preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) describe the provider route. OpenAI support for a feature does not mean this Integrator exports it.

## What has been tested

The signed Linux ARM64 installation passed a 30-request, ten-model serving matrix: text streaming and image/text/PDF/function/structured-output fixtures. All twenty serving cases passed; the ten fixture-fact observations and tool results matched. This is a bounded fixture sample rather than a model-quality ranking.

Effective compaction was separately tested for **GPT-6-Luna and GPT-6.1-Sol**, including normal restart, signed receipt recovery without new generation and duplicate-request refusal. Compaction is not inferred for the other models.

The same Linux API composed native Sol with a separate Apple Silicon worker serving the signed-registry Qwen3-TTS 0.6B 6-bit artifact. Cold/warm non-silent PCM, Chat automatic/completed playback and code rendering passed. These results do not qualify every voice, browser or hardware configuration.

Cold OS-service setup on macOS and Linux x86_64 remains unqualified; clean Linux ARM64 service setup and reboot were tested. The qualification record retains an interrupted uncompressed Luna control and an interrupted combined prose/code Chat request; neither was replayed automatically.

See the [implementation-bound qualification record](https://github.com/Foxlight-Foundation/skulk-openai/blob/f090e9fc12f8a7411264cc0ab08299036ab85c2d/docs/native-qualification-0.6.3.md) for exact source identities and limits. Routing/classification is not supplied by this preview.
