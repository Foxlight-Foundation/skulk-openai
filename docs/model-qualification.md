# Qualify enabled models through the unified API

`examples/qualify_models.py` is an optional account-free-testable probe tool.
It uses Skulk's public model/installation metadata and Chat Completions API;
it imports no private host modules or provider credentials. It does not link an
account, change settings, enroll clients or enable models. The integration must
already be installed and connected on a compatible Skulk host, with the desired
models enabled and the existing direct-owner API authority available.

The default is **metadata-only planning**. From the repository root:

```sh
uv run --no-project --with 'httpx>=0.28,<0.29' --with 'pydantic>=2.12,<3' \
  python examples/qualify_models.py
```

This command needs the two public Python dependencies, not the author SDK.
It does not establish that the complete SDK authoring/acquisition workflow is
available. It reads the current cached catalog; it never refreshes the upstream
account catalog. `--base-url` selects an HTTP(S) API origin without credentials,
a path or a query. Existing owner authorization remains enforced by Skulk;
ordinary LAN, relay and paired-device requests gain no new permission.

## Explicit execution

First inspect the plan. Then select exact IDs from your current catalog and a
**new** report filename:

```sh
uv run python examples/qualify_models.py \
  --model 'capability/openai.inference.chat/<enabled-model-slug>' \
  --execute --report ./qualification.json
```

This sends one small synthetic text-streaming request through the installed
capability and uses the connected account's plan/credit settings. `--model` can
be repeated. `--all-enabled` explicitly selects all currently enabled managed
models, bounded to 128 per run. There is no automatic generation retry or resume;
a report from an interrupted run cannot be re-executed by the library runner.
Existing report files are refused before network effects.

Add `--media` to plan two additional requests per model that advertises all four
required features: image input, text input files, PDF input and function tools.
The fixtures are generated in memory and contain no user files:

1. Send a small red PNG, a UTF-8 verification document and a one-page PDF with a
   required function. Require one complete call with an ID and schema-valid
   arguments.
2. Return that function result under the same ID and request a JSON schema with
   tools disabled. Require a normal completion with schema-valid content.

Definitions and tool execution belong to this caller. This fixture simulates one
local function; it does not execute model-supplied code or add hosted tools.
Only public assistant/tool fields enter the next request. Additive response
metadata cannot become input instructions.

## Reading the receipt

Each case records its concrete model, workload, observed selected/active runtime
identity and descriptor revision, HTTP attempt count, elapsed time and outcome.
The CLI includes its actual source-file SHA-256. It stores no prompts, generated
text, reasoning, function IDs/arguments, credentials or account identifiers.
Files are owner-readable and replaced atomically after each progress update.
Record your host build separately; installation metadata is an observation, not
independent cryptographic release verification.

- Text streaming requires nonempty assistant text, a normal stop, a complete
  `[DONE]` record and clean transport EOF. Late errors cannot become success.
- `not_advertised` means the media case was skipped; the model remains eligible
  for text. Untested never becomes a global unsupported claim.
- Expected color/document words and tool-result value preservation are separate
  answer observations. An unexpected valid answer does not by itself fail the
  serving contract. Valid output on this fixture does not qualify every schema.
- A runtime replacement during the run invalidates the aggregate identity-bound
  result without replaying generation. Interrupted receipts remain incomplete.
- Failure categories exclude provider-controlled error text. The exit status is
  nonzero on execution failure; inspect every case rather than inferring that a
  skipped feature passed.

The tool has its own whole-case deadline and bounded metadata/response parsers.
These are probe bounds; they do not change integration quotas or output settings.
Reports do not measure remaining plan allowance or promise included-only usage.

A pass covers the exercised installed API workloads only. Protected continuation,
compaction/headroom, reasoning controls, concurrent capacity, native speech,
browser behavior, independent author setup and separate-node execution retain
separate qualification requirements. This tool does not modify model capability
claims, routing policy or compaction profiles.

Maintainers run the credential-free tests with:

```sh
uv run pytest tests/test_model_qualification.py
```
