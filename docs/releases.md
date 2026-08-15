# Versioning and releases

Ferumind is `0.MINOR.PATCH`. The leading zero is a statement, not a
placeholder: the surfaces below still move, and this document says exactly
what that means for anyone running the code.

## What you may rely on

**Pin a tag.** Tags mark commits that were smoke-tested and are never moved
once pushed — `v0.2.0` will always be the same bytes.

```bash
git clone https://github.com/MetaphoricalSheep/ferumind.git
cd ferumind
git checkout v0.1.0
```

`main` moves. It is the integration line, it is always green, and it is not a
release. If you clone it you get whatever landed most recently, which is fine
for following along and wrong for anything you depend on.

If you want certainty beyond a tag, pin the commit the tag points at
(`git rev-parse v0.1.0`). A tag is a name for a commit; the commit is the
thing.

## What the version number promises

Exactly one thing: **a breaking change bumps the minor.** If you upgrade from
`0.3.1` to `0.3.4` nothing that worked will have stopped working. If you move
to `0.4.0`, read the changelog first.

Python has no `^` operator, so nothing enforces that for you. Write the
constraint yourself:

```
ferumind>=0.3,<0.4
```

That is the whole promise. It does not include:

- **Backports.** Fixes land on the newest line. `0.3.x` does not get patched
  once `0.4.0` exists.
- **A deprecation window.** Things can be removed in the release that
  announces their removal.
- **A support window.** There is no "supported version" other than the
  newest.
- **Packaged artifacts.** No wheel, no PyPI release, no container image. This
  is a source checkout.
- **The Python import API.** `import ferumind` gives no signal at all when it
  changes: the Python import API is private and unversioned. Use the MCP
  server or the CLI.

Versioning honestly is not the same as supporting old versions. Ferumind does
the first and not the second.

## The five versioned surfaces

Only these are covered:

1. **MCP tool surface** — tool names, arguments, result fields, error codes
2. **Workspace format** — the on-disk layout, tracked separately in
   `workspace/system/meta.yml` and specified in
   [product/spec-versioning.md](../product/spec-versioning.md)
3. **CLI** — command names and flags
4. **Configuration** — environment and config keys, and their defaults
5. **Resource URIs** — the `ferumind://` scheme

Everything else is internal. The SQLite schema in particular is rebuildable
system state that migrates itself at startup; you will never need to know its
number.

## Breaking, or not

| Breaking | Not breaking |
|---|---|
| The workspace format bumps | A new tool is added |
| A tool is removed or renamed | A new **optional** argument is added |
| A required argument is added | A new field is added to a result |
| An argument's meaning or type changes | A new error code covers a new condition |
| A result field is removed, renamed, or retyped | A new CLI command or optional flag |
| An error code is removed or changes meaning | A new config key with a safe default |
| A CLI command or flag is removed or renamed | A new folder or optional frontmatter key that older code ignores |
| A config key is removed, or a default changes behavior | The supported Python range gains a version |
| The supported Python range loses a version | Bug fixes, security fixes, performance, docs, refactors |

A workspace format bump is always breaking, and it never lands without its
migrator, fixtures, and tests in the same change. That rule is the one thing
this project will not trade away — see
[spec-versioning.md §1.4](../product/spec-versioning.md).

## Which number moves

Today, at `0.x`:

- **breaking** → bump the minor, reset the patch: `0.3.4` → `0.4.0`
- **anything else** → bump the patch: `0.3.4` → `0.3.5`

Two slots means new features and bug fixes share the patch position. The
changelog carries that distinction; the version number only answers "does
this break me."

After `1.0.0`, the table above does not change — only the mapping does:
breaking → major, additive → minor, fixes → patch.

## What you are running

```bash
uv run ferumind --version
uv run ferumind info          # adds the workspace path and its format
```

In a source checkout that reports the *installed* distribution metadata, so
it can lag a `pyproject.toml` edit until the next `uv sync`.

## Cutting a release

Landing a change and cutting a release are separate acts. Every pull request
records what it did; releases are cut when there is something worth pinning.

**Every pull request** that touches a versioned surface adds a line to
`## [Unreleased]` in [CHANGELOG.md](../CHANGELOG.md), under `Breaking`,
`Added`, `Changed`, or `Fixed`. Pull requests never edit the version in
`pyproject.toml` and never create tags.

**To cut a release:**

1. Read `## [Unreleased]`. If it has a `Breaking` section, this is a minor
   bump; otherwise a patch bump.
2. Set the new version in `pyproject.toml`.
3. Rename `## [Unreleased]` to `## [<version>] - <YYYY-MM-DD>` and open a
   fresh empty `## [Unreleased]` above it.
4. Land that as a pull request, like anything else.
5. Tag the merge commit and push:

   ```bash
   git tag -s v0.4.0 -m "v0.4.0"
   git push origin v0.4.0
   ```

The tag push runs `release-check`, which fails if the tag and the
`pyproject.toml` version disagree. One tag, one commit, one version, one
changelog entry — the check is there because those four drift the moment
nothing compares them.

Tags are signed. `v` is the tag prefix and appears nowhere else.

## Tags never move

Once a tag is pushed it is frozen, and the repository ruleset enforces that:
`v*` tags cannot be updated or deleted.

A movable tag is worse than no tag. Someone who pinned `v0.4.0` would get
different code tomorrow with no signal, which is exactly the failure a pin is
supposed to prevent.

If the wrong commit gets tagged, the fix is to burn the number and release the
next one. Deleting is only an option in the first few minutes, before anyone
could have fetched it. Version numbers are free; a moved tag is a
supply-chain problem.

## When this becomes 1.0.0

Either of:

- the five surfaces go a meaningful stretch of real outside use without a
  breaking change — a format bump becomes a surprise rather than an
  expectation; or
- Ferumind starts publishing installable artifacts, which requires deciding
  backports, deprecation, and support windows anyway.

Until one of those happens, `0.x` is the accurate description and costs
nothing.
