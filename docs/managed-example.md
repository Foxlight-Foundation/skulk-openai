# Exercise the public SDK interface

`examples/managed_child.py` is a credential-free child using only the capability
SDK's public `contracts`, `storage`, `wire` and `streams` modules. The SDK is a
separate licensed dependency; its implementation is not vendored here. Obtain a
compatible SDK through the supported distribution/access process. This slice
was exercised with author SDK 0.5.0, protocol 4.

In this repository's isolated development environment, install the licensed SDK
package you obtained, then explicitly run:

```sh
uv run basedpyright src/skulk_openai/managed.py examples tests_managed
uv run pytest tests_managed
```

These commands require SDK access and fail when it is absent. The ordinary core
suite does not silently skip managed tests or claim to have run them. Core CI
has no SDK distribution credentials and only runs the core suite. SDK package
availability and reproducible acquisition remain delivery prerequisites.

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
