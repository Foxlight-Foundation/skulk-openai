# Security boundaries of this slice

All automated provider tests and grants here are synthetic. The inference
adapter uses an explicit mock transport. The separate owner-local account setup
command implements OAuth and protected session storage; only an explicit
operator action enables authentication HTTP. Tests never sign into an account,
read environment credentials, start a Skulk node or install a service. All HTTP
clients disable environment proxy/configuration inheritance.

Installing a capability, discovering an offer, account consent and caller
invocation authority are separate events. This slice tests that untrusted request
fields cannot manufacture grants, hard constraints precede preference, and direct
invocation is checked before the mock HTTP sink sees content. It does not
implement authenticated caller/grant propagation across a distributed fabric.

The synthetic owner must supply authority and serialize admission with revocation.
Rechecks are local reference logic, not a cryptographically authenticated grant
format or a guarantee that remote revocation stops already-running upstream work.
Offer labels are assertions under host trust; a marker or advertised domain is
not attestation. An algorithm cannot infer private processing from a provider name.

The [session foundation](account-setup.md) implements protected host-local storage,
OAuth validation, account/client binding and rotation/disconnect fences. Before
live fabric admission, qualify these alongside caller authority across every
entry path, bounded usage enforcement and inference outbound endpoint policy.
No provider token belongs in descriptors, replicated
state, event logs, prompts, URLs, browser storage or routine diagnostics. Do not
infer a no-paid-credit guarantee without proven upstream enforcement.

Managed child processes provide lifecycle isolation, not an adversarial OS sandbox.
The example authenticates the startup identity/manifest through public SDK IPC;
that is distinct from authenticating the human caller. Primary disconnect and
shutdown own task cleanup. Individual media cancellation releases the call while
health remains available. Stream errors contain fixed codes, not upstream bodies.

No hidden billing fallback, automatic retry after uncertain execution, provider
switch after partial output or automatic execution of returned tool instructions
is provided. Self-hosting does not make a compromised host or plugin trustworthy.
