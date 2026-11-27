# Native SDK1 0.7.1 qualification — November 27, 2026

This is an implementation-bound qualification record, not a stable Skulk release
or a claim that every model, platform and account combination is tested. Check
the Store listing for live package availability.

## Immutable inputs

- Application: [App69](https://github.com/Foxlight-Foundation/skulk-openai/pull/69),
  accepted source `ff2e304084b88f3a0f67ce355f112e67973c780a`, tree
  `8224eb2897ae4af0bbbed479e3f76ed71735e9c6`.
- Signed application bundle: 184,501 bytes; SHA-256
  `6dcc7db0c98f9f83d58359e58cbdd8f5c1e5c216341b2026007901c546fec2c1`.
- Public SDK1.0.1, tools0.2.0, compiled kernel0.7.0 and owner0.18.1.
  Owner wheel SHA-256
  `80485f901e6ca4d005180ed714e1542e91bc3655591a70282ffbca06bc700532`.
- Core tested source `1574a38dce323d6a5ca4a79c9bbc0ad7eb9fbf25`, accepted through
  [Core1446](https://github.com/Foxlight-Foundation/Skulk/pull/1446). The
  subsequent Windows manual-only merge changes no runtime bytes.
- Production publisher signatures, the Store’s current TUF trust, all 94 wheel
  files across three platform closures, and the independent rebuild verify.
  Signing is separate from the later Store publication/readback gate.

This documentation update does not change that signed executable or its wheels.
The capability remains the owner of OAuth, provider calls and protected state;
Skulk carries the generic capability and caller-authority paths.

## Account-free package acceptance

macOS ARM64 and glibc Linux x86_64/ARM64 installed checks verify 25 populated
synthetic cases, 32 authentic-old-runtime update/restart/rollback cases and eight
disconnected-real-app cases on each platform: 195 assertions in total. The
99 distribution assertions overlap that total; they are not additional coverage.
The real disconnected child recovers a deliberately interrupted SQLite writer
through ordinary owner enable. Four old/current/fresh source builds compare
exactly. These use disposable issuers and synthetic providers, without accounts.

## Installed real-account acceptance

The exact production-signed Linux ARM64 package is installed on a packaged
Debian Core host using its normal OS-registered owner service and signed offline
installer. This proves signed-byte installation, not a post-publication network
Store download. Activation preserves the original account and initialization.

Six small generation requests verify:

- Browser Chat commits a protected first turn and recalls the requested word
  correctly on a second turn. The signing key stays in that browser.
- A separate API client initially discovers no native models, then receives only
  its approved model through two exact grants.
- A function-call/result round-trip, inline PNG input and strict JSON-schema
  output succeed through the unified API.

For the cold-start test, both normal services stop before a writer inserts only
uncommitted fixture rows and exits without a commit. The valid hot rollback
journal causes SQLite’s read-only probe to return `SQLITE_READONLY_ROLLBACK`.
Normal service startup of the signed application performs recovery. All caller,
encrypted-checkpoint and nonce rows retain their exact hashes; original account,
authorization epoch, settings and initialization are retained. No uncommitted
fixture row survives, integrity checking succeeds, and no journal is manually
deleted or backup restored. The first comparison also included the automatically
refreshed model catalog; that cache can change normally during startup and is
separate from the protected identity/checkpoint checks.

Browser receipt recovery then verifies public history. Native generation
submissions remain zero before and after recovery in the new process. This
counts generation calls, not every possible OAuth or catalog HTTP request.
Temporary API and continuation clients are revoked after verification.

## Limits and retained earlier evidence

The production-signed 0.7.0 predecessor already passed normal service reboot,
signed rollback/return and receipt recovery on the same packaged host. The
0.7.1 patch changes only SQLite recovery, with no SDK/Core/owner/state-format
change. Its independent current package checks and the cold-start test above
are the renewed patch acceptance.

The ten-model serving, effective Luna/Sol compaction and mounted-speech matrix
remain evidence for [0.6.3](native-qualification-0.6.3.md); they are not restated as
a fresh 0.7.1 matrix. Actual 0.7.1 account checks above use GPT-6.1-Sol. They do
not establish real-account success on every platform, universal voice/browser
support, Windows native capability hosting or a model-quality ranking. A local
macOS26 packaged-loader limitation remains disclosed separately; hosted macOS15
package success does not diagnose or qualify that host.

Skulk main is held. Routing and hosted publisher services are later work.
