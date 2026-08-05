# Ferumind

> Chats are disposable; Ferumind is where the continuity lives.

Ferumind is a local-first, Markdown-backed workspace shared by a person and
their AI agents. Documents remain inspectable on disk while a stateless MCP
server provides project scoping, retrieval, guarded edits, snapshots, and an
auditable operation history.

This repository is Ferumind v2, currently **alpha**. The product contract is in
[`product/`](product/00-what-is-ferumind.md); when code and
[`product/spec-mcp.md`](product/spec-mcp.md) disagree, the specification wins.

## Security status

The supported deployment today is single-user and local, over MCP stdio.
Direct SSE and streamable HTTP transports are disabled in code.

**Do not expose Ferumind to the public internet yet.** Server-verified OAuth
and subject-to-workspace authorization for ChatGPT and Claude are the next
remote-serving gate. See [SECURITY.md](SECURITY.md) for the threat model,
reporting process, and the complete internet deployment checklist.

The public-tree release check is a necessary current-tree control, not approval
to publish by itself: it classifies tracked paths but does not inspect file
contents or Git history. Before making an existing repository public, review
the complete history and current content for secrets and private data; revoke
any exposed credential and either rewrite the affected history or publish from
a reviewed clean export. Publishing or remotely serving a checkout that
contains a live workspace is never supported. Live workspace content,
databases, environment files, and generated agent configurations are ignored
by Git; the public repository must contain code and synthetic fixtures only.

## What it provides

- A versioned Markdown workspace with one explicit registry and project
  boundary.
- Folder-derived document roles and policy metadata.
- Lookup-first editing with guarded propose → apply semantics.
- Snapshot-before-mutation, restore, archive, and out-of-band reconciliation.
- SQLite FTS5 search, indexing, operation history, and metadata-only MCP call
  observations.
- Bounded binary uploads and SSRF-hardened ChatGPT file-reference ingestion.
- A Typer CLI, mechanical filesystem watcher, and local stdio MCP server.

The server is deliberately a librarian, not an autonomous knowledge agent:
documents carry the behavior contract and connected chat agents exercise
judgment.

## Architecture

```text
Interface          MCP server · CLI
                         │
Core domain        paths · registry · documents · policy · search
                   patches · writes · snapshots · operations · security
                         │
System state       Markdown workspace · SQLite index/history
                         │
Workers            index/watch · backup · maintenance (mechanical only)
```

Core safety logic lives in `src/ferumind/core`. Interface and worker layers
call core rather than duplicating it.

## Quick start

Requirements: Linux, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and
[`just`](https://github.com/casey/just) for the convenience commands.

```bash
git clone https://github.com/MetaphoricalSheep/ferumind.git
cd ferumind
just setup
just install-hooks
just bootstrap
just verify
```

Without `just`, the equivalent setup and verification commands are:

```bash
uv sync --all-extras --dev
scripts/install-hooks.sh
uv run python scripts/bootstrap_workspace.py
scripts/verify.sh
```

Useful commands:

| Command | Purpose |
|---|---|
| `just cli -- --help` | Show the CLI |
| `just bootstrap` | Initialize or repair the workspace skeleton |
| `just project-list` | Compare registry, folder, and database project state |
| `just verify` | Format, lint, type-check, and test with coverage |
| `just sync-agents` | Regenerate ignored agent configurations |

`just project-delete <key>` is intentionally only stale-state cleanup. It
refuses to remove a project folder containing user knowledge.

## Workspace model

```text
workspace/
  system/                  global contract, templates, registry, format marker
  projects/<key>/
    spine.md
    rules/
    canvases/
    memory/
    library/
    inbox/
    archive/
    .ferumind/              private snapshots and upload staging
  compacts/
  .ferumind/                private database and global snapshots/backups
```

Run the service as the same dedicated OS account that owns the workspace.
Bootstrap creates the workspace root and contract files with private
permissions. Never commit the contents of `workspace/`.

## Development

```bash
just format
just lint
just typecheck
just test-cov
just verify
```

The full gate checks the tracked public tree and immutable GitHub Action pins,
then requires Ruff formatting and lint, strict Pyright, all pytest tests, and
at least 80% global coverage. CI tests Python 3.12 and 3.13, audits the locked
Python and OpenCode dependency environments, and builds, inspects, and
smoke-tests the distributions.

See [CONTRIBUTING.md](CONTRIBUTING.md) and the repository
[`AGENTS.md`](AGENTS.md) before changing core behavior.

## Tunnel tooling

The repository includes outbound stdio relay scripts that expose the MCP
server to a remote LLM frontend without an inbound port. The MCP server does
not authenticate callers, so the relay and its credentials are the only access
control in front of the workspace — supported for a workspace owner running it
themselves, not as a production posture. Read [docs/tunnel.md](docs/tunnel.md)
and [SECURITY.md](SECURITY.md) before working on that tooling.

## License

[MIT](LICENSE)
