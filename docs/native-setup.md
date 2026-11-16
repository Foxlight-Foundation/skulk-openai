# Setting up the native Integrator preview

This guide covers the `0.6.2` preview native candidate. Use the compatible Skulk
build and signed release source identified by that release's qualification
notes. Source availability, a signed package, Store publication and stable
Skulk support are separate facts; the version number `2.0.3` alone does not
identify a native-capable Skulk build.

## A new installation

1. In **Plugins**, browse the release source and review **Skulk OpenAI
   Integrator**. Its permissions include account connection, external inference
   and optional local encrypted continuation storage. Install the signed release
   that matches your platform.
2. Configure the installed application with a new private state directory.
   A minimal configuration is:

   ```json
   {
     "application": {
       "state_directory": "~/.local/state/skulk-openai-native",
       "inference_enabled": true
     }
   }
   ```

   Select a new or empty folder belonging to your user account. Setup creates
   a disconnected journal bound to the actual installed child. It creates no
   provider account, caller grant or continuation key. Keep this directory
   across restarts and application updates.

3. Enable the application and open its management surface. **Connection**
   should show **Disconnected**, with settings available. A new installation
   needs no legacy node identifier, snapshot or migration receipt.
4. Use the existing [account connection](account-setup.md) flow. Connecting an
   account requires your explicit action and the provider's consent process.
   Provider availability and usage follow that connected account and its app
   settings.
5. Models load automatically after successful consent. Select the models that
   Skulk may use and save the allowlist and request defaults. Fresh installations
   enable inference, permit OpenAI processing and **Follow ChatGPT app settings**
   by default. **Connection → Advanced controls → Pause requests** pauses
   generation while keeping the account connected. Previously saved pauses and
   usage restrictions remain effective; resume or change them explicitly.
   Saved integration settings take precedence over the host configuration's
   initial `inference_enabled` fallback. Account, model and Skulk caller authority
   are independently required. Selecting models does not approve a gateway.
6. In the Integrator's **Skulk access**, find the compatible Skulk gateway and
   save its public key approval. If Skulk uses a different local API port, enter
   that port before **Find gateway**. This approval lets the gateway submit
   requests under the saved account and model policy.
7. On the compatible Core build, open **Settings → Inference access**. Enable
   approved access, choose **This dashboard (direct owner)** and select the
   exact installed capabilities it may use. Review their processing domains,
   then **Save inference access**. This is separate from provider consent.
   Enabled, authorized models then appear in Chat and the unified model catalog.
8. For an external API application, create a separate client key in that same
   panel and save the one-time disclosure in the application. A new key starts
   without capability permissions. Select the new caller, approve its exact
   capabilities and save. Use its bearer credential and exact returned native
   model IDs with the unified API. Revoke the key when it is no longer needed;
   approving this dashboard does not approve an external client.

The connection and settings UI is the normal integration management surface.
Trying a request is optional. [Client enrollment](client-enrollment.md) explains
continuation keys, which are a separate opt-in from an ordinary API credential.
Checkpoint storage stays disabled until configured; fresh setup does not enable
automatic compaction or make a token-savings claim.

## Existing state and setup failures

A populated state directory is preserved. Selecting it does not automatically
adopt its credentials or change its proof identity. Use the explicit
[existing-state handoff](native-managed.md#protected-state-adoption) when that is
actually needed. Ordinary first setup uses a new directory instead.

The private `native-initialization.json` record binds fresh state to its store,
installed child, application bundle and exact contract identities. It grants
neither account access nor caller authority. An adoption retains its actual
`native-adoption.json` receipt and original application proof target; those
receipts are not interchangeable.

An interrupted state publication keeps `state-transfer.pending`, so a partial
journal cannot become active. Preserve that directory for diagnosis and choose
another new directory for a fresh connection; do not remove guards or overwrite
a journal that may contain account state. An unsupported host, missing trust,
disconnected account and denied caller access are distinct setup conditions.

## Qualification scope

Account-free tests cover new/empty roots, restart, private permissions, immutable
existing journals, explicit adoption and interrupted creation. A real public-SDK
process reaches the disconnected settings UI without a migration fixture.
Signed installed Core, rendered browser behavior, provider consent and
account-backed requests each require their own recorded acceptance.
