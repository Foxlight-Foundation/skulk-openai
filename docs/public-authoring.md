# Build the Integrator with public author tools

The MIT Python SDK and author tools are immutable anonymous packages. No GitHub
account, private repository, provider account, model, GPU or fleet lease is
needed for the steps below. This application remains Apache-2.0; the separately
licensed owner runtime stays outside its source bundle and author environment.

The initiative's `0.7.0` source candidate selects SDK `0.5.16rc1`, tools
`0.2.0rc4` and compiled kernel `0.7.0rc7`. Its separately selected runtime owner
is `0.18.0rc8`. See [SDK1 adoption](sdk1-adoption.md) for scope and acceptance
boundaries. These source inputs do not reissue the signed `0.6.3` Store preview.
Supported native targets are macOS ARM64 and glibc Linux x86_64/ARM64, with
glibc 2.34 or later on Linux.

## Install and check the reference

From this candidate checkout, using Python 3.13 and uv:

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

`authoring/requirements.txt` pins the complete author dependency set, including
the public SDK, tools and platform-selected native kernel. Neither author lock
installs Core or the owner. `authoring/sdk-only.txt` selects the same SDK for an
independent managed-process check in CI. Refresh the author lock deliberately:

```sh
uv pip compile authoring/requirements.in --python-version 3.13 --universal \
  --generate-hashes --no-strip-extras --output-file authoring/requirements.txt
```

Core, managed-process and author-build jobs run independently on all three
targets. Process checks use synthetic identities and mock upstream responses;
they do not establish signed installation or real provider access.

## Build an account-free candidate

Set `INTEGRATOR_BUILD` to a new absolute directory outside the checkout. Stage
the native spec under the public CLI's `capability.toml` filename; this leaves
the checkout's legacy example spec unchanged.

```sh
umask 077
mkdir "$INTEGRATOR_BUILD"
export INTEGRATOR_BUILD
PYTHONPATH=src .author-venv/bin/python - <<'PY'
import importlib.util
import json
import os
import shutil
from pathlib import Path
from skulk_openai.native_contracts import build_native_manifest

assert importlib.util.find_spec("skulk") is None
assert importlib.util.find_spec("skulk_capability_nodes") is None
project = Path(os.environ["INTEGRATOR_BUILD"]) / "project"
project.mkdir(mode=0o700)
shutil.copytree("src", project / "src")
shutil.copyfile("LICENSE", project / "LICENSE")
shutil.copyfile("capability-native.toml", project / "capability.toml")
manifest = build_native_manifest(
    executable="bundle.pyz", executable_sha256="0" * 64, mock=True
)
(project / "manifest.json").write_text(json.dumps(manifest))
PY
.author-venv/bin/skulk-cap build --native \
  --project "$INTEGRATOR_BUILD/project" \
  --output "$INTEGRATOR_BUILD/unsigned"
```

The builder validates the actual native declaration, deterministic source archive
and import closure. The mock has independent bundle and callable identities.
Generating its manifest does not connect an account or grant execution.

## Explicit native runtime inputs

The [SDK/tools candidate release](https://github.com/Foxlight-Foundation/skulk-capability-sdk/releases/tag/v0.5.16-rc.1)
provides native runtime-five/catalog-four authoring. The SDK wheel SHA-256 is
`f61591b608e2f3991ad4052ce607ef57af9384c880b81472dc1fc3b897e4a15f`.
Read its [native publishing contract](https://github.com/Foxlight-Foundation/skulk-capability-sdk/blob/51cfa35e1ec7546163b0c9e2af0f546886c8893d/docs/native-publishing.md).

For a candidate runtime wheelhouse, select the owner root and this application's
additional requirements from its committed lock:

```sh
.author-venv/bin/skulk-cap wheelhouse --native-candidate \
  --lock uv.lock --root skulk-capability-nodes \
  --spec capability-native.toml --output "$INTEGRATOR_BUILD/wheelhouse"
```

The complete closure includes PyJWT as well as the owner, SDK, compiled kernel
and transitive dependencies. A generic SDK-reference owner recipe alone lacks
these application additions. The [0.6.3 acquisition guide](native-runtime.md)
documents a different issued closure and must not be substituted here. Keep
author tools, application runtime and publisher trust separate. Wheel acquisition
checks every selected size and digest and never builds private source.

## Sign, publish and install separately

Use the public CLI's explicit native signing/catalog options and the reviewed
release policy. `skulk-cap sign --help`, `catalog --help`, `runtime --help` and
`inspect --help` describe their separate inputs. Source bytes, wheel acquisition,
signatures, installer admission, child readiness and successful calls are
independent checks. Anonymous package access grants no publisher trust or host
authority. The candidate is not a stable SDK1 or Skulk release.

For an author experiment use the distinct mock bundle, a disposable key and a
new explicit `state_directory`. Never point it at an existing account store.
Use the managed-process suite for account-free configured streaming checks.
Real-account setup requires an explicit operator action and the independently
qualified signed runtime; changing `mock=True` is not qualification.

Installing requires a compatible host, explicit publisher trust, reviewed
permissions and normal management authority. Existing first-party distribution
also uses a protected maintainer source lock and signing gate; contributors do
not need private repository access for the workflow above. See
[release inputs](releasing.md) and [native setup](native-setup.md) for the issued
release's separate maintainer and operator paths.
