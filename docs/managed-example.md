# Exercise the public SDK interface

`examples/managed_child.py` is a credential-free child using only the MIT
capability SDK's public contracts, storage, wire and streams modules. Its
implementation is not vendored here. Public package downloads require no
account or private Git access.

In this repository's isolated environment:

```sh
uv sync --frozen --group managed
uv run --no-sync basedpyright src/skulk_openai/managed.py examples tests_managed
uv run --no-sync pytest tests_managed
```

The hash-pinned public SDK 0.5.5 wheel matches the currently qualified owner
0.10.5 runtime. CI also replaces only that SDK with the public author release
0.5.7 and repeats the managed checks. Their eleven runtime modules have identical
bytes; the distribution versions and package identities remain distinct.
Core and managed process jobs remain separate, and neither claims installed
qualification. No SDK test is silently skipped when a dependency is absent.

For unsigned builds, signing and your own feed, see the
[public author workflow](public-authoring.md). It installs tools 0.1.0 and
SDK 0.5.7 in a separate environment using public, hash-locked binary packages.

The explicit tests provision short-lived Unix sockets and an inherited startup
file descriptor, authenticate the child's hello/manifest, and exchange real SDK
frames with a subprocess. They run two profiles with distinct callable IDs, test
stream completion and cleanup acknowledgement, challenge health during
cancellation, then make a second call. Disabled inference withdraws offers and
rejects direct generation. No real token or remote HTTP endpoint is involved.

The harness implements the public protocol needed by these tests; it is not the
production managed owner or a Skulk API node. Two profiles sharing a synthetic
transport identity do not prove actual core discovery/loader admission. Signed
installation, production-owner integration, cross-node calls and live OAuth are
separate gates. Do not install this mock on a shared service or production fleet.
