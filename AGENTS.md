# Lattice — Agent Instructions

## Mission

**Chats are disposable; Lattice is where the continuity lives.** Lattice is a
local-first, Markdown-backed workspace shared by a human and their agents:
they collaborate in working documents, agents keep auditable memory there, and
durable knowledge settles into a retrievable library. It provides a hardened
MCP server, project-scoped stores, safe file operations with snapshots and
rollback, and SQLite-backed indexing/search/operation logs.

## Product status

This repository is **Lattice v2**. The locked product design lives in
`product/` ([00-what-is-lattice.md](product/00-what-is-lattice.md) and the
specs beside it); where existing code conflicts with
[product/spec-mcp.md](product/spec-mcp.md), **the spec wins**.

V2 was rebuilt from the specs in [product/roadmap.md](product/roadmap.md).
Superseded implementation details are not part of the repository.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     Interface Layer                  │
│          MCP Server  │  CLI (Typer)                 │
├─────────────────────────────────────────────────────┤
│                    Core Domain                       │
│  paths  │  config  │  registry  │  documents          │
│  context  │  policy  │  frontmatter  │  search         │
│  indexer  │  snapshots  │  operations  │  security    │
│  reconcile  │  format  │  locks  │  writes            │
├─────────────────────────────────────────────────────┤
│  Workers (index/watch, backup, maintenance)          │
│  — mechanical only: no LLM, no judgment              │
└─────────────────────────────────────────────────────┘
```

There is no sessions module and no agents layer: the server is stateless per
call, and "knowledge agents" are procedures executed by whichever chat agent
is connected (product D13).

### Core principles

- Core logic belongs in `lattice.core`.
- MCP, CLI, and workers must call core; they must not duplicate safety
  logic.
- Documents carry the intelligence; the server is a librarian: it protects,
  indexes, and assembles context — it never decides behavior.
- The workspace is live user data and must remain ignored by Git.
- Never write outside the configured workspace/project root.
- No symlink escape. No unsafe path joins.
- No hard delete for user knowledge content — archive/snapshot/restore flows.
- Every mutating operation is snapshot-protected and operation-logged.

## Repo Layout

```
lattice/
  AGENTS.md              ← This file — committed agent instructions
  README.md
  pyproject.toml
  product/               ← Locked v2 design: identity, specs, contract, roadmap
  .opencode/             ← Committed OpenCode config (source of truth)
  .githooks/             ← Git hooks (pre-commit, pre-push)
  scripts/               ← Bash and Python scripts
  src/lattice/           ← Package root
    core/                ← Domain logic (typed, tested, no framework leakage)
    mcp/                 ← MCP server
    cli/                 ← Typer CLI
    workers/             ← Mechanical background workers
    db/                  ← SQLite schema and migrations
  tests/
    unit/                ← Unit tests
    integration/         ← Integration tests
    fixtures/            ← Test fixtures
  docs/                  ← Documentation
  workspace/             ← Live user data (Git-ignored)
```

## Development Commands

| Command | Action |
|---------|--------|
| `just setup` | Install all dependencies |
| `just format` | Format code |
| `just lint` | Lint code |
| `just typecheck` | Type check |
| `just test` | Run tests |
| `just test-cov` | Tests with coverage |
| `just verify` | Full verification pipeline |
| `just sync-agents` | Sync agent configs |
| `just bootstrap` | Init workspace |
| `just cli -- --help` | Run the CLI |
| `just project-list` | List projects across registry/folder/database |
| `just project-delete <key>` | Clean stale registry/DB state after its folder is gone |
| `just tunnel --init` | Initialize the tunnel profile |
| `just tunnel-bg` | Start the tunnel in the background |
| `just tunnel-stop` | Stop the background tunnel |
| `uv sync --all-extras --dev` | Install all dependencies |
| `uv run ruff format .` | Format code |
| `uv run ruff check .` | Lint code |
| `uv run pyright` | Type check |
| `uv run pytest` | Run tests |
| `uv run pytest --cov` | Tests with coverage |
| `scripts/verify.sh` | Full verification pipeline |
| `uv run python scripts/sync_agent_configs.py` | Sync agent configs |
| `uv run python scripts/bootstrap_workspace.py` | Init workspace |
| `uv run lattice` | Run the CLI |
| `scripts/install-hooks.sh` | Install git hooks |

## Coding Standards

- Modern Python 3.12+
- Strong typing everywhere
- `src/` layout
- Small cohesive modules, no god files
- No circular imports
- No hidden side effects at import time
- No untyped public APIs
- No broad unstructured dicts at module boundaries
- No hardcoded absolute paths
- No business logic in CLI/MCP layers
- Use `pathlib`, not `os.path`
- Use Pydantic v2 for config/schema validation at boundaries
- Use dataclasses/value objects for simple internal structures
- Clean error types, no broad `except Exception` without re-raise
- No hidden global state for app behavior
- `Any` requires written justification
- `# type: ignore` requires written justification

## Typing Standards

- Pyright in strict mode
- All public functions need explicit parameter and return types
- Use `Protocol` for duck-typed interfaces
- Use `TypedDict` for structured dicts
- Use `Literal` for constrained string values
- Use `NewType` for domain primitives (ProjectKey, RepoRoot, etc.)
- No `Any` without justification

## Testing Standards

- pytest for all tests
- Use `tmp_path` for filesystem tests
- Tests should cover both success and failure paths
- Path-security code requires adversarial tests
- No mocking core behavior unless necessary
- No tests that simply assert imports
- Tests must be deterministic
- No fake tests
- Coverage threshold: 80% global (target 90%+ during implementation)

## Security and Path Safety

- Workspace isolation: all user data in `workspace/`
- Project scoping: every operation scoped to a project
- Path validation: all paths through `core.paths.contained_path()`
- Symlink escape defense: `assert_no_symlink_escape()`
- No hard delete: prefer archive/snapshot/restore
- Operation log: every mutation recorded
- No writing through user-controlled paths without canonicalization

## MCP Design Principles (v2 — product/spec-mcp.md is the full spec)

- **Stateless per call.** There are no sessions, no `session_id`, and no
  model-carried state except a short-lived patch `operation_id`. The server
  behaves identically if every call arrives on a fresh MCP connection.
- **Every project-scoped tool takes a required `project` argument.** It is an
  assertion validated against the registry — never an override. Missing →
  `PROJECT_REQUIRED`; unknown → `PROJECT_NOT_FOUND`. The project is the hard
  write boundary.
- `get_context` is the contract call: merged workspace + project rules, the
  spine, and the document map — uncapped, with payload-size telemetry.
- **Folder = role.** A document's role derives from its path (`spine.md`,
  `rules/`, `canvases/`, `memory/`, `library/`, `inbox/`, `archive/`). No
  `role:` frontmatter key. Behavioral frontmatter is `status`
  (active/gated/frozen/archived) and `edit_policy`
  (free/append/propose-first/ask-human) with folder defaults.
- **Propose → apply.** A `propose_*` result is not a saved edit: it carries
  `document_mutated=false`, `requires_apply=true`,
  `next_required_tool=apply_patch`. Only an `apply_patch` result with
  `document_mutated=true` means the file was saved. Proposals are bound to
  project + path + base hash, expire after 24 h (`PATCH_EXPIRED`), and are
  invalidated by out-of-band edits.
- **The server informs; agents honor.** Propose results echo the target's
  `edit_policy`/`status` with a `policy_note`; the server does not block on
  policy. The closed list of hard refusals: archived targets
  (`DOCUMENT_ARCHIVED`), protected frontmatter identity keys
  (`FRONTMATTER_PROTECTED`), out-of-project paths (`WORKSPACE_MISMATCH`).
- Out-of-band disk edits are first-class: reads reconcile (reindex, stale
  pending proposals, operation-log entry `source: out-of-band`); the watcher
  snapshots on detect. Hash guards make conflicting applies fail closed.
- Tool input schemas must be strict. No raw path writes; all paths resolve
  through the core path validator. All writes are snapshot-protected.
- Tool responses must be structured; errors must be machine-readable codes.
  No silent partial success. No hard delete (`archive_document` /
  `unarchive_document` instead).
- Tool names are stable snake_case; the server namespace is Lattice.
- The workspace format is versioned (`workspace/system/meta.yml`,
  `format: 2`; product/spec-versioning.md is the full spec). Writes against
  a mismatched format fail with `FORMAT_UNSUPPORTED`; migration is explicit
  (`lattice migrate`), never implicit. The MCP surface itself is not
  wire-versioned: additive within a format, breaking changes ride a format
  bump with a migrator in the same change. DB schema changes go through
  numbered migrations in `db/migrations/` (`PRAGMA user_version`) — never
  ad-hoc `ALTER` calls.

### Tool annotation taxonomy

Three categories ("read-only" means: does not mutate user Markdown; internal
observation-log metadata writes are allowed):

- **Read-only** (`readOnlyHint=true`, `idempotentHint=true`): `get_context`,
  `read_document`, `read_document_range`, `get_document_map`,
  `find_in_document`, `search_project`, `list_tree`, `list_files`,
  `read_file`, `list_pending_patches`, `operation_log`, `list_snapshots`,
  `read_snapshot`, `list_projects`.
- **Proposal / pending-edit** (`readOnlyHint=true`, `idempotentHint=false`):
  the `propose_*` family and `discard_patch` — they stage guarded diffs as
  pending operation records, never writing user Markdown.
- **Content-mutating** (`readOnlyHint=false`, `idempotentHint=false`):
  `apply_patch`, `create_document`, `capture_note`, `archive_document`,
  `unarchive_document`, `restore_snapshot`, `create_project`,
  `rebuild_index`.

## Non-Markdown files (spec-mcp §5.4)

A project holds arbitrary files alongside its Markdown. They are **not**
indexed, **not** searchable by content, and **never** part of `get_context`.
Two tiers serve them:

- `list_files` — generic discovery. Walks the project, returns
  project-relative paths with MIME type, size, a `lattice://` `resource_uri`,
  and a `context_support` value (`image` / `text` / `resource_only`).
  Markdown, Lattice upload sidecars, and `.lattice/` are excluded by default.
- `read_file` — puts one file into model context: a bounded image rendition
  (JPEG/PNG/WebP), a bounded text slice, or metadata only. Always returns a
  `ResourceLink` to the untouched original.
- `resources/read` on that URI — the exact original bytes, never a rendition
  and never truncated.

`read_document` vs `read_file`: `read_document` is the managed Markdown
surface and returns frontmatter plus the `document_sha256` that hash-guarded
edits need. `read_file` is generic bytes and knows nothing about documents;
it will serve a `.md` as plain text but flags `recommended_tool:
read_document`.

There is **no prescribed folder** for files — `library/` is where uploads
land, not where files must live. A file's meaning never follows from its
folder, filename, or extension; the documents that reference it are the
source of truth. Paths are always project-relative, and the server-local
workspace path never appears in a URI, result, or error.

`read_file` does **not** extract PDF text, render PDF pages, parse Office
documents, or run OCR. A `resource_only` result means the model has not seen
the contents. Whether a linked resource gets attached to the conversation is
the client's decision, not the server's. Host-specific outbound file
mechanisms (OpenAI tool-file outputs, Anthropic file outputs) are explicitly
deferred.

## Observability

The SQLite observation log records recent MCP calls: tool, project,
client metadata when exposed by the transport, ok/error, duration, result
size, and argument keys. It records metadata only — never document content,
patch bodies, or argument values. Redaction replaces secrets/tokens with
`[redacted]`; unavailable client metadata remains null and is never guessed.
The current stdio-only build does not expose an HTTP activity page. Do not
store operation ids or any tool bookkeeping in Markdown documents.

## Lookup-first editing

Agents must not patch large Markdown bodies when a narrower target is
available. Preferred order:

1. `search_project`
2. `get_document_map`
3. `find_in_document` or `read_document_range`
4. `propose_exact_replace_patch` (preferred: exact multi-line old/new text),
   or `propose_multi_edit_patch` for several edits to one document
5. `propose_section_patch`, `propose_range_patch`,
   `propose_search_replace_patch`, or `propose_insert_patch` for positional
   edits
6. `apply_patch`

Frontmatter/metadata changes go through `propose_frontmatter_patch` (identity
keys `id`/`type`/`project`/`created` and the automatic `updated` are
protected).

Use `propose_patch mode=body` only when intentionally replacing the full
body. Use `mode=full` only for explicit full-file operations.

Positional (line/range/section) edits must be guarded by document and target
hashes. Content-anchored edits (exact replace, multi edit, frontmatter) are
guarded by the matched text itself; `expected_document_sha256` is optional
extra safety there. After `apply_patch`, chain its returned `document_sha256`
into the next edit's `expected_document_sha256` instead of re-reading.

Stale or superseded proposals should be withdrawn with `discard_patch`; new
documents are created with `create_document`, never through raw patches to
nonexistent paths.

## MCP Stdio / Tunnel Rules

- MCP stdio wrappers must not print non-MCP output to stdout.
- Tunnel launchers may log status to stderr/stdout (they are user-facing
  scripts, not MCP protocol participants).
- The actual MCP command given to tunnel-client should be a quiet executable
  wrapper, never a shell command string.
- `scripts/lattice-mcp-stdio` is the canonical wrapper; it loads env files
  and `exec`s the server.
- The tunnel serves the workspace configured by `LATTICE_WORKSPACE`. The MCP
  server does not authenticate callers, so the relay and its control-plane
  credentials are the only access control in front of that workspace. Treat
  the tunnel URL and `CONTROL_PLANE_*` values as secrets granting full read
  and write access.
- This is accepted for single-user development on a private deployment. The
  OAuth, owner-authorization, and deployment gate in product D11 must still
  ship before remote serving is supported for anyone but the workspace owner
  running it themselves.
- Never bypass the launcher with a direct `tunnel-client run`: the launcher
  validates the profile, records a PID bound to its process start time, and
  strips control-plane credentials from the MCP child's environment.
- Starting a tunnel is operator-initiated; the launcher refuses to start
  when `CI` is set.

## Git Workflow

- OpenCode is committed source of truth
- Other agent configs are generated by `scripts/sync_agent_configs.py`
- `.opencode/` is committed
- `.cursor/`, `.claude/`, `.codex/`, `.github/copilot-instructions.md` are
  generated and ignored
- Never commit live user knowledge data
- Never commit SQLite files
- Never commit generated agent folders for non-OpenCode agents
- Pre-commit hook runs: ruff format check, ruff lint, pyright, pytest
- Pre-push hook runs: `scripts/verify.sh`
- The remote default branch is the release source of truth

## Forbidden Actions

- Writing outside the configured workspace/project root
- Symlink escape
- Unsafe path joins — never use `str(path).startswith(str(root))` for
  containment checks; always use `is_under_root()` from `lattice.core.paths`
- Hard delete for user knowledge content
- Reintroducing session state or `session_id` parameters (removed v1 concepts)
- Using `Any` without written justification
- Using `# type: ignore` without written justification
- Committing SQLite/DB files
- Committing live user knowledge data
- Committing generated agent configs
- Creating code with global mutable state
- Using `nano` in docs/scripts/instructions (use `vim`)

## Env / Config Sync Rules

- `.env`, `.env.example`, and `src/lattice/core/config.py` (or any Pydantic
  config module) must stay in sync.
- When adding a new key to one, add it to all three.
- Never overwrite existing values in `.env`. If a key already has a real
  value there, leave it alone.
- If the `.env` value looks sensitive (API key, secret, token, password),
  use a placeholder like `sk-xxx` or `your-{NAME}-here` in `.env.example`.
- When adding a new key to `.env.example`, use a safe placeholder value.
- When Pydantic config modules exist, the field names and defaults there are
  the source of truth for structure; `.env.example` is the source of truth
  for which variables are user-configurable.
- Tunnel control-plane keys (`CONTROL_PLANE_*`, `LATTICE_TUNNEL_PROFILE`,
  `TUNNEL_CLIENT_BIN`) are launcher-only shell configuration. They must stay
  aligned between `.env`, `.env.example`, and the tunnel scripts, and must
  **not** be added to the application `Config` model or inherited by the MCP
  child. `scripts/lattice-mcp-stdio` unsets that authority before `exec`.

## Required Verification Before Completion

1. Run `ruff format --check .` — no formatting issues
2. Run `ruff check .` — no lint errors
3. Run `pyright` — no type errors
4. Run `pytest --cov=src/lattice` — all tests pass, coverage >= 80%
5. Review diff for TODOs, stubs, `Any`, `# type: ignore`
6. Check boundary violations (core vs interface)
7. Check security implications of any path/file operations

## Agent Delegation Policy

Primary planning and build agents must delegate verification repair to the
test-fixer subagent when implementation reaches the verification phase.

This applies even when the user did not explicitly ask for test fixing.

Required flow:

1. Plan or implement the requested work.
2. When the plan reaches verification, invoke the test-fixer subagent.
3. The test-fixer must use the test-fix skill.
4. The test-fixer must run the full verification pipeline, preferably via
   `just verify`.
5. The test-fixer may fix only safe verification failures.
6. The task is not complete until test-fixer returns TEST FIX COMPLETE.
7. If test-fixer returns ESCALATION REQUIRED, the primary agent must resume
   control and resolve the design, implementation, security, product,
   dependency, or human-decision issue before re-running verification.

Primary agents must not burn premium-model tokens on routine lint,
typecheck, unit-test, coverage, or smoke-test repair loops when test-fixer
is available.

Primary agents must not bypass test-fixer by declaring success from partial
checks.

Escalated failures must be treated as unfinished work, not as successful
completion.
