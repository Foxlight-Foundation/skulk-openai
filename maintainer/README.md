# Protected first-party producer input

`publisher-source.lock` retains the exact private producer source pins from the
qualified 0.5.1 release for the existing protected signing workflow. It is read
as data by that workflow (`lock: maintainer/publisher-source.lock`), not used
with `uv sync` in this checkout. Its project metadata is historical.

Public contributors use root `uv.lock`, which pins public binary SDK/owner
packages without Git sources. Public own-feed authors use that root lock for
wheelhouse acquisition and the independent public tools environment described
in [the author guide](../docs/public-authoring.md). They need neither this
maintainer lock nor private producer access. Package access grants no publisher
or installed-host authority.
