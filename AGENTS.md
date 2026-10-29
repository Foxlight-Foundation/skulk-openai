# Working on skulk-openai

This public repository is a learning reference. Keep private platform source,
strategy, credentials, account information and environment details out of it.
The separately licensed capability SDK must be consumed through its public
interfaces; never vendor its implementation or import private owner modules.

Experimental implementation lives on `initiative/openai-provider`. Branch each
slice from that branch and target it with a ready pull request. Keep platform
Skulk changes separate, based on current dev and its repository instructions.
A green, reviewed slice may merge into this experimental branch. Promotion to
main requires an explicit accepted integration milestone; never imply source
checks qualify signed installation or real provider access.

Use strict frozen Pydantic models, exhaustive types, bounded asynchronous I/O,
owned task cleanup and docstrings on public classes/functions. Generic inference
contracts must not depend on OpenAI implementation modules. Tests must prove
observable policy, stream, cancellation and lifecycle boundaries.

Before every implementation commit run:

    uv run basedpyright
    uv run ruff check
    uv run ruff format --check
    uv run pytest
    git diff --check
    git diff --cached --check

Managed SDK tests are a separate explicit suite. Missing SDK access must be
reported as a gate, never silently treated as a passing managed test. Also run
`uv run basedpyright examples tests_managed` and `uv run pytest tests_managed`
when the licensed SDK is available; core CI covers only `src` and `tests`. Install
licensed packages only in this repository's isolated environment.

Use ready PRs and complete the review/check loop. Apply the Foxlight severity
formula: components are half of likelihood/impact (1-5, zero when 1); subtract
0.2 times complexity (0-2); for base >=1 raise to 1/(1+0.25*triggerability), with
triggerability 0-2. Impact 1 is informational. Fix scores >3.5; reply with the
rationale for deferring lower scores and resolve handled threads. Merge the
exact checked head into the initiative branch after checks and review pass.

Automated tests must remain credential-free and use mock upstream transports.
Owner-local account setup may contact the fixed official issuer only after an
explicit operator action. Never sign in, read another tool's tokens, contact a
real provider account or incur inference usage during unattended development.
Caller authority and protected credential handling must be qualified before
real-account fabric admission. Use only authorized isolated fleet leases under
normal coordination rules; never launch a default node into a shared fleet.
