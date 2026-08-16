# Contributing

Ferumind stores real user knowledge, so safety and compatibility changes need
the same care as application logic.

## Development setup

```bash
just setup
just install-hooks
just bootstrap
just verify
```

`uv` and one of the supported interpreters are required; the range and the
guards that keep it honest are in [docs/python-support.md](docs/python-support.md).
The locked product contract is in `product/`; where implementation and
`product/spec-mcp.md` disagree, the specification wins.

## How change lands

`main` is trunk and is protected. Nothing is pushed to it directly.

1. Branch from `main`.
2. Open a pull request.
3. Green CI — the `ci-gate` check covers the Python 3.12-3.14 matrix and the
   MCP SDK compatibility rows; `analyze` (CodeQL) is also required.
4. Merge (squash only).

Dependency updates from Dependabot follow the tier policy in
[docs/dependencies.md](docs/dependencies.md): patch (and Actions non-major)
updates may auto-merge once required checks pass; majors and `mcp` stay
manual.

Releases are cut separately from landing changes, so **a pull request never
edits the version in `pyproject.toml` and never creates a tag.** If your
change touches a versioned surface, add a line to `## [Unreleased]` in
[CHANGELOG.md](CHANGELOG.md) under `Breaking`, `Added`, `Changed`, or
`Fixed`. [docs/releases.md](docs/releases.md) defines which surfaces are
versioned, what counts as breaking, and how a release is cut.

If you cannot tell whether a change is breaking, say so in the pull request
rather than picking the smaller label.

A workspace format bump is always breaking and never lands alone: its
migrator, fixtures, and tests belong in the same pull request.

## Pull requests

- Keep core behavior in `ferumind.core`; the MCP, CLI, and dashboard layers
  call core rather than reimplementing it.
- Add deterministic success and failure tests, including adversarial tests
  for paths, files, network inputs, and concurrency where relevant.
- Do not add untyped public APIs, unjustified `Any`, broad exception
  suppression, or business logic in interface layers.
- Keep `.env`, `.env.example`, and typed configuration in sync.
- Run `just verify` and review the complete diff before requesting review.
- Keep changes focused. Explain security, migration, and compatibility impact.

Never commit live `workspace/` content, SQLite files, credentials, signed
URLs, logs containing user content, or generated agent configurations.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
