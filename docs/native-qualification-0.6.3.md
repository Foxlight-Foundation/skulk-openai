# Native 0.6.3 qualification

The signed native **0.6.3 preview** is published in the Foxlight capability Store
for macOS arm64 and glibc Linux x86_64/aarch64. Independent readers verified the
live signed catalog and all three packages. This is a native preview, with
separate compatibility and qualification boundaries from stable Skulk.

## Exact implementation

- Application: `eaf486e601aa692f1124b56e06947391040fc054`.
- Compatible experimental Core: `b203d3aea1750757046c50adce359511cab59691`.
- Owner: `0.18.0rc3`; runtime SDK: `0.5.10rc1`; native kernel: `0.7.0rc4`.
- [Signing run](https://github.com/Foxlight-Foundation/skulk-capability-nodes/actions/runs/44242846713)
  and [Store publication](https://github.com/Foxlight-Foundation/skulk-capability-nodes/actions/runs/44321242096).

The real-account checks below used the signed Linux ARM64 installation. Its
normal update retained the account, settings and installed child identity.
Caller access was explicitly approved for the new signed implementation. A
version string alone cannot establish host compatibility.

## Serving matrix

Thirty provider requests covered twenty text-streaming and combined
image/text/PDF/function-tool cases across all ten account-advertised models:
GPT-6.2 Sol/Astra, GPT-6.1 Terra/Sol, GPT-6 Astra/Sol/Luna and GPT-5.6
Sol/Terra/Luna. All serving contracts passed; all ten synthetic fact answers and
returned tool values matched. The public qualification tool bound each result
to the exact selected/active native runtime and full descriptor revision.

This is a bounded fixture sample, not a model-quality comparison. A serving
pass does not qualify unexercised features or every possible output schema.

## Effective compaction

GPT-6-Luna and GPT-6.1-Sol passed separate, explicit compaction profiles at
131,072 and 787,500 input-token thresholds. At the larger threshold, each began
with about 840,000 provider input tokens; the following request used about
252,000 while retaining the synthetic fact across public history exceeding one
million tokens. A matching moderate Sol control used 448,095 tokens without
compaction versus 280,158 with compaction, a 37.5% reduction for that fixture.
These observations establish effective context reduction on those paths, not a
general savings rate.

After normal application stop/start, signed receipt recovery made no provider
request. Fresh continuations retained the fact using 56 input tokens for Luna
and 113 for Sol. A duplicate committed request was refused without another
provider request. One large uncompressed Luna control was interrupted; it does
not establish a context-limit failure and was not replayed.

OpenAI performs the compression. The capability retains its encrypted
continuation state; the generic host transports authenticated calls and
receipts. Storage limits and model profiles remain explicit opt-ins. Tests
restored the original settings and removed their temporary profiles and client.
Other models retain their own compaction qualification requirement.

## Native Chat and mounted speech

Native GPT-6.1-Sol and the signed-registry
`mlx-community/Qwen3-TTS-12Hz-0.6B-Base-6bit` artifact composed through one
Linux unified API, with a separate Apple Silicon worker serving TTS over the
Fabric. The artifact was pinned to
`4e44ed4bcee28a0f89a493e07bde16e6dccd43eb`. First synthesis after a cold model
mount returned 142,080 non-silent PCM bytes; warm synthesis returned 130,560.
First-read times for that sample were 4.50 and 0.89 seconds respectively.

Browser checks covered automatic speech for generated prose, completed-message
playback and rendered Python code, without console errors. An earlier combined
prose/code Chat request returned an interrupted result and was not replayed;
distinct later prose and code prompts passed. This qualifies API/UI composition,
not acoustic quality, every voice, STT or all speech models and platforms.

## Remaining boundaries

These checks do not promote Skulk to stable, qualify cold OS-service setup on
macOS or Linux x86_64, or supply routing/classification, Steward delegation,
publisher onboarding or a complete SDK 1.0. Protected continuation, provider
consent and Core caller authorization remain independent requirements. No
request allowance or output ceiling is enabled by these tests.
