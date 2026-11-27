---
title: Release notes
sidebar_position: 11
---

## SDK1 candidate 0.7.1 (unpublished)

- Uses public SDK 1.0.1 and kernel 0.7.0 with the separately installed owner 0.18.1.
- Recovers interrupted SQLite writes before native identity checks or offline
  backups, preserving committed account, replay and encrypted continuation state.
- Requires the matching experimental SDK1 host. This source candidate does not
  replace the signed 0.6.3 Store preview or establish stable Skulk support.

## Native preview 0.6.3

- Accepts validated compaction-progress metadata for a pending compaction item under an explicit profile. It does not turn that progress into public model output or a committed checkpoint.
- Published as signed native Store packages on all three supported platform targets.
- Installed qualification covers the ten-model serving fixture, effective Luna/Sol compaction and native Chat/API composition with mounted speech.
- Automatic compaction remains opt-in. The compatible experimental host requirement remains in place.

The [qualification record](https://github.com/Foxlight-Foundation/skulk-openai/blob/f090e9fc12f8a7411264cc0ab08299036ab85c2d/docs/native-qualification-0.6.3.md) describes the tested implementation. [Compatibility](compatibility.md) lists current requirements and limits.

## Native preview 0.6.2

Accepts null or empty-string content on tool-only assistant messages. Native refusals retain bounded caller-input cleanup so a denied client can receive its semantic error cleanly.

## Native preview 0.6.1

Fresh connections enable inference and OpenAI processing and follow ChatGPT app settings. Consent completion clears the pending message; models load automatically. Advanced controls provides one Pause requests switch. The sidebar note scrolls with its navigation group.

## Native preview 0.6.0

Introduced the signed native Store preview, public compiler-free runtime acquisition and clean initialization of a new private application state directory.

## Updating

Review a Store update before installing it. Preserve the existing private state directory. After the update, confirm the account and settings, then review the new signed implementation in Skulk's caller permissions. See [Privacy and access](security.md).
