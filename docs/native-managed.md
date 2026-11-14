# Native managed entrypoint

The experimental `skulk-openai-native` entrypoint consumes public SDK
**0.5.10rc1** and native **0.7.0rc4**. OAuth, sessions, model catalog, settings,
provider requests and encrypted continuation history remain capability-owned.
The legacy entrypoint remains available and uses the same application services.

```sh
uv sync --frozen --group managed --group native-runtime
uv run --no-sync basedpyright --project pyrightconfig.managed.json
uv run --no-sync pytest tests_managed
```

These process checks use generated accounts, actual native codecs and mock
upstream transports. They are separate from normally signed host installation,
account-backed model qualification and stable release admission.

On macOS, a separate 12-check installed gate exercised the actual source bundle
through the normal signed publisher, Core installer and owner with an explicitly
built owner 0.18 candidate. It recovered a populated legacy checkpoint, enrolled
the actual installed child, discovered all eight native contracts, performed a
bearer-authorized bidirectional continuation, disabled the child normally,
recovered the new receipt through legacy rollback and refused signature replay.
Private backup verification and quarantined restore also passed. This synthetic
result does not qualify a real account, other platforms, distribution, native UI
opening or the unified chat adapter.

## Independent contract identities

`skulk_openai.native_contracts.build_native_manifest` builds and actually admits
all eight declarations with the released compiled kernel. Native descriptors
have complete 64-character revisions. Existing application proof contracts and
checkpoint targets retain their original 16-character revisions. Neither value
is padded, truncated or reinterpreted as the other.

A native request uses this explicit envelope:

```json
{
  "contract": "openai.legacy-envelope@1.0.0",
  "legacy_revision": "<original application revision>",
  "payload": {}
}
```

Signed descriptor annotations identify the legacy ID, version and revision.
The envelope's payload is validated by the unchanged strict application models.
This preserves their Unicode identifier rules, which cannot be re-exported as
portable/2 regular expressions. The envelope is a distinct native contract;
it does not claim that its outer schema replaces domain validation.

Metadata reads declare local processing. Generation, both chat versions and
continuation declare external inference. SDK startup, composition, exact target,
mode, native revision and request-byte checks precede application behavior.
Application grants, signatures, nonces, account generation, model permissions
and owner settings remain independently enforced. A context or migration receipt
cannot grant caller authority.

JSON control values obey portable/2's 64 KiB bound. Large request bodies use
binary caller media. Large text and function-argument deltas are fragmented
without truncation. A completed transport terminal establishes transport cleanup;
clients must also interpret the inner chat/continuation outcome.

## Protected state adoption

Native configuration supplies an explicit logical proof target and private root:

```json
{
  "legacy_target_node": "unchanged-application-proof-target",
  "application": {
    "state_directory": "/absolute/private/state",
    "inference_enabled": false
  }
}
```

The inherited startup supplies the actual installed native child identity. The
native entrypoint requires a private adoption receipt matching that child,
bundle, original store identity and both contract families before it opens or
recovers the application journal. There is no default-root fallback.

Stop the installed provider through its owner before offline operations. This
also applies to older children that predate the application fence. The tools
use stable process/session fences, a database write fence, private files and
coherent SQLite/key snapshots. They never unlink worker-lock inodes.

```sh
# For a deliberately new, empty installation only:
uv run skulk-openai-state initialize --state-root /absolute/private/state

uv run skulk-openai-state snapshot \
  --state-root /absolute/private/state --output /absolute/private/backup
uv run skulk-openai-state verify --backup /absolute/private/backup
uv run skulk-openai-state adopt \
  --state-root /absolute/private/state --backup /absolute/private/backup \
  --native-node-id actual-installed-child \
  --legacy-target-node unchanged-application-proof-target
```

Use `--mock` only for the independently identified synthetic bundle. A stale
snapshot is refused after a revocation, nonce use or other journal mutation.
Snapshots contain provider secrets and remain private; they are not release
artifacts. Core API credentials and manager identity are separate custody and
are not included in capability snapshots.

Rollback selects the legacy runtime against the **current** journal and original
logical proof target. It does not restore an older snapshot or reset replay/CAS
history. Process tests exercise legacy → native → legacy continuation with the
same keys, receipts and encrypted records.

Disaster restore always creates a new quarantined root:

```sh
uv run skulk-openai-state restore \
  --backup /absolute/private/backup --output /absolute/private/recovered
```

It retains encrypted records, known nonces, budgets and CAS versions, advances
account/sign-in fences, revokes old application grants and continuation clients,
clears gateway enrollment and disables inference. Fresh explicit enrollment is
required. An interrupted restore retains a pending marker that prevents serving.
The recognized credential journal is published only after the copied state is
reverified and its authority is quarantined, protecting older readers that do not
understand the pending marker. Publication refuses an existing journal.
Existing roots are never overwritten and uncertain submissions are never replayed.

## Management surface

The same capability-owned settings UI runs under native setup/cleanup. Frozen
native health accepts HTTPS links or private proxy ports; the local HTTP UI
therefore declares a proxied surface and reports its actual private port. It
does not publish an HTTP URL under a false HTTPS scheme. Host UI opening and
unified-chat discovery require their own native adoption qualification before
replacing an existing installed integration.
