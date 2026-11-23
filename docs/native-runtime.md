# Native runtime acquisition preview

This is an authoring input for the `0.6.3` Integrator preview. Operators install
a separately signed release through Skulk; see [first setup](native-setup.md).
Downloading wheels grants no publisher trust, account access or caller authority.

For the initiative's `0.7.0` SDK1 source candidate, use the
[candidate dependency and acceptance record](sdk1-adoption.md). The recipes on
this page describe the issued `0.6.3` closure and cannot satisfy the candidate's
new exact SDK/kernel requirements. The public SDK's generic owner `0.18.0rc8`
recipe also needs Integrator's additional runtime dependencies, including
PyJWT; assemble the complete candidate closure from its frozen lock and native
spec rather than treating that generic recipe as an Integrator release.

The [versioned runtime acquisition release](https://github.com/Foxlight-Foundation/skulk-openai/releases/tag/native-runtime-v0.18.0-rc.3)
contains exact, compiler-free recipes for macOS on Apple silicon and Linux glibc
on x86_64 or ARM64. Linux native binaries require glibc 2.34 or later. The
closure fixes owner `0.18.0rc3`, public SDK `0.5.10rc1` and kernel `0.7.0rc4`.
Each platform includes all application runtime dependencies, with exact wheel
sizes and SHA-256 hashes. No private Git access is required.

The SDK, kernel and public author tools remain MIT. This reference application
is Apache-2.0. The owner runtime is separately licensed and is not relicensed by
the SDK or this example.

## Acquire the selected platform

Use Python 3.13 and the explicitly selected public author tools `0.2.0rc2`.
Keep the author environment separate from the downloaded application runtime.
The [native publishing contract](https://github.com/Foxlight-Foundation/skulk-capability-sdk/blob/316bac7a41381d326874b8caee2f71406595ac01/docs/native-publishing.md)
describes that environment and the distinct signing inputs.

Download `SHA256SUMS` and the appropriate recipe from the release, and verify the
recipe against the published checksum. The recipe filenames are:

| Platform | Recipe |
| --- | --- |
| macOS on Apple silicon | `native-runtime-macos-arm64.json` |
| Linux on x86_64 | `native-runtime-linux-glibc-x86_64.json` |
| Linux on ARM64 | `native-runtime-linux-glibc-aarch64.json` |

For example, from an environment with the public author tools:

```sh
skulk-cap runtime --native \
  --recipe native-runtime-macos-arm64.json \
  --platform macos-arm64 --output ./native-runtime-wheels
```

The output directory must be new or empty. Acquisition checks every anonymous
HTTPS download against its exact size and digest before publishing any output.
Use the matching platform recipe rather than substituting wheels. Do not install
this complete closure into the author environment.

## Build and install separately

Build this reference using `capability-native.toml`, its exact public
SDK `0.5.10rc1` author requirement, the acquired wheelhouse and
`skulk_openai.native_contracts.build_native_manifest`. The newer SDK workspace
version does not override the application's declared SDK version. Native signing
uses `--inference-runtime`; a native wheelhouse build uses
`--native-candidate`. Ordinary legacy recipes keep their existing behavior.

The acquisition release identifies the exact qualified runtime inputs. Its
source-runtime digest is provenance, not an application signature or installation
approval. Use a disposable key and isolated profile for author experiments.
Normal installation requires a compatible native Core preview, reviewed
permissions, trusted publisher signatures and owner authorization.

Three-platform account-free qualification covers signed installation,
25 populated-state Chat/API/receipt checks and seven independent disconnected
first-setup checks per platform. The [native0.6.3 qualification](native-qualification-0.6.3.md) records the later
account-backed model, compaction and mounted-speech checks. Stable Skulk support
remains a separate gate.
A numeric application version is insufficient to identify a compatible host.
