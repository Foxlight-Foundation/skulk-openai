# Build the Integrator with public author tools

The MIT Python SDK and author tools are available as immutable anonymous
packages. No GitHub account, SDK account, private repository, provider account,
model, GPU or fleet lease is needed for the steps below. This repository remains
Apache-2.0; the separately licensed owner runtime stays outside its source bundle.

The earlier installed runtime qualified owner 0.10.5 and SDK 0.5.5. This source
candidate explicitly selects public SDK 0.5.10rc1 for its process and author
checks; it does not reissue that installed runtime. Tools remain 0.1.0, and the
legacy publisher group still selects owner 0.10.5. Native packaging requires a
separate compatible owner candidate; see [native managed adoption](native-managed.md).
The SDK/owner wheel files are
universal Python wheels even though their immutable origin directory is named
for the publishing runner; native dependencies are selected for the target
platform by the lock. Supported release targets remain macOS arm64 and glibc
Linux x86_64/aarch64. Tools and the qualified owner belong
in separate environments because their SDK version requirements differ.

## Install and check the reference

From this source checkout, using Python 3.13 and uv:

```sh
uv sync --frozen --group managed --group native-runtime
uv run --no-sync basedpyright
uv run --no-sync pytest
uv run --no-sync basedpyright --project pyrightconfig.managed.json
uv run --no-sync pytest tests_managed
node --test tests_web/*.test.cjs

uv venv .author-venv --python 3.13
uv pip install --python .author-venv/bin/python --require-hashes \
  --only-binary :all: -r authoring/requirements.txt
.author-venv/bin/skulk-cap --version
```

`authoring/requirements.txt` pins the complete portable author dependency set,
including the two public release wheels. `authoring/sdk-only.txt` selects the
new SDK for the separate compatibility check in CI. Neither installs the owner
or imports private owner modules. To refresh the author dependency lock deliberately:

```sh
uv pip compile authoring/requirements.in --python-version 3.13 --universal \
  --generate-hashes --no-strip-extras --output-file authoring/requirements.txt
```

Core, managed process and author build jobs run independently on macOS and Linux.
Process checks use synthetic identities and finite mock upstream responses. They
cannot establish signed installed behavior or real provider access.

## Build an account-free candidate

Set `INTEGRATOR_BUILD` to a new absolute directory outside the checkout. Keep it
private. Generate the distinct mock identity and build its source archive:

```sh
umask 077
mkdir "$INTEGRATOR_BUILD"
PYTHONPATH=src .author-venv/bin/python - <<'PY'
from pathlib import Path
from skulk_openai.managed import build_manifest, validate_manifest

manifest = build_manifest(
    executable="bundle.pyz", executable_sha256="0" * 64, mock=True
)
validate_manifest(manifest)
Path("manifest.json").write_text(manifest.model_dump_json())
PY
.author-venv/bin/skulk-cap build --project . \
  --output "$INTEGRATOR_BUILD/unsigned"
```

The builder validates the manifest, deterministic source archive and import
closure. This experimental branch's lock deliberately selects prereleases;
tools 0.1.0 does not admit them for runtime wheelhouse/signing. The source build
above is qualified, while public native packaging needs a coordinated tools/owner
refresh. Do not substitute dependency bytes or describe this source archive as an
installable native release.

For the released legacy wheelhouse and signing recipe, use the qualified release's
source and lock. Wheelhouse acquisition verifies every selected hash and requires
binary compatibility with the current platform. It does not build Core or the
owner from source. Repeat on each target before making a qualification claim.

The generated `manifest.json` is a local build input, not account state or a
signed release. Remove it after the build. For a real release, omit `mock=True`
only after reviewing the live contract and following the separate account and
installed qualification requirements. Merely importing the factory or building
the archive does not connect an account.

## Sign, publish and install

The public tools provide `skulk-cap keygen`, `sign`, `catalog`, `inspect`,
`publish` and journaled host lifecycle commands. Use `--help` for each command;
see the [SDK author guide](https://github.com/Foxlight-Foundation/skulk-capability-sdk/blob/main/docs/python-authoring.md)
and this reference's [release inputs and commands](releasing.md). Use the
`.author-venv/bin/skulk-cap` commands `sign`, `catalog` and `wheelhouse` in place
of the older `capability-build`, `capability-catalog` and `capability-wheelhouse`
entry points. Declare a new publisher identity and keep its private key outside
all release/feed inputs. Public package access grants no host or publisher trust.
Those released legacy commands do not enable signing this branch's native
candidate or its prerelease runtime lock.

The mock has a distinct bundle and callable IDs. Before enabling it on an
isolated author host, set its `state_directory` configuration to a new absolute
directory dedicated to that installation. It deliberately refuses a missing
explicit mock state directory. Never point it at an existing account store or
install it on a shared production fleet. Source `run/watch` in tools 0.1.0 is a
unary harness without configuration injection; use the explicit managed process
suite for this configured streaming reference rather than launching the live
profile with default account-state paths.

Installing requires a compatible dev host, explicit publisher trust, reviewed
permissions and normal management authority. Signed bytes, successful staging,
child readiness, actual calls and update/rollback are separate checks. Existing
first-party distribution uses an additional private maintainer source lock for
its protected signing job; public contributors do not need that path.
