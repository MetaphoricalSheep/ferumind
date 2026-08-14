# MCP SDK support policy

Ferumind supports the **MCP Python SDK 2.x**, declared in `pyproject.toml` as
`mcp>=2.0.0,<3`. Every version that range admits is exercised by a CI
compatibility matrix on every push. Nothing outside it is supported.

This is the SDK counterpart of [python-support.md](python-support.md), and it
exists for a sharper reason: Ferumind depends on two SDK internals that carry no
compatibility guarantee at all.

## The rule

> Advertise exactly the SDK versions CI has run the boundary suite against.
> Never more.

An uncapped `mcp>=2.0.0` would claim support for major 3 — a release that does
not exist, that nobody has tested, and that is free to rename the two private
attachment points below out from under the server.

## Why the cap is tighter than a normal dependency

Everything Ferumind needs from the SDK is public on 2.x except two things, both
isolated in [`src/ferumind/mcp/sdk_internals.py`](../src/ferumind/mcp/sdk_internals.py):

| Attachment point | Why no public equivalent | What breaks if it is renamed |
|---|---|---|
| `_lowlevel_server` | `MCPServer.run_stdio_async()` owns its own transport, so the bounded, redacting stdio streams and the per-file-MIME `resources/read` handler both need the `Server` underneath | stdio startup and resource reads |
| `_tool_manager` | Reaching the manager's *public* accessors, and replacing a registered tool's generated argument metadata, which nothing public exposes | argument validation and error sanitisation on every tool |

Both accessors **fail closed**: a missing hook raises at startup rather than
degrading to a server with no argument validation and no exception sanitisation.
That behaviour is what makes an untested SDK version a startup failure instead
of a silent security downgrade — but it is only a safety net. The cap is the
actual control, and the matrix is the evidence behind it.

A major bump is therefore never routine. It is the release the SDK is
*permitted* to rename these in.

## Where each declaration lives

| Location | Value | Purpose |
|---|---|---|
| `pyproject.toml` `dependencies` | `mcp>=2.0.0,<3` | The support claim; installers refuse anything else |
| `uv.lock` | one exact version | What a source checkout — the only supported install path — actually installs |
| `.github/workflows/ci.yml` `resolve-mcp-matrix` | derived | Resolves the rows from the specifier above |
| `.github/workflows/ci.yml` `mcp-sdk-matrix` | derived | The actual proof |
| `tests/unit/test_mcp_hardening.py` | guards | Fails if the declared range or the running SDK drifts |

Note what is **not** in that table: a hardcoded version list. The matrix reads
`pyproject.toml`, so widening the cap widens the matrix in the same commit,
automatically. There is one source of truth.

## Why the matrix has one row today

`scripts/mcp_matrix_versions.py` selects three interesting versions — the lowest
the specifier admits, the version `uv.lock` pins, and the highest the specifier
admits — then **deduplicates** them.

As of 2026-08-08, `mcp` has exactly one stable 2.x release on PyPI: `2.0.0`.
Every other 2.x artifact (`2.0.0a1`, `2.0.0a2`, `2.0.0a3`, `2.0.0b1`, `2.0.0b2`,
`2.0.0rc1`) is a pre-release, and Ferumind makes no claim about running on an
alpha, beta, or release candidate. Lowest, locked, and highest are all `2.0.0`,
so the matrix runs **one row**.

That is the honest amount of evidence available, and it is why the version set
is resolved rather than listed. A hardcoded three-row matrix would install
`2.0.0` three times and report three green rows for one version's worth of work
— which is precisely the failure this policy exists to prevent. **Do not "fix"
the single row by hardcoding versions.** When the next stable 2.x ships, the
matrix becomes two rows on its own; a release after that makes it three.

The resolver fails loudly — non-zero exit, named reason — if it cannot read the
specifier, cannot reach the index, finds no release satisfying the range, or
finds a locked version the declared range forbids. A matrix that runs zero rows
reports green having proved nothing, which is worse than no matrix.

## The install traps, and why the matrix asserts in-process

Each matrix row proves which SDK it ran against **from inside the test process**,
via `importlib.metadata.version("mcp")`, never from the install command's output.
Two `uv` behaviours make this mandatory, and both were reproduced on 2026-08-08
while building the matrix:

1. **A plain `uv run` re-syncs the environment**, uninstalling a pinned SDK and
   reinstalling the locked one. A row pinned to a non-locked version then runs
   the locked version and passes. The matrix uses `uv run --no-sync`.
2. **`uv pip install` targets `VIRTUAL_ENV`**, not `UV_PROJECT_ENVIRONMENT`. With
   a shell's virtualenv active it edits *that* environment while the operator
   believes otherwise. The matrix passes `--python .venv/bin/python` explicitly.

CI sets `FERUMIND_EXPECTED_MCP_VERSION` per row to the version that row is meant
to exercise, and `test_installed_mcp_sdk_matches_the_declared_range` refuses to
pass on anything else. It is a CI job parameter, not Ferumind configuration —
it belongs in no `Config` model and no `.env`. Unset, as in every ordinary
`just verify`, the test falls back to asserting the supported major.

If a row ever fails that assertion, **fix the install step**. Relaxing the
assertion restores the exact silent-green failure it was written to catch.

## Widening the cap

Raising `<3` is a support claim about a major release that is allowed to have
renamed both attachment points. It requires evidence, not optimism.

### Evidence required

1. **The full tracked suite passes on the new major**, not just the boundary
   suite. Resolve into a throwaway environment — never `.venv`:

   ```bash
   export UV_PROJECT_ENVIRONMENT=/tmp/ferumind-mcp-next
   uv sync --all-extras --dev
   uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" "mcp==<new version>"
   uv run --no-sync pytest -q
   ```

   `--no-sync` is load-bearing. Without it the run silently tests the locked
   version and tells you the new major is fine.

2. **Both attachment points resolve, by name.** Read
   `src/ferumind/mcp/sdk_internals.py` against the new SDK and confirm
   `_lowlevel_server` and `_tool_manager` still exist and still mean the same
   thing. A rename must be found here, deliberately — not discovered by a
   `RuntimeError` in front of a user.

3. **The behaviours those hooks carry still work**, which the boundary suite
   covers: tool registration, strict argument validation, observation
   middleware, `resources/read`, stdio startup, and wire conversion.

4. **The result contract is unchanged.** Confirm `structured_output=False` still
   suppresses `outputSchema` and that hand-built `CallToolResult` values are
   returned verbatim, or `isError` and `error_code` change shape for every
   caller.

### The change itself

Raise the cap and let the matrix follow. Widening `mcp>=2.0.0,<3` to
`mcp>=2.0.0,<4` makes the resolver offer the new major's releases as rows on the
next CI run — no workflow edit, no version list to update. Relock in the same
commit so `uv.lock` and the declared range cannot disagree; the resolver rejects
that contradiction rather than resolving around it.

`test_mcp_sdk_range_is_capped_to_the_tested_major` asserts the declared range
directly, so it fails until it is updated to match — deliberately, so that
widening the cap is never a one-character edit nobody reviews.

### What does not justify widening

- A green Dependabot PR. It proves the new version resolves, not that it works.
- Upstream release notes claiming backward compatibility. The two hooks are
  private; no compatibility promise covers them.
- A passing boundary suite alone, for a **major** bump. Minor and patch moves
  inside the declared range are what the matrix covers routinely; a new major
  needs the full suite.

## Current evidence

`mcp` 2.0.0 was verified on 2026-08-08, when the resolver returned a single row
and the boundary suite passed against it with the version asserted in-process.
Re-verified 2026-08-14: the boundary suite is 137 tests and all pass on 2.0.0,
with the full tracked suite green. Ferumind migrated off 1.x, which is in
upstream security-only maintenance.

Both counts are dated records. The suite grows, so the number says what had
been run by that date; re-run rather than trusting it.
